"""youtube source adapter implementing the sourceclient interface."""
from dataclasses import dataclass, field
import hashlib
import json
import logging
from pathlib import Path
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qs, urlparse

import requests

from src.core.models import CanonicalTrack
from .base import SourceClient
from .exceptions import (
    SourceAuthError,
    SourceError,
    SourceTemporaryError,
    UnsupportedSourceError,
    extract_http_metadata,
)

logger = logging.getLogger("scrobbler.sources.youtube")

# allowed hostnames for strict url validation and spoofing defense
_ALLOWED_HOSTS: Set[str] = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
}

# standard request headers for desktop youtubei web client emulation
_YOUTUBEI_HEADERS: Dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Origin": "https://music.youtube.com",
    "Referer": "https://music.youtube.com/",
}

# client context for youtube music internal api (web_remix)
_CLIENT_CONTEXT: Dict[str, Any] = {
    "client": {
        "clientName": "WEB_REMIX",
        "clientVersion": "1.20240401.01.00",
        "hl": "en",
        "gl": "US",
    }
}

# regex patterns for cleaning noise from raw youtube video titles
_CLEAN_TITLE_PATTERNS: List[str] = [
    # trailing pipe tags like: | (music video), | music video, | fan edit, | lyric video
    r"\s*\|\s*[\(\[]?\s*(?:official\s+)?(?:music\s+video|video|audio|visualizer|fan\s+edit|lyrics?)\s*[\)\]]?",
    # bracketed or parenthesized video/visualizer labels: (official video), [official music video], (music video), (visualizer), etc.
    r"\s*[\(\[]\s*(?:official\s+)?(?:music\s+)?video\s*[\)\]]",
    r"\s*[\(\[]\s*official\s+audio\s*[\)\]]",
    r"\s*[\(\[]\s*(?:official\s+)?visualizer\s*[\)\]]",
    # lyric video tags: (lyrics), [lyric video], (official lyric video), etc.
    r"\s*[\(\[]\s*(?:official\s+)?lyric(?:s)?(?:\s+video)?\s*[\)\]]",
    # keep version labels in the title.
    # standalone (audio) when in brackets or parens
    r"\s*[\(\[]\s*audio\s*[\)\]]",
    # remove video quality labels.
    r"\s*[\(\[]\s*(?:hd|hq|4k|8k)\s*[\)\]]",
]


@dataclass
class ItemOmission:
    """record of an observable item-level omission during youtube ingestion."""

    video_id: Optional[str]
    title: Optional[str]
    reason: str  # "unavailable_item", "ambiguous_metadata", "private_video", "deleted_video"
    details: str


def parse_youtube_url(url: str) -> Optional[Tuple[str, str]]:
    """check the host and return the source type and id, or none."""
    if not url or not isinstance(url, str):
        return None

    raw = url.strip()
    if not raw:
        return None

    try:
        parsed = urlparse(raw)
    except Exception:
        return None

    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        return None

    hostname = (parsed.hostname or "").lower()
    if hostname not in _ALLOWED_HOSTS:
        return None

    # short link format: youtu.be/<video_id>
    if hostname == "youtu.be":
        path_clean = parsed.path.strip("/")
        if path_clean and "/" not in path_clean:
            return ("track", path_clean)
        return None

    path = parsed.path.rstrip("/").lower()
    qs = {k.lower(): v for k, v in parse_qs(parsed.query).items()}

    if path == "/playlist":
        playlist_ids = qs.get("list")
        if playlist_ids and playlist_ids[0].strip():
            return ("playlist", playlist_ids[0].strip())
        return None

    if path == "/watch":
        video_ids = qs.get("v")
        if video_ids and video_ids[0].strip():
            return ("track", video_ids[0].strip())
        return None

    return None


