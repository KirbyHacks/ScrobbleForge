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

        # Exceed limit: limit = 2, count = 3
        self.tracker.record_scrobble("Artist C", "Track 3", timestamp=now - 900)
        count = self.tracker.get_rolling_24h_count(now=now)
        self.assertEqual(count, 3)

        allowed_capped, wait_capped = self.tracker.can_scrobble(max_daily_limit=2, now=now)
        self.assertFalse(allowed_capped)
        # Admission requires count < limit, so two rows must expire; the second-oldest
        # row is 1,800 seconds old and expires in 84,600 seconds.
        self.assertEqual(wait_capped, 84600)

    def test_state_persistence(self):
        self.tracker.set_state("queue_index", "42")
        self.assertEqual(self.tracker.get_state("queue_index"), "42")
        self.tracker.set_state("queue_index", "43")
        self.assertEqual(self.tracker.get_state("queue_index"), "43")

    def test_get_state_default(self):
        self.assertIsNone(self.tracker.get_state("non_existent_key"))
        self.assertEqual(self.tracker.get_state("non_existent_key", default="fallback"), "fallback")

    def test_exact_limit_thresholds(self):
        now = 1700000000

        # Scenario 1: count < limit (e.g. 1 < 2)
        self.tracker.record_scrobble("Artist A", "Track 1", timestamp=now - 1000)
        allowed, wait_sec = self.tracker.can_scrobble(max_daily_limit=2, now=now)
        self.assertTrue(allowed)
        self.assertEqual(wait_sec, 0)

        # Scenario 2: count == limit (e.g. 2 == 2) -> at threshold, cannot scrobble
        self.tracker.record_scrobble("Artist B", "Track 2", timestamp=now - 500)
        allowed, wait_sec = self.tracker.can_scrobble(max_daily_limit=2, now=now)
        self.assertFalse(allowed)
        self.assertEqual(wait_sec, (now - 1000 + 86400) - now)

        # Scenario 3: count > limit (e.g. 3 > 2): two rows must expire
        self.tracker.record_scrobble("Artist C", "Track 3", timestamp=now - 200)
        allowed, wait_sec = self.tracker.can_scrobble(max_daily_limit=2, now=now)
        self.assertFalse(allowed)
        expected_wait = max(1, ((now - 500) + 86400) - now)
        self.assertEqual(wait_sec, expected_wait)

    def test_duplicate_timestamps_deterministic_ordering(self):
        now = 1700000000
        # Two tracks recorded with identical timestamp
        ts_same = now - 7200
        self.tracker.record_scrobble("Artist A", "Track 1", timestamp=ts_same)  # id 1
        self.tracker.record_scrobble("Artist B", "Track 2", timestamp=ts_same)  # id 2
        self.tracker.record_scrobble("Artist C", "Track 3", timestamp=now - 3600)  # id 3

        # limit = 2, count = 3 -> two rows must expire; equal timestamps are
        # ordered by id, so the second row at ts_same is the threshold row.
        allowed, wait_sec = self.tracker.can_scrobble(max_daily_limit=2, now=now)
        self.assertFalse(allowed)
        self.assertEqual(wait_sec, (ts_same + 86400) - now)

        # limit = 1, count = 3 -> all three rows must expire; the newest row is last.
        allowed_l1, wait_sec_l1 = self.tracker.can_scrobble(max_daily_limit=1, now=now)
        self.assertFalse(allowed_l1)
        self.assertEqual(wait_sec_l1, ((now - 3600) + 86400) - now)


    def test_rolling_window_inclusive_cutoff_and_expiry(self):
        now = 1700000000
        self.tracker.record_scrobble("A", "at-cutoff", timestamp=now - 86400)
        self.tracker.record_scrobble("A", "inside", timestamp=now - 86399)
        self.assertEqual(self.tracker.get_rolling_24h_count(now=now), 2)
        # At the exact cutoff the record is included, so with limit 2 we're capped.
        allowed, wait_sec = self.tracker.can_scrobble(max_daily_limit=2, now=now)
        self.assertFalse(allowed)
        self.assertEqual(wait_sec, 1)
        # One second later, the first record is outside the inclusive window.
        self.assertEqual(self.tracker.get_rolling_24h_count(now=now + 1), 1)
        allowed, wait_sec = self.tracker.can_scrobble(max_daily_limit=2, now=now + 1)
        self.assertTrue(allowed)
        self.assertEqual(wait_sec, 0)

    def test_limit_zero_never_allows_and_limit_one_waits_for_expiry(self):
        now = 1700000000
        allowed_zero, wait_zero = self.tracker.can_scrobble(max_daily_limit=0, now=now)
        self.assertFalse(allowed_zero)
        self.assertGreaterEqual(wait_zero, 1)
        self.tracker.record_scrobble("A", "one", timestamp=now - 100)
        allowed_one, wait_one = self.tracker.can_scrobble(max_daily_limit=1, now=now)
        self.assertFalse(allowed_one)
        self.assertEqual(wait_one, 86300)


if __name__ == "__main__":
    unittest.main()
