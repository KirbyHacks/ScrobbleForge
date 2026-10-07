import logging
import random
import threading
import time
from typing import Optional
from .config import AppConfig
from .lastfm_client import LastFMClient, LastFMAuthError, LastFMRateLimitError, LastFMTemporaryError
from .models import Track
from .queue_manager import QueueManager
from .quota_tracker import QuotaTracker
from .spotify_client import SpotifyClient

logger = logging.getLogger("scrobbler.engine")


class ScrobblerEngine:
    """Core scrobbler coordinating playback timing, pacing modes, and quota limits."""

    def __init__(
        self,
        config: AppConfig,
        lastfm: LastFMClient,
        spotify: Optional[SpotifyClient],
        queue: QueueManager,
        tracker: QuotaTracker,
        stop_event: threading.Event,
    ):
        self.cfg = config
        self.lastfm = lastfm
        self.spotify = spotify
        self.queue = queue
        self.tracker = tracker
        self.stop_event = stop_event

        self.last_refresh_time = time.time()
        self.consecutive_errors = 0

        self.virtual_timeline_cursor: Optional[int] = None
        self._init_virtual_timeline()

    def _init_virtual_timeline(self):
        """Initializes virtual timeline cursor for non-overlapping max_limit backdating."""
        saved_cursor = self.tracker.get_state("virtual_timeline_cursor")
        now = int(time.time())
        if saved_cursor is not None:
            try:
                cur = int(saved_cursor)
                two_weeks_ago = now - (13 * 86400)
                if two_weeks_ago < cur < (now - 60):
                    self.virtual_timeline_cursor = cur
                    logger.info(f"Resumed max_limit timeline cursor at timestamp {cur}")
                    return
            except ValueError:
                pass

        # Start timeline 3 days ago to maintain non-overlapping sequential history
        self.virtual_timeline_cursor = now - (3 * 86400)
        self.tracker.set_state("virtual_timeline_cursor", str(self.virtual_timeline_cursor))

    def run(self):
        """Main scrobble loop."""
        mode = self.cfg.engine.mode
        logger.info("=" * 60)
        logger.info(f" Mode: [{mode.upper()}] | Tracks: {self.queue.total_tracks}")
        logger.info(f" Shuffle: {self.cfg.engine.shuffle} | Loop: {self.cfg.engine.loop}")
        logger.info(f" Daily Safety Quota: {self.cfg.engine.max_daily_scrobbles} scrobbles / 24h")
        logger.info("=" * 60)

        while not self.stop_event.is_set():
            try:
                self._check_periodic_refresh()
                self._check_and_enforce_daily_quota()

                if self.stop_event.is_set():
                    break

                track = self.queue.get_next_track()
                if track is None:
                    logger.info("Playlist queue completed.")
                    break

                if mode == "realistic":
                    self._execute_realistic_step(track)
                elif mode == "max_limit":
                    self._execute_max_limit_step(track)
                elif mode == "custom_interval":
                    self._execute_custom_interval_step(track)
                else:
                    self._execute_realistic_step(track)

                self.consecutive_errors = 0

            except LastFMRateLimitError as e:
                logger.warning(f"Rate limited by Last.fm: {e}. Sleeping 90s...")
                if self.stop_event.wait(timeout=90):
                    break

            except LastFMTemporaryError as e:
                self.consecutive_errors += 1
                backoff = min(300, 5 * (2 ** min(self.consecutive_errors, 6)))
                logger.warning(f"Last.fm service issue: {e}. Backing off {backoff}s...")
                if self.stop_event.wait(timeout=backoff):
                    break

            except LastFMAuthError as e:
                logger.error(f"Fatal Last.fm authentication error: {e}")
                break

            except Exception as e:
                logger.exception(f"Unexpected error: {e}")
                if self.stop_event.wait(timeout=10):
                    break

        logger.info("Scrobbler engine stopped cleanly.")

    def _check_and_enforce_daily_quota(self):
        """Pauses execution if rolling 24-hour limit is reached."""
        allowed, wait_seconds = self.tracker.can_scrobble(
            max_daily_limit=self.cfg.engine.max_daily_scrobbles
        )
        if not allowed:
            mins, secs = divmod(wait_seconds, 60)
            rolling_count = self.tracker.get_rolling_24h_count()
            logger.info(
                f"[QUOTA GUARD] Cap reached ({rolling_count}/{self.cfg.engine.max_daily_scrobbles}). "
                f"Pausing for {mins}m {secs:02d}s until rolling window clears..."
            )
            self.stop_event.wait(timeout=wait_seconds)

    def _execute_realistic_step(self, track: Track):
        """Simulates real-time human listening and scrobbles at half duration."""
        track_start_time = int(time.time())
        duration_sec = track.duration_sec
        scrobble_duration = max(30, duration_sec)

        if self.cfg.engine.update_now_playing:
            self.lastfm.update_now_playing(track)

        # Scrobble threshold: min(50% of track, 240s), enforcing Last.fm platform minimum 30s
        scrobble_threshold = max(30, min(scrobble_duration // 2, 240))
        remaining_duration = max(0, duration_sec - scrobble_threshold)

        logger.info(f"[NOW PLAYING] {track.display_name} [{track.formatted_duration}] (Scrobbling in {scrobble_threshold}s)")

        if self.stop_event.wait(timeout=scrobble_threshold):
            return

        self.lastfm.scrobble(track, timestamp=track_start_time)
        self.tracker.record_scrobble(track.artist, track.title, timestamp=track_start_time)
        rolling_count = self.tracker.get_rolling_24h_count()
        logger.info(
            f"[SCROBBLED] {track.display_name} | "
            f"Daily: {rolling_count}/{self.cfg.engine.max_daily_scrobbles} | "
            f"Total: {self.tracker.get_total_scrobbles()}"
        )

        if remaining_duration > 0:
            if self.stop_event.wait(timeout=remaining_duration):
                return

        jitter = random.uniform(
            self.cfg.engine.inter_track_pause_min,
            self.cfg.engine.inter_track_pause_max,
        )
        self.stop_event.wait(timeout=jitter)

    def _execute_max_limit_step(self, track: Track):
        """Paces at ~32s with sequential non-overlapping timeline backdating."""
        now = int(time.time())

        if self.virtual_timeline_cursor is None or self.virtual_timeline_cursor >= (now - 60):
            self.virtual_timeline_cursor = now - 86400

        track_timestamp = self.virtual_timeline_cursor
        duration_sec = track.duration_sec
        scrobble_duration = max(30, duration_sec)

        self.lastfm.scrobble(track, timestamp=track_timestamp)
        self.tracker.record_scrobble(track.artist, track.title, timestamp=now)

        self.virtual_timeline_cursor += scrobble_duration + 2
        self.tracker.set_state("virtual_timeline_cursor", str(self.virtual_timeline_cursor))

        rolling_count = self.tracker.get_rolling_24h_count()
        logger.info(
            f"[MAX_LIMIT SCROBBLE] {track.display_name} [{track.formatted_duration}] | "
            f"Daily: {rolling_count}/{self.cfg.engine.max_daily_scrobbles} | "
            f"Total: {self.tracker.get_total_scrobbles()}"
        )

        pace_delay = random.uniform(31.5, 33.0)
        self.stop_event.wait(timeout=pace_delay)

    def _execute_custom_interval_step(self, track: Track):
        """Scrobbles with a user-defined fixed interval."""
        now = int(time.time())
        self.lastfm.scrobble(track, timestamp=now)
        self.tracker.record_scrobble(track.artist, track.title, timestamp=now)

        rolling_count = self.tracker.get_rolling_24h_count()
        logger.info(
            f"[CUSTOM SCROBBLE] {track.display_name} | "
            f"Daily: {rolling_count}/{self.cfg.engine.max_daily_scrobbles} | "
            f"Total: {self.tracker.get_total_scrobbles()}"
        )

        self.stop_event.wait(timeout=self.cfg.engine.custom_interval_seconds)

    def _check_periodic_refresh(self):
        """Refreshes source playlists at configured hour intervals."""
        if not self.spotify or not self.cfg.spotify.sources:
            return

        interval_sec = self.cfg.spotify.refresh_interval_hours * 3600
        now = time.time()
        if (now - self.last_refresh_time) >= interval_sec:
            logger.info("Executing scheduled refresh of Spotify sources...")
            try:
                fresh_tracks = self.spotify.fetch_sources(
                    self.cfg.spotify.sources,
                    use_cache_if_available=False
                )
                if fresh_tracks:
                    self.queue.update_tracks(fresh_tracks)
                self.last_refresh_time = now
            except Exception as e:
                logger.warning(f"Periodic playlist refresh failed: {e}")
