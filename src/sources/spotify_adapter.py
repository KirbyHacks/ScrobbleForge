"""spotify adapter implementing the SourceClient interface."""
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from urllib.parse import urlparse

import requests

from src.core.models import CanonicalTrack, to_canonical_track
from src.models import Track
from src.spotify_client import (
    SpotifyClient,
    SpotifyIngestionError,
    _USER_AGENT,
)
from .base import SourceClient
from .exceptions import (
    SourceAuthError,
    SourceError,
    SourceTemporaryError,
    UnsupportedSourceError,
    extract_http_metadata,
)

logger = logging.getLogger("scrobbler.sources.spotify")


class SpotifyAdapter(SourceClient):
    """adapter bridging the existing SpotifyClient to the canonical SourceClient interface."""

    provider_id: str = "spotify"

    def __init__(
        self,
        client: Optional[SpotifyClient] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        cache_path: Optional[Path] = None,
        retries: int = 1,
        ttl_hours: float = 12.0,
    ):
        if client is not None:
            self._client = client
        else:
            self._client = SpotifyClient(
                client_id=client_id,
                client_secret=client_secret,
                cache_path=cache_path,
                retries=retries,
                ttl_hours=ttl_hours,
            )

    @classmethod
    def supports_url(cls, url: str) -> bool:
        """check whether the given url or uri is a supported spotify source."""
        if not url or not isinstance(url, str):
            return False

        raw = url.strip()
        if not raw:
            return False

        # spotify uri format: spotify:playlist:id, spotify:album:id, spotify:track:id
        if raw.lower().startswith("spotify:"):
            parts = raw.split(":")
            if len(parts) >= 3 and parts[1].lower() in (
                "playlist",
                "album",
                "track",
                "artist",
                "user",
            ):
                return True
            return False

        try:
            parsed = urlparse(raw)
        except Exception:
            return False

        if parsed.scheme in ("http", "https"):
            hostname = (parsed.hostname or "").lower()
            if hostname == "open.spotify.com":
                path_parts = [p for p in parsed.path.split("/") if p]
                if path_parts and path_parts[0].lower() in (
                    "playlist",
                    "album",
                    "track",
                    "embed",
                ):
                    return True
                # accept if path has at least an entity type
                if len(path_parts) >= 2:
                    return True

        return False

    @staticmethod
    def map_to_canonical(
        item: Union[Track, Dict[str, Any]],
        source_uri: Optional[str] = None,
    ) -> CanonicalTrack:
        """transform a Track instance or raw spotify dictionary into a CanonicalTrack."""
        if isinstance(item, Track):
            return to_canonical_track(item, provider_id="spotify", source_uri=source_uri)

        if isinstance(item, dict):
            title = str(item.get("title") or item.get("name") or "Unknown Track").strip()

            # preserve authentic artist ordering
            artists: List[str] = []
            raw_artists = item.get("artists")
            if isinstance(raw_artists, list):
                for a in raw_artists:
                    if isinstance(a, dict):
                        name = (a.get("name") or "").strip()
                        if name:
                            artists.append(name)
                    elif isinstance(a, str) and a.strip():
                        artists.append(a.strip())
            elif isinstance(raw_artists, str) and raw_artists.strip():
                artists.append(raw_artists.strip())

            if not artists:
                subtitle_artist = item.get("artist") or item.get("subtitle")
                if subtitle_artist and str(subtitle_artist).strip():
                    artists.append(str(subtitle_artist).strip())

            album_name: Optional[str] = None
            album_obj = item.get("album")
            if isinstance(album_obj, dict):
                album_name = album_obj.get("name") or album_obj.get("title")
            elif isinstance(album_obj, str) and album_obj.strip():
                album_name = album_obj.strip()

            duration_val = item.get("duration_ms") or item.get("duration")
            duration_ms: Optional[int] = None
            if duration_val is not None:
                try:
                    val_int = int(duration_val)
                    if val_int > 0:
                        duration_ms = val_int
                except (ValueError, TypeError):
                    pass

            isrc: Optional[str] = None
            raw_isrc = item.get("isrc")
            if not raw_isrc and isinstance(item.get("external_ids"), dict):
                raw_isrc = item["external_ids"].get("isrc")
            if raw_isrc and str(raw_isrc).strip():
                isrc = str(raw_isrc).strip()

            external_ids: Dict[str, str] = {}
            if isinstance(item.get("external_ids"), dict):
                external_ids.update({k: str(v) for k, v in item["external_ids"].items() if v})

            spotify_id = item.get("id") or item.get("spotify_id")
            if not spotify_id and item.get("uri") and "spotify:track:" in str(item.get("uri")):
                spotify_id = str(item.get("uri")).split(":")[-1]
            if spotify_id:
                external_ids["spotify"] = str(spotify_id)

            resolved_uri = source_uri or item.get("uri")
            if not resolved_uri and spotify_id:
                resolved_uri = f"spotify:track:{spotify_id}"

            source_name_val = item.get("source_name")
            return CanonicalTrack(
                title=title,
                artists=artists,
                album=album_name,
                duration_ms=duration_ms,
                isrc=isrc,
                external_ids=external_ids,
                provider_id="spotify",
                source_uri=resolved_uri,
                source_name=str(source_name_val) if source_name_val else None,
            )

        raise TypeError(f"Unsupported item type for Spotify canonical mapping: {type(item)}")

    def fetch_sources(
        self,
        urls: List[str],
        use_cache_if_available: bool = True,
    ) -> List[CanonicalTrack]:
        """fetch tracks from spotify urls using SpotifyClient and convert to canonical tracks."""
        for url in urls:
            if not self.supports_url(url):
                raise UnsupportedSourceError(
                    f"Unsupported Spotify source URL: '{url}'"
                )

        if not urls:
            return []

        try:
            legacy_tracks = self._client.fetch_sources(
                urls,
                use_cache_if_available=use_cache_if_available,
            )
        except (
            requests.exceptions.HTTPError,
            SpotifyIngestionError,
            requests.RequestException,
            ConnectionError,
            TimeoutError,
        ) as exc:
            status_code, retry_after = extract_http_metadata(exc)
            if status_code in (401, 403):
                raise SourceAuthError(
                    f"Spotify authentication failed: {exc}",
                    status_code=status_code,
                ) from exc
            if status_code == 429:
                raise SourceTemporaryError(
                    f"Spotify rate limit exceeded (429): {exc}",
                    status_code=429,
                    retry_after=retry_after,
                ) from exc
            if status_code is not None:
                raise SourceTemporaryError(
                    f"Spotify HTTP failure ({status_code}): {exc}",
                    status_code=status_code,
                    retry_after=retry_after,
                ) from exc
            raise SourceTemporaryError(f"Spotify temporary ingestion failure: {exc}") from exc
        except Exception as exc:
            if isinstance(exc, SourceError):
                raise
            raise SourceError(f"Unexpected Spotify provider failure: {exc}") from exc

        canonical_tracks: List[CanonicalTrack] = []
        for track in legacy_tracks:
            canonical_tracks.append(self.map_to_canonical(track))

        return canonical_tracks

    def check_health(self) -> bool:
        """lightweight reachability check against spotify public embed service."""
        try:
            resp = requests.head(
                "https://open.spotify.com/embed",
                headers={"User-Agent": _USER_AGENT},
                timeout=5,
            )
            return resp.status_code < 500
        except Exception:
            try:
                resp = requests.get(
                    "https://open.spotify.com/embed",
                    headers={"User-Agent": _USER_AGENT},
                    timeout=5,
                )
                return resp.status_code < 500
            except Exception:
                return False
