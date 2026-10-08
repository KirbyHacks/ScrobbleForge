import json
import sys
import tempfile
import time
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.unit._dependency_guard import ensure_dependencies_mocked

ensure_dependencies_mocked()

import requests
from src.spotify_client import (
    SpotifyClient,
    SpotifyIngestionError,
    extract_primary_artist,
    load_local_fallback_tracks,
)
from src.models import Track


def _build_embed_html(entity: dict) -> str:
    """Builds a realistic Next.js hydration payload inside HTML markup."""
    payload = {
        "props": {
            "pageProps": {
                "state": {
                    "data": {
                        "entity": entity,
                    }
                }
            }
        }
    }
    return (
        "<!DOCTYPE html><html><head>"
        f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>'
        "</head><body><div id=\"__next\"></div></body></html>"
    )


class TestSpotifyParsingAndCache(unittest.TestCase):
    def test_parse_spotify_uri(self):
        # Web playlist URL
        t, id_ = SpotifyClient.parse_spotify_uri("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=abcd123")
        self.assertEqual(t, "playlist")
        self.assertEqual(id_, "37i9dQZF1DXcBWIGoYBM5M")

        # URI album
        t, id_ = SpotifyClient.parse_spotify_uri("spotify:album:4m2880jivSbbyEGAKfITCa")
        self.assertEqual(t, "album")
        self.assertEqual(id_, "4m2880jivSbbyEGAKfITCa")

        # Track URL
        t, id_ = SpotifyClient.parse_spotify_uri("https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT?si=xyz")
        self.assertEqual(t, "track")
        self.assertEqual(id_, "4cOdK2wGLETKBW3PvgPWqT")

        # Bare ID defaults to playlist
        t, id_ = SpotifyClient.parse_spotify_uri("37i9dQZF1DXcBWIGoYBM5M")
        self.assertEqual(t, "playlist")
        self.assertEqual(id_, "37i9dQZF1DXcBWIGoYBM5M")

    @patch("src.spotify_client.requests.get")
    def test_single_track_fetch(self, mock_get):
        """Zero-network deterministic single track embed fetch with mock Next.js payload."""
        mock_entity = {
            "title": "Never Gonna Give You Up",
            "name": "Never Gonna Give You Up",
            "artists": [{"name": "Rick Astley"}],
            "duration": 213573,
            "uri": "spotify:track:4cOdK2wGLETKBW3PvgPWqT",
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = _build_embed_html(mock_entity)
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        client = SpotifyClient()
        tracks = client.fetch_sources(
            ["https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT"],
            use_cache_if_available=False,
        )

        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0].title, "Never Gonna Give You Up")
        self.assertEqual(tracks[0].artist, "Rick Astley")
        self.assertEqual(tracks[0].duration_ms, 213573)
        self.assertEqual(tracks[0].spotify_id, "4cOdK2wGLETKBW3PvgPWqT")
        self.assertEqual(tracks[0].source_name, "Single Track")

        mock_get.assert_called_once()
        call_url = mock_get.call_args[0][0]
        self.assertIn("open.spotify.com/embed/track/4cOdK2wGLETKBW3PvgPWqT", call_url)

    @patch("src.spotify_client.requests.get")
    def test_single_track_fetch_subtitle_fallback(self, mock_get):
        """Single track embed parsing using subtitle field when artists array is absent."""
        mock_entity = {
            "title": "Never Gonna Give You Up",
            "subtitle": "Rick Astley",
            "duration": 213573,
            "uri": "spotify:track:4cOdK2wGLETKBW3PvgPWqT",
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = _build_embed_html(mock_entity)
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        client = SpotifyClient()
        tracks = client.fetch_sources(
            ["https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT"],
            use_cache_if_available=False,
        )

        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0].title, "Never Gonna Give You Up")
        self.assertEqual(tracks[0].artist, "Rick Astley")

    @patch("src.spotify_client.requests.get")
    def test_playlist_fetch(self, mock_get):
        """Zero-network deterministic playlist embed fetch with multiple tracks."""
        mock_entity = {
            "name": "Synthwave Chill",
            "type": "playlist",
            "trackList": [
                {
                    "title": "Resonance",
                    "artists": [{"name": "HOME"}],
                    "duration": 212000,
                    "uri": "spotify:track:1TuopWDI4CuAe502grR32v",
                },
                {
                    "title": "Sunset",
                    "subtitle": "The Midnight",
                    "duration": 326000,
                    "uri": "spotify:track:4y3OI8hC5z0mQv3aF1U91b",
                },
                {
                    "title": "Days of Thunder",
                    "artists": [{"name": "The Midnight"}],
                    "duration": 310000,
                    "uri": "spotify:track:2z4W2j7lYtQ0e1r5tY",
                },
            ],
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = _build_embed_html(mock_entity)
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        client = SpotifyClient()
        tracks = client.fetch_sources(
            ["https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"],
            use_cache_if_available=False,
        )

        self.assertEqual(len(tracks), 3)
        self.assertEqual(tracks[0].title, "Resonance")
        self.assertEqual(tracks[0].artist, "HOME")
        self.assertEqual(tracks[0].duration_ms, 212000)
        self.assertEqual(tracks[0].track_number, 1)
        self.assertEqual(tracks[0].source_name, "Synthwave Chill")

        self.assertEqual(tracks[1].title, "Sunset")
        self.assertEqual(tracks[1].artist, "The Midnight")
        self.assertEqual(tracks[1].track_number, 2)

        self.assertEqual(tracks[2].title, "Days of Thunder")
        self.assertEqual(tracks[2].artist, "The Midnight")
        self.assertEqual(tracks[2].track_number, 3)

        mock_get.assert_called_once()
        call_url = mock_get.call_args[0][0]
        self.assertIn("open.spotify.com/embed/playlist/37i9dQZF1DXcBWIGoYBM5M", call_url)

    @patch("src.spotify_client.requests.get")
    def test_album_fetch(self, mock_get):
        """Zero-network deterministic album embed fetch."""
        mock_entity = {
            "name": "Random Access Memories",
            "type": "album",
            "trackList": [
                {
                    "title": "Give Life Back to Music",
                    "artists": [{"name": "Daft Punk"}],
                    "duration": 274000,
                    "uri": "spotify:track:0dEIa2jcVvEVvpHuuZuSpP",
                },
                {
                    "title": "Get Lucky",
                    "subtitle": "Daft Punk,\u00a0Pharrell Williams",
                    "duration": 248000,
                    "uri": "spotify:track:69kOkLUCkxIZYexIgSG8rq",
                },
            ],
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = _build_embed_html(mock_entity)
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        client = SpotifyClient()
        tracks = client.fetch_sources(
            ["spotify:album:4m2880jivSbbyEGAKfITCa"],
            use_cache_if_available=False,
        )

        self.assertEqual(len(tracks), 2)
        self.assertEqual(tracks[0].title, "Give Life Back to Music")
        self.assertEqual(tracks[0].artist, "Daft Punk")
        self.assertEqual(tracks[0].album, "Random Access Memories")

        self.assertEqual(tracks[1].title, "Get Lucky")
        self.assertEqual(tracks[1].artist, "Daft Punk")
        self.assertEqual(tracks[1].album, "Random Access Memories")

    @patch("src.spotify_client.requests.get")
    def test_fetch_embed_tracks_http_error(self, mock_get):
        """Verifies SpotifyIngestionError when requests.get fails."""
        mock_get.side_effect = requests.RequestException("Connection refused")
        client = SpotifyClient()
        with self.assertRaises(SpotifyIngestionError):
            client.fetch_embed_tracks("track", "dummy_id")

    @patch("src.spotify_client.requests.get")
    def test_fetch_embed_tracks_missing_next_data(self, mock_get):
        """Verifies SpotifyIngestionError when embed page has no __NEXT_DATA__ script tag."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "<html><body><h1>Embed Not Found</h1></body></html>"
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        client = SpotifyClient()
        with self.assertRaises(SpotifyIngestionError):
            client.fetch_embed_tracks("playlist", "dummy_playlist")

    @patch("src.spotify_client.requests.get")
    def test_fetch_embed_tracks_malformed_json(self, mock_get):
        """Verifies SpotifyIngestionError when embed payload contains broken JSON."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = '<html><script id="__NEXT_DATA__" type="application/json">{ broken json </script></html>'
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        client = SpotifyClient()
        with self.assertRaises(SpotifyIngestionError):
            client.fetch_embed_tracks("track", "dummy_track")

    def test_cache_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_file = Path(tmp_dir) / "tracks.cache.json"
            client = SpotifyClient(cache_path=cache_file)

            # Initially cache is empty
            self.assertEqual(client.load_cache(), [])

            tracks = [
                Track(title="Song A", artist="Artist A", duration_ms=180000),
                Track(title="Song B", artist="Artist B", duration_ms=200000),
            ]
            client.save_cache(tracks, ["source_1"])

            loaded = client.load_cache()
            self.assertEqual(len(loaded), 2)
            self.assertEqual(loaded[0].title, "Song A")
            self.assertEqual(loaded[1].title, "Song B")

    def test_cache_invalidation_on_source_change(self):
        """Verifies that changing source URLs invalidates disk cache and triggers re-fetch."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_file = Path(tmp_dir) / "tracks.cache.json"
            client = SpotifyClient(cache_path=cache_file)

            source_1 = ["https://open.spotify.com/playlist/playlist_A"]
            source_2 = ["https://open.spotify.com/playlist/playlist_B"]

            tracks_a = [Track(title="Song A", artist="Artist A", duration_ms=180000)]
            client.save_cache(tracks_a, source_1)

            # Matching source returns cached tracks
            self.assertEqual(len(client.load_cache(sources=source_1)), 1)

            # Different source returns empty list (invalidated)
            self.assertEqual(client.load_cache(sources=source_2), [])

            # Proves fetch_sources ignores cache when sources differ
            with patch.object(client, "fetch_embed_tracks") as mock_fetch:
                tracks_b = [Track(title="Song B", artist="Artist B", duration_ms=210000)]
                mock_fetch.return_value = tracks_b

                result = client.fetch_sources(source_2, use_cache_if_available=True)
                mock_fetch.assert_called_once()
                self.assertEqual(len(result), 1)
                self.assertEqual(result[0].title, "Song B")

    def test_cache_invalidation_on_ttl_expiry(self):
        """Verifies that cached tracks older than TTL are rejected."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_file = Path(tmp_dir) / "tracks.cache.json"
            # 12-hour TTL
            client = SpotifyClient(cache_path=cache_file, ttl_hours=12.0)

            sources = ["https://open.spotify.com/playlist/playlist_A"]
            tracks = [Track(title="Song A", artist="Artist A", duration_ms=180000)]
            client.save_cache(tracks, sources)

            # Simulate aging cache: 13 hours old (expired)
            with open(cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            data["saved_at"] = int(time.time()) - (13 * 3600)
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(data, f)

            # Expired cache must return empty list
            self.assertEqual(client.load_cache(sources=sources), [])

            # Fresh cache within TTL must return cached tracks
            data["saved_at"] = int(time.time()) - (1 * 3600)  # 1 hour old
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(data, f)

            self.assertEqual(len(client.load_cache(sources=sources)), 1)

    def test_extract_primary_artist_matrix(self):
        # Solo artists with internal commas
        self.assertEqual(extract_primary_artist("Tyler, The Creator"), "Tyler, The Creator")

        # Complex band names with multiple commas or ampersands
        self.assertEqual(extract_primary_artist("Earth, Wind & Fire"), "Earth, Wind & Fire")
        self.assertEqual(extract_primary_artist("Crosby, Stills, Nash & Young"), "Crosby, Stills, Nash & Young")

        # Multi-artist delimited with non-breaking spaces (,\xa0 and ,\u00a0)
        self.assertEqual(
            extract_primary_artist("Tyler, The Creator,\xa0A$AP Rocky"),
            "Tyler, The Creator",
        )
        self.assertEqual(
            extract_primary_artist("Kendrick Lamar,\xa0SZA"),
            "Kendrick Lamar",
        )
        self.assertEqual(
            extract_primary_artist("Clipse,\u00a0Tyler, The Creator,\u00a0Pusha T"),
            "Clipse",
        )

        # Punctuation and symbols
        self.assertEqual(extract_primary_artist("AC/DC"), "AC/DC")
        self.assertEqual(extract_primary_artist("Sunn O)))"), "Sunn O)))")
        self.assertEqual(extract_primary_artist("Panic! At The Disco"), "Panic! At The Disco")

        # Empty / fallback handling
        self.assertEqual(extract_primary_artist(""), "Unknown Artist")
        self.assertEqual(extract_primary_artist("   "), "Unknown Artist")

    def test_fallback_tracks_txt(self):
        with tempfile.NamedTemporaryFile("w+", delete=False, suffix=".txt") as f:
            f.write("# Sample Playlist\nRadiohead - Karma Police\nBlur - Song 2\n")
            f_path = Path(f.name)

        try:
            tracks = load_local_fallback_tracks(f_path)
            self.assertEqual(len(tracks), 2)
            self.assertEqual(tracks[0].artist, "Radiohead")
            self.assertEqual(tracks[0].title, "Karma Police")
            self.assertEqual(tracks[1].artist, "Blur")
            self.assertEqual(tracks[1].title, "Song 2")
        finally:
            f_path.unlink()

    def test_fallback_tracks_csv_quoted_comma(self):
        with tempfile.NamedTemporaryFile("w+", delete=False, suffix=".csv") as f:
            f.write('Track Name,Artist Name(s),Duration (ms)\n')
            f.write('"IGOR\'S THEME","Tyler, The Creator",200684\n')
            f.write('"September","Earth, Wind & Fire",215000\n')
            f.write('"Thunderstruck","AC/DC",292000\n')
            f_path = Path(f.name)

        try:
            tracks = load_local_fallback_tracks(f_path)
            self.assertEqual(len(tracks), 3)
            self.assertEqual(tracks[0].artist, "Tyler, The Creator")
            self.assertEqual(tracks[0].title, "IGOR'S THEME")
            self.assertEqual(tracks[0].duration_ms, 200684)

            self.assertEqual(tracks[1].artist, "Earth, Wind & Fire")
            self.assertEqual(tracks[1].title, "September")

            self.assertEqual(tracks[2].artist, "AC/DC")
            self.assertEqual(tracks[2].title, "Thunderstruck")
        finally:
            f_path.unlink()

    def test_fallback_tracks_json(self):
        with tempfile.NamedTemporaryFile("w+", delete=False, suffix=".json") as f:
            f.write(json.dumps([
                {"artist": "Boards of Canada", "title": "Roygbiv", "duration_ms": 150000},
                {"artist": "Aphex Twin", "title": "Selected Ambient Works", "duration_ms": 260000},
            ]))
            f_path = Path(f.name)

        try:
            tracks = load_local_fallback_tracks(f_path)
            self.assertEqual(len(tracks), 2)
            self.assertEqual(tracks[0].artist, "Boards of Canada")
            self.assertEqual(tracks[0].title, "Roygbiv")
            self.assertEqual(tracks[1].artist, "Aphex Twin")
            self.assertEqual(tracks[1].title, "Selected Ambient Works")
        finally:
            f_path.unlink()


if __name__ == "__main__":
    unittest.main()
