# Mikel Broström 🔥 Yolo Tracking 🧾 AGPL-3.0 license

import lap
import numpy as np
from pathlib import Path
from typing import List, Tuple

from boxmot.motion.kalman_filters.xywh_kf import KalmanFilterXYWH
from boxmot.trackers.botsort.basetrack import BaseTrack, TrackState
from boxmot.utils.iou import AssociationFunction
from boxmot.trackers.basetracker import BaseTracker
from boxmot.trackers.botsort.botsort_track import STrack
from boxmot.motion.cmc import get_cmc_method


def iou_distance(atracks, btracks):
    """
    Compute cost based on IoU
    :type atracks: list[STrack]
    :type btracks: list[STrack]

    :rtype cost_matrix np.ndarray
    """

    if (len(atracks) > 0 and isinstance(atracks[0], np.ndarray)) or (
        len(btracks) > 0 and isinstance(btracks[0], np.ndarray)
    ):
        atlbrs = atracks
        btlbrs = btracks
    else:
        atlbrs = [track.xyxy for track in atracks]
        btlbrs = [track.xyxy for track in btracks]

    ious = np.zeros((len(atlbrs), len(btlbrs)), dtype=np.float32)
    if ious.size == 0:
        return ious
    _ious = AssociationFunction.iou_batch(atlbrs, btlbrs)

    cost_matrix = 1 - _ious

    return cost_matrix

def joint_stracks(tlista: List['STrack'], tlistb: List['STrack']) -> List['STrack']:
    """
    Joins two lists of tracks, ensuring that there are no duplicates based on track IDs.

    Args:
        tlista (List[STrack]): The first list of tracks.
        tlistb (List[STrack]): The second list of tracks.

    Returns:
        List[STrack]: A combined list of tracks from both input lists, without duplicates.
    """
    exists = {}
    res = []
    for t in tlista:
        exists[t.id] = 1
        res.append(t)
    for t in tlistb:
        tid = t.id
        if not exists.get(tid, 0):
            exists[tid] = 1
            res.append(t)
    return res

def sub_stracks(tlista: List['STrack'], tlistb: List['STrack']) -> List['STrack']:
    """
    Subtracts the tracks in tlistb from tlista based on track IDs.

    Args:
        tlista (List[STrack]): The list of tracks from which tracks will be removed.
        tlistb (List[STrack]): The list of tracks to be removed from tlista.

    Returns:
        List[STTrack]: The remaining tracks after removal.
    """
    stracks = {t.id: t for t in tlista}
    for t in tlistb:
        tid = t.id
        if tid in stracks:
            del stracks[tid]
    return list(stracks.values())

def remove_duplicate_stracks(stracksa: List['STrack'], stracksb: List['STrack']) -> Tuple[List['STrack'], List['STrack']]:
    """
    Removes duplicate tracks between two lists based on their IoU distance and track duration.

    Args:
        stracksa (List[STrack]): The first list of tracks.
        stracksb (List[STrack]): The second list of tracks.

    Returns:
        Tuple[List[STrack], List[STrack]]: The filtered track lists, with duplicates removed.
    """
    pdist = iou_distance(stracksa, stracksb)
    pairs = np.where(pdist < 0.15)
    dupa, dupb = [], []

    for p, q in zip(*pairs):
        timep = stracksa[p].frame_id - stracksa[p].start_frame
        timeq = stracksb[q].frame_id - stracksb[q].start_frame
        if timep > timeq:
            dupb.append(q)
        else:
            dupa.append(p)

    resa = [t for i, t in enumerate(stracksa) if i not in dupa]
    resb = [t for i, t in enumerate(stracksb) if i not in dupb]
    
    return resa, resb

def fuse_score(cost_matrix, detections):
    if cost_matrix.size == 0:
        return cost_matrix
    iou_sim = 1 - cost_matrix
    det_confs = np.array([det.conf for det in detections])
    det_confs = np.expand_dims(det_confs, axis=0).repeat(cost_matrix.shape[0], axis=0)
    fuse_sim = iou_sim * det_confs
    fuse_cost = 1 - fuse_sim
    return fuse_cost

