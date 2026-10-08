import csv
import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
import requests
from .models import Track

logger = logging.getLogger("scrobbler.spotify")

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

def extract_primary_artist(raw_subtitle: str) -> str:
    """Extracts primary artist, handling Spotify comma + non-breaking space delimiters."""
    if not raw_subtitle or not raw_subtitle.strip():
        return "Unknown Artist"

    text = raw_subtitle.strip()
    for sep in (",\xa0", ",\u00a0"):
        if sep in text:
            parts = text.split(sep, 1)
            if parts[0].strip():
                return parts[0].strip()

    return text.replace("\xa0", " ").strip()


class SpotifyIngestionError(Exception):
    """Raised when track resolution fails."""
    pass


class SpotifyEmbedParser:
    """Stateless parser for extracting tracks from Spotify public embed HTML/JSON payloads."""

    @staticmethod
    def extract_artist(
        raw_artists: Any = None,
        subtitle: str = "",
        fallback: str = "Unknown Artist",
    ) -> str:
        """Extracts and normalizes primary artist from artists array, dict, string, or subtitle."""
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
        """Normalizes duration in milliseconds with fallback for missing or non-positive values."""
        if val is None:
            return default
        try:
            val_int = int(val)
            return val_int if val_int > 0 else default
        except (ValueError, TypeError):
            return default

    @staticmethod
    def extract_spotify_id(uri: Optional[str] = None, explicit_id: Optional[str] = None) -> Optional[str]:
        """Extracts Spotify ID from URI or explicit ID."""
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
        """Extracts Next.js payload JSON from HTML markup or raw JSON string."""
        if not content or not content.strip():
            raise SpotifyIngestionError("No metadata found on Spotify embed page (empty content)")

        trimmed = content.strip()
        # direct json string check
        if trimmed.startswith("{") and trimmed.endswith("}"):
            try:
                return json.loads(trimmed)
            except Exception:
                pass

        # next.js script tag match
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

        # retry direct json parse if content starts with brace
        if trimmed.startswith("{"):
            try:
                return json.loads(trimmed)
            except Exception as e:
                raise SpotifyIngestionError(f"Failed to parse embed JSON: {e}") from e

        raise SpotifyIngestionError("No metadata found on Spotify embed page")

    @classmethod
    def _extract_entity(cls, payload: dict) -> dict:
        """Navigates Next.js data paths to locate the Spotify entity."""
        if not isinstance(payload, dict):
            return {}

        # standard next.js hydration path: props.pageProps.state.data.entity
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

        # Direct entity payload check
        if any(k in payload for k in ("trackList", "tracks", "title", "name", "uri")):
            return payload

        return {}

    @classmethod
    def _infer_entity_type(cls, entity: dict) -> str:
        """Infers entity type ('track', 'album', 'playlist') from entity metadata."""
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

        if "album_type" in entity or entity.get("album"):
            return "album"

        if "trackList" in entity or "tracks" in entity:
            return "playlist"

        if "duration" in entity or "duration_ms" in entity or "isrc" in entity:
            return "track"

        return "playlist"

    @classmethod
    def _extract_raw_tracks(cls, entity: dict) -> list:
        """Extracts track items list from embed entity or nested structures."""
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

        return []

    @classmethod
    def _parse_track(cls, entity: dict, default_id: Optional[str] = None) -> List[Track]:
        """Parses single track entity into a List[Track]."""
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
        """Parses album entity and track list into a List[Track]."""
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
        """Parses playlist entity and track list into a List[Track]."""
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

            album_artist = item.get("album_artist") or item.get("albumArtist") or primary_artist
            duration_ms = cls.normalize_duration(item.get("duration") or item.get("duration_ms"))
            spotify_id = cls.extract_spotify_id(item.get("uri"), item.get("id"))
            track_number = int(item.get("track_number") or item.get("trackNumber") or idx)

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
        """Parses raw HTML or JSON string from Spotify embed into standardized Track models."""
        payload = cls._extract_json(content)
        entity = cls._extract_entity(payload)

        if not entity:
            return []

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
    """Computes a deterministic SHA-256 hash of normalized source URLs."""
    normalized = sorted([s.strip().lower() for s in sources if s.strip()])
    serialized = json.dumps(normalized)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class SpotifyClient:
    """Resolves Spotify playlists, albums, and tracks into standardized Track models."""

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

    @staticmethod
    def parse_spotify_uri(uri_or_url: str) -> Tuple[str, str]:
        """Parses a Spotify URL, URI, or ID into (item_type, item_id)."""
        raw = uri_or_url.strip()

        if raw.startswith("spotify:"):
            parts = raw.split(":")
            if len(parts) >= 3:
                return parts[1].lower(), parts[2].split("?")[0]

        match = re.search(r"open\.spotify\.com/(playlist|album|track)/([a-zA-Z0-9]+)", raw)
        if match:
            return match.group(1).lower(), match.group(2)

        return "playlist", raw.split("?")[0].strip()

    def load_cache(
        self,
        sources: Optional[List[str]] = None,
        max_age_seconds: Optional[float] = None,
    ) -> List[Track]:
        """Loads cached tracks from disk if valid, matching sources, and within TTL."""
        if not self.cache_path or not self.cache_path.is_file():
            return []
        try:
            with open(self.cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            if sources is not None:
                expected_hash = compute_sources_hash(sources)
                cached_hash = data.get("sources_hash")
                if cached_hash != expected_hash:
                    logger.info("[CACHE INVALID] source URLs changed -> invalidating track cache")
                    return []

            ttl = max_age_seconds if max_age_seconds is not None else self.ttl_seconds
            saved_at = data.get("saved_at", 0)
            now = int(time.time())
            if (now - saved_at) >= ttl:
                logger.info(
                    f"[CACHE EXPIRED] cache age ({(now - saved_at)/3600:.1f}h) exceeded TTL ({ttl/3600:.1f}h) -> re-fetching"
                )
                return []

            tracks = [Track.from_dict(item) for item in data.get("tracks", [])]
            if tracks:
                logger.info(f"Loaded {len(tracks)} cached tracks from {self.cache_path}")
            return tracks
        except Exception as e:
            logger.warning(f"Could not read track cache: {e}")
            return []

    def save_cache(self, tracks: List[Track], sources: List[str]):
        """Saves tracks to disk cache."""
        if not self.cache_path:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "saved_at": int(time.time()),
                "sources": sources,
                "sources_hash": compute_sources_hash(sources),
                "tracks": [t.to_dict() for t in tracks],
            }
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            logger.debug(f"Cached {len(tracks)} tracks to {self.cache_path}")
        except Exception as e:
            logger.warning(f"Could not save track cache: {e}")

    def fetch_embed_tracks(self, item_type: str, item_id: str) -> List[Track]:
        """Extracts tracks and exact durations from Spotify public embed page."""
        url = f"https://open.spotify.com/embed/{item_type}/{item_id}"
        headers = {"User-Agent": _USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}

        last_error = None
        response = None
        for attempt in range(self.retries):
            try:
                response = requests.get(url, headers=headers, timeout=15)
                response.raise_for_status()
                break
            except Exception as e:
                last_error = e
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
        """Resolves tracks from all configured sources with cache fallback."""
        if use_cache_if_available:
            cached = self.load_cache(sources=sources)
            if cached:
                return cached

        all_tracks: List[Track] = []
        try:
            for source in sources:
                source = source.strip()
                if not source:
                    continue

                item_type, item_id = self.parse_spotify_uri(source)
                tracks = self.fetch_embed_tracks(item_type, item_id)
                all_tracks.extend(tracks)

            if all_tracks:
                self.save_cache(all_tracks, sources)
                return all_tracks

        except Exception as e:
            logger.error(f"Error fetching tracks: {e}")
            cached = self.load_cache(sources=sources, max_age_seconds=float("inf"))
            if cached:
                logger.info("Using cached tracks after fetch failure.")
                return cached
            raise

        return all_tracks