class YouTubeMetadataNormalizer:
    """normalizes youtube video titles, artist listings, and duration formats."""

    _INVALID_ARTIST_NAMES = {
        "unidentified",
        "unknown",
        "unknown artist",
        "various artists",
        "various",
        "n/a",
        "none",
    }

    @classmethod
    def is_valid_artist(cls, name: Optional[str]) -> bool:
        """check if an artist name is non-empty, not a generic placeholder, and not a video label."""
        if not name or not isinstance(name, str):
            return False
        clean = name.strip()
        if not clean:
            return False
        lower = clean.lower()
        if lower in cls._INVALID_ARTIST_NAMES:
            return False
        if any(term in lower for term in ("music video", "official video", "lyric video", "fan edit", "visualizer")):
            return False
        return True

    @classmethod
    def normalize_artist(cls, artist: str) -> str:
        """normalize artist name representations and encoding anomalies."""
        if not artist or not isinstance(artist, str):
            return ""
        a = artist.strip()
        if a in ("JAŸ-Z", "JA\u0178-Z", "JA\ufffd-Z", "JA?-Z"):
            return "JAY-Z"
        return a

    @classmethod
    def clean_title(cls, raw_title: str) -> str:
        """strip standard video noise patterns like '(official music video)', '(lyrics)', and '[hd]'."""
        if not raw_title or not isinstance(raw_title, str):
            return ""
        cleaned = raw_title.replace("\ufffd", "-").replace("\u2013", "-").replace("\u2014", "-")
        for pattern in _CLEAN_TITLE_PATTERNS:
            cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        cleaned = cleaned.rstrip("|-–— ").strip()
        cleaned = cleaned.strip("\"' ")
        return cleaned

    @classmethod
    def extract_artist_and_title(
        cls,
        raw_title: str,
        channel_title: Optional[str] = None,
    ) -> Tuple[str, List[str], str]:
        """find artist and title from a topic channel or an explicit artist prefix."""
        cleaned = cls.clean_title(raw_title)

        # check for standard artist - title delimiters: ' - ', ' – ', ' — ', ' | '
        delimiters = [" - ", " – ", " — ", " | "]

        # if channel is a verified topic channel, prioritize topic artist identity
        if channel_title:
            norm_channel = channel_title.strip()
            if norm_channel.endswith(" - Topic"):
                topic_artist = cls.normalize_artist(norm_channel[:-8].strip())
                if cls.is_valid_artist(topic_artist):
                    # artist names can contain commas and '&'.
                    # match the full name before splitting artists.
                    for delim in delimiters:
                        prefix = topic_artist + delim
                        if cleaned.lower().startswith(prefix.lower()):
                            title = cleaned[len(prefix):].strip()
                            if title:
                                return title, [topic_artist], "reliable_parsed"
                    # check if the title explicitly includes the topic artist as a prefix delimiter
                    for delim in delimiters:
                        if delim in cleaned:
                            parts = cleaned.split(delim, 1)
                            artist_raw = parts[0].strip()
                            title_raw = parts[1].strip()
                            if artist_raw and title_raw:
                                raw_splits = [
                                    cls.normalize_artist(a.strip())
                                    for a in re.split(r",\s*|\s+(?:feat\.?|ft\.?|&|[xX])\s+", artist_raw)
                                    if a.strip()
                                ]
                                valid_artists = [a for a in raw_splits if cls.is_valid_artist(a)]
                                if any(a.lower() == topic_artist.lower() for a in valid_artists):
                                    return title_raw, valid_artists, "reliable_parsed"

                    # otherwise, title delimiters are part of the song title/version suffix
                    return cleaned, [topic_artist], "topic_channel"

        # if not a topic channel, check for standard artist - title delimiters
        for delim in delimiters:
            if delim in cleaned:
                parts = cleaned.split(delim, 1)
                artist_raw = parts[0].strip()
                title_raw = parts[1].strip()

                if artist_raw and title_raw:
                    # split multiple artists if separated by standard connectors
                    raw_splits = [
                        cls.normalize_artist(a.strip())
                        for a in re.split(r",\s*|\s+(?:feat\.?|ft\.?|&|[xX])\s+", artist_raw)
                        if a.strip()
                    ]
                    valid_artists = [a for a in raw_splits if cls.is_valid_artist(a)]
                    if valid_artists:
                        return title_raw, valid_artists, "reliable_parsed"

        # ambiguous metadata: artist cannot be determined with adequate confidence
        return cleaned, [], "ambiguous"

    @classmethod
    def parse_iso8601_duration(cls, duration_str: str) -> Optional[int]:
        """parse an iso 8601 duration string like 'pt3m45s' into milliseconds."""
        if not duration_str or not isinstance(duration_str, str):
            return None

        match = re.match(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$", duration_str)
        if not match:
            return None

        hours = int(match.group(1) or 0)
        minutes = int(match.group(2) or 0)
        seconds = int(match.group(3) or 0)
        total_seconds = hours * 3600 + minutes * 60 + seconds
        return total_seconds * 1000 if total_seconds > 0 else None

    @classmethod
    def album_from_description(
        cls, description: str, title: str, artists: List[str], channel: Optional[str]
    ) -> Optional[str]:
        """read the album from a matching topic description."""
        if not isinstance(description, str) or not channel or not channel.endswith(" - Topic"):
            return None
        blocks = re.split(r"\n\s*\n", description.strip().replace("\r\n", "\n"))
        if len(blocks) < 5 or not blocks[0].startswith("Provided to YouTube by "):
            return None
        if blocks[-1].strip() != "Auto-generated by YouTube.":
            return None
        credits = [s.strip() for s in blocks[1].split(" · ")]
        if len(credits) < 2 or credits[0].casefold() != title.casefold():
            return None
        credited_artists = {cls.normalize_artist(a).casefold() for a in credits[1:]}
        expected_artists = {a.casefold() for a in artists}
        topic_artist = cls.normalize_artist(channel[:-8].strip()).casefold()
        if credited_artists != expected_artists or topic_artist not in expected_artists:
            return None
        album = blocks[2].strip()
        if not album or "\n" in album or album.startswith(("℗", "©", "Released on:", "http")):
            return None
        if not any(b.startswith(("℗", "©")) for b in blocks[3:-1]):
            return None
        return album

    @classmethod
    def parse_time_duration(cls, time_str: str) -> Optional[int]:
        """parse a clock string like '3:45' or '1:02:30' into milliseconds."""
        if not time_str or not isinstance(time_str, str):
            return None

        parts = time_str.strip().split(":")
        try:
            if len(parts) == 2:
                mins, secs = int(parts[0]), int(parts[1])
                return (mins * 60 + secs) * 1000
            elif len(parts) == 3:
                hrs, mins, secs = int(parts[0]), int(parts[1]), int(parts[2])
                return (hrs * 3600 + mins * 60 + secs) * 1000
        except (ValueError, TypeError):
            return None
        return None


class YouTubeAdapter(SourceClient):
    """adapter for ingesting tracks from youtube and youtube music into canonicaltrack."""

    provider_id: str = "youtube"

    def __init__(
        self,
        api_key: Optional[str] = None,
        session: Optional[requests.Session] = None,
        timeout: float = 10.0,
        max_pages: int = 20,
        cache_path: Optional[Path] = None,
        ttl_hours: float = 12.0,
    ):
        raw_key = str(api_key).strip() if api_key else ""
        self.api_key = raw_key if raw_key and "placeholder" not in raw_key.lower() and "your_" not in raw_key.lower() else None
        self._session = session or requests.Session()
        self.timeout = float(timeout)
        self.max_pages = int(max_pages)
        self.cache_path = Path(cache_path) if cache_path else None
        self.ttl_hours = float(ttl_hours)
        self.last_omissions: List[ItemOmission] = []

    @classmethod
    def supports_url(cls, url: str) -> bool:
        """check whether the given url is a supported youtube or youtube music reference."""
        return parse_youtube_url(url) is not None

    def _translate_request_error(self, exc: Exception, context_msg: str) -> None:
        """keep http status and retry details in source errors."""
        status_code, retry_after = extract_http_metadata(exc)
        safe_exc_str = str(exc)
        if self.api_key and self.api_key in safe_exc_str:
            safe_exc_str = safe_exc_str.replace(self.api_key, "[REDACTED]")
        if status_code in (401, 403):
            raise SourceAuthError(
                f"{context_msg}: authentication/authorization failed ({status_code})",
                status_code=status_code,
            ) from exc
        if status_code == 429:
            raise SourceTemporaryError(
                f"{context_msg}: rate limit exceeded (429)",
                status_code=429,
                retry_after=retry_after,
            ) from exc
        if status_code is not None and status_code >= 500:
            raise SourceTemporaryError(
                f"{context_msg}: upstream server error ({status_code})",
                status_code=status_code,
                retry_after=retry_after,
            ) from exc
        if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
            raise SourceTemporaryError(
                f"{context_msg}: network timeout or connection failure ({safe_exc_str})",
                status_code=status_code,
                retry_after=retry_after,
            ) from exc
        if isinstance(exc, SourceError):
            raise exc
        raise SourceError(f"{context_msg}: {safe_exc_str}") from exc

    # reload cached tracks with the new album and title rules.
    cache_schema_version: int = 4

    @staticmethod
    def _linked_album(runs: List[Dict[str, Any]]) -> Optional[str]:
        """read the album name from an album link."""
        names = set()
        for run in runs:
            endpoint = run.get("navigationEndpoint", {}).get("browseEndpoint", {})
            config = endpoint.get("browseEndpointContextSupportedConfigs", {}).get("browseEndpointContextMusicConfig", {})
            if config.get("pageType") == "MUSIC_PAGE_TYPE_ALBUM":
                name = run.get("text", "").strip()
                if name:
                    names.add(name)
        return next(iter(names)) if len(names) == 1 else None

    def _load_cache(self, urls: List[str]) -> Optional[List[CanonicalTrack]]:
        """read cached tracks when sources, version, and age match."""
        if not self.cache_path or not self.cache_path.is_file():
            return None

        try:
            with open(self.cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            if data.get("schema_version") != self.cache_schema_version:
                logger.info("YouTube track cache invalidated: cache normalization schema mismatch.")
                return None

            expected_hash = hashlib.sha256(
                ",".join(u.strip() for u in urls).encode("utf-8")
            ).hexdigest()
            if data.get("sources_hash") != expected_hash:
                logger.debug("YouTube track cache invalidated: source urls changed.")
                return None

            saved_at = float(data.get("saved_at", 0))
            age_hours = (time.time() - saved_at) / 3600.0
            if age_hours > self.ttl_hours:
                logger.debug(f"YouTube track cache expired: age ({age_hours:.1f}h) > ttl ({self.ttl_hours:.1f}h).")
                return None

            tracks_data = data.get("tracks", [])
            canonical_tracks: List[CanonicalTrack] = []
            for item in tracks_data:
                canonical_tracks.append(
                    CanonicalTrack(
                        title=item["title"],
                        artists=list(item.get("artists", [])),
                        album=item.get("album"),
                        duration_ms=item.get("duration_ms"),
                        isrc=item.get("isrc"),
                        external_ids=dict(item.get("external_ids", {})),
                        provider_id=item.get("provider_id", "youtube"),
                        source_uri=item.get("source_uri"),
                        source_name=item.get("source_name"),
                        album_artist=item.get("album_artist"),
                        track_number=item.get("track_number"),
                    )
                )
            logger.info(f"Loaded {len(canonical_tracks)} cached YouTube tracks from {self.cache_path}")
            return canonical_tracks
        except Exception as e:
            logger.warning(f"Could not read YouTube track cache: {e}")
            return None

    def _save_cache(self, tracks: List[CanonicalTrack], urls: List[str]) -> None:
        """atomically save canonical tracks to disk cache."""
        if not self.cache_path:
            return

        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            sources_hash = hashlib.sha256(
                ",".join(u.strip() for u in urls).encode("utf-8")
            ).hexdigest()

            payload = {
                "schema_version": self.cache_schema_version,
                "saved_at": time.time(),
                "sources": list(urls),
                "sources_hash": sources_hash,
                "tracks": [
                    {
                        "title": t.title,
                        "artists": t.artists,
                        "album": t.album,
                        "duration_ms": t.duration_ms,
                        "isrc": t.isrc,
                        "external_ids": t.external_ids,
                        "provider_id": t.provider_id,
                        "source_uri": t.source_uri,
                        "source_name": t.source_name,
                        "album_artist": t.album_artist,
                        "track_number": t.track_number,
                    }
                    for t in tracks
                ],
            }

            tmp_path = self.cache_path.with_suffix(".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            tmp_path.replace(self.cache_path)
            logger.debug(f"Cached {len(tracks)} YouTube tracks to {self.cache_path}")
        except Exception as e:
            logger.warning(f"Could not save YouTube track cache: {e}")

    def _parse_youtubei_item_renderer(
        self,
        renderer: Dict[str, Any],
        source_url: str,
        playlist_title: Optional[str] = None,
    ) -> Optional[CanonicalTrack]:
        """read track metadata or record why an item was skipped."""
        # check display policy and playability
        display_policy = renderer.get("musicItemRendererDisplayPolicy")
        if display_policy == "MUSIC_ITEM_RENDERER_DISPLAY_POLICY_GREY_OUT":
            self.last_omissions.append(
                ItemOmission(
                    video_id=None,
                    title=None,
                    reason="unavailable_item",
                    details="Item greyed out by YouTube Music display policy",
                )
            )
            return None


        video_id: Optional[str] = None
        item_data = renderer.get("playlistItemData")
        if isinstance(item_data, dict):
            video_id = item_data.get("videoId")

        if not video_id:
            # check overlay or navigation endpoints
            overlay = renderer.get("overlay", {}).get("musicItemThumbnailOverlayRenderer", {})
            nav = overlay.get("content", {}).get("musicPlayButtonRenderer", {}).get("playNavigationEndpoint", {})
            video_id = nav.get("watchEndpoint", {}).get("videoId")

        flex_cols = renderer.get("flexColumns", [])
        if not flex_cols:
            self.last_omissions.append(
                ItemOmission(
                    video_id=video_id,
                    title=None,
                    reason="unavailable_item",
                    details="Missing flexColumns in item renderer",
                )
            )
            return None

        # column 0: track title
        title_runs = (
            flex_cols[0]
            .get("musicResponsiveListItemFlexColumnRenderer", {})
            .get("text", {})
            .get("runs", [])
        )
        if not title_runs:
            self.last_omissions.append(
                ItemOmission(
                    video_id=video_id,
                    title=None,
                    reason="unavailable_item",
                    details="Missing title runs in column 0",
                )
            )
            return None

        raw_title = title_runs[0].get("text", "").strip()
        if not raw_title or raw_title in ("[Deleted video]", "[Private video]"):
            self.last_omissions.append(
                ItemOmission(
                    video_id=video_id,
                    title=raw_title,
                    reason="deleted_video" if "Deleted" in raw_title else "private_video",
                    details="Video is deleted or private",
                )
            )
            return None

        # prefer the duration column when it is available.
        duration_ms: Optional[int] = None
        fixed_cols = renderer.get("fixedColumns", [])
        if fixed_cols:
            fixed_col = fixed_cols[0]
            fixed_renderer = fixed_col.get("musicResponsiveListItemFixedColumnRenderer") or fixed_col.get(
                "musicResponsiveListItemFlexColumnRenderer", {}
            )
            fixed_runs = fixed_renderer.get("text", {}).get("runs", [])
            if fixed_runs:
                dur_text = fixed_runs[0].get("text", "").strip()
                duration_ms = YouTubeMetadataNormalizer.parse_time_duration(dur_text)

        # the third text column contains the album.
        album_name: Optional[str] = None
        if len(flex_cols) > 2:
            album_runs = (
                flex_cols[2]
                .get("musicResponsiveListItemFlexColumnRenderer", {})
                .get("text", {})
                .get("runs", [])
            )
            album_name = self._linked_album(album_runs)

        # read artists and any missing album or duration from the subtitle.
        structured_artists: List[str] = []
        has_artist_page_type = False

        if len(flex_cols) > 1:
            subtitle_runs = (
                flex_cols[1]
                .get("musicResponsiveListItemFlexColumnRenderer", {})
                .get("text", {})
                .get("runs", [])
            )
            # partition subtitle runs by delimiter ' • ' or '•'
            segments: List[List[Dict[str, Any]]] = [[]]
            for run in subtitle_runs:
                txt = run.get("text", "")
                if "•" in txt:
                    segments.append([])
                else:
                    t_clean = txt.strip()
                    if t_clean and t_clean not in ("&", ","):
                        segments[-1].append(run)

            # segment 0 contains candidate artists
            if segments and segments[0]:
                for run in segments[0]:
                    txt = run.get("text", "").strip()
                    if not txt or txt in ("&", ","):
                        continue
                    cfg = (
                        run.get("navigationEndpoint", {})
                        .get("browseEndpoint", {})
                        .get("browseEndpointContextSupportedConfigs", {})
                        .get("browseEndpointContextMusicConfig", {})
                    )
                    page_type = cfg.get("pageType")
                    if page_type == "MUSIC_PAGE_TYPE_ARTIST":
                        has_artist_page_type = True
                    elif page_type == "MUSIC_PAGE_TYPE_USER_CHANNEL":
                        # explicit uploader channel -> do not treat as music artist
                        continue
                    norm_a = YouTubeMetadataNormalizer.normalize_artist(txt)
                    if YouTubeMetadataNormalizer.is_valid_artist(norm_a):
                        structured_artists.append(norm_a)

            # check remaining segments for album and duration
            for seg in segments[1:]:
                if not seg:
                    continue
                seg_text = " ".join(r.get("text", "").strip() for r in seg)
                dur = YouTubeMetadataNormalizer.parse_time_duration(seg_text)
                if dur is not None:
                    if duration_ms is None:
                        duration_ms = dur
                elif album_name is None:
                    album_name = self._linked_album(seg)


        parsed_title, parsed_artists, confidence = (
            YouTubeMetadataNormalizer.extract_artist_and_title(raw_title)
        )

        final_artists: List[str] = []
        final_title: str = ""

        if has_artist_page_type and structured_artists:
            # use the artist identified by youtube music.
            final_artists = structured_artists
            cln_t = YouTubeMetadataNormalizer.clean_title(raw_title)
            primary = final_artists[0].lower()
            if cln_t.lower().startswith(primary + " - "):
                final_title = cln_t[len(final_artists[0]) + 3:].strip()
            elif cln_t.lower().startswith(primary + " – "):
                final_title = cln_t[len(final_artists[0]) + 3:].strip()
            elif cln_t.lower().startswith(primary + " — "):
                final_title = cln_t[len(final_artists[0]) + 3:].strip()
            else:
                final_title = cln_t
        elif confidence in ("reliable_parsed", "topic_channel") and parsed_artists:
            # prefer an explicit artist in the title over the uploader.
            final_title = parsed_title
            final_artists = [
                YouTubeMetadataNormalizer.normalize_artist(a)
                for a in parsed_artists
                if YouTubeMetadataNormalizer.is_valid_artist(a)
            ]
        elif structured_artists:
            # use the artist listed in the subtitle.
            final_artists = structured_artists
            final_title = YouTubeMetadataNormalizer.clean_title(raw_title)

        if not final_artists or not final_title:
            self.last_omissions.append(
                ItemOmission(
                    video_id=video_id,
                    title=raw_title,
                    reason="ambiguous_metadata",
                    details="Could not determine music artist with adequate confidence",
                )
            )
            return None

        external_ids = {}
        if video_id:
            external_ids["youtube"] = video_id

        return CanonicalTrack(
            title=final_title,
            artists=final_artists,
            album=album_name,
            duration_ms=duration_ms,
            isrc=None,
            external_ids=external_ids,
            provider_id="youtube",
            source_uri=source_url,
            source_name=playlist_title or source_url,
        )

    def _fetch_playlist_youtubei(
        self,
        playlist_id: str,
        source_url: str,
    ) -> List[CanonicalTrack]:
        """fetch public playlist tracks using native unauthenticated youtubei web client api."""
        browse_id = playlist_id if playlist_id.startswith("VL") else f"VL{playlist_id}"
        payload = {"context": _CLIENT_CONTEXT, "browseId": browse_id}

        try:
            resp = self._session.post(
                "https://music.youtube.com/youtubei/v1/browse",
                headers=_YOUTUBEI_HEADERS,
                json=payload,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            self._translate_request_error(exc, f"YouTube playlist fetch failed for '{source_url}'")


        playlist_title: Optional[str] = None
        header = data.get("header", {})
        for header_key in ("musicDetailHeaderRenderer", "musicResponsiveHeaderRenderer"):
            h_obj = header.get(header_key)
            if h_obj and "title" in h_obj:
                runs = h_obj["title"].get("runs", [])
                if runs:
                    playlist_title = runs[0].get("text")
                    break


        alerts = data.get("alerts", [])
        if alerts:
            for alert in alerts:
                renderer = alert.get("alertRenderer", {})
                if renderer.get("type") == "ERROR":
                    text_runs = renderer.get("text", {}).get("runs", [])
                    err_msg = "".join(r.get("text", "") for r in text_runs).strip()
                    if err_msg:
                        raise SourceError(
                            f"YouTube playlist '{source_url}' error: {err_msg}. "
                            "Ensure the playlist exists and its privacy is set to Public or Unlisted."
                        )

        # extract items shelf across both singlecolumn and twocolumn layouts
        contents_dict = data.get("contents", {})
        section_contents = []

        two_col = contents_dict.get("twoColumnBrowseResultsRenderer")
        single_col = contents_dict.get("singleColumnBrowseResultsRenderer")

        if two_col:
            sec_contents = (
                two_col.get("secondaryContents", {})
                .get("sectionListRenderer", {})
                .get("contents", [])
            )
            if sec_contents:
                section_contents = sec_contents
            else:
                tabs = two_col.get("tabs", [])
                if tabs:
                    section_contents = (
                        tabs[0]
                        .get("tabRenderer", {})
                        .get("content", {})
                        .get("sectionListRenderer", {})
                        .get("contents", [])
                    )
        elif single_col:
            tabs = single_col.get("tabs", [])
            if tabs:
                section_contents = (
                    tabs[0]
                    .get("tabRenderer", {})
                    .get("content", {})
                    .get("sectionListRenderer", {})
                    .get("contents", [])
                )

        shelf = None
        for sec in section_contents:
            if isinstance(sec, dict):
                shelf = sec.get("musicPlaylistShelfRenderer") or sec.get("musicShelfRenderer")
                if shelf:
                    break

        if not contents_dict or shelf is None:
            raise SourceError(
                f"YouTube playlist '{source_url}' not found, unavailable, or private. "
                "Ensure the playlist exists and its privacy is set to Public or Unlisted."
            )

        raw_items = shelf.get("contents", [])
        continuation_token = None
        continuations = shelf.get("continuations", [])
        if continuations:
            continuation_token = (
                continuations[0]
                .get("nextContinuationData", {})
                .get("continuation")
            )

        canonical_tracks: List[CanonicalTrack] = []
        total_raw_items = len(raw_items)

        for item_wrapper in raw_items:
            renderer = item_wrapper.get("musicResponsiveListItemRenderer")
            if renderer:
                track = self._parse_youtubei_item_renderer(renderer, source_url, playlist_title)
                if track is not None:
                    canonical_tracks.append(track)


        page_count = 1
        seen_tokens: Set[str] = set()

        while continuation_token:
            if continuation_token in seen_tokens:
                raise SourceError(
                    f"YouTube playlist pagination cycle detected for '{source_url}': "
                    f"repeated continuation token '{continuation_token}'. "
                    "Incomplete playlist cannot be safely ingested."
                )
            if page_count >= self.max_pages:
                raise SourceError(
                    f"YouTube playlist '{source_url}' exceeded maximum page limit "
                    f"({self.max_pages}) with additional tracks remaining. "
                    "Incomplete playlist cannot be safely ingested."
                )
            seen_tokens.add(continuation_token)
            page_count += 1

            cont_url = f"https://music.youtube.com/youtubei/v1/browse?continuation={continuation_token}"
            cont_payload = {"context": _CLIENT_CONTEXT}

            try:
                c_resp = self._session.post(
                    cont_url,
                    headers=_YOUTUBEI_HEADERS,
                    json=cont_payload,
                    timeout=self.timeout,
                )
                c_resp.raise_for_status()
                c_data = c_resp.json()
            except Exception as exc:
                self._translate_request_error(
                    exc,
                    f"YouTube playlist pagination failed on page {page_count} for '{source_url}'",
                )

            cont_shelf = (
                c_data.get("continuationContents", {})
                .get("musicPlaylistShelfContinuation", {})
            )
            c_items = cont_shelf.get("contents", [])
            total_raw_items += len(c_items)

            for item_wrapper in c_items:
                renderer = item_wrapper.get("musicResponsiveListItemRenderer")
                if renderer:
                    track = self._parse_youtubei_item_renderer(renderer, source_url, playlist_title)
                    if track is not None:
                        canonical_tracks.append(track)

            c_conts = cont_shelf.get("continuations", [])
            if c_conts:
                continuation_token = (
                    c_conts[0]
                    .get("nextContinuationData", {})
                    .get("continuation")
                )
            else:
                continuation_token = None

        if total_raw_items > 0 and len(canonical_tracks) == 0:
            raise SourceError(
                f"Failed to resolve reliable music tracks from '{source_url}': "
                f"all {total_raw_items} items were ambiguous or unavailable."
            )

        return canonical_tracks

    def _fetch_track_youtubei(
        self,
        video_id: str,
        source_url: str,
    ) -> List[CanonicalTrack]:
        """fetch single video metadata using unauthenticated youtubei player endpoint."""
        payload = {"context": _CLIENT_CONTEXT, "videoId": video_id}

        try:
            resp = self._session.post(
                "https://music.youtube.com/youtubei/v1/player",
                headers=_YOUTUBEI_HEADERS,
                json=payload,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            # fallback to oembed if player endpoint fails with non-auth/non-ratelimit error
            status_code, _ = extract_http_metadata(exc)
            if status_code in (401, 403, 429):
                self._translate_request_error(exc, f"YouTube player endpoint failed for '{source_url}'")
            return self._fetch_track_oembed(video_id, source_url)

        details = data.get("videoDetails", {})
        if not details:
            return self._fetch_track_oembed(video_id, source_url)

        raw_title = details.get("title", "").strip()
        author = details.get("author", "").strip()
        length_sec = 0
        try:
            length_sec = int(details.get("lengthSeconds", 0))
        except (ValueError, TypeError):
            pass

        duration_ms = length_sec * 1000 if length_sec > 0 else None

        title, artists, confidence = YouTubeMetadataNormalizer.extract_artist_and_title(
            raw_title, channel_title=author
        )
        artists = [
            YouTubeMetadataNormalizer.normalize_artist(a)
            for a in artists
            if YouTubeMetadataNormalizer.is_valid_artist(a)
        ]
        if not artists or not title:
            raise SourceError(
                f"Could not determine artist for YouTube track '{source_url}' with adequate confidence"
            )

        return [
            CanonicalTrack(
                title=title,
                artists=artists,
                album=YouTubeMetadataNormalizer.album_from_description(
                    details.get("shortDescription", ""), title, artists, author
                ),
                duration_ms=duration_ms,
                isrc=None,
                external_ids={"youtube": video_id},
                provider_id="youtube",
                source_uri=source_url,
                source_name=title,
            )
        ]

    def _fetch_track_oembed(
        self,
        video_id: str,
        source_url: str,
    ) -> List[CanonicalTrack]:
        """fetch lightweight single video metadata using official public youtube oembed service."""
        oembed_url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
        try:
            resp = self._session.get(oembed_url, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            self._translate_request_error(exc, f"YouTube oEmbed resolution failed for '{source_url}'")

        raw_title = data.get("title", "").strip()
        author = data.get("author_name", "").strip()

        title, artists, confidence = YouTubeMetadataNormalizer.extract_artist_and_title(
            raw_title, channel_title=author
        )
        artists = [
            YouTubeMetadataNormalizer.normalize_artist(a)
            for a in artists
            if YouTubeMetadataNormalizer.is_valid_artist(a)
        ]
        if not artists or not title:
            raise SourceError(
                f"Could not determine artist for YouTube track '{source_url}' with adequate confidence"
            )

        return [
            CanonicalTrack(
                title=title,
                artists=artists,
                album=None,
                duration_ms=None,  # preserve authentic none when duration unavailable via oembed
                isrc=None,
                external_ids={"youtube": video_id},
                provider_id="youtube",
                source_uri=source_url,
                source_name=title,
            )
        ]

    def _fetch_playlist_data_api_v3(
        self,
        playlist_id: str,
        source_url: str,
    ) -> List[CanonicalTrack]:
        """fetch public playlist tracks using official youtube data api v3 when api_key is configured."""
        page_token: Optional[str] = None
        page_count = 0
        raw_items: List[Dict[str, Any]] = []
        seen_page_tokens: Set[str] = set()

        while True:
            if page_token:
                if page_token in seen_page_tokens:
                    raise SourceError(
                        f"YouTube Data API v3 playlist pagination cycle detected for '{source_url}': "
                        f"repeated pageToken '{page_token}'. Incomplete playlist cannot be safely ingested."
                    )
                seen_page_tokens.add(page_token)

            if page_count >= self.max_pages:
                raise SourceError(
                    f"YouTube Data API v3 playlist '{source_url}' exceeded maximum page limit "
                    f"({self.max_pages}) with additional tracks remaining. "
                    "Incomplete playlist cannot be safely ingested."
                )

            page_count += 1
            params = {
                "part": "snippet,status,contentDetails",
                "playlistId": playlist_id,
                "maxResults": 50,
                "key": self.api_key,
            }
            if page_token:
                params["pageToken"] = page_token

            try:
                resp = self._session.get(
                    "https://www.googleapis.com/youtube/v3/playlistItems",
                    params=params,
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                self._translate_request_error(
                    exc, f"YouTube Data API v3 playlist fetch failed for '{source_url}'"
                )

            items = data.get("items", [])
            raw_items.extend(items)
            page_token = data.get("nextPageToken")
            if not page_token:
                break

        if not raw_items:
            return []

        # filter out deleted/private items and collect valid video ids
        valid_items: List[Dict[str, Any]] = []
        video_ids_to_query: List[str] = []

        for item in raw_items:
            status = item.get("status", {})
            snippet = item.get("snippet", {})
            title = snippet.get("title", "")
            video_id = snippet.get("resourceId", {}).get("videoId")

            if (
                status.get("privacyStatus") == "private"
                or title in ("Private video", "Deleted video")
                or not video_id
            ):
                self.last_omissions.append(
                    ItemOmission(
                        video_id=video_id,
                        title=title,
                        reason="private_or_deleted",
                        details="Video is private, deleted, or missing ID",
                    )
                )
                continue

            valid_items.append(item)
            video_ids_to_query.append(video_id)

        # batch query videos.list in chunks of 50 to retrieve iso 8601 durations efficiently
        duration_map: Dict[str, Optional[int]] = {}
        video_snippets: Dict[str, Dict[str, Any]] = {}
        for i in range(0, len(video_ids_to_query), 50):
            chunk = video_ids_to_query[i : i + 50]
            try:
                v_resp = self._session.get(
                    "https://www.googleapis.com/youtube/v3/videos",
                    params={
                        "part": "contentDetails,status,snippet",
                        "id": ",".join(chunk),
                        "key": self.api_key,
                    },
                    timeout=self.timeout,
                )
                v_resp.raise_for_status()
                v_data = v_resp.json()
                for v_item in v_data.get("items", []):
                    vid = v_item.get("id")
                    if vid:
                        video_snippets[vid] = v_item.get("snippet", {})
                    iso_dur = v_item.get("contentDetails", {}).get("duration")
                    if vid and iso_dur:
                        duration_map[vid] = YouTubeMetadataNormalizer.parse_iso8601_duration(iso_dur)
            except Exception as exc:
                self._translate_request_error(
                    exc, f"YouTube Data API v3 videos.list failed for '{source_url}'"
                )

        canonical_tracks: List[CanonicalTrack] = []
        for item in valid_items:
            snippet = item.get("snippet", {})
            video_id = snippet.get("resourceId", {}).get("videoId")
            raw_title = snippet.get("title", "")
            channel_title = snippet.get("videoOwnerChannelTitle")

            title, artists, confidence = YouTubeMetadataNormalizer.extract_artist_and_title(
                raw_title, channel_title=channel_title
            )
            artists = [
                YouTubeMetadataNormalizer.normalize_artist(a)
                for a in artists
                if YouTubeMetadataNormalizer.is_valid_artist(a)
            ]
            if not artists or not title:
                self.last_omissions.append(
                    ItemOmission(
                        video_id=video_id,
                        title=raw_title,
                        reason="ambiguous_metadata",
                        details="Could not determine artist with adequate confidence",
                    )
                )
                continue

            canonical_tracks.append(
                CanonicalTrack(
                    title=title,
                    artists=artists,
                    album=YouTubeMetadataNormalizer.album_from_description(
                        video_snippets.get(video_id, {}).get("description", ""),
                        title, artists, video_snippets.get(video_id, {}).get("channelTitle", ""),
                    ),
                    duration_ms=duration_map.get(video_id),
                    isrc=None,
                    external_ids={"youtube": video_id} if video_id else {},
                    provider_id="youtube",
                    source_uri=source_url,
                    source_name=raw_title,
                )
            )

        if raw_items and len(canonical_tracks) == 0:
            raise SourceError(
                f"Failed to resolve reliable music tracks from '{source_url}': "
                f"all {len(raw_items)} items were ambiguous or unavailable."
            )

        return canonical_tracks

    def _fetch_track_data_api_v3(
        self,
        video_id: str,
        source_url: str,
    ) -> List[CanonicalTrack]:
        """fetch single video metadata using official youtube data api v3."""
        try:
            resp = self._session.get(
                "https://www.googleapis.com/youtube/v3/videos",
                params={
                    "part": "snippet,contentDetails,status",
                    "id": video_id,
                    "key": self.api_key,
                },
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            self._translate_request_error(
                exc, f"YouTube Data API v3 video fetch failed for '{source_url}'"
            )

        items = data.get("items", [])
        if not items:
            raise SourceError(f"YouTube video '{source_url}' not found or unavailable")

        item = items[0]
        snippet = item.get("snippet", {})
        raw_title = snippet.get("title", "")
        channel_title = snippet.get("channelTitle")
        iso_dur = item.get("contentDetails", {}).get("duration")
        duration_ms = (
            YouTubeMetadataNormalizer.parse_iso8601_duration(iso_dur) if iso_dur else None
        )

        title, artists, confidence = YouTubeMetadataNormalizer.extract_artist_and_title(
            raw_title, channel_title=channel_title
        )
        artists = [
            YouTubeMetadataNormalizer.normalize_artist(a)
            for a in artists
            if YouTubeMetadataNormalizer.is_valid_artist(a)
        ]
        if not artists or not title:
            raise SourceError(
                f"Could not determine artist for YouTube track '{source_url}' with adequate confidence"
            )

        return [
            CanonicalTrack(
                title=title,
                artists=artists,
                album=YouTubeMetadataNormalizer.album_from_description(
                    snippet.get("description", ""), title, artists, channel_title
                ),
                duration_ms=duration_ms,
                isrc=None,
                external_ids={"youtube": video_id},
                provider_id="youtube",
                source_uri=source_url,
                source_name=title,
            )
        ]

    def fetch_sources(
        self,
        urls: List[str],
        use_cache_if_available: bool = True,
    ) -> List[CanonicalTrack]:
        """fetch and transform tracks from youtube source urls into canonicaltrack instances."""
        for url in urls:
            if not self.supports_url(url):
                raise UnsupportedSourceError(f"Unsupported YouTube source URL: '{url}'")

        if not urls:
            return []

        if use_cache_if_available:
            cached = self._load_cache(urls)
            if cached is not None:
                return cached

        self.last_omissions.clear()
        canonical_tracks: List[CanonicalTrack] = []

        for url in urls:
            ref_parsed = parse_youtube_url(url)
            if not ref_parsed:
                raise UnsupportedSourceError(f"Malformed or unsupported YouTube reference: '{url}'")
            ref_type, ref_id = ref_parsed

            if ref_type == "playlist":
                if self.api_key:
                    tracks = self._fetch_playlist_data_api_v3(ref_id, url)
                else:
                    tracks = self._fetch_playlist_youtubei(ref_id, url)
                canonical_tracks.extend(tracks)
            elif ref_type == "track":
                if self.api_key:
                    tracks = self._fetch_track_data_api_v3(ref_id, url)
                else:
                    tracks = self._fetch_track_youtubei(ref_id, url)
                canonical_tracks.extend(tracks)

        if canonical_tracks:
            self._save_cache(canonical_tracks, urls)

        return canonical_tracks

    def check_health(self) -> bool:
        """lightweight reachability check against youtube public endpoint."""
        try:
            resp = self._session.get(
                "https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v=dQw4w9WgXcQ&format=json",
                timeout=5.0,
            )
            return resp.status_code < 500
        except Exception:
            return False
