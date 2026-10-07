import sys
from pathlib import Path
import tempfile
import time
import unittest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.unit._dependency_guard import ensure_dependencies_mocked

ensure_dependencies_mocked()

from src.models import Track
from src.quota_tracker import QuotaTracker
from src.queue_manager import QueueManager
from src.engine import ScrobblerEngine
from src.config import AppConfig, LastFMConfig, SpotifyConfig, EngineConfig, SystemConfig
import threading


class MockLastFM:
    def __init__(self):
        self.scrobbles = []
        self.now_playing = []

    def update_now_playing(self, track):
        self.now_playing.append(track)
        return True

    def scrobble(self, track, timestamp=None):
        self.scrobbles.append((track, timestamp))
        return True


class TestScrobblerEngine(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_state.db"
        self.tracker = QuotaTracker(self.db_path)
        self.mock_lfm = MockLastFM()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_max_limit_timeline_advancement(self):
        cfg = AppConfig(
            lastfm=LastFMConfig(api_key="k", api_secret="s", username="u"),
            spotify=SpotifyConfig(),
            engine=EngineConfig(mode="max_limit", max_daily_scrobbles=2750),
            system=SystemConfig(data_dir=Path(self.temp_dir.name)),
        )

        tracks = [
            Track(title="Song 1", artist="Artist 1", duration_ms=180000),  # 180s
            Track(title="Song 2", artist="Artist 2", duration_ms=200000),  # 200s
        ]
        qm = QueueManager(tracks=tracks, tracker=self.tracker, shuffle=False, loop=False)
        stop_event = threading.Event()

        engine = ScrobblerEngine(
            config=cfg,
            lastfm=self.mock_lfm,
            spotify=None,
            queue=qm,
            tracker=self.tracker,
            stop_event=stop_event,
        )

        initial_cursor = engine.virtual_timeline_cursor
        self.assertIsNotNone(initial_cursor)

        # Scrobble first track
        track1 = qm.get_next_track()
        ts1 = engine.virtual_timeline_cursor
        engine.lastfm.scrobble(track1, timestamp=ts1)
        engine.virtual_timeline_cursor += track1.duration_sec + 2

        # Check that track2 start time is exactly after track1 finished!
        ts2 = engine.virtual_timeline_cursor
        self.assertEqual(ts2, ts1 + 180 + 2)

        # Non-overlapping verification: ts2 > ts1 + track1 duration
        self.assertGreater(ts2, ts1 + track1.duration_sec)


if __name__ == "__main__":
    unittest.main()
