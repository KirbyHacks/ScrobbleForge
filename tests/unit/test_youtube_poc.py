"""isolated proof of concept for youtube / youtube music metadata extraction and validation."""
import json
import re
from typing import Any, Dict, List, Optional, Tuple
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import requests

from src.core.models import CanonicalTrack
from src.sources.exceptions import (
    SourceAuthError,
    SourceError,
    SourceTemporaryError,
    UnsupportedSourceError,
    extract_http_metadata,
)

_VALID_YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
}

_CLEAN_TITLE_PATTERNS = [
    r"\s*[\(\[]\s*official\s*(music\s*)?video\s*[\)\]]",
    r"\s*[\(\[]\s*official\s*audio\s*[\)\]]",
    r"\s*[\(\[]\s*official\s*lyric\s*video\s*[\)\]]",
    r"\s*[\(\[]\s*lyric\s*video\s*[\)\]]",
    r"\s*[\(\[]\s*lyrics\s*[\)\]]",
    r"\s*[\(\[]\s*audio\s*[\)\]]",
    r"\s*[\(\[]\s*visualizer\s*[\)\]]",
    r"\s*[\(\[]\s*hd\s*[\)\]]",
    r"\s*[\(\[]\s*4k\s*(remaster)?\s*[\)\]]",
    r"\s*[\(\[]\s*remaster(ed)?\s*[\)\]]",
]


class YouTubeUrlHelper:
    """isolated url validation and item identifier extractor."""

    @staticmethod
    def supports_url(url: str) -> bool:
        if not url or not isinstance(url, str):
            return False
        raw = url.strip()
        if not raw:
            return False
        try:
            parsed = urlparse(raw)
        except Exception:
            return False

        if parsed.scheme.lower() not in ("http", "https"):
            return False

        host = (parsed.hostname or "").lower()
        if host not in _VALID_YOUTUBE_HOSTS:
            return False

        path = parsed.path.lower()
        query = parse_qs(parsed.query)

        if host == "youtu.be":
            return bool(parsed.path.strip("/"))
        if path == "/playlist" and "list" in query:
            return bool(query["list"][0].strip())
        if path == "/watch" and "v" in query:
            return bool(query["v"][0].strip())

        return False

    @staticmethod
    def parse_url(url: str) -> Tuple[str, str]:
        parsed = urlparse(url.strip())
        host = (parsed.hostname or "").lower()
        if host not in _VALID_YOUTUBE_HOSTS:
            raise UnsupportedSourceError(f"Unsupported host for YouTube reference: {host}")

        query = parse_qs(parsed.query)
        path = parsed.path.lower()

        if host == "youtu.be":
            video_id = parsed.path.strip("/")
            if video_id:
                return "track", video_id
            raise UnsupportedSourceError(f"Missing video ID in short URL: {url}")

        if path == "/playlist" and "list" in query:
            return "playlist", query["list"][0].strip()

        if path == "/watch" and "v" in query:
            return "track", query["v"][0].strip()

        raise UnsupportedSourceError(f"Unrecognized YouTube URL format: {url}")


class YouTubeMetadataNormalizer:
    """normalizes youtube video titles and channel metadata into artist and title."""

    @classmethod
    def clean_title(cls, title: str) -> str:
        cleaned = title.strip()
        for pat in _CLEAN_TITLE_PATTERNS:
            cleaned = re.sub(pat, "", cleaned, flags=re.IGNORECASE)
        return cleaned.strip()

    @classmethod
    def extract_artist_and_title(
        cls,
        raw_title: str,
        channel_name: Optional[str] = None,
    ) -> Tuple[str, List[str]]:
        """returns (title, [artists]) using standard music video delimiter conventions."""
        cleaned = cls.clean_title(raw_title)

        # check for standard 'artist - title' format
        for delim in (" - ", " – ", " — "):
            if delim in cleaned:
                parts = cleaned.split(delim, 1)
                artist_part = parts[0].strip()
                title_part = parts[1].strip()
                # split multiple artists on comma or ampersand or feat
                artists = [a.strip() for a in re.split(r",|\s+&\s+|\s+feat\.?\s+|\s+ft\.?\s+", artist_part) if a.strip()]
                return title_part, (artists or [artist_part])

        # fallback: use channel name if provided (stripping topic channel suffix)
        title = cleaned
        artists: List[str] = []
        if channel_name:
            norm_channel = re.sub(r"\s*-\s*Topic$", "", channel_name, flags=re.IGNORECASE).strip()
            if norm_channel and norm_channel.lower() not in ("youtube", "vevo"):
                artists.append(norm_channel)

        if not artists:
            artists.append("Unknown Artist")

        return title, artists


