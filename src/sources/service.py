"""load independent sources through the provider registry."""
import logging
import time
from pathlib import Path
from typing import List, Optional
from dataclasses import replace

from src.core.models import to_legacy_track, valid_source_track
from src.core.sources import SourceLoadReport, SourceStatus
from src.core.source_errors import SourceError, SourceTemporaryError, UnsupportedSourceError
from src.models import Track
from .cache import SourceCache
from .source_factory import SourceFactory, _is_provider_wide_failure
from .exceptions import extract_http_metadata

logger = logging.getLogger("scrobbler.sources.service")


class SourceIngestionService:
    """preserve source order, cache ages, and recoverable failures."""

    def __init__(self, client_id=None, client_secret=None, cache_path: Optional[Path] = None,
                 ttl_hours: float = 12.0, spotify_client=None, youtube_api_key=None,
                 provider_options: Optional[dict] = None):
        self._spotify_client = spotify_client
        self.client_id = client_id
        self.client_secret = client_secret
        if spotify_client is not None:
            candidate = spotify_client.cache_path
            if isinstance(candidate, (str, Path)):
                cache_path = candidate
            ttl = spotify_client.ttl_seconds
            if isinstance(ttl, (int, float)):
                ttl_hours = ttl / 3600
        self.cache_path = Path(cache_path) if cache_path else None
        self.ttl_hours = float(ttl_hours)
        self.youtube_api_key = youtube_api_key
        self.provider_options = dict(provider_options or {})
        self._cache = SourceCache(self.cache_path)
        self._clients = {}
        self._states = {}
        self._provider_errors = {}
        self._legacy_batch = None
        self.last_report = SourceLoadReport()

    def _client(self, provider: str):
        if provider not in self._clients:
            options = dict(self.provider_options.get(provider, {}))
            # the service owns persistence; providers must report fresh failures.
            options["cache_path"] = None
            options.setdefault("ttl_hours", self.ttl_hours)
            if provider == "spotify":
                options.setdefault("client_id", self.client_id)
                options.setdefault("client_secret", self.client_secret)
                options["managed_cache"] = True
                options["legacy_cache_path"] = self.cache_path
                if self._spotify_client is not None:
                    options.setdefault("client", self._spotify_client)
            if provider == "youtube" and self.youtube_api_key:
                options.setdefault("api_key", self.youtube_api_key)
            client = SourceFactory.create_client(provider, **options)
            if client is None:
                raise UnsupportedSourceError(f"No provider registered for '{provider}'")
            self._clients[provider] = client
        return self._clients[provider]

    @property
    def spotify_client(self):
        """retain the old accessor for callers using the spotify adapter."""
        return self._client("spotify")._client

    def _migrate_legacy(self, sources: List[str]) -> None:
        if self._cache.entries or self._legacy_batch is not None or not sources:
            return
        providers = list(dict.fromkeys(SourceFactory.detect_source_type(url) for url in sources))
        for provider in providers:
            if provider == "unknown":
                continue
            urls = [url for url in sources if SourceFactory.detect_source_type(url) == provider]
            try:
                client = self._client(provider)
                legacy = client.load_legacy_cache(urls)
            except Exception as exc:
                logger.warning("Could not read legacy source cache: %s", exc)
                continue
            if legacy is None:
                continue
            tracks, grouped, saved_at = legacy
            version = getattr(client, "cache_schema_version", 1)
            if grouped:
                self._cache.write_many([(provider, url, version, rows, saved_at)
                                        for url, rows in grouped.items()])
            elif len(providers) == 1:
                # an old batch without source ownership stays whole until a full refresh.
                self._legacy_batch = (list(sources), tracks, saved_at)

    def fetch_sources_with_report(self, sources: List[str], use_cache_if_available: bool = True) -> SourceLoadReport:
        sources = [url.strip() for url in sources if url.strip()]
        if use_cache_if_available:
            self._migrate_legacy(sources)
        now = time.time()
        legacy = self._legacy_batch if self._legacy_batch and self._legacy_batch[0] == sources else None
        has_old_failures = any(self._states.get(url) and self._states[url].error for url in sources)
        if legacy and use_cache_if_available and not has_old_failures and now - legacy[2] < self.ttl_hours * 3600:
            provider = SourceFactory.detect_source_type(sources[0])
            states = tuple(SourceStatus(url, provider, None, legacy[2]) for url in sources)
            self._states.update({state.source: state for state in states})
            self.last_report = SourceLoadReport(tuple(to_legacy_track(track) for track in legacy[1]), states)
            return self.last_report

        tracks, states, writes = [], [], []
        results = {}
        for source in sources:
            if source in results:
                source_tracks, state = results[source]
                tracks.extend(to_legacy_track(track) for track in source_tracks)
                states.append(state)
                continue
            provider = SourceFactory.detect_source_type(source)
            cached = None
            source_tracks = []
            saved_at = None
            error = None
            retry_at = None
            stale = False
            try:
                if provider == "unknown":
                    raise UnsupportedSourceError(f"Unsupported source URL: '{source}'")
                client = self._client(provider)
                version = getattr(client, "cache_schema_version", 1)
                cached = self._cache.read(provider, source, version)
                old = self._states.get(source)
                blocked = self._provider_errors.get(provider)
                if use_cache_if_available and cached and not (old and old.error) and now - cached[1] < self.ttl_hours * 3600:
                    source_tracks, saved_at = cached
                else:
                    if blocked and blocked[1] > now:
                        retry_at = blocked[1]
                        raise blocked[0]
                    if use_cache_if_available and old and old.error and old.retry_at and old.retry_at > now:
                        retry_at = old.retry_at
                        raise old.error
                    source_tracks = client.fetch_sources([source], use_cache_if_available=False)
                    if not isinstance(source_tracks, list) or any(not valid_source_track(track, provider) for track in source_tracks):
                        raise SourceError("Source client returned invalid track metadata")
                    saved_at = time.time()
                    writes.append((provider, source, version, source_tracks, saved_at))
            except Exception as exc:
                source_tracks = []
                error = exc
                _, retry_after = extract_http_metadata(exc)
                retry_at = retry_at or now + max(900, retry_after or 0)
                if _is_provider_wide_failure(exc):
                    self._provider_errors[provider] = (exc, retry_at)
                if isinstance(exc, SourceTemporaryError) and cached is not None:
                    source_tracks, saved_at = cached
                    stale = True
                logger.warning("Source failed (%s): %s", source, exc)
            state = SourceStatus(source, provider, len(source_tracks), saved_at, stale, error, retry_at)
            self._states[source] = state
            results[source] = (source_tracks, state)
            tracks.extend(to_legacy_track(track) for track in source_tracks)
            states.append(state)

        failures = [state for state in states if state.error is not None]
        if legacy and failures and all(isinstance(state.error, SourceTemporaryError) for state in failures):
            tracks = [to_legacy_track(track) for track in legacy[1]]
            states = [replace(state, track_count=None, saved_at=legacy[2], stale=True) for state in states]
            self._states.update({state.source: state for state in states})
        else:
            self._legacy_batch = None
            self._cache.write_many(writes)
        self.last_report = SourceLoadReport(tuple(tracks), tuple(states))
        return self.last_report

    def fetch_sources(self, sources: List[str], use_cache_if_available: bool = True, allow_partial: bool = True) -> List[Track]:
        report = self.fetch_sources_with_report(sources, use_cache_if_available)
        if report.has_failures and (not allow_partial or not report.tracks):
            raise next(item.error for item in report.sources if item.error is not None)
        return list(report.tracks)

    def refresh_sources(self, sources: List[str], force: bool = False) -> List[Track]:
        """refresh expired sources, or explicitly refresh the whole configuration."""
        return self.fetch_sources(sources, use_cache_if_available=not force)

    def next_refresh_delay(self, sources: List[str]) -> float:
        now = time.time()
        delays = []
        for source in sources:
            state = self._states.get(source.strip())
            if state is None:
                delays.append(0.0)
            elif state.error and state.retry_at:
                delays.append(max(0.0, state.retry_at - now))
            else:
                delays.append(max(0.0, (state.saved_at or 0) + self.ttl_hours * 3600 - now))
        return min(delays, default=self.ttl_hours * 3600)

    def get_status(self) -> tuple[SourceStatus, ...]:
        """return the latest source results in configured order."""
        return self.last_report.sources

    def enrich_track_metadata(self, track: Track) -> None:
        if track.provider_id:
            self._client(track.provider_id).enrich_track_metadata(track)
