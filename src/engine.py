import logging
import random
import sqlite3
import sys
import threading
import time
from typing import Optional
import requests

try:
    import pylast
except ImportError:
    pylast = None

from .config import AppConfig
from .lastfm_client import (
    LastFMAuthError,
    LastFMRateLimitError,
    LastFMTemporaryError,
    LastFMClient,
)
from .models import Track
from .queue_manager import QueueManager
from .quota_tracker import QuotaTracker
from .spotify_client import SpotifyClient, SpotifyIngestionError

logger = logging.getLogger("scrobbler.engine")

LASTFM_MIN_SCROBBLE_DURATION_SEC = 30


def is_transient_error(exc: Exception) -> bool:
    """Identifies transient network or infrastructure errors eligible for backoff and retry."""
    if isinstance(exc, (LastFMTemporaryError, LastFMRateLimitError, requests.RequestException, sqlite3.OperationalError, SpotifyIngestionError, ConnectionError, TimeoutError)):
        return True
    if pylast is not None:
        if isinstance(exc, (pylast.NetworkError, pylast.MalformedResponseError)):
            return True
        if isinstance(exc, pylast.WSError):
            status = str(getattr(exc, "status", ""))
            details = str(getattr(exc, "details", "")).lower()
            if status in ("11", "16", "26", "29") or any(
                term in details for term in ("offline", "temporarily unavailable", "rate limit", "busy", "service")
            ):
                return True
    return False