def load_local_fallback_tracks(file_path: Path) -> List[Track]:
    """Loads fallback tracks from txt, csv, or json files."""
    path = Path(file_path)
    if not path.is_file():
        return []

    tracks: List[Track] = []
    suffix = path.suffix.lower()

    if suffix == ".json":
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and "artist" in item and "title" in item:
                            tracks.append(Track.from_dict(item))
        except Exception as e:
            logger.warning(f"Could not parse JSON track list: {e}")

    elif suffix == ".csv":
        try:
            with open(path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    title = row.get("Track Name") or row.get("title") or row.get("Track")
                    artist = row.get("Artist Name(s)") or row.get("artist") or row.get("Artist")
                    album = row.get("Album Name") or row.get("album") or ""
                    duration_ms = row.get("Duration (ms)") or row.get("duration_ms")

                    if title and artist:
                        ms = int(duration_ms) if duration_ms and str(duration_ms).isdigit() else 180000
                        tracks.append(Track(
                            title=title.strip(),
                            artist=artist.strip(),
                            album=album.strip(),
                            duration_ms=ms,
                            source_name="Local CSV",
                        ))
        except Exception as e:
            logger.warning(f"Could not parse CSV track list: {e}")

    else:
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if " - " in line:
                        parts = line.split(" - ", 1)
                        tracks.append(Track(artist=parts[0].strip(), title=parts[1].strip(), source_name="Local List"))
        except Exception as e:
            logger.warning(f"Could not parse text track list: {e}")

    return tracks
