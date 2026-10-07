import sys
from pathlib import Path
import tempfile
import unittest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.quota_tracker import QuotaTracker


class TestQuotaTracker(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_state.db"
        self.tracker = QuotaTracker(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_rolling_24h_quota_and_pause_calculation(self):
        now = 1700000000  # fixed epoch

        # Under limit
        self.tracker.record_scrobble("Artist A", "Track 1", timestamp=now - 3600)
        self.tracker.record_scrobble("Artist B", "Track 2", timestamp=now - 1800)

        count = self.tracker.get_rolling_24h_count(now=now)
        self.assertEqual(count, 2)

        allowed, wait_sec = self.tracker.can_scrobble(max_daily_limit=5, now=now)
        self.assertTrue(allowed)
        self.assertEqual(wait_sec, 0)

        # Exceed limit: limit = 2
        allowed_capped, wait_capped = self.tracker.can_scrobble(max_daily_limit=2, now=now)
        self.assertFalse(allowed_capped)
        # Oldest scrobble was at now - 3600. It expires at (now - 3600) + 86400.
        # Wait time should be 86400 - 3600 = 82800 seconds.
        self.assertEqual(wait_capped, 82800)

    def test_state_persistence(self):
        self.tracker.set_state("queue_index", "42")
        self.assertEqual(self.tracker.get_state("queue_index"), "42")
        self.tracker.set_state("queue_index", "43")
        self.assertEqual(self.tracker.get_state("queue_index"), "43")

    def test_get_state_default(self):
        self.assertIsNone(self.tracker.get_state("non_existent_key"))
        self.assertEqual(self.tracker.get_state("non_existent_key", default="fallback"), "fallback")


if __name__ == "__main__":
    unittest.main()