class ScrobblerEngine:
    """Coordinates playback timing, pacing modes, and quota limits."""

    def __init__(
        self,
        config: AppConfig,
        lastfm: LastFMClient,
        spotify: Optional[SpotifyClient],
        queue: QueueManager,
        tracker: QuotaTracker,
        stop_event: threading.Event,
        circuit_breaker_threshold: int = 5,
    ):
        self.cfg = config
        self.lastfm = lastfm
        self.spotify = spotify
        self.queue = queue
        self.tracker = tracker
        self.stop_event = stop_event

        self.last_refresh_time = time.time()
        self.consecutive_errors = 0
        self.circuit_breaker_threshold = max(1, circuit_breaker_threshold)
        self.circuit_broken = False
        self.pending_track: Optional[Track] = None

        self.virtual_timeline_cursor: Optional[int] = None
        self._init_virtual_timeline()

    def _init_virtual_timeline(self):
        saved_cursor = self.tracker.get_state("virtual_timeline_cursor")
        now = int(time.time())
        if saved_cursor is not None:
            try:
                cur = int(saved_cursor)
                two_weeks_ago = now - (13 * 86400)
                if cur > two_weeks_ago:
                    self.virtual_timeline_cursor = cur
                    logger.info(f"[TIMELINE] resumed max_limit cursor at timestamp {cur}")
                    return
            except ValueError:
                pass

        # start timeline 3 days ago to keep non-overlapping sequential history
        self.virtual_timeline_cursor = now - (3 * 86400)
        self.tracker.set_state("virtual_timeline_cursor", str(self.virtual_timeline_cursor))

    def run(self):
        mode = self.cfg.engine.mode
        logger.info(f"[INIT] mode: {mode} | tracks: {self.queue.total_tracks} | quota: {self.cfg.engine.max_daily_scrobbles:,}/24h | loop: {self.cfg.engine.loop}")

        while not self.stop_event.is_set():
            try:
                self._check_periodic_refresh()
                self._check_and_enforce_daily_quota()

                if self.stop_event.is_set():
                    break

                if self.pending_track is None:
                    track = self.queue.get_next_track()
                    if track is None:
                        logger.info("[QUEUE COMPLETE] playlist queue completed")
                        break
                    self.pending_track = track
                else:
                    track = self.pending_track

                if mode == "realistic":
                    self._execute_realistic_step(track)
                elif mode == "max_limit":
                    self._execute_max_limit_step(track)
                elif mode == "custom_interval":
                    self._execute_custom_interval_step(track)
                else:
                    self._execute_realistic_step(track)

                self.queue.commit_track_progress()
                self.pending_track = None
                self.consecutive_errors = 0

            except LastFMAuthError as e:
                logger.error(f"[AUTH ERROR] fatal last.fm authentication error: {e}")
                sys.exit(1)

            except Exception as e:
                if not is_transient_error(e):
                    logger.error(f"[FATAL BUG] non-transient error encountered: {type(e).__name__}: {e}")
                    raise

                self.consecutive_errors += 1
                if self.consecutive_errors >= self.circuit_breaker_threshold:
                    self.circuit_broken = True
                    logger.critical(
                        f"[CIRCUIT BREAKER] {self.consecutive_errors} consecutive transient failures reached threshold ({self.circuit_breaker_threshold}) -> halting engine: {e}"
                    )
                    break

                if isinstance(e, LastFMRateLimitError):
                    logger.warning(
                        f"[RATE LIMITED] last.fm rate limit: {e} -> sleeping 90s (failure {self.consecutive_errors}/{self.circuit_breaker_threshold})"
                    )
                    if self.stop_event.wait(timeout=90):
                        break
                else:
                    backoff = min(300, 5 * (2 ** min(self.consecutive_errors, 6)))
                    logger.warning(
                        f"[TRANSIENT ERROR] {type(e).__name__}: {e} -> backing off {backoff}s (failure {self.consecutive_errors}/{self.circuit_breaker_threshold})"
                    )
                    if self.stop_event.wait(timeout=backoff):
                        break

        logger.info("[STOP] scrobbler engine halted cleanly")

    def _check_and_enforce_daily_quota(self):
        allowed, wait_seconds = self.tracker.can_scrobble(
            max_daily_limit=self.cfg.engine.max_daily_scrobbles
        )
        if not allowed:
            mins, secs = divmod(wait_seconds, 60)
            time_str = f"{mins}m {secs:02d}s" if mins > 0 else f"{secs}s"
            logger.info(
                f"[THROTTLED] quota guard engaged -> sleeping {time_str} until oldest scrobble ages out"
            )
            self.stop_event.wait(timeout=wait_seconds)

    def _execute_realistic_step(self, track: Track):
        track_start_time = int(time.time())
        duration_sec = track.duration_sec
        # last.fm requires minimum 30s duration
        scrobble_duration = max(LASTFM_MIN_SCROBBLE_DURATION_SEC, duration_sec)

        if self.cfg.engine.update_now_playing:
            self.lastfm.update_now_playing(track)

        # scrobble at min(50%, 240s) with 30s platform floor
        scrobble_threshold = max(LASTFM_MIN_SCROBBLE_DURATION_SEC, min(scrobble_duration // 2, 240))
        remaining_duration = max(0, duration_sec - scrobble_threshold)

        logger.info(f"[NOW PLAYING] {track.display_name} ({track.formatted_duration}) -> scrobble at 50%")

        if self.stop_event.wait(timeout=scrobble_threshold):
            return

        scrobbled = self.lastfm.scrobble(track, timestamp=track_start_time)
        now = int(time.time())
        if scrobbled:
            self.tracker.record_scrobble(track.artist, track.title, timestamp=now)
            rolling_count = self.tracker.get_rolling_24h_count()
            logger.info(
                f"[SCROBBLED] {track.display_name} [ok] | 24h: {rolling_count:,}/{self.cfg.engine.max_daily_scrobbles:,}"
            )
        else:
            logger.warning(f"[SCROBBLE IGNORED] Last.fm ignored scrobble for {track.display_name}")

        if remaining_duration > 0:
            if self.stop_event.wait(timeout=remaining_duration):
                return

        jitter = random.uniform(
            self.cfg.engine.inter_track_pause_min,
            self.cfg.engine.inter_track_pause_max,
        )
        self.stop_event.wait(timeout=jitter)

    def _execute_max_limit_step(self, track: Track):
        now = int(time.time())

        # strictly monotonic virtual timeline advancement
        if self.virtual_timeline_cursor is None:
            self.virtual_timeline_cursor = now - (3 * 86400)

        duration_sec = track.duration_sec
        # last.fm requires minimum 30s duration
        scrobble_duration = max(LASTFM_MIN_SCROBBLE_DURATION_SEC, duration_sec)

        # Present catch-up policy
        is_caught_up = self.virtual_timeline_cursor >= (now - scrobble_duration - 10)

        if self.virtual_timeline_cursor > (now - scrobble_duration):
            wait_needed = self.virtual_timeline_cursor + scrobble_duration - now
            if wait_needed > 0:
                logger.info(
                    f"[TIMELINE] caught up to wall-clock present -> pausing {wait_needed}s for next non-future window"
                )
                self.stop_event.wait(timeout=wait_needed)
                now = int(time.time())

        effective_timestamp = min(self.virtual_timeline_cursor, now - scrobble_duration)
        if effective_timestamp > now:
            effective_timestamp = now - scrobble_duration

        scrobbled = self.lastfm.scrobble(track, timestamp=effective_timestamp)
        if scrobbled:
            self.tracker.record_scrobble(track.artist, track.title, timestamp=now)
            rolling_count = self.tracker.get_rolling_24h_count()
            logger.info(
                f"[SCROBBLED] {track.display_name} [ok] | 24h: {rolling_count:,}/{self.cfg.engine.max_daily_scrobbles:,}"
            )
        else:
            logger.warning(f"[SCROBBLE IGNORED] Last.fm ignored scrobble for {track.display_name}")

        self.virtual_timeline_cursor = effective_timestamp + scrobble_duration + 2
        self.tracker.set_state("virtual_timeline_cursor", str(self.virtual_timeline_cursor))

        if is_caught_up:
            pace_delay = max(float(scrobble_duration + 2), random.uniform(31.5, 33.0))
        else:
            pace_delay = random.uniform(31.5, 33.0)

        self.stop_event.wait(timeout=pace_delay)

    def _execute_custom_interval_step(self, track: Track):
        now = int(time.time())
        scrobbled = self.lastfm.scrobble(track, timestamp=now)
        if scrobbled:
            self.tracker.record_scrobble(track.artist, track.title, timestamp=now)
            rolling_count = self.tracker.get_rolling_24h_count()
            logger.info(
                f"[SCROBBLED] {track.display_name} [ok] | 24h: {rolling_count:,}/{self.cfg.engine.max_daily_scrobbles:,}"
            )
        else:
            logger.warning(f"[SCROBBLE IGNORED] Last.fm ignored scrobble for {track.display_name}")

        self.stop_event.wait(timeout=self.cfg.engine.custom_interval_seconds)

    def _check_periodic_refresh(self):
        if not self.spotify or not self.cfg.spotify.sources:
            return

        interval_sec = self.cfg.spotify.refresh_interval_hours * 3600
        now = time.time()
        if (now - self.last_refresh_time) >= interval_sec:
            logger.info("[REFRESH] refreshing spotify sources...")
            try:
                fresh_tracks = self.spotify.fetch_sources(
                    self.cfg.spotify.sources,
                    use_cache_if_available=False
                )
                if fresh_tracks:
                    self.queue.update_tracks(fresh_tracks)
                self.last_refresh_time = now
            except Exception as e:
                logger.warning(
                    f"[REFRESH] periodic playlist refresh failed: {e}. Backing off for 15 minutes."
                )
                self.last_refresh_time = now - interval_sec + 900
