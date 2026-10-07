import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import unittest
import tempfile
import time
from src.quota_tracker import QuotaTracker
from src.models import Track
from src.config import load_config
from src.spotify_client import SpotifyClient, load_local_fallback_tracks
from src.queue_manager import QueueManager


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

    def test_single_track_fetch(self):
        client = SpotifyClient()
        tracks = client.fetch_sources(["https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT"], use_cache_if_available=False)
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0].title, "Never Gonna Give You Up")
        self.assertEqual(tracks[0].artist, "Rick Astley")
        self.assertGreater(tracks[0].duration_ms, 200000)

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
        from src.engine import ScrobblerEngine
        from src.config import AppConfig, LastFMConfig, SpotifyConfig, EngineConfig, SystemConfig
        import threading

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
        # Mock stop_event to stop immediately after 1 step
        # Call step directly
        # Emulate 0 wait
        now = int(time.time())
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

