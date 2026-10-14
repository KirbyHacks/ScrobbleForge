import hashlib
import json
import logging
import random
from typing import List, Optional
from .models import Track
from .quota_tracker import QuotaTracker

logger = logging.getLogger("scrobbler.queue")


# tracks can repeat in a playlist, so we track duplicate songs at separate queue positions.
def _track_identifier(track: Track) -> str:
    """returns a unique identifier string for a track."""
    return track.queue_identity


def _compute_source_hash(tracks: List[Track]) -> str:
    """returns a hash of the original track order."""
    identifiers = [_track_identifier(t) for t in tracks]
    serialized = json.dumps(identifiers, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class QueueManager:
    """manages playlist order, shuffling, repeating, and saving progress."""

    def __init__(
        self,
        tracks: List[Track],
        tracker: Optional[QuotaTracker] = None,
        shuffle: bool = True,
        loop: bool = True,
    ):
        self.tracker = tracker
        self.shuffle = shuffle
        self.loop = loop
        self.original_tracks: List[Track] = list(tracks)
        self.queue: List[Track] = []
        self.current_index: int = 0

        self._setup_queue()

    def _save_permutation(self):
        """saves the current shuffle order to the database."""
        if not self.tracker or not self.queue:
            return
        perm = [_track_identifier(t) for t in self.queue]
        self.tracker.set_state("queue_permutation", json.dumps(perm))
        self.tracker.set_state(
            "queue_source_hash",
            _compute_source_hash(self.original_tracks),
        )
        self.tracker.set_state("queue_order_mode", "shuffle")

    def _clear_permutation(self):
        """clears saved shuffle order when shuffle is turned off."""
        if self.tracker:
            self.tracker.set_state("queue_permutation", "")

    def _setup_queue(self):
        if not self.original_tracks:
            self.queue = []
            self.current_index = 0
            return

        current_source_hash = _compute_source_hash(self.original_tracks)
        current_order_mode = "shuffle" if self.shuffle else "ordered"

        saved_source_hash = self.tracker.get_state("queue_source_hash") if self.tracker else None
        saved_order_mode = self.tracker.get_state("queue_order_mode") if self.tracker else None
        saved_perm_raw = self.tracker.get_state("queue_permutation") if self.tracker else None

        # infer saved order mode for backwards compatibility with legacy state
        if not saved_order_mode:
            if saved_perm_raw and saved_perm_raw.strip():
                saved_order_mode = "shuffle"
            elif saved_source_hash and saved_source_hash.strip():
                saved_order_mode = "ordered"

        order_mode_changed = bool(saved_order_mode and saved_order_mode != current_order_mode)
        source_changed = bool(saved_source_hash and saved_source_hash != current_source_hash)

        if self.shuffle:
            restored = False

            # restore saved shuffle order only if order semantics match and playlist has not changed
            if not order_mode_changed and saved_perm_raw and saved_source_hash == current_source_hash:
                try:
                    saved_perm = json.loads(saved_perm_raw)
                    if isinstance(saved_perm, list) and len(saved_perm) == len(self.original_tracks):
                        pool = {}
                        for t in self.original_tracks:
                            tid = _track_identifier(t)
                            pool.setdefault(tid, []).append(t)

                        reconstructed = []
                        for tid in saved_perm:
                            if tid in pool and pool[tid]:
                                reconstructed.append(pool[tid].pop(0))
                            else:
                                break

                        if len(reconstructed) == len(self.original_tracks):
                            self.queue = reconstructed
                            restored = True
                            logger.info(f"[QUEUE] restored shuffle permutation ({len(self.queue)} tracks)")
                except Exception as e:
                    logger.warning(f"[QUEUE] failed to restore saved shuffle permutation: {e}")

            if not restored:
                self.queue = list(self.original_tracks)
                random.shuffle(self.queue)
                self._save_permutation()
                self.current_index = 0
                if self.tracker:
                    self.tracker.set_state("queue_index", "0")
                logger.info(f"[QUEUE] generated fresh shuffle permutation ({len(self.queue)} tracks)")
                return
        else:
            self.queue = list(self.original_tracks)
            self._clear_permutation()
            if self.tracker:
                self.tracker.set_state("queue_source_hash", current_source_hash)
                self.tracker.set_state("queue_order_mode", "ordered")
                if order_mode_changed or source_changed:
                    self.tracker.set_state("queue_index", "0")

        # restore previous queue position only when order semantics and source match
        if order_mode_changed or source_changed:
            self.current_index = 0
            if self.tracker:
                self.tracker.set_state("queue_index", "0")
            return

        # restore previous queue position from the database
        saved_index = self.tracker.get_state("queue_index") if self.tracker else None
        if saved_index is not None:
            try:
                idx = int(saved_index)
                if 0 <= idx < len(self.queue):
                    self.current_index = idx
                    logger.info(f"[QUEUE] resumed playback from track {idx + 1} of {len(self.queue)}")
                elif idx >= len(self.queue):
                    if self.loop:
                        self.current_index = 0
                        if self.shuffle:
                            random.shuffle(self.queue)
                            self._save_permutation()
                            logger.info(f"[LOOP] playlist queue wrapped -> reshuffled {len(self.queue)} tracks")
                        else:
                            logger.info(f"[LOOP] playlist queue wrapped -> repeating {len(self.queue)} tracks")
                        if self.tracker:
                            self.tracker.set_state("queue_index", "0")
                    else:
                        self.current_index = idx
                else:
                    self.current_index = 0
            except ValueError:
                self.current_index = 0
        else:
            self.current_index = 0

    def update_tracks(self, new_tracks: List[Track]):
        logger.info(f"[QUEUE] updated track queue with {len(new_tracks)} fresh tracks")
        self.original_tracks = list(new_tracks)
        self._setup_queue()
        if not new_tracks and self.tracker:
            self.tracker.set_state("queue_index", "0")
            self.tracker.set_state("queue_source_hash", _compute_source_hash([]))
            self._clear_permutation()

    def get_next_track(self) -> Optional[Track]:
        if not self.queue:
            return None

        if self.current_index >= len(self.queue):
            if self.loop:
                self.current_index = 0
                if self.shuffle:
                    random.shuffle(self.queue)
                    self._save_permutation()
                    logger.info(f"[LOOP] playlist queue wrapped -> reshuffled {len(self.queue)} tracks")
                else:
                    logger.info(f"[LOOP] playlist queue wrapped -> repeating {len(self.queue)} tracks")
            else:
                logger.info(f"[QUEUE COMPLETE] finished {len(self.queue)} tracks -> loop disabled")
                return None

        track = self.queue[self.current_index]
        self.current_index += 1
        return track

    def commit_track_progress(self):
        """saves current queue position after a confirmed scrobble."""
        if self.tracker:
            self.tracker.set_state("queue_index", str(self.current_index))

    def peek_current_track(self) -> Optional[Track]:
        """returns the next track without moving the queue position forward."""
        if not self.queue or self.current_index >= len(self.queue):
            return None
        return self.queue[self.current_index]

    @property
    def total_tracks(self) -> int:
        return len(self.queue)

    @property
    def remaining_tracks(self) -> int:
        return max(0, len(self.queue) - self.current_index)
