"""integration tests for src/main.py entrypoint, runtime wiring, and lifecycle."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.config import (
    AppConfig,
    EngineConfig,
    LastFMConfig,
    SourceConfig,
    SpotifyConfig,
    SystemConfig,
    load_config,
)
from src.core.models import CanonicalTrack
from src.engine import ScrobblerEngine
from src.main import cmd_init, main
from src.models import Track
from src.queue_manager import QueueManager
from src.quota_tracker import QuotaTracker
from src.sources.exceptions import SourceTemporaryError, UnsupportedSourceError
from src.spotify_client import SpotifyClient


class TestMainIntegration(unittest.TestCase):
    """validates application startup wiring, multi-provider integration, and cli init."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name) / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.orig_cwd = os.getcwd()

    def tearDown(self):
        os.chdir(self.orig_cwd)
        self.temp_dir.cleanup()

    def _make_config(
        self,
        sources=None,
        spotify_sources=None,
        youtube_api_key=None,
        provider_options=None,
    ) -> AppConfig:
        sources_list = list(sources) if sources is not None else []
        sp_sources = list(spotify_sources) if spotify_sources is not None else []
        return AppConfig(
            lastfm=LastFMConfig(
                api_key="mock_lfm_key",
                api_secret="mock_lfm_secret",
                username="mock_user",
                session_key="mock_session_key",
            ),
            spotify=SpotifyConfig(
                sources=sp_sources,
                client_id="sp_cid",
                client_secret="sp_csec",
                refresh_interval_hours=12.0,
            ),
            sources=SourceConfig(
                sources=sources_list,
                refresh_interval_hours=12.0,
                provider_options=dict(provider_options or {}),
                youtube_api_key=youtube_api_key,
            ),
            engine=EngineConfig(mode="realistic"),
            system=SystemConfig(data_dir=self.data_dir),
        )

    # 1. legacy spotify-only startup
    @patch("src.main.ScrobblerEngine")
    @patch("src.main.LastFMClient")
    @patch("src.main.load_config")
    @patch("src.spotify_client.SpotifyClient.fetch_sources")
    def test_legacy_spotify_only_startup(
        self, mock_sp_fetch, mock_load_config, mock_lfm_cls, mock_engine_cls
    ):
        """startup with legacy spotify config resolves tracks and injects engine correctly."""
        cfg = self._make_config(
            sources=[],
            spotify_sources=["https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"],
        )
        mock_load_config.return_value = cfg
        mock_sp_fetch.return_value = [
            Track(title="Spotify Track", artist="Spotify Artist", spotify_id="sp_1")
        ]
        mock_engine = MagicMock()
        mock_engine_cls.return_value = mock_engine

        main()

        mock_sp_fetch.assert_called_once()
        mock_engine_cls.assert_called_once()
        kwargs = mock_engine_cls.call_args[1]

        # verify engine dependency wiring
        self.assertIsNotNone(kwargs.get("source_service"))
        self.assertIs(kwargs.get("spotify"), kwargs.get("source_service"))
        queue = kwargs["queue"]
        self.assertEqual(queue.total_tracks, 1)
        self.assertEqual(queue.queue[0].title, "Spotify Track")
        mock_engine.run.assert_called_once()

    # 2. generic spotify-only startup
    @patch("src.main.ScrobblerEngine")
    @patch("src.main.LastFMClient")
    @patch("src.main.load_config")
    @patch("src.spotify_client.SpotifyClient.fetch_sources")
    def test_generic_spotify_only_startup(
        self, mock_sp_fetch, mock_load_config, mock_lfm_cls, mock_engine_cls
    ):
        """startup with music_sources pointing to spotify ingests tracks successfully."""
        cfg = self._make_config(
            sources=["https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"],
            spotify_sources=[],
        )
        mock_load_config.return_value = cfg
        mock_sp_fetch.return_value = [
            Track(title="Generic Sp Track", artist="Artist", spotify_id="sp_gen")
        ]
        mock_engine = MagicMock()
        mock_engine_cls.return_value = mock_engine

        main()

        mock_sp_fetch.assert_called_once()
        kwargs = mock_engine_cls.call_args[1]
        queue = kwargs["queue"]
        self.assertEqual(queue.total_tracks, 1)
        self.assertEqual(queue.queue[0].title, "Generic Sp Track")
        mock_engine.run.assert_called_once()

    # 3. youtube-only startup without spotifyclient construction
    @patch("src.main.ScrobblerEngine")
    @patch("src.main.LastFMClient")
    @patch("src.main.load_config")
    @patch("src.sources.youtube_adapter.YouTubeAdapter._fetch_playlist_youtubei")
    @patch.object(SpotifyClient, "__init__", side_effect=AssertionError("SpotifyClient initialized for YouTube-only!"))
    def test_youtube_only_startup_without_spotify_client(
        self, mock_sp_init, mock_yt_fetch, mock_load_config, mock_lfm_cls, mock_engine_cls
    ):
        """youtube-only source must not instantiate or touch spotifyclient."""
        cfg = self._make_config(
            sources=["https://music.youtube.com/playlist?list=PLrAl46nJ81c_k3gD-6VnK1d0l6nQx8_i4"],
            spotify_sources=[],
        )
        mock_load_config.return_value = cfg
        mock_yt_fetch.return_value = [
            CanonicalTrack(
                title="YT Track 1",
                artists=["YT Artist 1"],
                provider_id="youtube",
                external_ids={"youtube": "yt_vid_1"},
            )
        ]
        mock_engine = MagicMock()
        mock_engine_cls.return_value = mock_engine

        main()

        mock_sp_init.assert_not_called()
        mock_yt_fetch.assert_called_once()
        kwargs = mock_engine_cls.call_args[1]
        queue = kwargs["queue"]
        self.assertEqual(queue.total_tracks, 1)
        track = queue.queue[0]
        self.assertEqual(track.title, "YT Track 1")
        self.assertEqual(track.artist, "YT Artist 1")
        self.assertEqual(track.provider_id, "youtube")
        self.assertIsNone(track.spotify_id)
        mock_engine.run.assert_called_once()

    # 4. youtube api key forwarding
    @patch("src.main.ScrobblerEngine")
    @patch("src.main.LastFMClient")
    @patch("src.main.load_config")
    @patch("src.sources.youtube_adapter.YouTubeAdapter._fetch_playlist_data_api_v3")
    def test_youtube_api_key_forwarding(
        self, mock_yt_api_fetch, mock_load_config, mock_lfm_cls, mock_engine_cls
    ):
        """configured youtube_api_key is forwarded to youtubeadapter backend."""
        from src.sources.source_factory import SourceFactory

        test_api_key = "AIzaSySecretTestKey999"
        cfg = self._make_config(
            sources=["https://www.youtube.com/playlist?list=PLrAl46nJ81c_k3gD-6VnK1d0l6nQx8_i4"],
            youtube_api_key=test_api_key,
            provider_options={"youtube": {"api_key": test_api_key}},
        )
        mock_load_config.return_value = cfg
        mock_yt_api_fetch.return_value = [
            CanonicalTrack(
                title="Data API Track",
                artists=["Data API Artist"],
                provider_id="youtube",
                external_ids={"youtube": "vid_999"},
            )
        ]
        mock_engine = MagicMock()
        mock_engine_cls.return_value = mock_engine

        with patch("src.sources.source_factory.SourceFactory.create_client", wraps=SourceFactory.create_client) as spy_create:
            main()
            # check that youtube client was created with api_key
            yt_call = [c for c in spy_create.call_args_list if c[0][0] == "youtube"]
            self.assertTrue(len(yt_call) >= 1)
            self.assertEqual(yt_call[0][1].get("api_key"), test_api_key)

        # verify the data api fetch path was called
        mock_yt_api_fetch.assert_called_once()
        mock_engine.run.assert_called_once()

    # 5. mixed-provider startup
    @patch("src.main.ScrobblerEngine")
    @patch("src.main.LastFMClient")
    @patch("src.main.load_config")
    @patch("src.spotify_client.SpotifyClient.fetch_sources")
    @patch("src.sources.youtube_adapter.YouTubeAdapter._fetch_playlist_youtubei")
    def test_mixed_provider_startup_preserves_order(
        self, mock_yt_fetch, mock_sp_fetch, mock_load_config, mock_lfm_cls, mock_engine_cls
    ):
        """mixed spotify and youtube sources maintain exact ordering and provider metadata."""
        cfg = self._make_config(
            sources=[
                "https://open.spotify.com/playlist/sp_mix_1",
                "https://music.youtube.com/playlist?list=PLyt_mix_2",
            ]
        )
        cfg.engine.shuffle = False
        mock_load_config.return_value = cfg
        mock_sp_fetch.return_value = [
            Track(title="Spotify In Mixed", artist="Artist S", spotify_id="sp_mix_id")
        ]
        mock_yt_fetch.return_value = [
            CanonicalTrack(
                title="YouTube In Mixed",
                artists=["Artist Y"],
                provider_id="youtube",
                external_ids={"youtube": "yt_mix_id"},
            )
        ]
        mock_engine = MagicMock()
        mock_engine_cls.return_value = mock_engine

        main()

        kwargs = mock_engine_cls.call_args[1]
        queue = kwargs["queue"]
        self.assertEqual(queue.total_tracks, 2)
        # track 1 is spotify
        self.assertEqual(queue.queue[0].title, "Spotify In Mixed")
        self.assertEqual(queue.queue[0].spotify_id, "sp_mix_id")
        # track 2 is youtube
        self.assertEqual(queue.queue[1].title, "YouTube In Mixed")
        self.assertEqual(queue.queue[1].provider_id, "youtube")
        self.assertIsNone(queue.queue[1].spotify_id)

    # 6. correct injection into scrobblerengine
    @patch("src.main.ScrobblerEngine")
    @patch("src.main.LastFMClient")
    @patch("src.main.load_config")
    @patch("src.sources.youtube_adapter.YouTubeAdapter._fetch_playlist_youtubei")
    def test_scrobbler_engine_injection_aliases(
        self, mock_yt_fetch, mock_load_config, mock_lfm_cls, mock_engine_cls
    ):
        """engine receives both source_service and backward-compatible spotify alias."""
        cfg = self._make_config(
            sources=["https://music.youtube.com/playlist?list=PLtest"]
        )
        mock_load_config.return_value = cfg
        mock_yt_fetch.return_value = [
            CanonicalTrack(title="Song", artists=["Artist"], provider_id="youtube")
        ]
        mock_engine = MagicMock()
        mock_engine_cls.return_value = mock_engine

        main()

        kwargs = mock_engine_cls.call_args[1]
        service = kwargs["source_service"]
        self.assertIsNotNone(service)
        self.assertIs(kwargs["spotify"], service)

        # test backward-compatible alias inside scrobblerengine itself
        engine = ScrobblerEngine(
            config=cfg,
            lastfm=MagicMock(),
            queue=kwargs["queue"],
            tracker=MagicMock(),
            stop_event=MagicMock(),
            source_service=service,
        )
        self.assertIs(engine.source_service, service)
        self.assertIs(engine.spotify, service)

    # 7. successful queue creation with youtube tracks
    @patch("src.main.ScrobblerEngine")
    @patch("src.main.LastFMClient")
    @patch("src.main.load_config")
    @patch("src.sources.youtube_adapter.YouTubeAdapter._fetch_playlist_youtubei")
    def test_queue_creation_with_youtube_tracks(
        self, mock_yt_fetch, mock_load_config, mock_lfm_cls, mock_engine_cls
    ):
        """youtube tracks populate queuemanager correctly with distinct identifiers."""
        cfg = self._make_config(
            sources=["https://music.youtube.com/playlist?list=PLyt_queue"]
        )
        mock_load_config.return_value = cfg
        mock_yt_fetch.return_value = [
            CanonicalTrack(
                title="Song A",
                artists=["Artist 1"],
                album="Album 1",
                duration_ms=210000,
                provider_id="youtube",
                external_ids={"youtube": "yt_a"},
            ),
            CanonicalTrack(
                title="Song B",
                artists=["Artist 2"],
                album="Album 2",
                duration_ms=180000,
                provider_id="youtube",
                external_ids={"youtube": "yt_b"},
            ),
        ]
        mock_engine = MagicMock()
        mock_engine_cls.return_value = mock_engine

        main()

        queue = mock_engine_cls.call_args[1]["queue"]
        self.assertEqual(queue.total_tracks, 2)
        next_track = queue.get_next_track()
        self.assertIsNotNone(next_track)
        self.assertIn(next_track.title, ["Song A", "Song B"])
        self.assertEqual(next_track.provider_id, "youtube")

    # 8. source ingestion failure aborts safely
    @patch("src.main.ScrobblerEngine")
    @patch("src.main.LastFMClient")
    @patch("src.main.load_config")
    @patch("src.sources.service.SourceIngestionService.fetch_sources")
    def test_source_ingestion_failure_aborts_without_fallback(
        self, mock_fetch, mock_load_config, mock_lfm_cls, mock_engine_cls
    ):
        """when source ingestion raises an error, startup aborts and does not load fallback."""
        fallback_file = self.data_dir / "tracks.txt"
        fallback_file.write_text("Fallback Artist - Fallback Song\n", encoding="utf-8")

        cfg = self._make_config(
            sources=["https://music.youtube.com/playlist?list=PLfailing"]
        )
        mock_load_config.return_value = cfg
        mock_fetch.side_effect = SourceTemporaryError("Upstream 503")

        with self.assertRaises(SystemExit) as ctx:
            main()

        self.assertEqual(ctx.exception.code, 1)
        mock_engine_cls.assert_not_called()

    # 9. local fallback behavior remains compatible
    @patch("src.main.ScrobblerEngine")
    @patch("src.main.LastFMClient")
    @patch("src.main.load_config")
    def test_local_fallback_compatibility(
        self, mock_load_config, mock_lfm_cls, mock_engine_cls
    ):
        """when no sources configured, local tracks.txt fallback loads successfully."""
        fallback_file = self.data_dir / "tracks.txt"
        fallback_file.write_text("Fallback Band - Fallback Anthem\n", encoding="utf-8")

        cfg = self._make_config(sources=[], spotify_sources=[])
        mock_load_config.return_value = cfg
        mock_engine = MagicMock()
        mock_engine_cls.return_value = mock_engine

        main()

        mock_engine_cls.assert_called_once()
        queue = mock_engine_cls.call_args[1]["queue"]
        self.assertEqual(queue.total_tracks, 1)
        self.assertEqual(queue.queue[0].title, "Fallback Anthem")
        self.assertEqual(queue.queue[0].artist, "Fallback Band")

    # 10. refresh of youtube and mixed sources
    def test_periodic_refresh_youtube_and_mixed_sources(self):
        """scrobblerengine periodic refresh updates queue for generic and youtube sources."""
        cfg = self._make_config(
            sources=[
                "https://open.spotify.com/playlist/sp1",
                "https://music.youtube.com/playlist?list=yt1",
            ]
        )
        cfg.sources.refresh_interval_hours = 1.0  # 1 hour
        mock_service = MagicMock()
        mock_service.fetch_sources.return_value = [
            Track(title="Refreshed Track 1", artist="Artist 1"),
            Track(title="Refreshed Track 2", artist="Artist 2", provider_id="youtube"),
        ]

        tracker = MagicMock()
        queue = QueueManager(
            tracks=[Track(title="Old Track", artist="Old Artist")],
            tracker=tracker,
            shuffle=False,
            loop=False,
        )

        engine = ScrobblerEngine(
            config=cfg,
            lastfm=MagicMock(),
            queue=queue,
            tracker=tracker,
            stop_event=MagicMock(),
            source_service=mock_service,
        )

        # artificially trigger refresh by moving last_refresh_time back
        engine.last_refresh_time = 0.0
        engine._check_periodic_refresh()

        mock_service.fetch_sources.assert_called_once_with(
            cfg.sources.sources,
            use_cache_if_available=False,
        )
        self.assertEqual(queue.total_tracks, 2)
        self.assertEqual(queue.queue[0].title, "Refreshed Track 1")
        self.assertEqual(queue.queue[1].title, "Refreshed Track 2")

    def test_periodic_refresh_failure_preserves_queue(self):
        """periodic refresh failure backs off and preserves the healthy queue."""
        cfg = self._make_config(sources=["https://music.youtube.com/playlist?list=yt1"])
        cfg.sources.refresh_interval_hours = 1.0
        mock_service = MagicMock()
        mock_service.fetch_sources.side_effect = SourceTemporaryError("Upstream timeout")

        tracker = MagicMock()
        initial_tracks = [Track(title="Healthy Track", artist="Healthy Artist")]
        queue = QueueManager(tracks=initial_tracks, tracker=tracker, shuffle=False)

        engine = ScrobblerEngine(
            config=cfg,
            lastfm=MagicMock(),
            queue=queue,
            tracker=tracker,
            stop_event=MagicMock(),
            source_service=mock_service,
        )

        engine.last_refresh_time = 0.0
        engine._check_periodic_refresh()

        # queue must retain healthy tracks
        self.assertEqual(queue.total_tracks, 1)
        self.assertEqual(queue.queue[0].title, "Healthy Track")

    # 11. cmd_init output and preservation of existing .env
    def test_cmd_init_creates_files_with_multi_provider_template(self):
        """cmd_init creates docker-compose.yml and .env containing multi-provider settings."""
        init_dir = Path(self.temp_dir.name) / "init_test"
        init_dir.mkdir(parents=True, exist_ok=True)
        os.chdir(init_dir)

        cmd_init()

        env_file = init_dir / ".env"
        compose_file = init_dir / "docker-compose.yml"
        self.assertTrue(env_file.is_file())
        self.assertTrue(compose_file.is_file())

        content = env_file.read_text(encoding="utf-8")
        self.assertIn("MUSIC_SOURCES=", content)
        self.assertIn("YOUTUBE_API_KEY=", content)
        self.assertIn("LASTFM_API_KEY=", content)
        self.assertIn("SPOTIFY_PLAYLIST_URL=", content)

    def test_cmd_init_preserves_existing_env(self):
        """cmd_init never overwrites an existing .env file."""
        init_dir = Path(self.temp_dir.name) / "init_preserve"
        init_dir.mkdir(parents=True, exist_ok=True)
        os.chdir(init_dir)

        env_file = init_dir / ".env"
        env_file.write_text("LASTFM_API_KEY=custom_preserved_key\n", encoding="utf-8")

        cmd_init()

        content = env_file.read_text(encoding="utf-8")
        self.assertEqual(content, "LASTFM_API_KEY=custom_preserved_key\n")

    # 12. security: api key redaction in exceptions
    def test_youtube_api_key_redacted_in_exceptions(self):
        """youtubeadapter translates errors redacting configured api_key from exception text."""
        from src.sources.youtube_adapter import YouTubeAdapter
        import requests

        secret_key = "AIzaSySuperSecretKey123"
        adapter = YouTubeAdapter(api_key=secret_key)

        req_exc = requests.RequestException(
            f"Failed to connect to https://www.googleapis.com/youtube/v3/videos?key={secret_key}&part=snippet"
        )

        with self.assertRaises(Exception) as ctx:
            adapter._translate_request_error(req_exc, "Failed YouTube API call")

        exc_str = str(ctx.exception)
        self.assertNotIn(secret_key, exc_str)
        self.assertIn("[REDACTED]", exc_str)

    # 13. cache isolation: spotify and youtube caches remain distinct
    def test_cache_isolation_between_spotify_and_youtube(self):
        """sourcefactory isolates youtube cache from spotify cache."""
        from src.sources.source_factory import SourceFactory

        base_cache = Path(self.temp_dir.name) / "tracks_cache.json"
        sp_kwargs = SourceFactory._resolve_provider_kwargs("spotify", cache_path=base_cache)
        yt_kwargs = SourceFactory._resolve_provider_kwargs("youtube", cache_path=base_cache)

        self.assertEqual(sp_kwargs.get("cache_path"), base_cache)
        self.assertEqual(yt_kwargs.get("cache_path"), Path(self.temp_dir.name) / "youtube_tracks_cache.json")
        self.assertNotEqual(sp_kwargs.get("cache_path"), yt_kwargs.get("cache_path"))


if __name__ == "__main__":
    unittest.main()
