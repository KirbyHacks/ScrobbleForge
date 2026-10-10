import os
import sys
from pathlib import Path
import unittest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.spotify_client import SpotifyClient


@unittest.skipUnless(
    os.environ.get("RUN_LIVE_TESTS") == "1",
    "Live API integration tests are disabled by default. Set RUN_LIVE_TESTS=1 to execute live network calls.",
)
class TestSpotifyLiveIntegration(unittest.TestCase):
    def test_live_single_track_fetch(self):
        """verify real HTTP fetch and parsing against live open.spotify.com embed."""
        client = SpotifyClient()
        tracks = client.fetch_sources(
            ["https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT"],
            use_cache_if_available=False,
        )
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0].title, "Never Gonna Give You Up")
        self.assertEqual(tracks[0].artist, "Rick Astley")
        self.assertGreater(tracks[0].duration_ms, 200000)


if __name__ == "__main__":
    unittest.main()
