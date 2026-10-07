import hashlib
import json
import logging
import random
from typing import List, Optional
from .models import Track
from .quota_tracker import QuotaTracker

logger = logging.getLogger("scrobbler.queue")


def _track_identifier(track: Track) -> str:
    """Generates a stable unique identifier string for a track."""
    if track.spotify_id:
        return f"spotify:{track.spotify_id}"
    return f"{track.artist.strip()}::{track.title.strip()}::{track.album.strip()}::{track.track_number}::{track.duration_ms}"


def _compute_source_hash(tracks: List[Track]) -> str:
    """Computes a deterministic hash of the source track sequence."""
    identifiers = [_track_identifier(t) for t in tracks]
    serialized = json.dumps(identifiers, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class QueueManager:
    """
    Manages the playlist queue, shuffling, loop repetition, and index persistence across restarts.
    Ensures that shuffled permutations are persisted deterministically across restarts.
    """

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
        """Persists the current shuffle permutation and source hash into tracker state."""
        if not self.tracker or not self.queue:
            return
        perm = [_track_identifier(t) for t in self.queue]
        self.tracker.set_state("queue_permutation", json.dumps(perm))
        self.tracker.set_state(
            "queue_source_hash",
            _compute_source_hash(self.original_tracks),
        )

    def _clear_permutation(self):
        """Clears persisted permutation if shuffle is disabled."""
        if self.tracker:
            self.tracker.set_state("queue_permutation", "")
            self.tracker.set_state("queue_source_hash", "")

    def _setup_queue(self):
        if not self.original_tracks:
            self.queue = []
            self.current_index = 0
            return

        current_source_hash = _compute_source_hash(self.original_tracks)

        if self.shuffle:
            restored = False
            saved_perm_raw = self.tracker.get_state("queue_permutation") if self.tracker else None
            saved_source_hash = self.tracker.get_state("queue_source_hash") if self.tracker else None

            # restore saved permutation if source hash matches
            if saved_perm_raw and saved_source_hash == current_source_hash:
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

        # restore previous playback index from state db
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
        if not new_tracks:
            return

        logger.info(f"[QUEUE] updated track queue with {len(new_tracks)} fresh tracks")
        self.original_tracks = list(new_tracks)
        self._setup_queue()

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
        if self.tracker:
            self.tracker.set_state("queue_index", str(self.current_index))
        return track

    def peek_current_track(self) -> Optional[Track]:
        """Looks at the next track without advancing."""
        if not self.queue or self.current_index >= len(self.queue):
            return None
        return self.queue[self.current_index]

    @property
    def total_tracks(self) -> int:
        return len(self.queue)

    @property
    def remaining_tracks(self) -> int:
        return max(0, len(self.queue) - self.current_index)