class YouTubePoCParser:
    """proof of concept demonstrating public playlist and video transformation into canonicaltrack."""

    @classmethod
    def parse_youtube_api_playlist_page(
        cls,
        payload: dict,
        source_url: str,
    ) -> Tuple[List[CanonicalTrack], Optional[str]]:
        """parses official youtube data api v3 playlistitems response with video metadata."""
        items = payload.get("items", [])
        next_page_token = payload.get("nextPageToken")
        canonical: List[CanonicalTrack] = []

        for item in items:
            snippet = item.get("snippet", {})
            title_raw = snippet.get("title", "")
            # filter private or deleted entries
            if title_raw.lower() in ("private video", "deleted video"):
                continue

            video_id = snippet.get("resourceId", {}).get("videoId")
            if not video_id:
                continue

            channel_title = snippet.get("channelTitle")
            title, artists = YouTubeMetadataNormalizer.extract_artist_and_title(
                title_raw,
                channel_name=channel_title,
            )

            # duration from contentdetails if present in extended mock
            duration_ms = None
            content_details = item.get("contentDetails", {})
            dur_iso = content_details.get("duration")
            if dur_iso:
                duration_ms = cls.iso8601_to_ms(dur_iso)

            canonical.append(
                CanonicalTrack(
                    title=title,
                    artists=artists,
                    album=None,
                    duration_ms=duration_ms,
                    external_ids={"youtube": video_id},
                    provider_id="youtube",
                    source_uri=source_url,
                    source_name=snippet.get("videoOwnerChannelTitle") or channel_title,
                )
            )

        return canonical, next_page_token

    @classmethod
    def parse_youtubei_playlist(
        cls,
        payload: dict,
        source_url: str,
    ) -> List[CanonicalTrack]:
        """parses youtube music internal browse playlist response with native music metadata."""
        canonical: List[CanonicalTrack] = []
        two_col = payload.get("contents", {}).get("twoColumnBrowseResultsRenderer", {})
        sec_contents = two_col.get("secondaryContents", {}).get("sectionListRenderer", {}).get("contents", [])
        if not sec_contents:
            return canonical

        music_shelf = sec_contents[0].get("musicPlaylistShelfRenderer", {})
        for item_wrapper in music_shelf.get("contents", []):
            item = item_wrapper.get("musicResponsiveListItemRenderer", {})
            item_data = item.get("playlistItemData", {})
            video_id = item_data.get("videoId")
            if not video_id:
                continue

            flex_columns = item.get("flexColumns", [])
            if not flex_columns:
                continue

            # column 0: title runs
            title_runs = (
                flex_columns[0]
                .get("musicResponsiveListItemFlexColumnRenderer", {})
                .get("text", {})
                .get("runs", [])
            )
            title = "".join(r.get("text", "") for r in title_runs).strip()
            if not title:
                continue

            # column 1: artists & album runs
            artists: List[str] = []
            album: Optional[str] = None
            if len(flex_columns) > 1:
                col1_runs = (
                    flex_columns[1]
                    .get("musicResponsiveListItemFlexColumnRenderer", {})
                    .get("text", {})
                    .get("runs", [])
                )
                artist_candidates = [r.get("text", "").strip() for r in col1_runs if r.get("text", "").strip() not in ("&", "•", "")]
                if artist_candidates:
                    artists = [artist_candidates[0]]
                    if len(artist_candidates) > 1:
                        # last item is frequently album
                        album = artist_candidates[-1]

            if not artists:
                artists = ["Unknown Artist"]

            # fixed columns: duration
            duration_ms = None
            fixed_cols = item.get("fixedColumns", [])
            if fixed_cols:
                dur_text = (
                    fixed_cols[0]
                    .get("musicResponsiveListItemFixedColumnRenderer", {})
                    .get("text", {})
                    .get("runs", [{}])[0]
                    .get("text", "")
                )
                if dur_text and ":" in dur_text:
                    parts = dur_text.split(":")
                    if len(parts) == 2:
                        duration_ms = (int(parts[0]) * 60 + int(parts[1])) * 1000

            canonical.append(
                CanonicalTrack(
                    title=title,
                    artists=artists,
                    album=album,
                    duration_ms=duration_ms,
                    external_ids={"youtube": video_id},
                    provider_id="youtube",
                    source_uri=source_url,
                )
            )

        return canonical

    @staticmethod
    def iso8601_to_ms(duration_str: str) -> Optional[int]:
        """converts iso 8601 duration (e.g., pt3m33s) into milliseconds."""
        match = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", duration_str)
        if not match:
            return None
        hours = int(match.group(1) or 0)
        minutes = int(match.group(2) or 0)
        seconds = int(match.group(3) or 0)
        return (hours * 3600 + minutes * 60 + seconds) * 1000


