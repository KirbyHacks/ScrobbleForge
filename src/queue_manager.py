import json
import logging
import random
from typing import List, Optional
from .models import Track
from .quota_tracker import QuotaTracker

logger = logging.getLogger("scrobbler.queue")


class QueueManager:
    """
    Manages the playlist queue, shuffling, loop repetition, and index persistence across restarts.
    """

    def __init__(
        self,
        tracks: List[Track],
        tracker: QuotaTracker,
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

    def _setup_queue(self):
        if not self.original_tracks:
            self.queue = []
            return

        self.queue = list(self.original_tracks)
        if self.shuffle:
            random.shuffle(self.queue)

        # Attempt to restore previous index from state DB
        saved_index = self.tracker.get_state("queue_index")
        if saved_index is not None:
            try:
                idx = int(saved_index)
                if 0 <= idx < len(self.queue):
                    self.current_index = idx
                    logger.info(f"Resumed playback queue from track {idx + 1} of {len(self.queue)}")
                else:
                    self.current_index = 0
            except ValueError:
                self.current_index = 0
        else:
            self.current_index = 0

    def update_tracks(self, new_tracks: List[Track]):
        """Updates the track pool when playlists are refreshed."""
        if not new_tracks:
            return

        logger.info(f"Updating track queue with {len(new_tracks)} fresh tracks from source.")
        self.original_tracks = list(new_tracks)
        self._setup_queue()

    def get_next_track(self) -> Optional[Track]:
        """
        Retrieves the next track to scrobble and advances the cursor.
        Returns None if queue is exhausted and loop is False.
        """
        if not self.queue:
            return None

        if self.current_index >= len(self.queue):
            if self.loop:
                logger.info("Reached end of playlist queue. Repeating...")
                self.current_index = 0
                if self.shuffle:
                    random.shuffle(self.queue)
            else:
                logger.info("Finished entire playlist queue. Loop is disabled.")
                return None

        track = self.queue[self.current_index]
        self.current_index += 1
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
