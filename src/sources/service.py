"""service bridging SourceFactory and canonical tracks to legacy Track consumers."""
import logging
from pathlib import Path
from typing import List, Optional

from src.core.models import to_legacy_track
from src.models import Track
from src.spotify_client import SpotifyClient
from .source_factory import SourceFactory

logger = logging.getLogger("scrobbler.sources.service")


class SourceIngestionService:
    """bridges SourceFactory to legacy Track workflows for engine and queue compatibility."""

    def __init__(
        self,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        cache_path: Optional[Path] = None,
        ttl_hours: float = 12.0,
        spotify_client: Optional[SpotifyClient] = None,
    ):
        self._spotify_client = spotify_client
        if spotify_client is not None:
            self.client_id = spotify_client.client_id
            self.client_secret = spotify_client.client_secret
            self.cache_path = spotify_client.cache_path
            self.ttl_hours = spotify_client.ttl_seconds / 3600.0
        else:
            self.client_id = client_id
            self.client_secret = client_secret
            self.cache_path = Path(cache_path) if cache_path else None
            self.ttl_hours = float(ttl_hours)

    @property
    def spotify_client(self) -> SpotifyClient:
        """underlying SpotifyClient instance for compatibility inspection (lazily initialized)."""
        if self._spotify_client is None:
            self._spotify_client = SpotifyClient(
                client_id=self.client_id,
                client_secret=self.client_secret,
                cache_path=self.cache_path,
                ttl_hours=self.ttl_hours,
            )
        return self._spotify_client

    def fetch_sources(
        self,
        sources: List[str],
        use_cache_if_available: bool = True,
    ) -> List[Track]:
        """fetch tracks across sources via SourceFactory and convert to legacy Track models.

        preserves source ordering, track ordering, and legacy model fields.
        raises on provider errors to avoid silent partial queues.
        """
        if not sources:
            return []

        has_spotify = any(SourceFactory.detect_source_type(s) == "spotify" for s in sources)
        provider_kwargs = {}
        if has_spotify or self._spotify_client is not None:
            provider_kwargs["spotify"] = {"client": self.spotify_client}

        canonical_tracks = SourceFactory.fetch_all_sources(
            sources=sources,
            use_cache_if_available=use_cache_if_available,
            allow_partial=False,
            provider_kwargs=provider_kwargs,
        )

        legacy_tracks: List[Track] = []
        for idx, ct in enumerate(canonical_tracks, start=1):
            legacy_tracks.append(to_legacy_track(ct, track_number=idx))
        return legacy_tracks
