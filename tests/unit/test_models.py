import sys
from pathlib import Path
import unittest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.models import Track


class TestTrackModel(unittest.TestCase):
    def test_duration_minimum_enforcement(self):
        # Last.fm requires tracks >= 30s
        short_track = Track(title="Short", artist="Artist", duration_ms=15000)
        self.assertEqual(short_track.duration_sec, 30)

        normal_track = Track(title="Normal", artist="Artist", duration_ms=215000)
        self.assertEqual(normal_track.duration_sec, 215)
        self.assertEqual(normal_track.formatted_duration, "3m 35s")

    def test_serialization(self):
        track = Track(
            title="Get Lucky",
            artist="Daft Punk",
            album="Random Access Memories",
            duration_ms=248000,
            spotify_id="69kOkLUCkxIZYexIgSG8rq",
        )
        data = track.to_dict()
        restored = Track.from_dict(data)
        self.assertEqual(restored.title, "Get Lucky")
        self.assertEqual(restored.artist, "Daft Punk")
        self.assertEqual(restored.duration_sec, 248)

    def test_display_name_and_formatting(self):
        track = Track(title="Around the World", artist="Daft Punk", duration_ms=429000)
        self.assertEqual(track.display_name, "Daft Punk - Around the World")
        self.assertEqual(track.formatted_duration, "7m 09s")


if __name__ == "__main__":
    unittest.main()
