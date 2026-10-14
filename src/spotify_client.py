import base64
import binascii
import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import urlparse
import requests
from .models import Track
from .core.source_errors import SourceTemporaryError
from .core.local_tracks import load_local_fallback_tracks

logger = logging.getLogger("scrobbler.spotify")

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_METADATA_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 Version/17.0 Mobile/15E148 Safari/604.1"
)

def parse_spotify_reference(raw: str) -> Optional[Tuple[str, str]]:
    """resolve supported spotify urls and uris using the same rules."""
    if not isinstance(raw, str):
        return None
    raw = raw.strip()
    if raw.lower().startswith("spotify:"):
        parts = raw.split(":")
        if len(parts) == 3 and parts[1].lower() in ("playlist", "album", "track"):
            identifier = parts[2].split("?")[0]
            if re.fullmatch(r"[a-zA-Z0-9_-]+", identifier):
                return parts[1].lower(), identifier
        return None
    try:
        parsed = urlparse(raw)
        if parsed.scheme.lower() not in ("http", "https") or parsed.hostname != "open.spotify.com":
            return None
        parts = [part for part in parsed.path.split("/") if part]
        if parts and re.fullmatch(r"intl-[a-zA-Z-]+", parts[0]):
            parts.pop(0)
        if parts and parts[0].lower() == "embed":
            parts.pop(0)
        if len(parts) == 2 and parts[0].lower() in ("playlist", "album", "track") and re.fullmatch(r"[a-zA-Z0-9_-]+", parts[1]):
            return parts[0].lower(), parts[1]
    except ValueError:
        pass
    return None


def extract_primary_artist(raw_subtitle: str) -> str:
    """extract primary artist, handling spotify comma + non-breaking space delimiters."""
    if not raw_subtitle or not raw_subtitle.strip():
        return "Unknown Artist"

    text = raw_subtitle.strip()
    for sep in (",\xa0", ",\u00a0"):
        if sep in text:
            parts = text.split(sep, 1)
            if parts[0].strip():
                return parts[0].strip()

    return text.replace("\xa0", " ").strip()


class SpotifyIngestionError(SourceTemporaryError):
    """raised when track resolution fails."""
    pass