def linear_assignment(cost_matrix, thresh):
    if cost_matrix.size == 0:
        return (
            np.empty((0, 2), dtype=int),
            tuple(range(cost_matrix.shape[0])),
            tuple(range(cost_matrix.shape[1])),
        )
    matches, unmatched_a, unmatched_b = [], [], []
    cost, x, y = lap.lapjv(cost_matrix, extend_cost=True, cost_limit=thresh)
    for ix, mx in enumerate(x):
        if mx >= 0:
            matches.append([ix, mx])
    unmatched_a = np.where(x < 0)[0]
    unmatched_b = np.where(y < 0)[0]
    matches = np.asarray(matches)
    return matches, unmatched_a, unmatched_b

class BotSort(BaseTracker):
    """
    BoTSORT Tracker: A tracking algorithm that combines appearance and motion-based tracking.

    Args:
        per_class (bool, optional): Whether to perform per-class tracking.
        track_high_thresh (float, optional): Detection confidence threshold for first association.
        track_low_thresh (float, optional): Detection confidence threshold for ignoring detections.
        new_track_thresh (float, optional): Threshold for creating a new track.
        track_buffer (int, optional): Frames to keep a track alive after last detection.
        match_thresh (float, optional): Matching threshold for data association.
        proximity_thresh (float, optional): IoU threshold for first-round association.
        appearance_thresh (float, optional): Appearance embedding distance threshold for ReID.
        cmc_method (str, optional): Method for correcting camera motion, e.g., "sof" (simple optical flow).
        frame_rate (int, optional): Video frame rate, used to scale the track buffer.
        fuse_first_associate (bool, optional): Fuse appearance and motion in the first association step.
        with_reid (bool, optional): Use ReID features for association.
    """

    def __init__(
        self,
        per_class: bool = False,
        track_high_thresh: float = 0.5,
        track_low_thresh: float = 0.1,
        new_track_thresh: float = 0.6,
        track_buffer: int = 30,
        match_thresh: float = 0.8,
        proximity_thresh: float = 0.5,
        appearance_thresh: float = 0.25,
        cmc_method: str = "ecc",
        frame_rate=30,
        fuse_first_associate: bool = False,
        with_reid: bool = False,
    ):
        super().__init__(per_class=per_class)
        self.lost_stracks = []  # type: list[STrack]
        self.removed_stracks = []  # type: list[STrack]
        BaseTrack.clear_count()

        self.per_class = per_class
        self.track_high_thresh = track_high_thresh
        self.track_low_thresh = track_low_thresh
        self.new_track_thresh = new_track_thresh
        self.match_thresh = match_thresh

        self.buffer_size = int(frame_rate / 30.0 * track_buffer)
        self.max_time_lost = self.buffer_size
        self.kalman_filter = KalmanFilterXYWH()

        # ReID module
        self.proximity_thresh = proximity_thresh
        self.appearance_thresh = appearance_thresh
        self.with_reid = with_reid
        if self.with_reid:
            raise NotImplementedError("ReID was removed to exclude PyTorch dependency.")

        self.cmc = get_cmc_method(cmc_method)()
        self.fuse_first_associate = fuse_first_associate

    @BaseTracker.on_first_frame_setup
    @BaseTracker.per_class_decorator
    def update(self, dets: np.ndarray, img: np.ndarray, embs: np.ndarray = None) -> np.ndarray:
        self.check_inputs(dets, img)
        self.frame_count += 1

        activated_stracks, refind_stracks, lost_stracks, removed_stracks = [], [], [], []

        # Preprocess detections
        dets, dets_first, embs_first, dets_second = self._split_detections(dets, embs)

        # Extract appearance features
        if self.with_reid and embs is None:
            raise NotImplementedError("ReID was removed to exclude PyTorch dependency.")
        else:
            features_high = embs_first if embs_first is not None else []

        # Create detections
        detections = self._create_detections(dets_first, features_high)

        # Separate unconfirmed and active tracks
        unconfirmed, active_tracks = self._separate_tracks()
        
        strack_pool = joint_stracks(active_tracks, self.lost_stracks)

        # First association
        matches_first, u_track_first, u_detection_first = self._first_association(dets, dets_first, active_tracks, unconfirmed, img, detections, activated_stracks, refind_stracks, strack_pool)

        # Second association
        matches_second, u_track_second, u_detection_second = self._second_association(dets_second, activated_stracks, lost_stracks, refind_stracks, u_track_first, strack_pool)

        # Handle unconfirmed tracks
        matches_unc, u_track_unc, u_detection_unc = self._handle_unconfirmed_tracks(u_detection_first, detections, activated_stracks, removed_stracks, unconfirmed)

        # Initialize new tracks
        self._initialize_new_tracks(u_detection_unc, activated_stracks, [detections[i] for i in u_detection_first])

        # Update lost and removed tracks
        self._update_track_states(lost_stracks, removed_stracks)

        # Merge and prepare output
        return self._prepare_output(activated_stracks, refind_stracks, lost_stracks, removed_stracks)

    def _split_detections(self, dets, embs):
        dets = np.hstack([dets, np.arange(len(dets)).reshape(-1, 1)])
        confs = dets[:, 4]
        second_mask = np.logical_and(confs > self.track_low_thresh, confs < self.track_high_thresh)
        dets_second = dets[second_mask]
        first_mask = confs > self.track_high_thresh
        dets_first = dets[first_mask]
        embs_first = embs[first_mask] if embs is not None else None
        return dets, dets_first, embs_first, dets_second

    def _create_detections(self, dets_first, features_high):
        if len(dets_first) > 0:
            if self.with_reid:
                raise NotImplementedError("ReID was removed to exclude PyTorch dependency.")
            else:
                detections = [STrack(det, max_obs=self.max_obs) for det in dets_first]
        else:
            detections = []
        return detections

    def _separate_tracks(self):
        unconfirmed, active_tracks = [], []
        for track in self.active_tracks:
            if not track.is_activated:
                unconfirmed.append(track)
            else:
                active_tracks.append(track)
        return unconfirmed, active_tracks

    def _first_association(self, dets, dets_first, active_tracks, unconfirmed, img, detections, activated_stracks, refind_stracks, strack_pool):
        
        STrack.multi_predict(strack_pool)

        # Fix camera motion
        warp = self.cmc.apply(img, dets)
        STrack.multi_gmc(strack_pool, warp)
        STrack.multi_gmc(unconfirmed, warp)

        # Associate with high confidence detection boxes
        ious_dists = iou_distance(strack_pool, detections)
        ious_dists_mask = ious_dists > self.proximity_thresh
        if self.fuse_first_associate:
            ious_dists = fuse_score(ious_dists, detections)

        if self.with_reid:
            raise NotImplementedError("ReID was removed to exclude PyTorch dependency.")
        else:
            dists = ious_dists

        matches, u_track, u_detection = linear_assignment(dists, thresh=self.match_thresh)
                
        for itracked, idet in matches:
            track = strack_pool[itracked]
            det = detections[idet]
            if track.state == TrackState.Tracked:
                track.update(detections[idet], self.frame_count)
                activated_stracks.append(track)
            else:
                track.re_activate(det, self.frame_count, new_id=False)
                refind_stracks.append(track)
                
        return matches, u_track, u_detection

    def _second_association(self, dets_second, activated_stracks, lost_stracks, refind_stracks, u_track_first, strack_pool):
        if len(dets_second) > 0:
            detections_second = [STrack(det, max_obs=self.max_obs) for det in dets_second]
        else:
            detections_second = []

        r_tracked_stracks = [
            strack_pool[i]
            for i in u_track_first
            if strack_pool[i].state == TrackState.Tracked
        ]

        dists = iou_distance(r_tracked_stracks, detections_second)
        matches, u_track, u_detection = linear_assignment(dists, thresh=0.5)
        
        for itracked, idet in matches:
            track = r_tracked_stracks[itracked]
            det = detections_second[idet]
            if track.state == TrackState.Tracked:
                track.update(det, self.frame_count)
                activated_stracks.append(track)
            else:
                track.re_activate(det, self.frame_count, new_id=False)
                refind_stracks.append(track)

        for it in u_track:
            track = r_tracked_stracks[it]
            if not track.state == TrackState.Lost:
                track.mark_lost()
                lost_stracks.append(track)
                
        return matches, u_track, u_detection


    def _handle_unconfirmed_tracks(self, u_detection, detections, activated_stracks, removed_stracks, unconfirmed):
        """
        Handle unconfirmed tracks (tracks with only one detection frame).

        Args:
            u_detection: Unconfirmed detection indices.
            detections: Current list of detections.
            activated_stracks: List of newly activated tracks.
            removed_stracks: List of tracks to remove.
        """
        # Only use detections that are unconfirmed (filtered by u_detection)
        detections = [detections[i] for i in u_detection]
        
        # Calculate IoU distance between unconfirmed tracks and detections
        ious_dists = iou_distance(unconfirmed, detections)
        
        # Apply IoU mask to filter out distances that exceed proximity threshold
        ious_dists_mask = ious_dists > self.proximity_thresh
        ious_dists = fuse_score(ious_dists, detections)
        
        # Fuse scores for IoU-based and embedding-based matching (if applicable)
        if self.with_reid:
            raise NotImplementedError("ReID was removed to exclude PyTorch dependency.")
        else:
            dists = ious_dists

        # Perform data association using linear assignment on the combined distances
        matches, u_unconfirmed, u_detection = linear_assignment(dists, thresh=0.7)
        
        # Update matched unconfirmed tracks
        for itracked, idet in matches:
            unconfirmed[itracked].update(detections[idet], self.frame_count)
            activated_stracks.append(unconfirmed[itracked])

        # Mark unmatched unconfirmed tracks as removed
        for it in u_unconfirmed:
            track = unconfirmed[it]
            track.mark_removed()
            removed_stracks.append(track)
            
        return matches, u_unconfirmed, u_detection

    def _initialize_new_tracks(self, u_detections, activated_stracks, detections):
        for inew in u_detections:
            track = detections[inew]
            if track.conf < self.new_track_thresh:
                continue

            track.activate(self.kalman_filter, self.frame_count)
            activated_stracks.append(track)

    def _update_tracks(self, matches, strack_pool, detections, activated_stracks, refind_stracks, mark_removed=False):
        # Update or reactivate matched tracks
        for itracked, idet in matches:
            track = strack_pool[itracked]
            det = detections[idet]
            if track.state == TrackState.Tracked:
                track.update(det, self.frame_count)
                activated_stracks.append(track)
            else:
                track.re_activate(det, self.frame_count, new_id=False)
                refind_stracks.append(track)
        
        # Mark only unmatched tracks as removed, if mark_removed flag is True
        if mark_removed:
            unmatched_tracks = [strack_pool[i] for i in range(len(strack_pool)) if i not in [m[0] for m in matches]]
            for track in unmatched_tracks:
                track.mark_removed()

    def _update_track_states(self, lost_stracks, removed_stracks):
        for track in self.lost_stracks:
            if self.frame_count - track.end_frame > self.max_time_lost:
                track.mark_removed()
                removed_stracks.append(track)

    def _prepare_output(self, activated_stracks, refind_stracks, lost_stracks, removed_stracks):
        self.active_tracks = [
            t for t in self.active_tracks if t.state == TrackState.Tracked
        ]
        self.active_tracks = joint_stracks(self.active_tracks, activated_stracks)
        self.active_tracks = joint_stracks(self.active_tracks, refind_stracks)
        self.lost_stracks = sub_stracks(self.lost_stracks, self.active_tracks)
        self.lost_stracks.extend(lost_stracks)
        self.lost_stracks = sub_stracks(self.lost_stracks, self.removed_stracks)
        self.removed_stracks.extend(removed_stracks)
        self.active_tracks, self.lost_stracks = remove_duplicate_stracks(
            self.active_tracks, self.lost_stracks
        )

        outputs = [
            [*t.xyxy, t.id, t.conf, t.cls, t.det_ind]
            for t in self.active_tracks if t.is_activated
        ]

        return np.asarray(outputs)
