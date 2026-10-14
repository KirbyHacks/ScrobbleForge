import logging
import random
import sqlite3
import sys
import threading
import time
from typing import Optional, Any, Tuple
import requests

try:
    import pylast
except ImportError:
    pylast = None

from .config import AppConfig
from .lastfm_client import (
    LastFMAuthError,
    LastFMRateLimitError,
    LastFMDailyLimitError,
    LastFMTemporaryError,
    LastFMClient,
)
from .models import Track
from .queue_manager import QueueManager
from .quota_tracker import QuotaTracker
from .core.source_errors import SourceTemporaryError
from .core.sources import TrackSource

logger = logging.getLogger("scrobbler.engine")

LASTFM_MIN_SCROBBLE_DURATION_SEC = 30


def is_transient_error(exc: Exception) -> bool:
    """check if an error is transient and safe to retry with backoff."""
    if isinstance(exc, (LastFMTemporaryError, LastFMRateLimitError, requests.RequestException, sqlite3.OperationalError, SourceTemporaryError, ConnectionError, TimeoutError)):
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
    """coordinate playback timing, pacing modes, and quota limits."""

    def __init__(
        self,
        config: AppConfig,
        lastfm: LastFMClient,
        spotify: Optional[Any] = None,
        queue: Optional[QueueManager] = None,
        tracker: Optional[QuotaTracker] = None,
        stop_event: Optional[threading.Event] = None,
        circuit_breaker_threshold: int = 5,
        source_service: Optional[TrackSource] = None,
    ):
        self.cfg = config
        self.lastfm = lastfm
        self.source_service = source_service if source_service is not None else spotify
        self.spotify = self.source_service
        self.queue = queue
        self.tracker = tracker
        self.stop_event = stop_event

        self.last_refresh_time = time.time()
        self._refresh_retry_at = 0.0
        self.consecutive_errors = 0
        self.circuit_breaker_threshold = max(1, circuit_breaker_threshold)
        self.circuit_broken = False
        self.pending_track: Optional[Track] = None
        self._realistic_submission: Optional[Tuple[Track, int, float]] = None
        self._submission_timestamp = None
        self._submission_result = None
        self._submission_recorded = False
        self._pending_resolved = False

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
                    if cur > now:
                        if cur > now + 60:
                            logger.warning(f"[TIMELINE] persisted cursor {cur} is in the future -> clamped to now")
                        cur = now
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
                if self._pending_resolved:
                    self._commit_pending_track()
                    continue
                self._check_periodic_refresh()
                if self._submission_result is None:
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

                enrich_metadata = getattr(self.source_service, "enrich_track_metadata", None)
                if callable(enrich_metadata) and self._submission_result is None:
                    enrich_metadata(track)

                if mode == "realistic":
                    resolved = self._execute_realistic_step(track)
                elif mode == "max_limit":
                    resolved = self._execute_max_limit_step(track)
                elif mode == "custom_interval":
                    resolved = self._execute_custom_interval_step(track)
                else:
                    resolved = self._execute_realistic_step(track)

                if resolved:
                    self._pending_resolved = True
                    self._commit_pending_track()
                else:
                    # aborted before submission: do not commit progress and keep pending_track
                    if self.stop_event.is_set():
                        break
                    continue

            except LastFMDailyLimitError as e:
                logger.warning("[DAILY LIMIT] %s -> keeping the pending track and waiting one hour", e)
                if self.stop_event.wait(timeout=3600):
                    break

            except LastFMAuthError as e:
                logger.error(f"[AUTH ERROR] fatal last.fm authentication error: {e}")
                sys.exit(1)

            except Exception as e:
                if not is_transient_error(e):
                    logger.error(f"[FATAL BUG] non-transient error encountered: {type(e).__name__}: {e}")
                    raise

                self.consecutive_errors += 1
                local_save_pending = self._pending_resolved or self._submission_result is not None
                if self.consecutive_errors >= self.circuit_breaker_threshold and not local_save_pending:
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

    def _commit_pending_track(self):
        """retry local progress without repeating a completed submission."""
        self.queue.commit_track_progress()
        self.pending_track = None
        self._pending_resolved = False
        self._clear_submission()
        self.consecutive_errors = 0

    def _clear_submission(self):
        self._submission_timestamp = None
        self._submission_result = None
        self._submission_recorded = False

    def _submit_track(self, track: Track, timestamp: int) -> Tuple[bool, int]:
        """keep the response while local bookkeeping is retried."""
        if self._submission_timestamp is None or self._submission_timestamp[0] is not track:
            self._submission_timestamp = (track, timestamp)
            self._submission_result = None
            self._submission_recorded = False
        timestamp = self._submission_timestamp[1]
        if self._submission_result is None:
            accepted = self.lastfm.scrobble(track, timestamp=timestamp)
            self._submission_result = (accepted, int(time.time()))
        accepted, submitted_at = self._submission_result
        if accepted and not self._submission_recorded:
            self.tracker.record_scrobble(track.artist, track.title, timestamp=submitted_at)
            self._submission_recorded = True
        return accepted, timestamp

    def _check_and_enforce_daily_quota(self):
        while not self.stop_event.is_set():
            allowed, wait_seconds = self.tracker.can_scrobble(
                max_daily_limit=self.cfg.engine.max_daily_scrobbles
            )
            if allowed:
                return
            mins, secs = divmod(wait_seconds, 60)
            time_str = f"{mins}m {secs:02d}s" if mins > 0 else f"{secs}s"
            logger.info(
                f"[THROTTLED] quota guard engaged -> sleeping {time_str} until oldest scrobble ages out"
            )
            if self.stop_event.wait(timeout=wait_seconds):
                return

    def _execute_realistic_step(self, track: Track) -> bool:
        if self.stop_event.is_set():
            return False

        duration_sec = track.duration_sec
        # last.fm requires a minimum 30s track duration
        scrobble_duration = max(LASTFM_MIN_SCROBBLE_DURATION_SEC, duration_sec)

        # scrobble at min(50%, 240s) with 30s platform floor
        scrobble_threshold = max(LASTFM_MIN_SCROBBLE_DURATION_SEC, min(scrobble_duration // 2, 240))
        remaining_duration = max(0, duration_sec - scrobble_threshold)

        if self._realistic_submission is None or self._realistic_submission[0] is not track:
            track_start_time = int(time.time())
            if self.cfg.engine.update_now_playing:
                self.lastfm.update_now_playing(track)

            logger.info(f"[PLAYBACK] {track.display_name} ({track.formatted_duration}) -> scrobble at 50%")
            if self.stop_event.wait(timeout=scrobble_threshold):
                return False
            # retry with the same start time without replaying the track.
            self._realistic_submission = (track, track_start_time, time.monotonic())

        _, track_start_time, threshold_time = self._realistic_submission

        if self.stop_event.is_set():
            return False

        scrobbled, _ = self._submit_track(track, track_start_time)
        remaining_duration = max(0, remaining_duration - (time.monotonic() - threshold_time))
        if scrobbled:
            rolling_count = self.tracker.get_rolling_24h_count()
            logger.info(
                f"[SCROBBLED] {track.display_name} [ok] | 24h: {rolling_count:,}/{self.cfg.engine.max_daily_scrobbles:,}"
            )
        else:
            logger.warning(f"[SCROBBLE IGNORED] Last.fm ignored scrobble for {track.display_name}")

        self._realistic_submission = None
        self._clear_submission()
        if remaining_duration > 0:
            if self.stop_event.wait(timeout=remaining_duration):
                return True

        jitter = random.uniform(
            self.cfg.engine.inter_track_pause_min,
            self.cfg.engine.inter_track_pause_max,
        )
        self.stop_event.wait(timeout=jitter)
        return True

    def _execute_max_limit_step(self, track: Track) -> bool:
        now = int(time.time())

        # advance virtual timeline monotonically to avoid timestamp collisions
        if self.virtual_timeline_cursor is None:
            self.virtual_timeline_cursor = now - (3 * 86400)

        duration_sec = track.duration_sec
        # last.fm requires a minimum 30s track duration
        scrobble_duration = max(LASTFM_MIN_SCROBBLE_DURATION_SEC, duration_sec)

        wait_needed = 0
        if self.virtual_timeline_cursor > (now - scrobble_duration):
            wait_needed = self.virtual_timeline_cursor + scrobble_duration - now
            if wait_needed > 0:
                logger.info(
                    f"[TIMELINE] caught up to wall-clock present -> pausing {wait_needed}s for next non-future window"
                )
                if self.stop_event.wait(timeout=wait_needed):
                    return False
                now = int(time.time())

        if self.stop_event.is_set():
            return False

        effective_timestamp = min(self.virtual_timeline_cursor, now - scrobble_duration)

        scrobbled, effective_timestamp = self._submit_track(track, effective_timestamp)
        if scrobbled:
            rolling_count = self.tracker.get_rolling_24h_count()
            logger.info(
                f"[SCROBBLED] {track.display_name} [ok] | 24h: {rolling_count:,}/{self.cfg.engine.max_daily_scrobbles:,}"
            )
        else:
            logger.warning(f"[SCROBBLE IGNORED] Last.fm ignored scrobble for {track.display_name}")

        self.virtual_timeline_cursor = effective_timestamp + scrobble_duration + 2
        self.tracker.set_state("virtual_timeline_cursor", str(self.virtual_timeline_cursor))

        self._clear_submission()
        pace_delay = 2.0 if wait_needed > 0 else random.uniform(31.5, 33.0)
        self.stop_event.wait(timeout=pace_delay)
        return True

    def _execute_custom_interval_step(self, track: Track) -> bool:
        if self.stop_event.is_set():
            return False

        now = int(time.time())
        scrobbled, _ = self._submit_track(track, now)
        if scrobbled:
            rolling_count = self.tracker.get_rolling_24h_count()
            logger.info(
                f"[SCROBBLED] {track.display_name} [ok] | 24h: {rolling_count:,}/{self.cfg.engine.max_daily_scrobbles:,}"
            )
        else:
            logger.warning(f"[SCROBBLE IGNORED] Last.fm ignored scrobble for {track.display_name}")

        self._clear_submission()
        self.stop_event.wait(timeout=self.cfg.engine.custom_interval_seconds)
        return True

    def get_status(self) -> dict:
        """read playback state without changing queue progress."""
        get_sources = getattr(self.source_service, "get_status", None)
        return {
            "mode": self.cfg.engine.mode,
            "queue_size": self.queue.total_tracks,
            "queue_index": self.queue.current_index,
            "pending_track": self.pending_track.to_dict() if self.pending_track is not None else None,
            "quota_count": self.tracker.get_rolling_24h_count(),
            "quota_limit": self.cfg.engine.max_daily_scrobbles,
            "circuit_broken": self.circuit_broken,
            "sources": get_sources() if callable(get_sources) else (),
        }

    def refresh_sources(self, force: bool = False) -> bool:
        """refresh at a track boundary on the engine's owning thread."""
        if self.pending_track is not None or self.source_service is None:
            return False
        sources = self.cfg.sources.sources
        if not sources:
            return False
        tracks = self.source_service.fetch_sources(sources, use_cache_if_available=not force)
        self.queue.update_tracks(tracks)
        self.last_refresh_time = time.time()
        self._refresh_retry_at = 0.0
        return True

    def _check_periodic_refresh(self):
        if self.pending_track is not None or self.source_service is None:
            return
        sources = self.cfg.sources.sources
        if not sources:
            return
        now = time.time()
        if now < self._refresh_retry_at:
            return
        interval_sec = self.cfg.sources.refresh_interval_hours * 3600
        next_delay = getattr(self.source_service, "next_refresh_delay", None)
        delay = next_delay(sources) if callable(next_delay) else None
        if isinstance(delay, (int, float)):
            due = delay <= 0
        else:
            due = now - self.last_refresh_time >= interval_sec
        if not due:
            return
        logger.info("[REFRESH] refreshing configured music sources...")
        try:
            self.refresh_sources(force=not isinstance(delay, (int, float)))
        except Exception as exc:
            logger.warning("[REFRESH] source refresh failed: %s. Backing off for 15 minutes.", exc)
            self._refresh_retry_at = now + 900
            self.last_refresh_time = now - interval_sec + 900