class SpotifyEmbedParser:
    """stateless parser for extracting tracks from spotify public embed html/json payloads."""

    @staticmethod
    def extract_artist(
        raw_artists: Any = None,
        subtitle: str = "",
        fallback: str = "Unknown Artist",
    ) -> str:
        """extract and normalize primary artist from artists array, dict, string, or subtitle."""
        if raw_artists:
            if isinstance(raw_artists, list) and len(raw_artists) > 0:
                first = raw_artists[0]
                if isinstance(first, dict):
                    name = (first.get("name") or "").strip()
                    if name:
                        return name
                elif isinstance(first, str) and first.strip():
                    return first.strip()
            elif isinstance(raw_artists, dict):
                name = (raw_artists.get("name") or "").strip()
                if name:
                    return name
            elif isinstance(raw_artists, str) and raw_artists.strip():
                return raw_artists.strip()

        if subtitle and subtitle.strip():
            extracted = extract_primary_artist(subtitle)
            if extracted and extracted != "Unknown Artist":
                return extracted

        return fallback

    @staticmethod
    def normalize_duration(val: Any, default: int = 180000) -> int:
        """normalize duration in milliseconds with fallback for missing or non-positive values."""
        if val is None:
            return default
        try:
            val_int = int(val)
            return val_int if val_int > 0 else default
        except (ValueError, TypeError):
            return default

    @staticmethod
    def extract_spotify_id(uri: Optional[str] = None, explicit_id: Optional[str] = None) -> Optional[str]:
        """extract spotify id from uri or explicit id."""
        if uri and ":" in uri:
            return uri.split(":")[-1]
        if uri and "/" in uri:
            return uri.split("/")[-1].split("?")[0]
        if explicit_id:
            return explicit_id.split("?")[0].strip()
        if uri:
            return uri.split("?")[0].strip()
        return None

    @classmethod
    def _extract_json(cls, content: str) -> dict:
        """extract next.js payload json from html markup or raw json string."""
        if not content or not content.strip():
            raise SpotifyIngestionError("No metadata found on Spotify embed page (empty content)")

        trimmed = content.strip()
        if trimmed.startswith("{") and trimmed.endswith("}"):
            try:
                return json.loads(trimmed)
            except Exception:
                pass

        # find next.js script tag in page html
        match = re.search(
            r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
            trimmed,
            re.DOTALL,
        )
        if match:
            try:
                return json.loads(match.group(1))
            except Exception as e:
                raise SpotifyIngestionError(f"Failed to parse embed JSON: {e}") from e

        if trimmed.startswith("{"):
            try:
                return json.loads(trimmed)
            except Exception as e:
                raise SpotifyIngestionError(f"Failed to parse embed JSON: {e}") from e

        raise SpotifyIngestionError("No metadata found on Spotify embed page")

    @classmethod
    def _extract_entity(cls, payload: dict) -> dict:
        """navigate next.js data paths to locate the spotify entity."""
        if not isinstance(payload, dict):
            return {}

        # standard next.js hydration path: props.pageprops.state.data.entity
        entity = (
            payload.get("props", {})
            .get("pageProps", {})
            .get("state", {})
            .get("data", {})
            .get("entity")
        )
        if isinstance(entity, dict):
            return entity

        # alternate next.js paths
        for path in [
            ("props", "pageProps", "entity"),
            ("props", "pageProps", "data", "entity"),
            ("data", "entity"),
            ("entity",),
        ]:
            curr = payload
            for key in path:
                if isinstance(curr, dict):
                    curr = curr.get(key)
                else:
                    curr = None
                    break
            if isinstance(curr, dict):
                return curr

        # direct entity payload fallback
        if any(k in payload for k in ("trackList", "tracks", "title", "name", "uri")):
            return payload

        return {}

    @classmethod
    def _infer_entity_type(cls, entity: dict) -> str:
        """infer entity type ('track', 'album', 'playlist') from entity metadata."""
        etype = (entity.get("type") or "").lower().strip()
        if etype in ("track", "album", "playlist"):
            return etype

        uri = entity.get("uri", "")
        if "spotify:track:" in uri:
            return "track"
        if "spotify:album:" in uri:
            return "album"
        if "spotify:playlist:" in uri:
            return "playlist"

        if "album_type" in entity:
            return "album"

        if "trackList" in entity or "tracks" in entity:
            return "playlist"

        if "duration" in entity or "duration_ms" in entity or "isrc" in entity or entity.get("album"):
            return "track"

        return "playlist"

    @classmethod
    def _extract_raw_tracks(cls, entity: dict) -> list:
        """extract track items list from embed entity or nested structures."""
        if "trackList" in entity and isinstance(entity["trackList"], list):
            return entity["trackList"]

        tracks_val = entity.get("tracks")
        if isinstance(tracks_val, list):
            return tracks_val
        elif isinstance(tracks_val, dict):
            items = tracks_val.get("items")
            if isinstance(items, list):
                return items

        items = entity.get("items")
        if isinstance(items, list):
            return items

        raise SpotifyIngestionError("Spotify source is missing its track list")

    @classmethod
    def _parse_track(cls, entity: dict, default_id: Optional[str] = None) -> List[Track]:
        """parse single track entity into a track list."""
        title = (entity.get("title") or entity.get("name") or "Unknown Track").strip()
        primary_artist = cls.extract_artist(entity.get("artists"), entity.get("subtitle", ""))

        album_name = ""
        album_obj = entity.get("album")
        if isinstance(album_obj, dict):
            album_name = (album_obj.get("name") or album_obj.get("title") or "").strip()
        elif isinstance(album_obj, str):
            album_name = album_obj.strip()

        album_artist = ""
        if isinstance(album_obj, dict):
            album_artist = cls.extract_artist(album_obj.get("artists"), album_obj.get("subtitle", ""), fallback="")
        if not album_artist:
            album_artist = entity.get("album_artist") or entity.get("albumArtist") or primary_artist

        duration_ms = cls.normalize_duration(entity.get("duration") or entity.get("duration_ms"))
        spotify_id = cls.extract_spotify_id(entity.get("uri"), default_id or entity.get("id"))
        track_number = int(entity.get("track_number") or entity.get("trackNumber") or 1)

        track = Track(
            title=title,
            artist=primary_artist,
            album=album_name,
            album_artist=album_artist,
            duration_ms=duration_ms,
            track_number=track_number,
            spotify_id=spotify_id,
            source_name="Single Track",
        )
        logger.info(f"Loaded single track: {track.display_name} [{track.formatted_duration}]")
        return [track]

    @classmethod
    def _parse_album(cls, entity: dict, default_id: Optional[str] = None) -> List[Track]:
        """parse album entity and track list into a track list."""
        album_name = (entity.get("name") or entity.get("title") or f"Album ({default_id or 'unknown'})").strip()
        album_artist = cls.extract_artist(entity.get("artists"), entity.get("subtitle", ""), fallback="")

        raw_tracks = cls._extract_raw_tracks(entity)
        tracks: List[Track] = []
        for idx, item in enumerate(raw_tracks, start=1):
            if isinstance(item, dict) and "track" in item and isinstance(item["track"], dict):
                item = item["track"]
            if not isinstance(item, dict):
                continue

            title = (item.get("title") or item.get("name") or "").strip()
            if not title:
                continue

            primary_artist = cls.extract_artist(item.get("artists"), item.get("subtitle", ""))
            item_album_artist = item.get("album_artist") or item.get("albumArtist") or album_artist or primary_artist
            duration_ms = cls.normalize_duration(item.get("duration") or item.get("duration_ms"))
            spotify_id = cls.extract_spotify_id(item.get("uri"), item.get("id"))
            track_number = int(item.get("track_number") or item.get("trackNumber") or idx)

            tracks.append(Track(
                title=title,
                artist=primary_artist,
                album=album_name,
                album_artist=item_album_artist,
                duration_ms=duration_ms,
                track_number=track_number,
                spotify_id=spotify_id,
                source_name=album_name,
            ))

        logger.info(f"Loaded {len(tracks)} tracks from album '{album_name}'")
        return tracks

    @classmethod
    def _parse_playlist(cls, entity: dict, default_id: Optional[str] = None) -> List[Track]:
        """parse playlist entity and track list into a track list."""
        playlist_name = (entity.get("name") or entity.get("title") or f"Playlist ({default_id or 'unknown'})").strip()
        raw_tracks = cls._extract_raw_tracks(entity)

        tracks: List[Track] = []
        for idx, item in enumerate(raw_tracks, start=1):
            if isinstance(item, dict) and "track" in item and isinstance(item["track"], dict):
                item = item["track"]
            if not isinstance(item, dict):
                continue

            title = (item.get("title") or item.get("name") or "").strip()
            if not title:
                continue

            primary_artist = cls.extract_artist(item.get("artists"), item.get("subtitle", ""))

            album_name = ""
            album_obj = item.get("album")
            if isinstance(album_obj, dict):
                album_name = (album_obj.get("name") or album_obj.get("title") or "").strip()
            elif isinstance(album_obj, str):
                album_name = album_obj.strip()

            album_artist = item.get("album_artist") or item.get("albumArtist")
            if not album_artist and isinstance(album_obj, dict):
                album_artist = cls.extract_artist(album_obj.get("artists"), fallback="")
            album_artist = album_artist or primary_artist
            duration_ms = cls.normalize_duration(item.get("duration") or item.get("duration_ms"))
            spotify_id = cls.extract_spotify_id(item.get("uri"), item.get("id"))
            raw_number = item.get("track_number") or item.get("trackNumber")
            try:
                track_number = int(raw_number) if raw_number is not None else None
                if isinstance(raw_number, bool) or (track_number is not None and track_number <= 0):
                    track_number = None
            except (ValueError, TypeError, OverflowError):
                track_number = None

            tracks.append(Track(
                title=title,
                artist=primary_artist,
                album=album_name,
                album_artist=album_artist,
                duration_ms=duration_ms,
                track_number=track_number,
                spotify_id=spotify_id,
                source_name=playlist_name,
            ))

        logger.info(f"Loaded {len(tracks)} tracks from playlist '{playlist_name}'")
        return tracks

    @classmethod
    def parse(
        cls,
        content: str,
        entity_type: Optional[str] = None,
        default_id: Optional[str] = None,
    ) -> List[Track]:
        """parse raw html or json string from spotify embed into standardized track models."""
        payload = cls._extract_json(content)
        entity = cls._extract_entity(payload)

        if not entity:
            raise SpotifyIngestionError("Spotify response is missing its source entity")

        resolved_type = (entity_type or "").lower().strip()
        if not resolved_type:
            resolved_type = cls._infer_entity_type(entity)

        if resolved_type == "track":
            return cls._parse_track(entity, default_id=default_id)
        elif resolved_type == "album":
            return cls._parse_album(entity, default_id=default_id)
        else:
            return cls._parse_playlist(entity, default_id=default_id)


