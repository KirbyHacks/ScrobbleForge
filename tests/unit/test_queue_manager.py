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
        # in-memory index advances, but sqlite is not updated until confirmed
        self.assertIsNone(self.tracker.get_state("queue_index"))
        qm.commit_track_progress()
        self.assertEqual(self.tracker.get_state("queue_index"), "1")

        t2 = qm.get_next_track()
        self.assertEqual(t2.title, "Track 1")
        qm.commit_track_progress()

        t3 = qm.get_next_track()
        self.assertEqual(t3.title, "Track 2")
        qm.commit_track_progress()

        # loops back to track 0
        t4 = qm.get_next_track()
        self.assertEqual(t4.title, "Track 0")

    def test_at_least_once_uncommitted_track_replayed_on_restart(self):
        """verify that an unconfirmed track during shutdown is not lost and is replayed on restart."""
        tracks = [
            Track(title="Track 1", artist="Artist", duration_ms=180000),
            Track(title="Track 2", artist="Artist", duration_ms=180000),
        ]
        qm1 = QueueManager(tracks=tracks, tracker=self.tracker, shuffle=False, loop=False)

        # track 1 fetched for playback
        t1 = qm1.get_next_track()
        self.assertEqual(t1.title, "Track 1")
        # simulated shutdown before confirmation: commit_track_progress() is not called
        # engine restarts with same tracker
        qm2 = QueueManager(tracks=tracks, tracker=self.tracker, shuffle=False, loop=False)
        replayed = qm2.get_next_track()
        self.assertEqual(replayed.title, "Track 1")

        # now confirmed and committed
        qm2.commit_track_progress()
        self.assertEqual(self.tracker.get_state("queue_index"), "1")

        # next restart resumes to track 2
        qm3 = QueueManager(tracks=tracks, tracker=self.tracker, shuffle=False, loop=False)
        t2 = qm3.get_next_track()
        self.assertEqual(t2.title, "Track 2")

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
        """verify that reloading preserves the exact shuffled permutation and saved index."""
        tracks = [
            Track(title=f"Song {i}", artist="Artist", duration_ms=180000, spotify_id=f"id_{i}")
            for i in range(10)
        ]
        qm1 = QueueManager(tracks=tracks, tracker=self.tracker, shuffle=True, loop=True)
        initial_order = [t.title for t in qm1.queue]

        # scrobble 3 tracks and commit each
        t0 = qm1.get_next_track()
        qm1.commit_track_progress()
        t1 = qm1.get_next_track()
        qm1.commit_track_progress()
        t2 = qm1.get_next_track()
        qm1.commit_track_progress()

        self.assertEqual(t0.title, initial_order[0])
        self.assertEqual(t1.title, initial_order[1])
        self.assertEqual(t2.title, initial_order[2])
        self.assertEqual(qm1.current_index, 3)
        self.assertEqual(self.tracker.get_state("queue_index"), "3")

        # simulate restart with new queuemanager instance sharing the same tracker db
        qm2 = QueueManager(tracks=tracks, tracker=self.tracker, shuffle=True, loop=True)

        # exact sequence must be preserved
        reloaded_order = [t.title for t in qm2.queue]
        self.assertEqual(reloaded_order, initial_order)

        # current index must be restored to 3
        self.assertEqual(qm2.current_index, 3)

        # next track must be the 4th track from the initial permutation
        t3 = qm2.get_next_track()
        qm2.commit_track_progress()
        self.assertEqual(t3.title, initial_order[3])
        self.assertEqual(qm2.current_index, 4)
        self.assertEqual(self.tracker.get_state("queue_index"), "4")

    def test_shuffle_generates_new_permutation_on_loop(self):
        """verify that reaching the end of the queue generates a fresh permutation upon loop wrap-around."""
        tracks = [
            Track(title=f"Song {i}", artist="Artist", duration_ms=180000, spotify_id=f"id_{i}")
            for i in range(20)
        ]
        qm = QueueManager(tracks=tracks, tracker=self.tracker, shuffle=True, loop=True)
        perm1 = [t.title for t in qm.queue]

        # advance through all 20 tracks
        for _ in range(20):
            qm.get_next_track()

        self.assertEqual(qm.current_index, 20)

        # 21st call triggers wrap-around and reshuffle
        first_track_cycle2 = qm.get_next_track()
        self.assertIsNotNone(first_track_cycle2)
        self.assertEqual(qm.current_index, 1)

        perm2 = [t.title for t in qm.queue]
        # should contain the exact same tracks, but in a newly shuffled order
        self.assertEqual(set(perm2), set(perm1))
        self.assertNotEqual(perm2, perm1)
        self.assertEqual(first_track_cycle2.title, perm2[0])

    def test_source_tracks_change_resets_shuffle_permutation(self):
        """verify that changing source tracks triggers a fresh permutation and resets index."""
        tracks1 = [Track(title=f"Original {i}", artist="Artist") for i in range(5)]
        qm1 = QueueManager(tracks=tracks1, tracker=self.tracker, shuffle=True, loop=True)
        qm1.get_next_track()
        qm1.get_next_track()
        self.assertEqual(qm1.current_index, 2)

        # refresh with completely new source tracks
        tracks2 = [Track(title=f"New {i}", artist="Artist") for i in range(5)]
        qm2 = QueueManager(tracks=tracks2, tracker=self.tracker, shuffle=True, loop=True)
        self.assertEqual(qm2.current_index, 0)
        self.assertEqual(len(qm2.queue), 5)
        self.assertEqual(set(t.title for t in qm2.queue), set(t.title for t in tracks2))

    def test_update_tracks_resets_permutation_and_index(self):
        """verify that update_tracks triggers a fresh permutation and resets index."""
        tracks1 = [Track(title=f"Track {i}", artist="Artist") for i in range(5)]
        qm = QueueManager(tracks=tracks1, tracker=self.tracker, shuffle=True, loop=True)
        qm.get_next_track()
        self.assertEqual(qm.current_index, 1)

        new_tracks = [Track(title=f"Updated {i}", artist="Artist") for i in range(5)]
        qm.update_tracks(new_tracks)
        self.assertEqual(qm.current_index, 0)
        self.assertEqual(set(t.title for t in qm.queue), set(t.title for t in new_tracks))

    def test_ordered_queue_resets_changed_sources_on_restart_and_refresh(self):
        for restart in (False, True):
            with self.subTest(restart=restart):
                tracker = QuotaTracker(Path(self.temp_dir.name) / f"ordered_{restart}.db")
                tracks = [Track(title=t, artist="Artist") for t in ("A", "B", "C")]
                queue = QueueManager(tracks, tracker, shuffle=False, loop=False)
                queue.get_next_track()
                queue.commit_track_progress()
                replacement = [Track(title=t, artist="Artist") for t in ("X", "Y", "Z")]
                if restart:
                    queue = QueueManager(replacement, tracker, shuffle=False, loop=False)
                else:
                    queue.update_tracks(replacement)
                self.assertEqual(tracker.get_state("queue_index"), "0")
                self.assertEqual(queue.get_next_track().title, "X")

    def test_ordered_queue_refresh_preserves_unchanged_sources(self):
        tracks = [Track(title=t, artist="Artist") for t in ("A", "B")]
        queue = QueueManager(tracks, self.tracker, shuffle=False, loop=False)
        queue.get_next_track()
        queue.commit_track_progress()
        queue.update_tracks(tracks)
        self.assertEqual(queue.get_next_track().title, "B")

    def test_switch_shuffle_to_ordered_resets_checkpoint_and_avoids_omission(self):
        """scrobbleforge-d3j: switching from shuffle to ordered resets checkpoint preventing track omission."""
        tracker = QuotaTracker(Path(self.temp_dir.name) / "shuffle_to_ordered.db")
        tracks = [Track(title=t, artist="Artist") for t in ("A", "B", "C")]

        # session 1: run in shuffle mode, play 1 track and commit index 1
        q_shuffled = QueueManager(tracks, tracker, shuffle=True, loop=False)
        first_shuffled_track = q_shuffled.get_next_track()
        self.assertIsNotNone(first_shuffled_track)
        q_shuffled.commit_track_progress()
        self.assertEqual(tracker.get_state("queue_index"), "1")

        # session 2: restart with same source tracks but shuffle=false
        q_ordered = QueueManager(tracks, tracker, shuffle=False, loop=False)
        self.assertEqual(tracker.get_state("queue_index"), "0")
        self.assertEqual(q_ordered.current_index, 0)

        # plays all tracks sequentially without omitting a
        t1 = q_ordered.get_next_track()
        self.assertEqual(t1.title, "A")
        t2 = q_ordered.get_next_track()
        self.assertEqual(t2.title, "B")
        t3 = q_ordered.get_next_track()
        self.assertEqual(t3.title, "C")
        self.assertIsNone(q_ordered.get_next_track())

    def test_switch_ordered_to_shuffle_resets_checkpoint(self):
        """scrobbleforge-d3j: switching from ordered to shuffle resets checkpoint."""
        tracker = QuotaTracker(Path(self.temp_dir.name) / "ordered_to_shuffle.db")
        tracks = [Track(title=t, artist="Artist") for t in ("A", "B", "C")]

        # session 1: ordered mode, play track a
        q_ordered = QueueManager(tracks, tracker, shuffle=False, loop=False)
        t1 = q_ordered.get_next_track()
        self.assertEqual(t1.title, "A")
        q_ordered.commit_track_progress()
        self.assertEqual(tracker.get_state("queue_index"), "1")

        # session 2: restart with shuffle=true
        q_shuffled = QueueManager(tracks, tracker, shuffle=True, loop=False)
        self.assertEqual(tracker.get_state("queue_index"), "0")
        self.assertEqual(q_shuffled.current_index, 0)
        self.assertEqual(len(q_shuffled.queue), 3)

    def test_same_order_mode_restores_progress_matching_semantics(self):
        """scrobbleforge-d3j: progress is restored only when order semantics match."""
        tracker = QuotaTracker(Path(self.temp_dir.name) / "same_mode.db")
        tracks = [Track(title=t, artist="Artist") for t in ("A", "B", "C")]

        # ordered -> ordered
        q1 = QueueManager(tracks, tracker, shuffle=False, loop=False)
        self.assertEqual(q1.get_next_track().title, "A")
        q1.commit_track_progress()

        q2 = QueueManager(tracks, tracker, shuffle=False, loop=False)
        self.assertEqual(q2.get_next_track().title, "B")


if __name__ == "__main__":
    unittest.main()
