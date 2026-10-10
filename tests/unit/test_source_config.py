"""tests for generic source configuration, backward compatibility, and precedence."""
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from src.config import AppConfig, ConfigError, EngineConfig, LastFMConfig, SourceConfig, SpotifyConfig, SystemConfig, load_config
from src.engine import ScrobblerEngine
from src.models import Track
from src.queue_manager import QueueManager
from src.quota_tracker import QuotaTracker
from src.sources import SourceFactory, SourceIngestionService, UnsupportedSourceError
from src.sources.base import SourceClient


class TestSourceConfig(unittest.TestCase):
    def setUp(self):
        self.orig_env = {k: v for k, v in os.environ.items() if len(v) < 32000}
        import dotenv
        self.dotenv_patcher = patch(
            "src.config.load_dotenv",
            side_effect=lambda dotenv_path=None: dotenv.load_dotenv(dotenv_path=dotenv_path) if (dotenv_path and "tmp" in str(dotenv_path).lower()) else None,
        )
        self.dotenv_patcher.start()
        # clear source-related env vars for clean test isolation
        for key in [
            "MUSIC_SOURCES",
            "SPOTIFY_TRACK_URL",
            "SPOTIFY_PLAYLIST_URL",
            "SPOTIFY_URL",
            "SOURCES_REFRESH_INTERVAL_HOURS",
            "SPOTIFY_REFRESH_INTERVAL_HOURS",
            "SPOTIFY_CLIENT_ID",
            "SPOTIFY_CLIENT_SECRET",
        ]:
            os.environ.pop(key, None)

    def tearDown(self):
        self.dotenv_patcher.stop()
        os.environ.clear()
        os.environ.update(self.orig_env)

    def test_generic_music_sources_parsing(self):
        """verify MUSIC_SOURCES parses comma-separated sources, trims whitespace, and does not contaminate spotify.sources."""
        os.environ["MUSIC_SOURCES"] = (
            "  https://open.spotify.com/playlist/alpha , https://open.spotify.com/album/beta  "
        )
        cfg = AppConfig.from_env()

        expected = [
            "https://open.spotify.com/playlist/alpha",
            "https://open.spotify.com/album/beta",
        ]
        self.assertEqual(cfg.sources.sources, expected)
        # generic sources must not be copied into spotify.sources
        self.assertEqual(cfg.spotify.sources, [])
        self.assertEqual(cfg.sources.refresh_interval_hours, 12.0)
        self.assertEqual(cfg.spotify.refresh_interval_hours, 12.0)

    def test_legacy_spotify_track_url_fallback(self):
        """verify SPOTIFY_TRACK_URL is respected when MUSIC_SOURCES is absent."""
        os.environ["SPOTIFY_TRACK_URL"] = "https://open.spotify.com/track/123"
        cfg = AppConfig.from_env()

        self.assertEqual(cfg.sources.sources, ["https://open.spotify.com/track/123"])
        self.assertEqual(cfg.spotify.sources, ["https://open.spotify.com/track/123"])

    def test_legacy_spotify_playlist_url_fallback(self):
        """verify SPOTIFY_PLAYLIST_URL is respected when MUSIC_SOURCES is absent."""
        os.environ["SPOTIFY_PLAYLIST_URL"] = "https://open.spotify.com/playlist/xyz"
        cfg = AppConfig.from_env()

        self.assertEqual(cfg.sources.sources, ["https://open.spotify.com/playlist/xyz"])
        self.assertEqual(cfg.spotify.sources, ["https://open.spotify.com/playlist/xyz"])

    def test_legacy_spotify_url_fallback(self):
        """verify SPOTIFY_URL is respected when MUSIC_SOURCES is absent."""
        os.environ["SPOTIFY_URL"] = "https://open.spotify.com/album/abc"
        cfg = AppConfig.from_env()

        self.assertEqual(cfg.sources.sources, ["https://open.spotify.com/album/abc"])
        self.assertEqual(cfg.spotify.sources, ["https://open.spotify.com/album/abc"])

    def test_legacy_variable_precedence(self):
        """verify legacy precedence: SPOTIFY_TRACK_URL > SPOTIFY_PLAYLIST_URL > SPOTIFY_URL."""
        os.environ["SPOTIFY_TRACK_URL"] = "https://open.spotify.com/track/pri1"
        os.environ["SPOTIFY_PLAYLIST_URL"] = "https://open.spotify.com/playlist/pri2"
        os.environ["SPOTIFY_URL"] = "https://open.spotify.com/album/pri3"

        cfg = AppConfig.from_env()
        self.assertEqual(cfg.sources.sources, ["https://open.spotify.com/track/pri1"])

        os.environ.pop("SPOTIFY_TRACK_URL")
        cfg2 = AppConfig.from_env()
        self.assertEqual(cfg2.sources.sources, ["https://open.spotify.com/playlist/pri2"])

        os.environ.pop("SPOTIFY_PLAYLIST_URL")
        cfg3 = AppConfig.from_env()
        self.assertEqual(cfg3.sources.sources, ["https://open.spotify.com/album/pri3"])

    def test_coexistence_generic_precedence_over_legacy(self):
        """verify MUSIC_SOURCES overrides legacy variables without silent merging or contamination."""
        os.environ["MUSIC_SOURCES"] = "https://open.spotify.com/playlist/generic"
        os.environ["SPOTIFY_PLAYLIST_URL"] = "https://open.spotify.com/playlist/legacy"
        os.environ["SPOTIFY_TRACK_URL"] = "https://open.spotify.com/track/legacy_track"

        cfg = AppConfig.from_env()
        self.assertEqual(cfg.sources.sources, ["https://open.spotify.com/playlist/generic"])
        self.assertEqual(len(cfg.sources.sources), 1)
        # legacy spotify variables are ignored and not merged
        self.assertEqual(cfg.spotify.sources, [])

    def test_empty_generic_falls_back_to_legacy(self):
        """verify empty or whitespace MUSIC_SOURCES falls back to legacy variables."""
        os.environ["MUSIC_SOURCES"] = "   "
        os.environ["SPOTIFY_PLAYLIST_URL"] = "https://open.spotify.com/playlist/legacy_fallback"

        cfg = AppConfig.from_env()
        self.assertEqual(cfg.sources.sources, ["https://open.spotify.com/playlist/legacy_fallback"])
        self.assertEqual(cfg.spotify.sources, ["https://open.spotify.com/playlist/legacy_fallback"])

    def test_both_generic_and_legacy_empty(self):
        """verify empty sources list when all source variables are unset or blank."""
        os.environ["MUSIC_SOURCES"] = ""
        os.environ["SPOTIFY_PLAYLIST_URL"] = ""

        cfg = AppConfig.from_env()
        self.assertEqual(cfg.sources.sources, [])
        self.assertEqual(cfg.spotify.sources, [])

    def test_repeated_source_references_preserved_in_order(self):
        """verify repeated source references in MUSIC_SOURCES are preserved for replay weighting."""
        os.environ["MUSIC_SOURCES"] = "url_a, url_b, url_a, url_c, url_b, url_d"
        cfg = AppConfig.from_env()

        self.assertEqual(cfg.sources.sources, ["url_a", "url_b", "url_a", "url_c", "url_b", "url_d"])

    def test_legacy_repeated_source_references_preserved_in_order(self):
        """verify historical behavior: repeated legacy source references are preserved."""
        os.environ["SPOTIFY_TRACK_URL"] = "track_1, track_2, track_1"
        cfg = AppConfig.from_env()

        self.assertEqual(cfg.sources.sources, ["track_1", "track_2", "track_1"])
        self.assertEqual(cfg.spotify.sources, ["track_1", "track_2", "track_1"])

    def test_generic_refresh_interval(self):
        """verify SOURCES_REFRESH_INTERVAL_HOURS is parsed and reflected across configs."""
        os.environ["SOURCES_REFRESH_INTERVAL_HOURS"] = "8.5"
        cfg = AppConfig.from_env()

        self.assertEqual(cfg.sources.refresh_interval_hours, 8.5)
        self.assertEqual(cfg.spotify.refresh_interval_hours, 8.5)

    def test_generic_refresh_precedence_over_legacy_refresh(self):
        """verify SOURCES_REFRESH_INTERVAL_HOURS overrides SPOTIFY_REFRESH_INTERVAL_HOURS for sources."""
        os.environ["SOURCES_REFRESH_INTERVAL_HOURS"] = "4.0"
        os.environ["SPOTIFY_REFRESH_INTERVAL_HOURS"] = "18.0"

        cfg = AppConfig.from_env()
        self.assertEqual(cfg.sources.refresh_interval_hours, 4.0)
        self.assertEqual(cfg.spotify.refresh_interval_hours, 18.0)

    def test_legacy_refresh_fallback(self):
        """verify SPOTIFY_REFRESH_INTERVAL_HOURS applies when SOURCES_REFRESH_INTERVAL_HOURS is omitted."""
        os.environ["SPOTIFY_REFRESH_INTERVAL_HOURS"] = "6.5"
        cfg = AppConfig.from_env()

        self.assertEqual(cfg.sources.refresh_interval_hours, 6.5)
        self.assertEqual(cfg.spotify.refresh_interval_hours, 6.5)

    def test_invalid_sources_refresh_interval_raises_config_error(self):
        """verify non-numeric or non-positive SOURCES_REFRESH_INTERVAL_HOURS raises ConfigError."""
        for invalid_val in ["abc", "0", "-1.5"]:
            os.environ["SOURCES_REFRESH_INTERVAL_HOURS"] = invalid_val
            with self.assertRaises(ConfigError) as ctx:
                AppConfig.from_env()
            self.assertIn("SOURCES_REFRESH_INTERVAL_HOURS", str(ctx.exception))

    def test_invalid_spotify_refresh_interval_raises_config_error(self):
        """verify non-numeric or non-positive SPOTIFY_REFRESH_INTERVAL_HOURS raises ConfigError."""
        for invalid_val in ["not_a_number", "0", "-10"]:
            os.environ["SPOTIFY_REFRESH_INTERVAL_HOURS"] = invalid_val
            with self.assertRaises(ConfigError) as ctx:
                AppConfig.from_env()
            self.assertIn("SPOTIFY_REFRESH_INTERVAL_HOURS", str(ctx.exception))

    def test_mixed_provider_references_at_configuration_level(self):
        """verify configuration parsing accepts provider-independent URLs without contaminating spotify.sources."""
        os.environ["MUSIC_SOURCES"] = (
            "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M, "
            "https://music.youtube.com/playlist?list=PL12345, "
            "https://custom-radio.org/stream"
        )
        cfg = AppConfig.from_env()

        self.assertEqual(
            cfg.sources.sources,
            [
                "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M",
                "https://music.youtube.com/playlist?list=PL12345",
                "https://custom-radio.org/stream",
            ],
        )
        # verify no cross-provider contamination into spotify.sources
        self.assertEqual(cfg.spotify.sources, [])

    def test_no_network_requests_during_config_loading(self):
        """verify configuration parsing is pure and does not issue any network requests."""
        os.environ["MUSIC_SOURCES"] = "https://open.spotify.com/playlist/test, https://example.com/audio"

        with patch("requests.get", side_effect=RuntimeError("Unexpected network call in requests.get")), \
             patch("requests.request", side_effect=RuntimeError("Unexpected network call in requests.request")), \
             patch("socket.socket", side_effect=RuntimeError("Unexpected network call in socket")):
            cfg = load_config()

        self.assertEqual(len(cfg.sources.sources), 2)

    def test_unsupported_provider_rejected_at_ingestion_boundary(self):
        """verify unsupported providers configured in generic sources fail at ingestion boundary."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_file = Path(tmp_dir) / "test_cache.json"
            service = SourceIngestionService(cache_path=cache_file)
            sources = ["https://unsupported-provider.example.com/playlist/123"]

            with self.assertRaises(UnsupportedSourceError) as ctx:
                service.fetch_sources(sources)

            self.assertIn("unsupported-provider.example.com", str(ctx.exception))

    def test_legacy_dot_env_file_compatibility(self):
        """verify old .env files containing only legacy variables load identical effective config."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_file = Path(tmp_dir) / ".env"
            env_file.write_text(
                "LASTFM_API_KEY=legacy_lfm_key\n"
                "LASTFM_API_SECRET=legacy_lfm_sec\n"
                "SPOTIFY_PLAYLIST_URL=https://open.spotify.com/playlist/legacy_old\n"
                "SPOTIFY_REFRESH_INTERVAL_HOURS=7.5\n"
                "SPOTIFY_CLIENT_ID=client_abc\n"
                "SPOTIFY_CLIENT_SECRET=secret_xyz\n",
                encoding="utf-8",
            )

            cfg = AppConfig.from_env(env_path=str(env_file))

            self.assertEqual(cfg.sources.sources, ["https://open.spotify.com/playlist/legacy_old"])
            self.assertEqual(cfg.spotify.sources, ["https://open.spotify.com/playlist/legacy_old"])
            self.assertEqual(cfg.sources.refresh_interval_hours, 7.5)
            self.assertEqual(cfg.spotify.refresh_interval_hours, 7.5)
            self.assertEqual(cfg.spotify.client_id, "client_abc")
            self.assertEqual(cfg.spotify.client_secret, "secret_xyz")

    def test_app_config_direct_instantiation_synchronization(self):
        """verify direct constructor calls to AppConfig maintain one-way compatibility without contamination."""
        # 1. caller supplies spotify config, sources config omitted -> one-way sync for legacy callers
        cfg1 = AppConfig(
            lastfm=LastFMConfig(api_key="k", api_secret="s"),
            spotify=SpotifyConfig(sources=["https://open.spotify.com/track/1"], refresh_interval_hours=5.0),
        )
        self.assertEqual(cfg1.sources.sources, ["https://open.spotify.com/track/1"])
        self.assertEqual(cfg1.sources.refresh_interval_hours, 5.0)

        # 2. caller supplies generic sources, spotify config empty -> do NOT reverse-contaminate spotify.sources
        cfg2 = AppConfig(
            lastfm=LastFMConfig(api_key="k", api_secret="s"),
            spotify=SpotifyConfig(),
            sources=SourceConfig(sources=["https://music.youtube.com/track/2"], refresh_interval_hours=9.0),
        )
        self.assertEqual(cfg2.sources.sources, ["https://music.youtube.com/track/2"])
        self.assertEqual(cfg2.spotify.sources, [])

    def test_scrobbler_engine_refresh_with_generic_sources(self):
        """verify ScrobblerEngine uses generic SourceConfig for periodic refreshes."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "test.db"
            tracker = QuotaTracker(db_path=db_path)
            initial_track = Track(title="Song 1", artist="Artist 1", duration_ms=180000)
            qm = QueueManager(tracks=[initial_track], tracker=tracker, shuffle=False, loop=False)

            cfg = AppConfig(
                lastfm=LastFMConfig(api_key="k", api_secret="s", username="u"),
                spotify=SpotifyConfig(),
                sources=SourceConfig(
                    sources=["https://open.spotify.com/playlist/generic_refresh"],
                    refresh_interval_hours=2.0,
                ),
                engine=EngineConfig(mode="realistic"),
                system=SystemConfig(data_dir=Path(tmp_dir)),
            )

            mock_service = MagicMock(spec=SourceIngestionService)
            mock_service.fetch_sources.return_value = [
                Track(title="Song Refreshed", artist="Artist Refreshed", duration_ms=180000)
            ]

            engine = ScrobblerEngine(
                config=cfg,
                lastfm=MagicMock(),
                spotify=mock_service,
                queue=qm,
                tracker=tracker,
                stop_event=threading.Event(),
            )

            now = time.time()
            engine.last_refresh_time = now - (3 * 3600)  # exceeded 2.0h interval

            engine._check_periodic_refresh()

            mock_service.fetch_sources.assert_called_once_with(
                ["https://open.spotify.com/playlist/generic_refresh"],
                use_cache_if_available=False,
            )
            self.assertEqual(len(qm.queue), 1)
            self.assertEqual(qm.queue[0].title, "Song Refreshed")

    def test_create_client_rejects_unexpected_arguments(self):
        """verify SourceFactory.create_client does not silently swallow unexpected keyword arguments."""
        with self.assertRaises(TypeError):
            SourceFactory.create_client("spotify", unknown_bogus_arg="value")

    def test_create_client_rejects_missing_required_arguments(self):
        """verify SourceFactory.create_client enforces required constructor parameters."""
        class RequiredArgProvider(SourceClient):
            provider_id = "req_arg"

            def __init__(self, required_key: str):
                self.required_key = required_key

            @classmethod
            def supports_url(cls, url: str) -> bool:
                return "req-arg" in url

            def fetch_sources(self, sources, use_cache_if_available=True):
                return []

            def check_health(self) -> bool:
                return True

        SourceFactory.register_provider("req_arg", RequiredArgProvider)
        try:
            # missing required argument should raise TypeError
            with self.assertRaises(TypeError):
                SourceFactory.create_client("req_arg")

            # valid argument instantiates properly
            client = SourceFactory.create_client("req_arg", required_key="valid_secret")
            self.assertEqual(client.required_key, "valid_secret")
        finally:
            SourceFactory.unregister_provider("req_arg")

    def test_fetch_sources_with_report_routes_provider_scoped_kwargs_without_leaking(self):
        """verify fetch_sources_with_report passes provider-specific kwargs and does not leak Spotify client."""
        class MockOtherProvider(SourceClient):
            provider_id = "other_provider"

            def __init__(self, other_token: str = "default_token"):
                self.other_token = other_token

            @classmethod
            def supports_url(cls, url: str) -> bool:
                return "other-service.com" in url

            def fetch_sources(self, sources, use_cache_if_available=True):
                return []

            def check_health(self) -> bool:
                return True

        SourceFactory.register_provider("other_provider", MockOtherProvider)
        try:
            # call with mixed sources and provider_kwargs
            report = SourceFactory.fetch_sources_with_report(
                sources=[
                    "https://other-service.com/playlist/123",
                ],
                use_cache_if_available=False,
                provider_kwargs={
                    "spotify": {"client": MagicMock()},
                    "other_provider": {"other_token": "custom_token"},
                },
                client="dummy_spotify_client",  # top-level spotify client argument
            )
            # MockOtherProvider must NOT raise TypeError about unexpected 'client' kwarg
            self.assertFalse(report.has_failures)
        finally:
            SourceFactory.unregister_provider("other_provider")

    def test_repeated_sources_queue_composition(self):
        """verify repeated source references preserve queue track count and order without collapsing."""
        tracks = [
            Track(title="Song A", artist="Artist", duration_ms=180000),
            Track(title="Song B", artist="Artist", duration_ms=180000),
            Track(title="Song A", artist="Artist", duration_ms=180000),
        ]
        qm = QueueManager(tracks=tracks, shuffle=False, loop=False)

        self.assertEqual(qm.total_tracks, 3)
        self.assertEqual(len(qm.queue), 3)
        self.assertEqual(qm.queue[0].title, "Song A")
        self.assertEqual(qm.queue[1].title, "Song B")
        self.assertEqual(qm.queue[2].title, "Song A")


if __name__ == "__main__":
    unittest.main()
