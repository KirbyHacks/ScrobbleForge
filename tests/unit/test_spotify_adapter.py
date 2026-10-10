"""unit tests for SpotifyAdapter and canonical transformation."""
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.unit._dependency_guard import ensure_dependencies_mocked

ensure_dependencies_mocked()

import requests
from src.core.models import CanonicalTrack
from src.models import Track
from src.sources.exceptions import (
    SourceAuthError,
    SourceError,
    SourceTemporaryError,
    UnsupportedSourceError,
)
from src.sources.spotify_adapter import SpotifyAdapter
from src.spotify_client import SpotifyClient, SpotifyIngestionError


class TestSpotifyAdapter(unittest.TestCase):
    def setUp(self):
        self.mock_client = MagicMock(spec=SpotifyClient)
        self.adapter = SpotifyAdapter(client=self.mock_client)

    def test_supports_url_valid_spotify_sources(self):
        valid_urls = [
            "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M",
            "http://open.spotify.com/album/4m2880jivSbbyEGAKfITCa",
            "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT?si=abc123xyz",
            "https://open.spotify.com/embed/playlist/37i9dQZF1DXcBWIGoYBM5M",
            "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M",
            "spotify:album:4m2880jivSbbyEGAKfITCa",
            "spotify:track:4cOdK2wGLETKBW3PvgPWqT",
            "SPOTIFY:TRACK:4cOdK2wGLETKBW3PvgPWqT",
        ]
        for url in valid_urls:
            with self.subTest(url=url):
                self.assertTrue(self.adapter.supports_url(url))

    def test_supports_url_spoofing_and_unsupported(self):
        invalid_urls = [
            "https://open.spotify.com.evil.example/playlist/37i9dQZF1DXcBWIGoYBM5M",
            "https://evil-open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M",
            "https://spotify.com.fake.org/track/123",
            "https://youtube.com/watch?v=dQw4w9WgXcQ",
            "spotify:invalid",
            "not-a-url",
            "",
            None,
        ]
        for url in invalid_urls:
            with self.subTest(url=url):
                self.assertFalse(self.adapter.supports_url(url))  # type: ignore

    def test_map_to_canonical_from_track_model(self):
        legacy = Track(
            title="Instant Crush",
            artist="Daft Punk",
            album="Random Access Memories",
            duration_ms=337000,
            spotify_id="2cGx047T2F7Mn2O2b00Ilv",
            source_name="spotify:album:4m2880jivSbbyEGAKfITCa",
        )
        canonical = self.adapter.map_to_canonical(legacy)

        self.assertIsInstance(canonical, CanonicalTrack)
        self.assertEqual(canonical.title, "Instant Crush")
        self.assertEqual(canonical.artists, ["Daft Punk"])
        self.assertEqual(canonical.primary_artist, "Daft Punk")
        self.assertEqual(canonical.album, "Random Access Memories")
        self.assertEqual(canonical.duration_ms, 337000)
        self.assertIsNone(canonical.isrc)
        self.assertEqual(canonical.external_ids, {"spotify": "2cGx047T2F7Mn2O2b00Ilv"})
        self.assertEqual(canonical.provider_id, "spotify")
        self.assertEqual(canonical.source_uri, "spotify:album:4m2880jivSbbyEGAKfITCa")

    def test_map_to_canonical_from_rich_dict_multiple_artists_and_isrc(self):
        raw_dict = {
            "title": "Get Lucky",
            "artists": [
                {"name": "Daft Punk"},
                {"name": "Pharrell Williams"},
                {"name": "Nile Rodgers"},
            ],
            "album": {"name": "Random Access Memories"},
            "duration": 248000,
            "id": "69kOkLUCkxIZYexIgSG8rq",
            "isrc": "USQX91300105",
            "uri": "spotify:track:69kOkLUCkxIZYexIgSG8rq",
        }
        canonical = self.adapter.map_to_canonical(raw_dict)

        self.assertEqual(canonical.title, "Get Lucky")
        self.assertEqual(canonical.artists, ["Daft Punk", "Pharrell Williams", "Nile Rodgers"])
        self.assertEqual(canonical.primary_artist, "Daft Punk")
        self.assertEqual(canonical.album, "Random Access Memories")
        self.assertEqual(canonical.duration_ms, 248000)
        self.assertEqual(canonical.isrc, "USQX91300105")
        self.assertEqual(canonical.external_ids, {"spotify": "69kOkLUCkxIZYexIgSG8rq"})
        self.assertEqual(canonical.source_uri, "spotify:track:69kOkLUCkxIZYexIgSG8rq")

    def test_map_to_canonical_missing_optional_metadata(self):
        minimal_dict = {
            "title": "Bare Bones Track",
            "artist": "Solo Artist",
        }
        canonical = self.adapter.map_to_canonical(minimal_dict)

        self.assertEqual(canonical.title, "Bare Bones Track")
        self.assertEqual(canonical.artists, ["Solo Artist"])
        self.assertEqual(canonical.primary_artist, "Solo Artist")
        self.assertIsNone(canonical.album)
        self.assertIsNone(canonical.duration_ms)
        self.assertIsNone(canonical.isrc)
        self.assertEqual(canonical.external_ids, {})
        self.assertIsNone(canonical.source_uri)

    def test_fetch_sources_preserves_track_ordering(self):
        track1 = Track(title="Song A", artist="Artist A", spotify_id="id_a")
        track2 = Track(title="Song B", artist="Artist B", spotify_id="id_b")
        track3 = Track(title="Song C", artist="Artist C", spotify_id="id_c")

        self.mock_client.fetch_sources.return_value = [track1, track2, track3]

        urls = ["https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"]
        canonical_tracks = self.adapter.fetch_sources(urls)

        self.assertEqual(len(canonical_tracks), 3)
        self.assertEqual([t.title for t in canonical_tracks], ["Song A", "Song B", "Song C"])
        self.assertEqual([t.primary_artist for t in canonical_tracks], ["Artist A", "Artist B", "Artist C"])
        self.mock_client.fetch_sources.assert_called_once_with(urls, use_cache_if_available=True)

    def test_fetch_sources_cache_flag_forwarding(self):
        self.mock_client.fetch_sources.return_value = []
        url = "https://open.spotify.com/album/4m2880jivSbbyEGAKfITCa"

        self.adapter.fetch_sources([url], use_cache_if_available=False)
        self.mock_client.fetch_sources.assert_called_with([url], use_cache_if_available=False)

        self.adapter.fetch_sources([url], use_cache_if_available=True)
        self.mock_client.fetch_sources.assert_called_with([url], use_cache_if_available=True)

    def test_fetch_sources_empty_successful_result(self):
        self.mock_client.fetch_sources.return_value = []
        urls = ["https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"]

        result = self.adapter.fetch_sources(urls)
        self.assertEqual(result, [])

    def test_fetch_sources_unsupported_url_raises_error(self):
        with self.assertRaises(UnsupportedSourceError):
            self.adapter.fetch_sources(["https://unsupported.service.org/playlist/123"])

    def test_fetch_sources_auth_failure_classification(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        http_err = requests.exceptions.HTTPError(response=mock_resp)

        self.mock_client.fetch_sources.side_effect = http_err

        urls = ["https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"]
        with self.assertRaises(SourceAuthError):
            self.adapter.fetch_sources(urls)

    def test_fetch_sources_temporary_failure_classification(self):
        self.mock_client.fetch_sources.side_effect = SpotifyIngestionError("Embed timeout")

        urls = ["https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"]
        with self.assertRaises(SourceTemporaryError):
            self.adapter.fetch_sources(urls)

    def test_fetch_sources_unexpected_exception_classification(self):
        self.mock_client.fetch_sources.side_effect = KeyError("corrupted_internal_key")

        urls = ["https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"]
        with self.assertRaises(SourceError) as ctx:
            self.adapter.fetch_sources(urls)
        self.assertIn("corrupted_internal_key", str(ctx.exception))

    @patch("src.sources.spotify_adapter.requests.head")
    def test_check_health_success(self, mock_head):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_head.return_value = mock_resp

        self.assertTrue(self.adapter.check_health())
        mock_head.assert_called_once()

    @patch("src.sources.spotify_adapter.requests.get")
    @patch("src.sources.spotify_adapter.requests.head")
    def test_check_health_fallback_to_get_on_head_error(self, mock_head, mock_get):
        mock_head.side_effect = requests.RequestException("HEAD not allowed")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_get.return_value = mock_resp

        self.assertTrue(self.adapter.check_health())
        mock_get.assert_called_once()

    @patch("src.sources.spotify_adapter.requests.get")
    @patch("src.sources.spotify_adapter.requests.head")
    def test_check_health_failure_returns_false(self, mock_head, mock_get):
        mock_head.side_effect = requests.RequestException("Unreachable")
        mock_get.side_effect = requests.RequestException("Unreachable")

        self.assertFalse(self.adapter.check_health())


if __name__ == "__main__":
    unittest.main()
