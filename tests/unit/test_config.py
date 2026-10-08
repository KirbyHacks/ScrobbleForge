import os
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import AppConfig, ConfigError, load_config


class TestConfigValidation(unittest.TestCase):
    def setUp(self):
        # Clean environment copy for isolated testing
        self.orig_env = os.environ.copy()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.orig_env)

    def test_default_config_parsing(self):
        """Verifies default assignments when environment variables are omitted."""
        # Clear engine & spotify specific env vars
        for k in [
            "SCROBBLE_MODE",
            "CUSTOM_INTERVAL_SECONDS",
            "SHUFFLE",
            "LOOP",
            "UPDATE_NOW_PLAYING",
            "MAX_DAILY_SCROBBLES",
            "INTER_TRACK_PAUSE_MIN",
            "INTER_TRACK_PAUSE_MAX",
            "SPOTIFY_REFRESH_INTERVAL_HOURS",
        ]:
            os.environ.pop(k, None)

        cfg = AppConfig.from_env()

        self.assertEqual(cfg.engine.mode, "realistic")
        self.assertEqual(cfg.engine.custom_interval_seconds, 60)
        self.assertTrue(cfg.engine.shuffle)
        self.assertTrue(cfg.engine.loop)
        self.assertTrue(cfg.engine.update_now_playing)
        self.assertEqual(cfg.engine.max_daily_scrobbles, 2750)
        self.assertEqual(cfg.engine.inter_track_pause_min, 1.0)
        self.assertEqual(cfg.engine.inter_track_pause_max, 4.0)
        self.assertEqual(cfg.spotify.refresh_interval_hours, 12.0)

    def test_valid_custom_config_parsing(self):
        """Verifies custom valid settings are parsed accurately."""
        os.environ["SCROBBLE_MODE"] = "max_limit"
        os.environ["CUSTOM_INTERVAL_SECONDS"] = "45"
        os.environ["SHUFFLE"] = "false"
        os.environ["LOOP"] = "0"
        os.environ["UPDATE_NOW_PLAYING"] = "no"
        os.environ["MAX_DAILY_SCROBBLES"] = "2500"
        os.environ["INTER_TRACK_PAUSE_MIN"] = "2.5"
        os.environ["INTER_TRACK_PAUSE_MAX"] = "5.0"
        os.environ["SPOTIFY_REFRESH_INTERVAL_HOURS"] = "6.5"

        cfg = load_config()

        self.assertEqual(cfg.engine.mode, "max_limit")
        self.assertEqual(cfg.engine.custom_interval_seconds, 45)
        self.assertFalse(cfg.engine.shuffle)
        self.assertFalse(cfg.engine.loop)
        self.assertFalse(cfg.engine.update_now_playing)
        self.assertEqual(cfg.engine.max_daily_scrobbles, 2500)
        self.assertEqual(cfg.engine.inter_track_pause_min, 2.5)
        self.assertEqual(cfg.engine.inter_track_pause_max, 5.0)
        self.assertEqual(cfg.spotify.refresh_interval_hours, 6.5)

    def test_invalid_scrobble_mode_raises_config_error(self):
        """Verifies invalid SCROBBLE_MODE fails fast with actionable message."""
        os.environ["SCROBBLE_MODE"] = "turbo_blast"
        with self.assertRaises(ConfigError) as ctx:
            AppConfig.from_env()
        self.assertIn("Invalid SCROBBLE_MODE='turbo_blast'", str(ctx.exception))
        self.assertIn("Expected one of: realistic, max_limit, custom_interval", str(ctx.exception))

    def test_non_numeric_integer_fields_raise_config_error(self):
        """Verifies non-numeric values in integer fields raise ConfigError."""
        # Non-numeric custom interval
        os.environ["CUSTOM_INTERVAL_SECONDS"] = "banana"
        with self.assertRaises(ConfigError) as ctx:
            AppConfig.from_env()
        self.assertIn("CUSTOM_INTERVAL_SECONDS", str(ctx.exception))
        os.environ.pop("CUSTOM_INTERVAL_SECONDS")

        # Zero custom interval (must be positive)
        os.environ["CUSTOM_INTERVAL_SECONDS"] = "0"
        with self.assertRaises(ConfigError) as ctx:
            AppConfig.from_env()
        self.assertIn("CUSTOM_INTERVAL_SECONDS", str(ctx.exception))
        os.environ.pop("CUSTOM_INTERVAL_SECONDS")

        # Negative custom interval
        os.environ["CUSTOM_INTERVAL_SECONDS"] = "-10"
        with self.assertRaises(ConfigError) as ctx:
            AppConfig.from_env()
        self.assertIn("CUSTOM_INTERVAL_SECONDS", str(ctx.exception))
        os.environ.pop("CUSTOM_INTERVAL_SECONDS")

        # Non-numeric max daily scrobbles
        os.environ["MAX_DAILY_SCROBBLES"] = "unlimited"
        with self.assertRaises(ConfigError) as ctx:
            AppConfig.from_env()
        self.assertIn("MAX_DAILY_SCROBBLES", str(ctx.exception))
        os.environ.pop("MAX_DAILY_SCROBBLES")

        # Exceeding max daily limit (2800 cap)
        os.environ["MAX_DAILY_SCROBBLES"] = "3500"
        with self.assertRaises(ConfigError) as ctx:
            AppConfig.from_env()
        self.assertIn("MAX_DAILY_SCROBBLES", str(ctx.exception))
        os.environ.pop("MAX_DAILY_SCROBBLES")

        # Zero max daily limit
        os.environ["MAX_DAILY_SCROBBLES"] = "0"
        with self.assertRaises(ConfigError) as ctx:
            AppConfig.from_env()
        self.assertIn("MAX_DAILY_SCROBBLES", str(ctx.exception))

    def test_non_numeric_float_fields_raise_config_error(self):
        """Verifies non-numeric values in float fields raise ConfigError."""
        # Non-numeric refresh interval
        os.environ["SPOTIFY_REFRESH_INTERVAL_HOURS"] = "never"
        with self.assertRaises(ConfigError) as ctx:
            AppConfig.from_env()
        self.assertIn("SPOTIFY_REFRESH_INTERVAL_HOURS", str(ctx.exception))
        os.environ.pop("SPOTIFY_REFRESH_INTERVAL_HOURS")

        # Non-positive refresh interval
        os.environ["SPOTIFY_REFRESH_INTERVAL_HOURS"] = "0"
        with self.assertRaises(ConfigError) as ctx:
            AppConfig.from_env()
        self.assertIn("SPOTIFY_REFRESH_INTERVAL_HOURS", str(ctx.exception))
        os.environ.pop("SPOTIFY_REFRESH_INTERVAL_HOURS")

        # Negative pause minimum
        os.environ["INTER_TRACK_PAUSE_MIN"] = "-2.0"
        with self.assertRaises(ConfigError) as ctx:
            AppConfig.from_env()
        self.assertIn("INTER_TRACK_PAUSE_MIN", str(ctx.exception))
        os.environ.pop("INTER_TRACK_PAUSE_MIN")

        # Non-numeric pause maximum
        os.environ["INTER_TRACK_PAUSE_MAX"] = "fast"
        with self.assertRaises(ConfigError) as ctx:
            AppConfig.from_env()
        self.assertIn("INTER_TRACK_PAUSE_MAX", str(ctx.exception))

    def test_pause_min_greater_than_max_raises_config_error(self):
        """Verifies INTER_TRACK_PAUSE_MIN > INTER_TRACK_PAUSE_MAX raises ConfigError."""
        os.environ["INTER_TRACK_PAUSE_MIN"] = "10.0"
        os.environ["INTER_TRACK_PAUSE_MAX"] = "2.0"
        with self.assertRaises(ConfigError) as ctx:
            AppConfig.from_env()
        self.assertIn("cannot be greater than INTER_TRACK_PAUSE_MAX", str(ctx.exception))

    def test_boolean_nonsense_raises_config_error(self):
        """Verifies nonsense boolean string values fail fast."""
        for key in ["SHUFFLE", "LOOP", "UPDATE_NOW_PLAYING"]:
            os.environ[key] = "maybe"
            with self.assertRaises(ConfigError) as ctx:
                AppConfig.from_env()
            self.assertIn(f"Invalid boolean value for {key}", str(ctx.exception))
            os.environ.pop(key)

    def test_placeholder_credentials_resolve_to_none(self):
        """Verifies that template placeholders ('your_*', 'placeholder', empty strings) resolve to None."""
        os.environ["LASTFM_API_KEY"] = "your_lastfm_api_key"
        os.environ["LASTFM_API_SECRET"] = "your_lastfm_api_secret"
        os.environ["LASTFM_USERNAME"] = "your_lastfm_username"
        os.environ["LASTFM_SESSION_KEY"] = "your_session_key"
        os.environ["LASTFM_PASSWORD"] = "   "
        os.environ["SPOTIFY_CLIENT_ID"] = "placeholder_client_id"
        os.environ["SPOTIFY_CLIENT_SECRET"] = "YOUR_CLIENT_SECRET"

        cfg = AppConfig.from_env()

        self.assertIsNone(cfg.lastfm.api_key)
        self.assertIsNone(cfg.lastfm.api_secret)
        self.assertIsNone(cfg.lastfm.username)
        self.assertIsNone(cfg.lastfm.session_key)
        self.assertIsNone(cfg.lastfm.password)
        self.assertIsNone(cfg.spotify.client_id)
        self.assertIsNone(cfg.spotify.client_secret)


if __name__ == "__main__":
    unittest.main()
