"""spotify adapter implementing the sourceclient interface."""
import logging
import copy
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import requests

from src.core.models import CanonicalTrack, to_canonical_track
from src.models import Track
from src.spotify_client import (
    SpotifyClient,
    SpotifyIngestionError,
    parse_spotify_reference,
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
    """adapter bridging the existing spotifyclient to the canonical sourceclient interface."""

    provider_id: str = "spotify"
    cache_schema_version: int = 2

    def __init__(
        self,
        client: Optional[SpotifyClient] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        cache_path: Optional[Path] = None,
        retries: int = 1,
        ttl_hours: float = 12.0,
        managed_cache: bool = False,
        legacy_cache_path: Optional[Path] = None,
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
        self._legacy_cache_path = legacy_cache_path or getattr(self._client, "cache_path", None)
        if managed_cache:
            self._client = copy.copy(self._client)
            self._client.cache_path = None

    def enrich_track_metadata(self, track: Track) -> None:
        if track.spotify_id and not track.album:
            self._client.enrich_track_metadata(track)

    def load_legacy_cache(self, urls: List[str]):
        """read the old batch without guessing which source owns a track."""
        if not isinstance(self._legacy_cache_path, (str, Path)):
            return None
        reader = copy.copy(self._client)
        reader.cache_path = Path(self._legacy_cache_path)
        try:
            data = json.loads(reader.cache_path.read_text(encoding="utf-8"))
            saved_sources = [source.strip() for source in data.get("sources", [])]
            if not saved_sources or not all(self.supports_url(url) for url in saved_sources):
                return None
            if len(set(url.lower() for url in saved_sources)) != len(set(saved_sources)):
                return None
            tracks = reader.load_cache(sources=saved_sources)
            if not tracks:
                return None
            raw = data.get("source_tracks", {})
            grouped = {}
            for url in set(saved_sources):
                rows = raw.get(url.lower())
                if isinstance(rows, list):
                    grouped[url] = [self.map_to_canonical(Track.from_dict(row), url) for row in rows]
            if not all(url in grouped for url in saved_sources):
                if saved_sources != urls:
                    return None
                grouped = {}
            return [self.map_to_canonical(track) for track in tracks], grouped, float(data["saved_at"])
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return None

    @classmethod
    def supports_url(cls, url: str) -> bool:
        """check whether the given url or uri is a supported spotify source."""
        return parse_spotify_reference(url) is not None

    @staticmethod
    def map_to_canonical(
        item: Union[Track, Dict[str, Any]],
        source_uri: Optional[str] = None,
    ) -> CanonicalTrack:
        """transform a track instance or raw spotify dictionary into a canonicaltrack."""
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

            album_artist: Optional[str] = None
            if isinstance(album_obj, dict):
                alb_artists = album_obj.get("artists")
                if isinstance(alb_artists, list) and alb_artists:
                    first_alb_artist = alb_artists[0]
                    if isinstance(first_alb_artist, dict):
                        album_artist = (first_alb_artist.get("name") or "").strip() or None
                    elif isinstance(first_alb_artist, str) and first_alb_artist.strip():
                        album_artist = first_alb_artist.strip()
                if not album_artist:
                    subtitle = album_obj.get("subtitle")
                    if subtitle and str(subtitle).strip():
                        album_artist = str(subtitle).strip()
            if not album_artist:
                raw_aa = item.get("album_artist") or item.get("albumArtist")
                if raw_aa and str(raw_aa).strip():
                    album_artist = str(raw_aa).strip()

            raw_track_num = item.get("track_number") or item.get("trackNumber")
            track_number: Optional[int] = None
            if raw_track_num is not None:
                try:
                    num_int = int(raw_track_num)
                    if num_int > 0:
                        track_number = num_int
                except (ValueError, TypeError):
                    pass

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
                album_artist=album_artist,
                track_number=track_number,
            )

        raise TypeError(f"Unsupported item type for Spotify canonical mapping: {type(item)}")

    def fetch_sources(
        self,
        urls: List[str],
        use_cache_if_available: bool = True,
    ) -> List[CanonicalTrack]:
        """fetch tracks from spotify urls using spotifyclient and convert to canonical tracks."""
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
