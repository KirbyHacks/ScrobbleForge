"""service bridging sourcefactory and canonical tracks to legacy track consumers."""
import logging
from pathlib import Path
from typing import List, Optional

from src.core.models import to_legacy_track
from src.models import Track
from src.spotify_client import SpotifyClient
from .source_factory import SourceFactory

logger = logging.getLogger("scrobbler.sources.service")


class SourceIngestionService:
    """load tracks from registered providers for the engine."""

    def __init__(
        self,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        cache_path: Optional[Path] = None,
        ttl_hours: float = 12.0,
        spotify_client: Optional[SpotifyClient] = None,
        youtube_api_key: Optional[str] = None,
        provider_options: Optional[dict] = None,
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
        self.youtube_api_key = youtube_api_key
        self.provider_options = dict(provider_options or {})

    @property
    def spotify_client(self) -> SpotifyClient:
        """create the spotify client when it is first needed."""
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
        """load tracks in source order. raise errors rather than return a partial queue."""
        if not sources:
            return []

        has_spotify = any(SourceFactory.detect_source_type(s) == "spotify" for s in sources)
        has_youtube = any(SourceFactory.detect_source_type(s) == "youtube" for s in sources)
        provider_kwargs = dict(self.provider_options)
        if has_spotify or self._spotify_client is not None:
            sp_opts = dict(provider_kwargs.get("spotify", {}))
            if "client" not in sp_opts:
                sp_opts["client"] = self.spotify_client
            provider_kwargs["spotify"] = sp_opts
        if has_youtube and self.youtube_api_key:
            yt_opts = dict(provider_kwargs.get("youtube", {}))
            if "api_key" not in yt_opts:
                yt_opts["api_key"] = self.youtube_api_key
            provider_kwargs["youtube"] = yt_opts

        canonical_tracks = SourceFactory.fetch_all_sources(
            sources=sources,
            use_cache_if_available=use_cache_if_available,
            allow_partial=False,
            provider_kwargs=provider_kwargs,
            cache_path=self.cache_path,
            ttl_hours=self.ttl_hours,
        )

        legacy_tracks: List[Track] = []
        for ct in canonical_tracks:
            legacy_tracks.append(to_legacy_track(ct))
        return legacy_tracks
