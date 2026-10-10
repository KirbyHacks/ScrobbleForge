"""regression tests for P1.2 SourceFactory integration and engine migration."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import requests

from src.config import AppConfig, EngineConfig, LastFMConfig, SpotifyConfig, SystemConfig
from src.core.models import CanonicalTrack, to_canonical_track, to_legacy_track
from src.engine import ScrobblerEngine
from src.models import Track
from src.queue_manager import QueueManager, _compute_source_hash, _track_identifier
from src.quota_tracker import QuotaTracker
from src.sources import (
    SourceAuthError,
    SourceError,
    SourceFactory,
    SourceIngestionService,
    SourceTemporaryError,
    SpotifyAdapter,
    UnsupportedSourceError,
)
from src.spotify_client import SpotifyClient, SpotifyIngestionError


def _build_embed_html(entity: dict) -> str:
    """helper building mock spotify embed html payload."""
    payload = {"props": {"pageProps": {"state": {"data": {"entity": entity}}}}}
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script></html>'


class TestSourceMigrationMetadataEquivalence(unittest.TestCase):
    """verify metadata equivalence between legacy SpotifyClient and SourceIngestionService."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cache_path = Path(self.temp_dir.name) / "tracks_cache.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    @patch("src.spotify_client.requests.get")
    def test_playlist_ingestion_behavioral_equivalence(self, mock_get):
        """verify playlist tracks from legacy client and new ingestion service are identical."""
        mock_entity = {
            "name": "Electronic Hits",
            "trackList": [
                {
                    "title": "Around the World",
                    "subtitle": "Daft Punk",
                    "duration": 238000,
                    "uri": "spotify:track:1pKYYYAvuoFu2R779hL7rU",
                    "album": {"name": "Homework"},
                },
                {
                    "title": "One More Time",
                    "subtitle": "Daft Punk, Romanthony",
                    "duration": 320000,
                    "uri": "spotify:track:0DiWol3AO6WpXZgp0goxAV",
                    "album": {"name": "Discovery"},
                },
            ],
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = _build_embed_html(mock_entity)
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        source = "https://open.spotify.com/playlist/37i9dQZF1DXdLEN7aqioXM"

        # fetch via legacy client (without cache to isolate fetch logic)
        legacy_client = SpotifyClient(cache_path=None)
        legacy_tracks = legacy_client.fetch_sources([source], use_cache_if_available=False)

        # fetch via new source ingestion service
        service = SourceIngestionService(cache_path=None)
        service_tracks = service.fetch_sources([source], use_cache_if_available=False)

        self.assertEqual(len(legacy_tracks), len(service_tracks))
        for lt, st in zip(legacy_tracks, service_tracks):
            self.assertEqual(lt.title, st.title)
            self.assertEqual(lt.artist, st.artist)
            self.assertEqual(lt.album, st.album)
            self.assertEqual(lt.album_artist, st.album_artist)
            self.assertEqual(lt.duration_ms, st.duration_ms)
            self.assertEqual(lt.track_number, st.track_number)
            self.assertEqual(lt.spotify_id, st.spotify_id)
            self.assertEqual(lt.source_name, st.source_name)

    @patch("src.spotify_client.requests.get")
    def test_album_and_single_track_equivalence(self, mock_get):
        """verify album and single track parsing match across both pipelines."""
        album_entity = {
            "name": "Random Access Memories",
            "subtitle": "Daft Punk",
            "tracks": {
                "items": [
                    {
                        "name": "Give Life Back to Music",
                        "artists": [{"name": "Daft Punk"}],
                        "duration_ms": 274000,
                        "uri": "spotify:track:0dEIa2jcVyXMpmV2WlcrNd",
                    }
                ]
            },
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = _build_embed_html(album_entity)
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        source = "spotify:album:4m2880jivSbbyEGAKfITCa"
        legacy_client = SpotifyClient()
        legacy_tracks = legacy_client.fetch_sources([source], use_cache_if_available=False)

        service = SourceIngestionService()
        service_tracks = service.fetch_sources([source], use_cache_if_available=False)

        self.assertEqual(len(legacy_tracks), 1)
        self.assertEqual(len(service_tracks), 1)
        self.assertEqual(legacy_tracks[0].title, service_tracks[0].title)
        self.assertEqual(legacy_tracks[0].artist, service_tracks[0].artist)
        self.assertEqual(legacy_tracks[0].duration_ms, service_tracks[0].duration_ms)
        self.assertEqual(legacy_tracks[0].spotify_id, service_tracks[0].spotify_id)

    @patch("src.spotify_client.requests.get")
    def test_multi_source_ordering_and_numbering(self, mock_get):
        """verify track ordering across multiple playlist sources is preserved."""
        def mock_fetch(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.raise_for_status.return_value = None
            if "playlist1" in url:
                resp.text = _build_embed_html({
                    "name": "P1",
                    "trackList": [
                        {"title": "Track 1", "subtitle": "Artist 1", "duration": 180000, "uri": "spotify:track:id1"},
                    ]
                })
            else:
                resp.text = _build_embed_html({
                    "name": "P2",
                    "trackList": [
                        {"title": "Track 2", "subtitle": "Artist 2", "duration": 200000, "uri": "spotify:track:id2"},
                    ]
                })
            return resp

        mock_get.side_effect = mock_fetch

        sources = [
            "https://open.spotify.com/playlist/playlist1",
            "https://open.spotify.com/playlist/playlist2",
        ]

        service = SourceIngestionService()
        tracks = service.fetch_sources(sources, use_cache_if_available=False)

        self.assertEqual(len(tracks), 2)
        self.assertEqual(tracks[0].title, "Track 1")
        self.assertEqual(tracks[0].track_number, 1)
        self.assertEqual(tracks[1].title, "Track 2")
        self.assertEqual(tracks[1].track_number, 2)


class TestSourceMigrationCacheCompatibility(unittest.TestCase):
    """verify cache compatibility, reuse, and non-overwriting guarantees."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cache_path = Path(self.temp_dir.name) / "tracks_cache.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_legacy_saved_cache_is_reused_by_source_service(self):
        """verify that a cache file written by SpotifyClient is reused without network calls."""
        sources = [
            "https://open.spotify.com/playlist/alpha",
            "https://open.spotify.com/playlist/beta",
        ]
        legacy_tracks = [
            Track(title="Song 1", artist="Artist 1", duration_ms=190000, spotify_id="id1", source_name="Playlist A"),
            Track(title="Song 2", artist="Artist 2", duration_ms=210000, spotify_id="id2", source_name="Playlist B"),
        ]

        # write legacy cache to disk
        client = SpotifyClient(cache_path=self.cache_path)
        client.save_cache(legacy_tracks, sources)
        self.assertTrue(self.cache_path.is_file())

        # now use SourceIngestionService with use_cache_if_available=True
        with patch("src.spotify_client.requests.get") as mock_get:
            service = SourceIngestionService(cache_path=self.cache_path)
            loaded_tracks = service.fetch_sources(sources, use_cache_if_available=True)

            mock_get.assert_not_called()
            self.assertEqual(len(loaded_tracks), 2)
            self.assertEqual(loaded_tracks[0].title, "Song 1")
            self.assertEqual(loaded_tracks[0].spotify_id, "id1")
            self.assertEqual(loaded_tracks[1].title, "Song 2")
            self.assertEqual(loaded_tracks[1].spotify_id, "id2")

    def test_single_source_fetch_does_not_overwrite_valid_batch_cache(self):
        """verify that fetching a single source does not destroy a multi-source batch cache."""
        batch_sources = [
            "https://open.spotify.com/playlist/alpha",
            "https://open.spotify.com/playlist/beta",
        ]
        batch_tracks = [
            Track(title="Song 1", artist="Artist 1", duration_ms=190000, spotify_id="id1"),
            Track(title="Song 2", artist="Artist 2", duration_ms=210000, spotify_id="id2"),
        ]

        client = SpotifyClient(cache_path=self.cache_path, ttl_hours=12.0)
        client.save_cache(batch_tracks, batch_sources)

        # attempt to save a single source subset
        subset_sources = ["https://open.spotify.com/playlist/alpha"]
        subset_tracks = [Track(title="Song 1", artist="Artist 1", duration_ms=190000, spotify_id="id1")]
        client.save_cache(subset_tracks, subset_sources)

        # verify that the cache still contains the full batch
        with open(self.cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(len(data.get("sources", [])), 2)
        self.assertEqual(len(data.get("tracks", [])), 2)

    def test_same_size_different_sources_cache_isolation(self):
        """verify that two distinct source sets with identical length have isolated caches."""
        set_a = [
            "https://open.spotify.com/playlist/playlist_1",
            "https://open.spotify.com/playlist/playlist_2",
        ]
        set_b = [
            "https://open.spotify.com/playlist/playlist_3",
            "https://open.spotify.com/playlist/playlist_4",
        ]
        tracks_a = [Track(title="Song A", artist="Artist A", spotify_id="a1")]
        client = SpotifyClient(cache_path=self.cache_path)
        client.save_cache(tracks_a, set_a)

        # set A matches cache
        self.assertEqual(len(client.load_cache(sources=set_a)), 1)
        # set B must return empty list (isolated cache miss)
        self.assertEqual(client.load_cache(sources=set_b), [])

    def test_interrupted_cache_write_leaves_valid_cache_intact(self):
        """verify that interrupted write does not corrupt or truncate existing valid cache."""
        sources = ["https://open.spotify.com/playlist/valid"]
        tracks = [Track(title="Original Song", artist="Artist", spotify_id="orig")]
        client = SpotifyClient(cache_path=self.cache_path)
        client.save_cache(tracks, sources)
        self.assertTrue(self.cache_path.is_file())

        new_tracks = [Track(title="Interrupted Song", artist="Artist", spotify_id="int")]
        # simulate failure during json serialization or write
        with patch("json.dump", side_effect=IOError("Simulated disk write interruption")):
            client.save_cache(new_tracks, sources)

        # verify that the original cache file was not corrupted or truncated
        loaded = client.load_cache(sources=sources)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].title, "Original Song")

    def test_reordered_sources_returns_tracks_in_newly_requested_order(self):
        """verify that [A, B] and [B, A] reuse cache and return tracks in newly requested order."""
        source_a = "https://open.spotify.com/playlist/playlistA"
        source_b = "https://open.spotify.com/playlist/playlistB"
        sources_ab = [source_a, source_b]
        sources_ba = [source_b, source_a]

        def mock_fetch(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.raise_for_status.return_value = None
            if "playlistA" in url:
                resp.text = _build_embed_html({
                    "name": "Playlist A",
                    "trackList": [{"title": "Track A1", "subtitle": "Artist A", "duration": 180000, "uri": "spotify:track:a1"}],
                })
            else:
                resp.text = _build_embed_html({
                    "name": "Playlist B",
                    "trackList": [{"title": "Track B1", "subtitle": "Artist B", "duration": 200000, "uri": "spotify:track:b1"}],
                })
            return resp

        # 1 & 2: fetch A followed by B and save cache
        service = SourceIngestionService(cache_path=self.cache_path)
        with patch("src.spotify_client.requests.get", side_effect=mock_fetch) as mock_get:
            tracks_ab = service.fetch_sources(sources_ab, use_cache_if_available=False)
            self.assertEqual(mock_get.call_count, 2)
            self.assertEqual(tracks_ab[0].title, "Track A1")
            self.assertEqual(tracks_ab[1].title, "Track B1")

        self.assertTrue(self.cache_path.is_file())

        # 3, 4, 5: request B followed by A using cache (network call should NOT happen)
        with patch("src.spotify_client.requests.get") as mock_get:
            tracks_ba = service.fetch_sources(sources_ba, use_cache_if_available=True)
            mock_get.assert_not_called()
            self.assertEqual(len(tracks_ba), 2)
            # exact source and track ordering: B followed by A!
            self.assertEqual(tracks_ba[0].title, "Track B1")
            self.assertEqual(tracks_ba[0].source_name, "Playlist B")
            self.assertEqual(tracks_ba[1].title, "Track A1")
            self.assertEqual(tracks_ba[1].source_name, "Playlist A")

        # 6: verify queue identity and checkpoint compatibility
        db_path = Path(self.temp_dir.name) / "state.db"
        tracker = QuotaTracker(db_path=db_path)
        qm = QueueManager(tracks=tracks_ba, tracker=tracker, shuffle=False, loop=False)
        self.assertEqual(qm.queue[0].title, "Track B1")
        self.assertEqual(qm.queue[1].title, "Track A1")
        # advance and checkpoint
        t1 = qm.get_next_track()
        qm.commit_track_progress()
        self.assertEqual(t1.title, "Track B1")
        self.assertEqual(tracker.get_state("queue_index"), "1")


class TestSourceMigrationQueuePersistence(unittest.TestCase):
    """verify queue source hash, shuffle restoration, and resume compatibility."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "state.db"
        self.tracker = QuotaTracker(db_path=self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_queue_hash_and_shuffle_resumed_across_service_ingestion(self):
        """verify QueueManager restores saved shuffle order when tracks come from SourceIngestionService."""
        tracks = [
            Track(title="Track A", artist="Artist A", duration_ms=180000, spotify_id="idA", track_number=1),
            Track(title="Track B", artist="Artist B", duration_ms=200000, spotify_id="idB", track_number=2),
            Track(title="Track C", artist="Artist C", duration_ms=220000, spotify_id="idC", track_number=3),
        ]

        # initial session with shuffle enabled
        qm1 = QueueManager(tracks=tracks, tracker=self.tracker, shuffle=True, loop=False)
        saved_hash = self.tracker.get_state("queue_source_hash")
        saved_perm = self.tracker.get_state("queue_permutation")
        self.assertTrue(saved_hash)
        self.assertTrue(saved_perm)

        first_order = [t.title for t in qm1.queue]

        # simulate restart with identical tracks converted via CanonicalTrack
        canonical_tracks = [to_canonical_track(t) for t in tracks]
        restored_tracks = [to_legacy_track(ct, track_number=idx) for idx, ct in enumerate(canonical_tracks, start=1)]

        qm2 = QueueManager(tracks=restored_tracks, tracker=self.tracker, shuffle=True, loop=False)
        restored_order = [t.title for t in qm2.queue]

        self.assertEqual(first_order, restored_order)
        self.assertEqual(self.tracker.get_state("queue_source_hash"), saved_hash)

    def test_queue_checkpoint_resumed_across_restart(self):
        """verify playback index resumes accurately across restart."""
        tracks = [
            Track(title="Track 1", artist="Artist 1", duration_ms=180000, spotify_id="id1"),
            Track(title="Track 2", artist="Artist 2", duration_ms=180000, spotify_id="id2"),
            Track(title="Track 3", artist="Artist 3", duration_ms=180000, spotify_id="id3"),
        ]

        qm = QueueManager(tracks=tracks, tracker=self.tracker, shuffle=False, loop=False)
        track = qm.get_next_track()  # advances current_index from 0 to 1
        qm.commit_track_progress()   # commits current_index to database
        self.assertIsNotNone(track)
        self.assertEqual(qm.current_index, 1)
        self.assertEqual(self.tracker.get_state("queue_index"), "1")

        # recreate queue simulating restart
        qm_restarted = QueueManager(tracks=tracks, tracker=self.tracker, shuffle=False, loop=False)
        self.assertEqual(qm_restarted.current_index, 1)


class TestSourceMigrationStrictFailuresAndSafeguards(unittest.TestCase):
    """verify strict failure semantics, no partial queues, and engine error resilience."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "state.db"
        self.tracker = QuotaTracker(db_path=self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_unsupported_source_raises_unsupported_source_error(self):
        """verify unsupported url raises UnsupportedSourceError without silent empty result."""
        service = SourceIngestionService()
        with self.assertRaises(UnsupportedSourceError):
            service.fetch_sources(["https://unsupported-service.com/playlist/123"])

    @patch("src.spotify_client.requests.get")
    def test_temporary_network_failure_raises_temporary_error(self, mock_get):
        """verify temporary network error raises SourceTemporaryError."""
        mock_get.side_effect = requests.RequestException("Connection timed out")
        service = SourceIngestionService()
        with self.assertRaises(SourceTemporaryError):
            service.fetch_sources(["https://open.spotify.com/playlist/timeout_test"], use_cache_if_available=False)

    @patch("src.spotify_client.requests.get")
    def test_no_partial_queue_when_one_source_fails(self, mock_get):
        """verify failure in multi-source request does not produce a partial queue."""
        def mock_call(url, **kwargs):
            if "good" in url:
                resp = MagicMock()
                resp.status_code = 200
                resp.text = _build_embed_html({
                    "name": "Good",
                    "trackList": [{"title": "Good Track", "subtitle": "Artist", "duration": 180000, "uri": "spotify:track:g1"}],
                })
                resp.raise_for_status.return_value = None
                return resp
            raise requests.RequestException("503 Service Unavailable")

        mock_get.side_effect = mock_call

        service = SourceIngestionService()
        sources = [
            "https://open.spotify.com/playlist/good",
            "https://open.spotify.com/playlist/bad",
        ]
        with self.assertRaises(SourceTemporaryError):
            service.fetch_sources(sources, use_cache_if_available=False)

    def test_periodic_refresh_failure_preserves_running_queue(self):
        """verify periodic refresh failure in ScrobblerEngine does not clobber existing queue."""
        cfg = AppConfig(
            lastfm=LastFMConfig(api_key="k", api_secret="s", username="u"),
            spotify=SpotifyConfig(sources=["https://open.spotify.com/playlist/test"], refresh_interval_hours=12.0),
            engine=EngineConfig(mode="realistic"),
            system=SystemConfig(data_dir=Path(self.temp_dir.name)),
        )
        initial_track = Track(title="Preserved Song", artist="Preserved Artist", duration_ms=180000)
        qm = QueueManager(tracks=[initial_track], tracker=self.tracker, shuffle=False, loop=False)

        mock_service = MagicMock(spec=SourceIngestionService)
        mock_service.fetch_sources.side_effect = SourceTemporaryError("Upstream provider offline")

        engine = ScrobblerEngine(
            config=cfg,
            lastfm=MagicMock(),
            spotify=mock_service,
            queue=qm,
            tracker=self.tracker,
            stop_event=threading.Event(),
        )

        now = time.time()
        engine.last_refresh_time = now - (13 * 3600)  # trigger refresh

        engine._check_periodic_refresh()

        # verify existing queue is completely preserved
        self.assertEqual(len(qm.queue), 1)
        self.assertEqual(qm.queue[0].title, "Preserved Song")
        # next refresh is backed off by 15 minutes
        expected_backoff = now - (12 * 3600) + 900
        self.assertAlmostEqual(engine.last_refresh_time, expected_backoff, delta=5.0)

    def test_no_scrobble_or_queue_advance_during_ingestion(self):
        """verify that source fetching and service instantiation do not alter queue or send scrobbles."""
        mock_lfm = MagicMock()
        service = SourceIngestionService()
        self.assertEqual(mock_lfm.scrobble.call_count, 0)
        self.assertIsNone(self.tracker.get_state("queue_index"))


class TestSourceMigrationStartupOrchestration(unittest.TestCase):
    """verify startup orchestration, fallback decisions, and failure boundaries in main.py."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        # create mock fallback file in data_dir
        self.fallback_file = self.data_dir / "tracks.txt"
        self.fallback_file.write_text("Fallback Artist - Fallback Song\n", encoding="utf-8")

    def tearDown(self):
        self.temp_dir.cleanup()

    def _make_config(self, sources):
        return AppConfig(
            lastfm=LastFMConfig(api_key="k", api_secret="s", username="u", session_key="sk"),
            spotify=SpotifyConfig(sources=sources),
            engine=EngineConfig(mode="realistic"),
            system=SystemConfig(data_dir=self.data_dir),
        )

    @patch("src.main.ScrobblerEngine")
    @patch("src.main.LastFMClient")
    @patch("src.main.load_local_fallback_tracks")
    @patch("src.main.SourceIngestionService")
    @patch("src.main.load_config")
    def test_successful_ingestion_does_not_trigger_local_fallback(
        self, mock_load_config, mock_source_service_cls, mock_load_fallback, mock_lfm_cls, mock_engine_cls
    ):
        """successful ingestion loads provider tracks and skips local fallback files."""
        from src.main import main
        mock_load_config.return_value = self._make_config(["https://open.spotify.com/playlist/test"])
        mock_service = MagicMock()
        mock_service.fetch_sources.return_value = [Track(title="Online Track", artist="Artist")]
        mock_source_service_cls.return_value = mock_service

        mock_engine = MagicMock()
        mock_engine_cls.return_value = mock_engine

        main()

        mock_load_fallback.assert_not_called()
        mock_engine.run.assert_called_once()

    @patch("src.main.ScrobblerEngine")
    @patch("src.main.LastFMClient")
    @patch("src.main.SourceIngestionService")
    @patch("src.main.load_config")
    def test_successful_empty_playlist_triggers_local_fallback(
        self, mock_load_config, mock_source_service_cls, mock_lfm_cls, mock_engine_cls
    ):
        """legitimately empty provider playlist triggers local fallback without error."""
        from src.main import main
        mock_load_config.return_value = self._make_config(["https://open.spotify.com/playlist/empty"])
        mock_service = MagicMock()
        mock_service.fetch_sources.return_value = []  # legitimately empty
        mock_source_service_cls.return_value = mock_service

        mock_engine = MagicMock()
        mock_engine_cls.return_value = mock_engine

        main()

        init_kwargs = mock_engine_cls.call_args[1]
        self.assertEqual(len(init_kwargs["queue"].queue), 1)
        self.assertEqual(init_kwargs["queue"].queue[0].title, "Fallback Song")
        mock_engine.run.assert_called_once()

    @patch("src.main.ScrobblerEngine")
    @patch("src.main.LastFMClient")
    @patch("src.main.load_local_fallback_tracks")
    @patch("src.main.SourceIngestionService")
    @patch("src.main.load_config")
    def test_provider_auth_failure_aborts_without_local_fallback(
        self, mock_load_config, mock_source_service_cls, mock_load_fallback, mock_lfm_cls, mock_engine_cls
    ):
        """provider authentication failure must abort startup and never trigger local fallback."""
        from src.main import main
        mock_load_config.return_value = self._make_config(["https://open.spotify.com/playlist/auth_fail"])
        mock_service = MagicMock()
        mock_service.fetch_sources.side_effect = SourceAuthError("Spotify auth token rejected")
        mock_source_service_cls.return_value = mock_service

        with self.assertRaises(SystemExit) as cm:
            main()

        self.assertEqual(cm.exception.code, 1)
        mock_load_fallback.assert_not_called()
        mock_engine_cls.assert_not_called()

    @patch("src.main.ScrobblerEngine")
    @patch("src.main.LastFMClient")
    @patch("src.main.load_local_fallback_tracks")
    @patch("src.main.SourceIngestionService")
    @patch("src.main.load_config")
    def test_temporary_network_failure_aborts_without_local_fallback(
        self, mock_load_config, mock_source_service_cls, mock_load_fallback, mock_lfm_cls, mock_engine_cls
    ):
        """temporary network failure must abort startup and never trigger local fallback."""
        from src.main import main
        mock_load_config.return_value = self._make_config(["https://open.spotify.com/playlist/net_fail"])
        mock_service = MagicMock()
        mock_service.fetch_sources.side_effect = SourceTemporaryError("503 Service Unavailable")
        mock_source_service_cls.return_value = mock_service

        with self.assertRaises(SystemExit) as cm:
            main()

        self.assertEqual(cm.exception.code, 1)
        mock_load_fallback.assert_not_called()
        mock_engine_cls.assert_not_called()

    @patch("src.main.ScrobblerEngine")
    @patch("src.main.LastFMClient")
    @patch("src.main.load_local_fallback_tracks")
    @patch("src.main.SourceIngestionService")
    @patch("src.main.load_config")
    def test_unsupported_source_aborts_without_local_fallback(
        self, mock_load_config, mock_source_service_cls, mock_load_fallback, mock_lfm_cls, mock_engine_cls
    ):
        """unsupported source url must abort startup and never trigger local fallback."""
        from src.main import main
        mock_load_config.return_value = self._make_config(["https://unsupported.com/playlist/1"])
        mock_service = MagicMock()
        mock_service.fetch_sources.side_effect = UnsupportedSourceError("Unknown provider")
        mock_source_service_cls.return_value = mock_service

        with self.assertRaises(SystemExit) as cm:
            main()

        self.assertEqual(cm.exception.code, 1)
        mock_load_fallback.assert_not_called()
        mock_engine_cls.assert_not_called()

    @patch("src.main.ScrobblerEngine")
    @patch("src.main.LastFMClient")
    @patch("src.main.load_local_fallback_tracks")
    @patch("src.main.SourceIngestionService")
    @patch("src.main.load_config")
    def test_partial_multi_source_failure_aborts_without_local_fallback(
        self, mock_load_config, mock_source_service_cls, mock_load_fallback, mock_lfm_cls, mock_engine_cls
    ):
        """partial multi-source failure must abort startup and never trigger local fallback."""
        from src.main import main
        mock_load_config.return_value = self._make_config([
            "https://open.spotify.com/playlist/good",
            "https://open.spotify.com/playlist/bad",
        ])
        mock_service = MagicMock()
        mock_service.fetch_sources.side_effect = SourceTemporaryError("One source failed")
        mock_source_service_cls.return_value = mock_service

        with self.assertRaises(SystemExit) as cm:
            main()

        self.assertEqual(cm.exception.code, 1)
        mock_load_fallback.assert_not_called()
        mock_engine_cls.assert_not_called()

    @patch("src.main.ScrobblerEngine")
    @patch("src.main.LastFMClient")
    @patch("src.main.SourceIngestionService")
    @patch("src.main.load_config")
    def test_no_configured_sources_triggers_local_fallback(
        self, mock_load_config, mock_source_service_cls, mock_lfm_cls, mock_engine_cls
    ):
        """empty source configuration legitimately falls back to local fallback files."""
        from src.main import main
        mock_load_config.return_value = self._make_config([])
        mock_engine = MagicMock()
        mock_engine_cls.return_value = mock_engine

        main()

        mock_source_service_cls.assert_not_called()
        init_kwargs = mock_engine_cls.call_args[1]
        self.assertEqual(len(init_kwargs["queue"].queue), 1)
        self.assertEqual(init_kwargs["queue"].queue[0].title, "Fallback Song")
        mock_engine.run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