class TestYouTubeProofOfConcept(unittest.TestCase):
    """unit tests validating the youtube provider proof-of-concept components."""

    def test_url_detection_supported_patterns(self):
        valid_urls = [
            "https://www.youtube.com/playlist?list=PLrAl46nJ81c_k3gD-6VnK1d0l6nQx8_i4",
            "https://youtube.com/playlist?list=PL12345",
            "https://music.youtube.com/playlist?list=PLmusic123",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ",
        ]
        for url in valid_urls:
            self.assertTrue(YouTubeUrlHelper.supports_url(url), f"Failed for valid URL: {url}")

    def test_url_detection_spoofing_and_invalid_rejected(self):
        invalid_urls = [
            "https://youtube.com.attacker.example/playlist?list=123",
            "https://notyoutube.com/watch?v=123",
            "https://music.youtube.com.fake.org/watch?v=123",
            "http://malicious.com/?url=https://youtube.com",
            "https://www.youtube.com/@rickastley/videos",  # channels out of initial scope
            "https://www.youtube.com/shorts/abc12345",     # shorts out of initial scope
            "not-a-url",
            "",
        ]
        for url in invalid_urls:
            self.assertFalse(YouTubeUrlHelper.supports_url(url), f"Should reject invalid URL: {url}")

    def test_metadata_cleaning_and_artist_extraction(self):
        cases = [
            (
                "Daft Punk - Around The World (Official Music Video)",
                "Around The World",
                ["Daft Punk"],
            ),
            (
                "Queen - Bohemian Rhapsody [Official Audio]",
                "Bohemian Rhapsody",
                ["Queen"],
            ),
            (
                "Kendrick Lamar feat. Rihanna - LOYALTY. (Official Video)",
                "LOYALTY.",
                ["Kendrick Lamar", "Rihanna"],
            ),
            (
                "Never Gonna Give You Up (4K Remaster)",
                "Never Gonna Give You Up",
                ["Rick Astley"],  # from channel fallback
            ),
        ]
        for raw, expected_title, expected_artists in cases:
            title, artists = YouTubeMetadataNormalizer.extract_artist_and_title(
                raw,
                channel_name="Rick Astley - Topic",
            )
            self.assertEqual(title, expected_title)
            self.assertEqual(artists, expected_artists)

    def test_mocked_youtube_api_playlist_parsing_with_unavailable_filtering(self):
        mock_payload = {
            "items": [
                {
                    "snippet": {
                        "title": "Daft Punk - One More Time",
                        "channelTitle": "Daft Punk",
                        "resourceId": {"videoId": "FGBhQbmPwH8"},
                    },
                    "contentDetails": {"duration": "PT5M20S"},
                },
                {
                    "snippet": {
                        "title": "Private video",
                        "channelTitle": "Unknown",
                        "resourceId": {"videoId": "priv123"},
                    },
                },
                {
                    "snippet": {
                        "title": "Deleted video",
                        "channelTitle": "Unknown",
                        "resourceId": {"videoId": "del123"},
                    },
                },
                {
                    "snippet": {
                        "title": "Daft Punk - Harder, Better, Faster, Stronger",
                        "channelTitle": "Daft Punk",
                        "resourceId": {"videoId": "gAjR4_CbPpQ"},
                    },
                    "contentDetails": {"duration": "PT3M44S"},
                },
            ],
            "nextPageToken": "CAUQAA",
        }

        tracks, next_token = YouTubePoCParser.parse_youtube_api_playlist_page(
            mock_payload,
            source_url="https://www.youtube.com/playlist?list=PLtest",
        )

        self.assertEqual(len(tracks), 2)  # private and deleted filtered
        self.assertEqual(next_token, "CAUQAA")
        self.assertEqual(tracks[0].title, "One More Time")
        self.assertEqual(tracks[0].artists, ["Daft Punk"])
        self.assertEqual(tracks[0].duration_ms, 320000)
        self.assertEqual(tracks[0].provider_id, "youtube")
        self.assertEqual(tracks[0].external_ids["youtube"], "FGBhQbmPwH8")

    def test_mocked_youtubei_playlist_parsing(self):
        mock_payload = {
            "contents": {
                "twoColumnBrowseResultsRenderer": {
                    "secondaryContents": {
                        "sectionListRenderer": {
                            "contents": [
                                {
                                    "musicPlaylistShelfRenderer": {
                                        "contents": [
                                            {
                                                "musicResponsiveListItemRenderer": {
                                                    "playlistItemData": {"videoId": "vid001"},
                                                    "flexColumns": [
                                                        {
                                                            "musicResponsiveListItemFlexColumnRenderer": {
                                                                "text": {"runs": [{"text": "Song A"}]}
                                                            }
                                                        },
                                                        {
                                                            "musicResponsiveListItemFlexColumnRenderer": {
                                                                "text": {"runs": [{"text": "Artist A"}, {"text": "Album A"}]}
                                                            }
                                                        },
                                                    ],
                                                    "fixedColumns": [
                                                        {
                                                            "musicResponsiveListItemFixedColumnRenderer": {
                                                                "text": {"runs": [{"text": "3:30"}]}
                                                            }
                                                        }
                                                    ],
                                                }
                                            },
                                            {
                                                "musicResponsiveListItemRenderer": {
                                                    "playlistItemData": {"videoId": "vid001"},  # duplicate intentional
                                                    "flexColumns": [
                                                        {
                                                            "musicResponsiveListItemFlexColumnRenderer": {
                                                                "text": {"runs": [{"text": "Song A"}]}
                                                            }
                                                        },
                                                        {
                                                            "musicResponsiveListItemFlexColumnRenderer": {
                                                                "text": {"runs": [{"text": "Artist A"}, {"text": "Album A"}]}
                                                            }
                                                        },
                                                    ],
                                                    "fixedColumns": [
                                                        {
                                                            "musicResponsiveListItemFixedColumnRenderer": {
                                                                "text": {"runs": [{"text": "3:30"}]}
                                                            }
                                                        }
                                                    ],
                                                }
                                            },
                                        ]
                                    }
                                }
                            ]
                        }
                    }
                }
            }
        }

        tracks = YouTubePoCParser.parse_youtubei_playlist(
            mock_payload,
            source_url="https://music.youtube.com/playlist?list=PLmusic",
        )

        self.assertEqual(len(tracks), 2)
        # duplicate preservation and ordering verified
        self.assertEqual(tracks[0].title, "Song A")
        self.assertEqual(tracks[1].title, "Song A")
        self.assertEqual(tracks[0].duration_ms, 210000)
        self.assertEqual(tracks[0].album, "Album A")
        self.assertEqual(tracks[0].provider_id, "youtube")

    def test_error_classification_semantics(self):
        # 1. quota / rate limit error (http 429)
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.headers = {"Retry-After": "60"}
        http_err = requests.exceptions.HTTPError(response=mock_resp)

        status_code, retry_after = extract_http_metadata(http_err)
        self.assertEqual(status_code, 429)
        self.assertEqual(retry_after, 60)

        # 2. invalid api key / auth failure (http 401 or 403)
        mock_auth_resp = MagicMock()
        mock_auth_resp.status_code = 403
        auth_http_err = requests.exceptions.HTTPError(response=mock_auth_resp)

        status_code, _ = extract_http_metadata(auth_http_err)
        self.assertEqual(status_code, 403)

    def test_live_oembed_verification_bounded(self):
        """bounded live verification against public official youtube oembed service."""
        try:
            resp = requests.get(
                "https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v=dQw4w9WgXcQ&format=json",
                timeout=5,
            )
            if resp.status_code == 200:
                data = resp.json()
                self.assertIn("Rick Astley", data.get("title", ""))
                self.assertEqual(data.get("author_name"), "Rick Astley")
                self.assertEqual(data.get("type"), "video")
        except (requests.RequestException, ConnectionError, TimeoutError) as exc:
            self.skipTest(f"Live network unavailable for bounded oEmbed test: {exc}")
