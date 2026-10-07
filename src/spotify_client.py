import csv
import json
import logging
import re
import time
from pathlib import Path
from typing import List, Optional, Tuple
import requests
from .models import Track

logger = logging.getLogger("scrobbler.spotify")

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


class SpotifyIngestionError(Exception):
    """Raised when track resolution fails."""
    pass


class SpotifyClient:
    """Resolves Spotify playlists, albums, and tracks into standardized Track models."""

    def __init__(
        self,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        cache_path: Optional[Path] = None,
    ):
        if client_id and "your_" in client_id.lower():
            client_id = None
        if client_secret and "your_" in client_secret.lower():
            client_secret = None

        self.client_id = client_id
        self.client_secret = client_secret
        self.cache_path = Path(cache_path) if cache_path else None
        self._access_token: Optional[str] = None
        self._token_expires: float = 0

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

    def load_cache(self) -> List[Track]:
        """Loads cached tracks from disk."""
        if not self.cache_path or not self.cache_path.is_file():
            return []
        try:
            with open(self.cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
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
                "tracks": [t.to_dict() for t in tracks],
            }
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            logger.debug(f"Cached {len(tracks)} tracks to {self.cache_path}")
        except Exception as e:
            logger.warning(f"Could not save track cache: {e}")

    def _get_api_token(self) -> Optional[str]:
        """Requests client credentials bearer token if credentials are provided."""
        if not self.client_id or not self.client_secret:
            return None
        if self._access_token and time.time() < self._token_expires:
            return self._access_token

        try:
            resp = requests.post(
                "https://accounts.spotify.com/api/token",
                data={"grant_type": "client_credentials"},
                auth=(self.client_id, self.client_secret),
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                self._access_token = data.get("access_token")
                self._token_expires = time.time() + data.get("expires_in", 3600) - 60
                return self._access_token
        except Exception as e:
            logger.warning(f"Spotify token request failed: {e}")
        return None

    def fetch_embed_tracks(self, item_type: str, item_id: str) -> List[Track]:
        """Extracts tracks and exact durations from Spotify public embed page."""
        url = f"https://open.spotify.com/embed/{item_type}/{item_id}"
        headers = {"User-Agent": _USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}

        try:
            response = requests.get(url, headers=headers, timeout=15)
            response.raise_for_status()
        except Exception as e:
            raise SpotifyIngestionError(f"Failed to access Spotify embed ({url}): {e}") from e

        match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', response.text)
        if not match:
            raise SpotifyIngestionError(f"No metadata found on Spotify embed page ({url})")

        try:
            payload = json.loads(match.group(1))
            entity = payload.get("props", {}).get("pageProps", {}).get("state", {}).get("data", {}).get("entity", {})
        except Exception as e:
            raise SpotifyIngestionError(f"Failed to parse embed JSON: {e}") from e

        # Handle single track source
        if item_type == "track":
            title = (entity.get("title") or entity.get("name") or "Unknown Track").strip()
            artists = entity.get("artists", [])
            if artists and isinstance(artists, list):
                primary_artist = artists[0].get("name", "Unknown Artist").strip()
            else:
                subtitle = entity.get("subtitle", "").replace("\xa0", " ").strip()
                primary_artist = subtitle.split(",")[0].strip() if subtitle else "Unknown Artist"

            duration_ms = entity.get("duration", 180000)
            uri = entity.get("uri", "")
            spotify_id = uri.split(":")[-1] if uri else item_id

            track = Track(
                title=title,
                artist=primary_artist,
                album="",
                album_artist=primary_artist,
                duration_ms=duration_ms,
                track_number=1,
                spotify_id=spotify_id,
                source_name="Single Track",
            )
            logger.info(f"Loaded single track: {track.display_name} [{track.formatted_duration}]")
            return [track]

        container_name = entity.get("name") or entity.get("title") or f"{item_type.capitalize()} ({item_id})"
        raw_tracks = entity.get("trackList", [])

        tracks: List[Track] = []
        for idx, item in enumerate(raw_tracks, start=1):
            title = item.get("title", "").strip()
            if not title:
                continue

            subtitle = item.get("subtitle", "").replace("\xa0", " ").strip()
            primary_artist = subtitle.split(",")[0].strip() if subtitle else "Unknown Artist"
            duration_ms = item.get("duration", 180000)
            uri = item.get("uri", "")
            spotify_id = uri.split(":")[-1] if uri else None
            album_name = container_name if item_type == "album" else ""

            tracks.append(Track(
                title=title,
                artist=primary_artist,
                album=album_name,
                album_artist=primary_artist,
                duration_ms=duration_ms,
                track_number=idx,
                spotify_id=spotify_id,
                source_name=container_name,
            ))

        logger.info(f"Loaded {len(tracks)} tracks from {item_type} '{container_name}'")
        return tracks

    def fetch_sources(self, sources: List[str], use_cache_if_available: bool = True) -> List[Track]:
        """Resolves tracks from all configured sources with cache fallback."""
        if use_cache_if_available:
            cached = self.load_cache()
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
            cached = self.load_cache()
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
                            artist=artist.split(",")[0].strip(),
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