def compute_sources_hash(sources: List[str]) -> str:
    """compute deterministic sha-256 hash of normalized source urls."""
    normalized = sorted([s.strip().lower() for s in sources if s.strip()])
    serialized = json.dumps(normalized)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class SpotifyClient:
    """resolve spotify playlists, albums, and tracks into standardized track models."""

    def __init__(
        self,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        cache_path: Optional[Path] = None,
        retries: int = 1,
        ttl_hours: float = 12.0,
    ):
        if client_id and "your_" in client_id.lower():
            client_id = None
        if client_secret and "your_" in client_secret.lower():
            client_secret = None

        self.client_id = client_id
        self.client_secret = client_secret
        self.cache_path = Path(cache_path) if cache_path else None
        self.retries = max(1, retries)
        self.ttl_seconds = float(ttl_hours) * 3600.0
        self._album_metadata: Dict[str, Tuple[float, Optional[dict]]] = {}
        self._metadata_retry_after = 0.0

    @staticmethod
    def _track_page_metadata(content: str, track: Track) -> Optional[dict]:
        """read the album for this track id."""
        match = re.search(r'<script\b[^>]*\bid="initialState"[^>]*>(.*?)</script>', content, re.DOTALL)
        if not match:
            return None
        state = json.loads(base64.b64decode(match.group(1).strip(), validate=True))
        uri = f"spotify:track:{track.spotify_id}"
        if not isinstance(state, dict) or not isinstance(state.get("entities"), dict):
            return None
        items = state["entities"].get("items")
        if not isinstance(items, dict):
            return None
        item = items.get(uri)
        if not isinstance(item, dict) or item.get("uri") != uri or item.get("__typename") != "Track":
            return None
        title = item.get("name", "")
        first_artist = item.get("firstArtist")
        artists = first_artist.get("items", []) if isinstance(first_artist, dict) else []
        if not isinstance(title, str) or not isinstance(artists, list) or not artists or not isinstance(artists[0], dict):
            return None
        profile = artists[0].get("profile")
        artist = profile.get("name", "") if isinstance(profile, dict) else ""
        if not isinstance(artist, str):
            return None
        if title.casefold() != track.title.casefold() or artist.casefold() != track.artist.casefold():
            return None
        album = item.get("albumOfTrack", {})
        if not isinstance(album, dict) or not isinstance(album.get("name"), str) or not isinstance(album.get("uri"), str):
            return None
        name = album.get("name", "").strip()
        if not name or not re.fullmatch(r"spotify:album:[A-Za-z0-9]{22}", album.get("uri", "")):
            return None
        number = item.get("trackNumber")
        return {"title": title, "artist": artist, "album": name,
                "track_number": number if isinstance(number, int) and not isinstance(number, bool) and number > 0 else None}

    def enrich_track_metadata(self, track: Track) -> None:
        """look up a missing album before playback and reuse the result."""
        if track.album or track.provider_id not in (None, "spotify"):
            return
        if not track.spotify_id or not re.fullmatch(r"[A-Za-z0-9]{22}", track.spotify_id):
            return
        now = time.monotonic()
        cached = self._album_metadata.get(track.spotify_id)
        if cached is not None and cached[0] > now:
            metadata = cached[1]
        else:
            if now < self._metadata_retry_after:
                return
            metadata = None
            try:
                response = requests.get(
                    f"https://open.spotify.com/track/{track.spotify_id}",
                    headers={"User-Agent": _METADATA_USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
                    timeout=10,
                )
                if response.status_code in (401, 403, 429):
                    self._metadata_retry_after = now + 900
                response.raise_for_status()
                metadata = self._track_page_metadata(response.text, track)
            except (requests.RequestException, ValueError, binascii.Error) as exc:
                logger.warning(f"[METADATA] Spotify album lookup failed for {track.display_name}: {type(exc).__name__}")
            self._album_metadata[track.spotify_id] = (
                now + (self.ttl_seconds if metadata else 300), metadata
            )
        if metadata and metadata["title"].casefold() == track.title.casefold() and metadata["artist"].casefold() == track.artist.casefold():
            track.album = metadata["album"]
            if metadata["track_number"] is not None:
                track.track_number = metadata["track_number"]
            logger.info(f"[METADATA] {track.display_name} -> album: {track.album}")

    @staticmethod
    def parse_spotify_uri(uri_or_url: str) -> Tuple[str, str]:
        """parse a spotify url, uri, or id into (item_type, item_id)."""
        raw = uri_or_url.strip()

        reference = parse_spotify_reference(raw)
        if reference is not None:
            return reference
        if re.fullmatch(r"[a-zA-Z0-9]+", raw):
            return "playlist", raw
        raise SpotifyIngestionError("Unsupported Spotify source reference")

    def load_cache(
        self,
        sources: Optional[List[str]] = None,
        max_age_seconds: Optional[float] = None,
    ) -> List[Track]:
        """load cached tracks from disk if valid, matching sources, and within ttl."""
        if not self.cache_path or not self.cache_path.is_file():
            return []
        try:
            with open(self.cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            # check cache age before changing the source order.
            ttl = max_age_seconds if max_age_seconds is not None else self.ttl_seconds
            saved_at = data.get("saved_at", 0)
            now = int(time.time())
            if (now - saved_at) >= ttl:
                logger.info(
                    f"[CACHE EXPIRED] cache age ({(now - saved_at)/3600:.1f}h) exceeded TTL ({ttl/3600:.1f}h) -> re-fetching"
                )
                return []

            if sources is not None:
                expected_hash = compute_sources_hash(sources)
                cached_hash = data.get("sources_hash")
                if cached_hash != expected_hash:
                    logger.info("[CACHE INVALID] source URLs changed -> invalidating track cache")
                    return []

                norm_req = [s.strip().lower() for s in sources if s.strip()]
                norm_cached = [s.strip().lower() for s in data.get("sources", []) if s.strip()]
                # if source order differs from how cache was saved, reconstruct from source_tracks
                if norm_req != norm_cached:
                    cached_source_tracks = data.get("source_tracks")
                    if isinstance(cached_source_tracks, dict):
                        reordered = []
                        all_found = True
                        for s in norm_req:
                            if s in cached_source_tracks:
                                reordered.extend([Track.from_dict(item) for item in cached_source_tracks[s]])
                            else:
                                all_found = False
                                break
                        if all_found and reordered:
                            logger.info(f"Loaded {len(reordered)} cached tracks reordered to match source order")
                            return reordered
                    # if per-source tracks not available in legacy cache, invalidate to enforce requested ordering
                    logger.info("[CACHE INVALID] source ordering changed without per-source breakdown -> re-fetching")
                    return []

            tracks = [Track.from_dict(item) for item in data.get("tracks", [])]
            if tracks:
                logger.info(f"Loaded {len(tracks)} cached tracks from {self.cache_path}")
            return tracks
        except Exception as e:
            logger.warning(f"Could not read track cache: {e}")
            return []

    def save_cache(
        self,
        tracks: List[Track],
        sources: List[str],
        source_tracks: Optional[Dict[str, List[Track]]] = None,
    ):
        """replace the cache using a temporary file. concurrent writers are not supported."""
        if not self.cache_path:
            return
        tmp_path = None
        try:
            if self.cache_path.is_file():
                try:
                    with open(self.cache_path, "r", encoding="utf-8") as f:
                        existing = json.load(f)
                    existing_sources = existing.get("sources", [])
                    existing_saved_at = existing.get("saved_at", 0)
                    now = int(time.time())
                    # do not overwrite a valid multi-source batch cache with a smaller subset fetch
                    if len(existing_sources) > len(sources) and (now - existing_saved_at) < self.ttl_seconds:
                        logger.debug("Preserving existing batch cache; refusing overwrite by smaller source list.")
                        return
                except Exception:
                    pass

            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "saved_at": int(time.time()),
                "sources": sources,
                "sources_hash": compute_sources_hash(sources),
                "tracks": [t.to_dict() for t in tracks],
            }
            if source_tracks is None and len(sources) == 1 and sources[0].strip():
                source_tracks = {sources[0].strip().lower(): tracks}

            if source_tracks:
                payload["source_tracks"] = {
                    s: [t.to_dict() for t in s_tracks]
                    for s, s_tracks in source_tracks.items()
                }

            tmp_path = self.cache_path.with_suffix(".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            tmp_path.replace(self.cache_path)
            tmp_path = None
            logger.debug(f"Cached {len(tracks)} tracks to {self.cache_path}")
        except Exception as e:
            if tmp_path and tmp_path.is_file():
                try:
                    tmp_path.unlink()
                except Exception:
                    pass
            logger.warning(f"Could not save track cache: {e}")

    def fetch_embed_tracks(self, item_type: str, item_id: str) -> List[Track]:
        """extract tracks and exact durations from spotify public embed page."""
        url = f"https://open.spotify.com/embed/{item_type}/{item_id}"
        headers = {"User-Agent": _USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}

        last_error = None
        response = None
        for attempt in range(self.retries):
            try:
                candidate = requests.get(url, headers=headers, timeout=15)
                candidate.raise_for_status()
                response = candidate
                break
            except Exception as e:
                last_error = e
                # let the adapter handle auth and rate limits.
                if isinstance(e, requests.HTTPError) and e.response is not None:
                    if e.response.status_code in (401, 403, 429):
                        break
                if attempt + 1 < self.retries:
                    time.sleep(0.5)

        if response is None:
            raise SpotifyIngestionError(f"Failed to access Spotify embed ({url}): {last_error}") from last_error

        return SpotifyEmbedParser.parse(
            response.text,
            entity_type=item_type,
            default_id=item_id,
        )

    def fetch_sources(self, sources: List[str], use_cache_if_available: bool = True) -> List[Track]:
        """resolve tracks from all configured sources with cache fallback."""
        if use_cache_if_available:
            cached = self.load_cache(sources=sources)
            if cached:
                return cached

        all_tracks: List[Track] = []
        source_tracks: Dict[str, List[Track]] = {}
        try:
            for source in sources:
                source = source.strip()
                if not source:
                    continue

                item_type, item_id = self.parse_spotify_uri(source)
                tracks = self.fetch_embed_tracks(item_type, item_id)
                all_tracks.extend(tracks)
                source_tracks[source.strip().lower()] = tracks

            if all_tracks:
                self.save_cache(all_tracks, sources, source_tracks=source_tracks)
                return all_tracks

        except Exception as e:
            logger.error(f"Error fetching tracks: {e}")
            cached = self.load_cache(sources=sources, max_age_seconds=float("inf"))
            if cached:
                logger.info("Using cached tracks after fetch failure.")
                return cached
            raise

        return all_tracks
