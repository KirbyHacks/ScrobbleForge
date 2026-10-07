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

    def test_shuffle_persistence_across_reloads(self):
        """Verifies that reloading/restarting preserves the exact shuffled permutation and saved index."""
        tracks = [
            Track(title=f"Song {i}", artist="Artist", duration_ms=180000, spotify_id=f"id_{i}")
            for i in range(10)
        ]
        qm1 = QueueManager(tracks=tracks, tracker=self.tracker, shuffle=True, loop=True)
        initial_order = [t.title for t in qm1.queue]

        # Scrobble 3 tracks
        t0 = qm1.get_next_track()
        t1 = qm1.get_next_track()
        t2 = qm1.get_next_track()

        self.assertEqual(t0.title, initial_order[0])
        self.assertEqual(t1.title, initial_order[1])
        self.assertEqual(t2.title, initial_order[2])
        self.assertEqual(qm1.current_index, 3)
        self.assertEqual(self.tracker.get_state("queue_index"), "3")

        # Simulate restart/reload with new QueueManager instance sharing the same tracker state DB
        qm2 = QueueManager(tracks=tracks, tracker=self.tracker, shuffle=True, loop=True)

        # Exact sequence must be preserved
        reloaded_order = [t.title for t in qm2.queue]
        self.assertEqual(reloaded_order, initial_order)

        # Current index must be restored to 3
        self.assertEqual(qm2.current_index, 3)

        # Next track must be the 4th track from the initial permutation
        t3 = qm2.get_next_track()
        self.assertEqual(t3.title, initial_order[3])
        self.assertEqual(qm2.current_index, 4)
        self.assertEqual(self.tracker.get_state("queue_index"), "4")

    def test_shuffle_generates_new_permutation_on_loop(self):
        """Verifies that reaching the end of the queue generates a fresh permutation upon loop wrap-around."""
        tracks = [
            Track(title=f"Song {i}", artist="Artist", duration_ms=180000, spotify_id=f"id_{i}")
            for i in range(20)
        ]
        qm = QueueManager(tracks=tracks, tracker=self.tracker, shuffle=True, loop=True)
        perm1 = [t.title for t in qm.queue]

        # Advance through all 20 tracks
        for _ in range(20):
            qm.get_next_track()

        self.assertEqual(qm.current_index, 20)

        # 21st call triggers wrap-around and reshuffle
        first_track_cycle2 = qm.get_next_track()
        self.assertIsNotNone(first_track_cycle2)
        self.assertEqual(qm.current_index, 1)

        perm2 = [t.title for t in qm.queue]
        # Should contain the exact same tracks, but in a newly shuffled order
        self.assertEqual(set(perm2), set(perm1))
        self.assertNotEqual(perm2, perm1)
        self.assertEqual(first_track_cycle2.title, perm2[0])

    def test_source_tracks_change_resets_shuffle_permutation(self):
        """Verifies that changing source tracks triggers a fresh permutation and resets index."""
        tracks1 = [Track(title=f"Original {i}", artist="Artist") for i in range(5)]
        qm1 = QueueManager(tracks=tracks1, tracker=self.tracker, shuffle=True, loop=True)
        qm1.get_next_track()
        qm1.get_next_track()
        self.assertEqual(qm1.current_index, 2)

        # Refresh with completely new source tracks
        tracks2 = [Track(title=f"New {i}", artist="Artist") for i in range(5)]
        qm2 = QueueManager(tracks=tracks2, tracker=self.tracker, shuffle=True, loop=True)
        self.assertEqual(qm2.current_index, 0)
        self.assertEqual(len(qm2.queue), 5)
        self.assertEqual(set(t.title for t in qm2.queue), set(t.title for t in tracks2))

    def test_update_tracks_resets_permutation_and_index(self):
        """Verifies that update_tracks triggers a fresh permutation and resets index."""
        tracks1 = [Track(title=f"Track {i}", artist="Artist") for i in range(5)]
        qm = QueueManager(tracks=tracks1, tracker=self.tracker, shuffle=True, loop=True)
        qm.get_next_track()
        self.assertEqual(qm.current_index, 1)

        new_tracks = [Track(title=f"Updated {i}", artist="Artist") for i in range(5)]
        qm.update_tracks(new_tracks)
        self.assertEqual(qm.current_index, 0)
        self.assertEqual(set(t.title for t in qm.queue), set(t.title for t in new_tracks))


if __name__ == "__main__":
    unittest.main()
