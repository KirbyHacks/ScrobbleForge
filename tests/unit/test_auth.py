import os
import sys
import tempfile
import threading
from pathlib import Path
import unittest
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.unit._dependency_guard import ensure_dependencies_mocked

ensure_dependencies_mocked()

import pylast
from src.config import load_config
from src.lastfm_client import (
    poll_web_auth,
    LastFMAuthCancelledError,
    LastFMAuthTimeoutError,
)


class TestAuthResolution(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_session_key_loaded_from_data_dir(self):
        old_env_key = os.environ.pop("LASTFM_SESSION_KEY", None)
        old_data_dir = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = str(self.data_dir)
        try:
            session_file = self.data_dir / "session.key"
            session_file.write_text("persisted_test_session_key_12345\n", encoding="utf-8")
            empty_env = self.data_dir / ".env"
            empty_env.write_text("LASTFM_API_KEY=test\nLASTFM_API_SECRET=test\n", encoding="utf-8")

            cfg = load_config(env_path=str(empty_env))
            self.assertEqual(cfg.lastfm.session_key, "persisted_test_session_key_12345")
        finally:
            if old_env_key is not None:
                os.environ["LASTFM_SESSION_KEY"] = old_env_key
            if old_data_dir is not None:
                os.environ["DATA_DIR"] = old_data_dir
            else:
                os.environ.pop("DATA_DIR", None)

    def test_poll_web_auth_handles_error_14_and_succeeds(self):
        mock_skg = MagicMock()
        auth_url = "https://www.last.fm/api/auth/?api_key=test&token=abc"
        stop_event = threading.Event()

        # Simulate Error 14 on first call, success on second call
        err14 = pylast.WSError(network=None, status="14", details="This token has not been authorized")
        mock_skg.get_web_auth_session_key_username.side_effect = [
            err14,
            ("retrieved_session_key", "test_user"),
        ]

        session_key, username = poll_web_auth(
            skg=mock_skg,
            auth_url=auth_url,
            stop_event=stop_event,
            poll_interval=0.01,
            timeout_seconds=5.0,
        )

        self.assertEqual(session_key, "retrieved_session_key")
        self.assertEqual(username, "test_user")
        self.assertEqual(mock_skg.get_web_auth_session_key_username.call_count, 2)

    def test_poll_web_auth_cancels_on_stop_event(self):
        mock_skg = MagicMock()
        auth_url = "https://www.last.fm/api/auth/?api_key=test&token=abc"
        err14 = pylast.WSError(network=None, status="14", details="This token has not been authorized")

        stop_event = threading.Event()

        def trigger_stop(*args, **kwargs):
            stop_event.set()
            raise err14

        mock_skg.get_web_auth_session_key_username.side_effect = trigger_stop

        with self.assertRaises(LastFMAuthCancelledError):
            poll_web_auth(
                skg=mock_skg,
                auth_url=auth_url,
                stop_event=stop_event,
                poll_interval=0.01,
                timeout_seconds=5.0,
            )

    def test_poll_web_auth_times_out(self):
        mock_skg = MagicMock()
        auth_url = "https://www.last.fm/api/auth/?api_key=test&token=abc"
        err14 = pylast.WSError(network=None, status="14", details="This token has not been authorized")
        mock_skg.get_web_auth_session_key_username.side_effect = err14

        stop_event = threading.Event()
        with self.assertRaises(LastFMAuthTimeoutError):
            poll_web_auth(
                skg=mock_skg,
                auth_url=auth_url,
                stop_event=stop_event,
                poll_interval=0.01,
                timeout_seconds=0.05,
            )


if __name__ == "__main__":
    unittest.main()
