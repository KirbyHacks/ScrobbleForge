import random
import sqlite3
import sys
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.unit._dependency_guard import ensure_dependencies_mocked

ensure_dependencies_mocked()

import requests
from src.models import Track
from src.quota_tracker import QuotaTracker
from src.queue_manager import QueueManager
from src.engine import ScrobblerEngine, is_transient_error
from src.lastfm_client import LastFMClient, LastFMTemporaryError, LastFMRateLimitError, LastFMAuthError
from src.spotify_client import SpotifyIngestionError
from src.config import AppConfig, LastFMConfig, SpotifyConfig, EngineConfig, SystemConfig


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
        stop_event.set()

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

        track1 = qm.get_next_track()
        engine._execute_max_limit_step(track1)
        ts1 = initial_cursor
        ts2 = engine.virtual_timeline_cursor

        self.assertEqual(ts2, ts1 + 180 + 2)
        self.assertGreater(ts2, ts1 + track1.duration_sec)

    def test_max_limit_timeline_never_moves_backwards_when_catching_up_to_present(self):
        """Verifies that virtual timeline never jumps backwards when cursor catches up to or exceeds now."""
        cfg = AppConfig(
            lastfm=LastFMConfig(api_key="k", api_secret="s", username="u"),
            spotify=SpotifyConfig(),
            engine=EngineConfig(mode="max_limit", max_daily_scrobbles=2750),
            system=SystemConfig(data_dir=Path(self.temp_dir.name)),
        )
        tracks = [
            Track(title="Song A", artist="Artist A", duration_ms=180000),  # 180s
            Track(title="Song B", artist="Artist B", duration_ms=200000),  # 200s
            Track(title="Song C", artist="Artist C", duration_ms=210000),  # 210s
        ]
        qm = QueueManager(tracks=tracks, tracker=self.tracker, shuffle=False, loop=False)
        stop_event = threading.Event()
        stop_event.set()

        engine = ScrobblerEngine(
            config=cfg,
            lastfm=self.mock_lfm,
            spotify=None,
            queue=qm,
            tracker=self.tracker,
            stop_event=stop_event,
        )

        now = int(time.time())
        # Set cursor right at now - 30s
        engine.virtual_timeline_cursor = now - 30
        self.mock_lfm.scrobbles.clear()

        for t in tracks:
            engine._execute_max_limit_step(t)

        submitted_timestamps = [ts for (_, ts) in self.mock_lfm.scrobbles]
        durations = [t.duration_sec for t in tracks]

        self.assertEqual(len(submitted_timestamps), 3)
        for i in range(len(submitted_timestamps) - 1):
            ts_n = submitted_timestamps[i]
            ts_next = submitted_timestamps[i + 1]
            prev_dur = durations[i]
            self.assertGreater(
                ts_next,
                ts_n + prev_dur,
                f"Timeline jumped backwards or overlapped: ts[{i+1}]={ts_next} <= ts[{i}]={ts_n} + {prev_dur}",
            )

    def test_max_limit_monotonicity_property_over_consecutive_tracks(self):
        """Property test: 20 consecutive tracks produce strictly monotonic timestamps with no negative deltas."""
        cfg = AppConfig(
            lastfm=LastFMConfig(api_key="k", api_secret="s", username="u"),
            spotify=SpotifyConfig(),
            engine=EngineConfig(mode="max_limit", max_daily_scrobbles=2750),
            system=SystemConfig(data_dir=Path(self.temp_dir.name)),
        )
        tracks = [
            Track(
                title=f"Track {i}",
                artist="Artist",
                duration_ms=random.randint(25000, 300000),
            )
            for i in range(20)
        ]
        qm = QueueManager(tracks=tracks, tracker=self.tracker, shuffle=False, loop=False)
        stop_event = threading.Event()
        stop_event.set()

        engine = ScrobblerEngine(
            config=cfg,
            lastfm=self.mock_lfm,
            spotify=None,
            queue=qm,
            tracker=self.tracker,
            stop_event=stop_event,
        )

        self.mock_lfm.scrobbles.clear()
        for t in tracks:
            engine._execute_max_limit_step(t)

        submitted_timestamps = [ts for (_, ts) in self.mock_lfm.scrobbles]
        self.assertEqual(len(submitted_timestamps), 20)
        for i in range(len(submitted_timestamps) - 1):
            delta = submitted_timestamps[i + 1] - submitted_timestamps[i]
            self.assertGreater(
                delta,
                0,
                f"Negative or zero delta at index {i}: {submitted_timestamps[i+1]} - {submitted_timestamps[i]} = {delta}",
            )
            expected_min_delta = max(30, tracks[i].duration_sec) + 2
            self.assertGreaterEqual(delta, expected_min_delta)

    def test_max_limit_restart_preserves_forward_continuity(self):
        """Verifies that engine restart resumes virtual timeline cursor without resetting to past."""
        cfg = AppConfig(
            lastfm=LastFMConfig(api_key="k", api_secret="s", username="u"),
            spotify=SpotifyConfig(),
            engine=EngineConfig(mode="max_limit", max_daily_scrobbles=2750),
            system=SystemConfig(data_dir=Path(self.temp_dir.name)),
        )
        track1 = Track(title="Song 1", artist="Artist", duration_ms=180000)
        track2 = Track(title="Song 2", artist="Artist", duration_ms=200000)
        qm1 = QueueManager(tracks=[track1], tracker=self.tracker, shuffle=False, loop=False)
        stop_event = threading.Event()
        stop_event.set()

        engine1 = ScrobblerEngine(
            config=cfg,
            lastfm=self.mock_lfm,
            spotify=None,
            queue=qm1,
            tracker=self.tracker,
            stop_event=stop_event,
        )
        self.mock_lfm.scrobbles.clear()
        engine1._execute_max_limit_step(track1)
        emitted_ts1 = self.mock_lfm.scrobbles[-1][1]
        saved_cursor = engine1.virtual_timeline_cursor

        # Simulate engine restart with same persistent tracker
        qm2 = QueueManager(tracks=[track2], tracker=self.tracker, shuffle=False, loop=False)
        engine2 = ScrobblerEngine(
            config=cfg,
            lastfm=self.mock_lfm,
            spotify=None,
            queue=qm2,
            tracker=self.tracker,
            stop_event=stop_event,
        )
        self.assertEqual(engine2.virtual_timeline_cursor, saved_cursor)
        engine2._execute_max_limit_step(track2)
        emitted_ts2 = self.mock_lfm.scrobbles[-1][1]
        self.assertEqual(emitted_ts2, saved_cursor)
        self.assertGreater(emitted_ts2, emitted_ts1 + track1.duration_sec)

    def test_max_limit_timeline_advancement_enforces_30s_minimum_for_short_tracks(self):
        """Verifies that short tracks (<30s) advance virtual timeline by at least 30s + 2s padding."""
        cfg = AppConfig(
            lastfm=LastFMConfig(api_key="k", api_secret="s", username="u"),
            spotify=SpotifyConfig(),
            engine=EngineConfig(mode="max_limit", max_daily_scrobbles=2750),
            system=SystemConfig(data_dir=Path(self.temp_dir.name)),
        )
        short_track = Track(title="Interlude", artist="Artist", duration_ms=12000)  # 12 seconds authentic
        qm = QueueManager(tracks=[short_track], tracker=self.tracker, shuffle=False, loop=False)
        stop_event = threading.Event()
        stop_event.set()  # Avoid real-time pacing delay during unit test execution

        engine = ScrobblerEngine(
            config=cfg,
            lastfm=self.mock_lfm,
            spotify=None,
            queue=qm,
            tracker=self.tracker,
            stop_event=stop_event,
        )

        initial_cursor = engine.virtual_timeline_cursor
        # Execute max limit step directly
        engine._execute_max_limit_step(short_track)

        # Scrobble duration constraint is max(30, 12) = 30s
        # Timeline must advance by 30 + 2 = 32s, NOT 12 + 2 = 14s
        expected_cursor = initial_cursor + 30 + 2
        self.assertEqual(engine.virtual_timeline_cursor, expected_cursor)

    def test_lastfm_client_scrobble_and_now_playing_enforces_30s_clamp(self):
        """Verifies that LastFMClient passes max(30, duration_sec) to Last.fm network calls."""
        client = LastFMClient.__new__(LastFMClient)
        client.network = MagicMock()

        # Short track: 10s authentic
        short_track = Track(title="Intro", artist="Artist", duration_ms=10000)
        self.assertEqual(short_track.duration_sec, 10)

        client.scrobble(short_track, timestamp=1700000000)
        client.network.scrobble.assert_called_with(
            artist="Artist",
            title="Intro",
            timestamp=1700000000,
            album=None,
            album_artist=None,
            track_number=1,
            duration=30,  # Clamped to 30s platform rule!
        )

        client.update_now_playing(short_track)
        client.network.update_now_playing.assert_called_with(
            artist="Artist",
            title="Intro",
            album=None,
            album_artist=None,
            track_number=1,
            duration=30,  # Clamped to 30s platform rule!
        )

        # Normal track: 200s authentic
        normal_track = Track(title="Full Song", artist="Artist", duration_ms=200000)
        self.assertEqual(normal_track.duration_sec, 200)

        client.scrobble(normal_track, timestamp=1700000100)
        client.network.scrobble.assert_called_with(
            artist="Artist",
            title="Full Song",
            timestamp=1700000100,
            album=None,
            album_artist=None,
            track_number=1,
            duration=200,  # Authentic 200s!
        )

    @patch("threading.Event.wait", return_value=False)
    def test_transient_error_triggers_retry_and_resets_on_success(self, mock_wait):
        """Verifies that transient errors increment error count and retry without skipping track."""
        cfg = AppConfig(
            lastfm=LastFMConfig(api_key="k", api_secret="s", username="u"),
            spotify=SpotifyConfig(),
            engine=EngineConfig(mode="custom_interval", custom_interval_seconds=0),
            system=SystemConfig(data_dir=Path(self.temp_dir.name)),
        )
        track = Track(title="Retry Song", artist="Retry Artist", duration_ms=180000)
        qm = QueueManager(tracks=[track], tracker=self.tracker, shuffle=False, loop=False)
        stop_event = threading.Event()

        mock_lfm = MagicMock()
        # Fail once with transient error, then succeed
        mock_lfm.scrobble.side_effect = [LastFMTemporaryError("Temporary 503"), True]

        engine = ScrobblerEngine(
            config=cfg,
            lastfm=mock_lfm,
            spotify=None,
            queue=qm,
            tracker=self.tracker,
            stop_event=stop_event,
        )

        engine.run()

        self.assertEqual(mock_lfm.scrobble.call_count, 2)
        self.assertEqual(engine.consecutive_errors, 0)
        self.assertFalse(engine.circuit_broken)
        self.assertIsNone(engine.pending_track)

    def test_deterministic_bugs_halt_immediately(self):
        """Verifies that KeyError and AttributeError are raised and stop the loop immediately."""
        cfg = AppConfig(
            lastfm=LastFMConfig(api_key="k", api_secret="s", username="u"),
            spotify=SpotifyConfig(),
            engine=EngineConfig(mode="custom_interval", custom_interval_seconds=0),
            system=SystemConfig(data_dir=Path(self.temp_dir.name)),
        )
        stop_event = threading.Event()

        # Test KeyError
        track_key = Track(title="Bug Song", artist="Bug Artist", duration_ms=180000)
        qm_key = QueueManager(tracks=[track_key], tracker=None, shuffle=False, loop=False)
        mock_lfm_key = MagicMock()
        mock_lfm_key.scrobble.side_effect = KeyError("missing_key_in_logic")
        engine_key = ScrobblerEngine(
            config=cfg,
            lastfm=mock_lfm_key,
            spotify=None,
            queue=qm_key,
            tracker=self.tracker,
            stop_event=stop_event,
        )
        with self.assertRaises(KeyError):
            engine_key.run()

        # Test AttributeError
        track_attr = Track(title="Bug Song 2", artist="Bug Artist 2", duration_ms=180000)
        qm_attr = QueueManager(tracks=[track_attr], tracker=None, shuffle=False, loop=False)
        mock_lfm_attr = MagicMock()
        mock_lfm_attr.scrobble.side_effect = AttributeError("NoneType has no attribute 'name'")
        engine_attr = ScrobblerEngine(
            config=cfg,
            lastfm=mock_lfm_attr,
            spotify=None,
            queue=qm_attr,
            tracker=self.tracker,
            stop_event=stop_event,
        )
        with self.assertRaises(AttributeError):
            engine_attr.run()

    @patch("threading.Event.wait", return_value=False)
    def test_circuit_breaker_trips_after_threshold_consecutive_failures(self, mock_wait):
        """Verifies that the circuit breaker trips after threshold consecutive transient failures."""
        cfg = AppConfig(
            lastfm=LastFMConfig(api_key="k", api_secret="s", username="u"),
            spotify=SpotifyConfig(),
            engine=EngineConfig(mode="custom_interval", custom_interval_seconds=0),
            system=SystemConfig(data_dir=Path(self.temp_dir.name)),
        )
        track = Track(title="CB Song", artist="CB Artist", duration_ms=180000)
        qm = QueueManager(tracks=[track], tracker=self.tracker, shuffle=False, loop=True)
        stop_event = threading.Event()

        mock_lfm = MagicMock()
        mock_lfm.scrobble.side_effect = LastFMTemporaryError("Service Unavailable 503")

        engine = ScrobblerEngine(
            config=cfg,
            lastfm=mock_lfm,
            spotify=None,
            queue=qm,
            tracker=self.tracker,
            stop_event=stop_event,
            circuit_breaker_threshold=5,
        )

        engine.run()

        self.assertTrue(engine.circuit_broken)
        self.assertEqual(engine.consecutive_errors, 5)
        self.assertEqual(mock_lfm.scrobble.call_count, 5)

    def test_transient_error_classification(self):
        """Verifies is_transient_error classifies transient vs deterministic logic bugs."""
        self.assertTrue(is_transient_error(LastFMTemporaryError("503")))
        self.assertTrue(is_transient_error(LastFMRateLimitError("rate limited")))
        self.assertTrue(is_transient_error(requests.RequestException("connection drop")))
        self.assertTrue(is_transient_error(sqlite3.OperationalError("database is locked")))
        self.assertTrue(is_transient_error(SpotifyIngestionError("network issue")))
        self.assertTrue(is_transient_error(ConnectionError("socket dropped")))
        self.assertTrue(is_transient_error(TimeoutError("timed out")))

        # Deterministic bugs must be False
        self.assertFalse(is_transient_error(KeyError("missing_field")))
        self.assertFalse(is_transient_error(AttributeError("no attribute")))
        self.assertFalse(is_transient_error(TypeError("bad type")))
        self.assertFalse(is_transient_error(ZeroDivisionError("zero division")))
        self.assertFalse(is_transient_error(ValueError("bad value")))


if __name__ == "__main__":
    unittest.main()
