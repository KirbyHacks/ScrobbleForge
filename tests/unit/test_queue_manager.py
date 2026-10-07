import sys
from pathlib import Path
import tempfile
import unittest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.models import Track
from src.quota_tracker import QuotaTracker
from src.queue_manager import QueueManager


class TestQueueManager(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_state.db"
        self.tracker = QuotaTracker(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_queue_loop_and_persistence(self):
        tracks = [
            Track(title=f"Track {i}", artist="Artist", duration_ms=180000)
            for i in range(3)
        ]
        qm = QueueManager(tracks=tracks, tracker=self.tracker, shuffle=False, loop=True)

        t1 = qm.get_next_track()
        self.assertEqual(t1.title, "Track 0")
        self.assertEqual(self.tracker.get_state("queue_index"), "1")

        t2 = qm.get_next_track()
        self.assertEqual(t2.title, "Track 1")

        t3 = qm.get_next_track()
        self.assertEqual(t3.title, "Track 2")

        # Loops back to Track 0
        t4 = qm.get_next_track()
        self.assertEqual(t4.title, "Track 0")

    def test_queue_exhaustion_without_loop(self):
        tracks = [
            Track(title="Track 1", artist="Artist", duration_ms=180000),
            Track(title="Track 2", artist="Artist", duration_ms=180000),
        ]
        qm = QueueManager(tracks=tracks, tracker=self.tracker, shuffle=False, loop=False)

        t1 = qm.get_next_track()
        self.assertEqual(t1.title, "Track 1")
        t2 = qm.get_next_track()
        self.assertEqual(t2.title, "Track 2")
        t3 = qm.get_next_track()
        self.assertIsNone(t3)


if __name__ == "__main__":
    unittest.main()
