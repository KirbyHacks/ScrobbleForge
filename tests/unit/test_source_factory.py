"""unit tests for SourceFactory, registry dispatch, URL detection, and source orchestration."""
from pathlib import Path
import sys
from typing import List
import unittest
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.unit._dependency_guard import ensure_dependencies_mocked

ensure_dependencies_mocked()

from src.core.models import CanonicalTrack
from src.sources.base import SourceClient
from src.sources.exceptions import (
    SourceAuthError,
    SourceError,
    SourceTemporaryError,
    UnsupportedSourceError,
)
from src.sources.source_factory import FetchReport, SourceFactory
from src.sources.spotify_adapter import SpotifyAdapter


class DummySourceClient(SourceClient):
    provider_id = "dummy"

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.fetch_calls: List[List[str]] = []

    @classmethod
    def supports_url(cls, url: str) -> bool:
        return url.startswith("dummy:")

    def fetch_sources(self, urls: List[str], use_cache_if_available: bool = True) -> List[CanonicalTrack]:
        self.fetch_calls.append(urls)
        return [
            CanonicalTrack(title=f"Track from {u}", artists=["Dummy Artist"], provider_id="dummy")
            for u in urls
        ]

    def check_health(self) -> bool:
        return True


class TestSourceFactory(unittest.TestCase):
    def setUp(self):
        # reset registry to isolated default state before each test
        SourceFactory.reset_registry()

    def tearDown(self):
        # ensure no test leaves behind altered registry state
        SourceFactory.reset_registry()

    # --- URL and URI Detection Tests ---

    def test_spotify_https_url_detection(self):
        urls = [
            "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M",
            "http://open.spotify.com/album/4m2880jivSbbyEGAKfITCa",
            "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT",
            "https://open.spotify.com/playlist/xyz?si=12345",
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(SourceFactory.detect_source_type(url), "spotify")

    def test_spotify_uri_detection(self):
        uris = [
            "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M",
            "spotify:album:4m2880jivSbbyEGAKfITCa",
            "spotify:track:4cOdK2wGLETKBW3PvgPWqT",
            "SPOTIFY:TRACK:4cOdK2wGLETKBW3PvgPWqT",
            "spotify:artist:06HL4z0CvFAxyc27GXpf02",
        ]
        for uri in uris:
            with self.subTest(uri=uri):
                self.assertEqual(SourceFactory.detect_source_type(uri), "spotify")

    def test_hostname_spoofing_defense(self):
        spoofed_urls = [
            "https://open.spotify.com.evil.example/playlist/37i9dQZF1DXcBWIGoYBM5M",
            "https://evil.open.spotify.com/track/123",
            "https://open.spotify.com.attacker.com/album/456",
            "http://open.spotify.com@evil.com/playlist/123",
            "https://notspotify.com/open.spotify.com",
        ]
        for url in spoofed_urls:
            with self.subTest(url=url):
                self.assertEqual(SourceFactory.detect_source_type(url), "unknown")

    def test_invalid_and_unsupported_urls(self):
        invalid_sources = [
            "https://youtube.com/watch?v=dQw4w9WgXcQ",
            "https://music.apple.com/us/album/random-access-memories/617154241",
            "spotify:invalid",
            "random_string_here",
            "",
            "   ",
            None,
        ]
        for source in invalid_sources:
            with self.subTest(source=source):
                self.assertEqual(SourceFactory.detect_source_type(source), "unknown")  # type: ignore

    # --- Registry & Client Creation Tests ---

    def test_registry_dispatch_default_spotify(self):
        client = SourceFactory.create_client("spotify")
        self.assertIsNotNone(client)
        self.assertIsInstance(client, SpotifyAdapter)
        self.assertEqual(client.provider_id, "spotify")

    def test_create_client_unknown_provider_returns_none(self):
        client = SourceFactory.create_client("unknown_provider")
        self.assertIsNone(client)

    def test_explicit_provider_registration_and_reset(self):
        SourceFactory.register_provider("dummy", DummySourceClient)
        self.assertIn("dummy", SourceFactory.get_registered_providers())

        client = SourceFactory.create_client("dummy", foo="bar")
        self.assertIsInstance(client, DummySourceClient)
        self.assertEqual(client.kwargs, {"foo": "bar"})

        # resetting removes dummy provider and restores default isolation
        SourceFactory.reset_registry()
        self.assertNotIn("dummy", SourceFactory.get_registered_providers())
        self.assertIsNone(SourceFactory.create_client("dummy"))

    def test_register_invalid_provider_raises(self):
        with self.assertRaises(ValueError):
            SourceFactory.register_provider("", DummySourceClient)

        with self.assertRaises(TypeError):
            SourceFactory.register_provider("bad", object)  # type: ignore

    # --- Ingestion & Orchestration Tests ---

    @patch.object(SpotifyAdapter, "fetch_sources")
    def test_fetch_all_sources_single_provider_client_reuse(self, mock_fetch):
        mock_fetch.return_value = [
            CanonicalTrack(title="Track 1", artists=["Artist 1"], provider_id="spotify"),
            CanonicalTrack(title="Track 2", artists=["Artist 2"], provider_id="spotify"),
        ]

        sources = [
            "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M",
            "https://open.spotify.com/album/4m2880jivSbbyEGAKfITCa",
        ]

        tracks = SourceFactory.fetch_all_sources(sources, use_cache_if_available=True)

        self.assertEqual(len(tracks), 2)
        # passing multiple urls to a single provider calls fetch_sources once with all urls
        mock_fetch.assert_called_once_with(sources, use_cache_if_available=True)

    @patch.object(SpotifyAdapter, "fetch_sources")
    def test_preserves_track_and_source_ordering(self, mock_fetch):
        track_a = CanonicalTrack(title="A", artists=["Artist A"], provider_id="spotify")
        track_b = CanonicalTrack(title="B", artists=["Artist B"], provider_id="spotify")
        track_c = CanonicalTrack(title="C", artists=["Artist C"], provider_id="spotify")

        mock_fetch.return_value = [track_a, track_b, track_c]

        sources = ["https://open.spotify.com/playlist/source_1"]
        tracks = SourceFactory.fetch_all_sources(sources)

        self.assertEqual([t.title for t in tracks], ["A", "B", "C"])

    @patch.object(SpotifyAdapter, "fetch_sources")
    def test_cache_flag_forwarding_in_fetch_all_sources(self, mock_fetch):
        mock_fetch.return_value = []
        sources = ["https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"]

        SourceFactory.fetch_all_sources(sources, use_cache_if_available=False)
        mock_fetch.assert_called_with(sources, use_cache_if_available=False)

    @patch.object(SpotifyAdapter, "fetch_sources")
    def test_empty_successful_result(self, mock_fetch):
        mock_fetch.return_value = []
        sources = ["https://open.spotify.com/playlist/empty_playlist"]

        tracks = SourceFactory.fetch_all_sources(sources)
        self.assertEqual(tracks, [])

    def test_unsupported_source_raises_unsupported_error(self):
        sources = [
            "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M",
            "https://unsupported.service.org/playlist/bad",
        ]
        with self.assertRaises(UnsupportedSourceError) as ctx:
            SourceFactory.fetch_all_sources(sources)
        self.assertIn("unsupported.service.org", str(ctx.exception))

    def test_mixed_supported_and_unsupported_sources_with_report(self):
        sources = [
            "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M",
            "https://unsupported.service.org/bad",
        ]
        with patch.object(SpotifyAdapter, "fetch_sources") as mock_fetch:
            mock_fetch.return_value = [
                CanonicalTrack(title="Good Track", artists=["Artist"], provider_id="spotify")
            ]
            report: FetchReport = SourceFactory.fetch_sources_with_report(sources)

            self.assertTrue(report.has_failures)
            self.assertEqual(len(report.failed_sources), 1)
            failed_url, exc = report.failed_sources[0]
            self.assertEqual(failed_url, "https://unsupported.service.org/bad")
            self.assertIsInstance(exc, UnsupportedSourceError)
            self.assertEqual(len(report.tracks), 1)
            self.assertEqual(report.tracks[0].title, "Good Track")

    @patch.object(SpotifyAdapter, "fetch_sources")
    def test_auth_failure_propagates_without_swallowing(self, mock_fetch):
        mock_fetch.side_effect = SourceAuthError("Invalid credentials")
        sources = ["https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"]

        with self.assertRaises(SourceAuthError):
            SourceFactory.fetch_all_sources(sources)

    @patch.object(SpotifyAdapter, "fetch_sources")
    def test_temporary_failure_propagates_without_swallowing(self, mock_fetch):
        mock_fetch.side_effect = SourceTemporaryError("Network timeout")
        sources = ["https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"]

        with self.assertRaises(SourceTemporaryError):
            SourceFactory.fetch_all_sources(sources)

    @patch.object(SpotifyAdapter, "fetch_sources")
    def test_unexpected_exception_propagates(self, mock_fetch):
        mock_fetch.side_effect = RuntimeError("Fatal hardware or logic fault")
        sources = ["https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"]

        with self.assertRaises(RuntimeError):
            SourceFactory.fetch_all_sources(sources)

    def test_empty_sources_list_returns_empty_list(self):
        self.assertEqual(SourceFactory.fetch_all_sources([]), [])

    def test_no_network_on_module_imports_and_detection(self):
        """verify that module imports and supports_url / detect_source_type make zero network calls."""
        with patch("requests.get") as mock_get, patch("requests.head") as mock_head, patch("requests.post") as mock_post:
            # verify detection on various urls
            self.assertEqual(SourceFactory.detect_source_type("https://open.spotify.com/track/123"), "spotify")
            self.assertEqual(SourceFactory.detect_source_type("spotify:playlist:123"), "spotify")
            self.assertEqual(SourceFactory.detect_source_type("https://example.com"), "unknown")

            # verify supports_url on SpotifyAdapter
            self.assertTrue(SpotifyAdapter.supports_url("https://open.spotify.com/album/123"))
            self.assertFalse(SpotifyAdapter.supports_url("https://evil.com"))

            mock_get.assert_not_called()
            mock_head.assert_not_called()
            mock_post.assert_not_called()

    def test_interleaved_mixed_providers_preserves_order_and_reuses_clients(self):
        """verify that [spotify:A, dummy:B, spotify:C] preserves source order A, B, C and reuses clients."""
        SourceFactory.register_provider("dummy", DummySourceClient)

        spotify_mock_client = MagicMock(spec=SpotifyAdapter)

        def mock_spotify_fetch(urls, use_cache_if_available=True):
            return [
                CanonicalTrack(title=f"Track from {u}", artists=["Spotify Artist"], provider_id="spotify")
                for u in urls
            ]

        spotify_mock_client.fetch_sources.side_effect = mock_spotify_fetch

        original_create_client = SourceFactory.create_client
        created_providers = []

        def tracked_create_client(source_type, **kwargs):
            created_providers.append(source_type)
            if source_type == "spotify":
                return spotify_mock_client
            return original_create_client(source_type, **kwargs)

        sources = [
            "spotify:playlist:A",
            "dummy:playlist:B",
            "spotify:playlist:C",
        ]

        with patch.object(SourceFactory, "create_client", side_effect=tracked_create_client):
            tracks = SourceFactory.fetch_all_sources(sources)

        # source ordering A, B, C must be strictly preserved
        self.assertEqual(len(tracks), 3)
        self.assertEqual(
            [t.title for t in tracks],
            ["Track from spotify:playlist:A", "Track from dummy:playlist:B", "Track from spotify:playlist:C"],
        )

        # client reuse: each provider instantiated at most once
        self.assertEqual(created_providers.count("spotify"), 1)
        self.assertEqual(created_providers.count("dummy"), 1)

    def test_interleaved_mixed_providers_source_with_zero_tracks(self):
        """verify that an interleaved source yielding zero tracks preserves remaining source order without error."""
        SourceFactory.register_provider("dummy", DummySourceClient)

        spotify_mock = MagicMock(spec=SpotifyAdapter)
        spotify_mock.fetch_sources.side_effect = lambda urls, **kw: [
            CanonicalTrack(title=f"Track from {u}", artists=["Spotify Artist"], provider_id="spotify")
            for u in urls
        ]

        dummy_mock = MagicMock(spec=DummySourceClient)
        dummy_mock.fetch_sources.return_value = []  # middle source returns zero tracks

        def fake_create(source_type, **kw):
            return spotify_mock if source_type == "spotify" else dummy_mock

        sources = [
            "spotify:playlist:A",
            "dummy:playlist:B",
            "spotify:playlist:C",
        ]

        with patch.object(SourceFactory, "create_client", side_effect=fake_create):
            report = SourceFactory.fetch_sources_with_report(sources)

        self.assertFalse(report.has_failures)
        self.assertEqual(len(report.tracks), 2)
        self.assertEqual(
            [t.title for t in report.tracks],
            ["Track from spotify:playlist:A", "Track from spotify:playlist:C"],
        )

    def test_interleaved_mixed_providers_middle_source_fails(self):
        """verify that when the middle source fails, partial report tracks failures and fetch_all raises."""
        SourceFactory.register_provider("dummy", DummySourceClient)

        spotify_mock = MagicMock(spec=SpotifyAdapter)
        spotify_mock.fetch_sources.side_effect = lambda urls, **kw: [
            CanonicalTrack(title=f"Track from {u}", artists=["Spotify Artist"], provider_id="spotify")
            for u in urls
        ]

        dummy_mock = MagicMock(spec=DummySourceClient)
        dummy_mock.fetch_sources.side_effect = SourceTemporaryError("Upstream timeout on dummy source")

        def fake_create(source_type, **kw):
            return spotify_mock if source_type == "spotify" else dummy_mock

        sources = [
            "spotify:playlist:A",
            "dummy:playlist:B",
            "spotify:playlist:C",
        ]

        with patch.object(SourceFactory, "create_client", side_effect=fake_create):
            # fetch_all_sources with allow_partial=False must raise the failure
            with self.assertRaises(SourceTemporaryError):
                SourceFactory.fetch_all_sources(sources, allow_partial=False)

            # fetch_sources_with_report must provide partial tracks and the exact failure
            report = SourceFactory.fetch_sources_with_report(sources)
            self.assertTrue(report.has_failures)
            self.assertEqual(len(report.failed_sources), 1)
            failed_url, exc = report.failed_sources[0]
            self.assertEqual(failed_url, "dummy:playlist:B")
            self.assertIsInstance(exc, SourceTemporaryError)
            self.assertEqual(
                [t.title for t in report.tracks],
                ["Track from spotify:playlist:A", "Track from spotify:playlist:C"],
            )

    def test_fetch_sources_contract_violation_none_raises_source_error(self):
        """verify that returning None from fetch_sources raises SourceError in both single and mixed paths."""
        bad_client = MagicMock(spec=SpotifyAdapter)
        bad_client.fetch_sources.return_value = None

        with patch.object(SourceFactory, "create_client", return_value=bad_client):
            # single provider path
            with self.assertRaises(SourceError) as ctx:
                SourceFactory.fetch_all_sources(["spotify:playlist:single"])
            self.assertIn("contract", str(ctx.exception).lower())

            # mixed provider path
            SourceFactory.register_provider("dummy", DummySourceClient)
            with self.assertRaises(SourceError):
                SourceFactory.fetch_all_sources(["spotify:playlist:A", "dummy:playlist:B"])

    def test_register_provider_rejects_instance_method_supports_url(self):
        """verify that registering a provider with an instance method supports_url raises TypeError."""
        class InvalidInstanceClient(SourceClient):
            provider_id = "invalid"

            def supports_url(self, url: str) -> bool:
                return url.startswith("invalid:")

            def fetch_sources(self, urls: List[str], use_cache_if_available: bool = True) -> List[CanonicalTrack]:
                return []

            def check_health(self) -> bool:
                return True

        with self.assertRaises(TypeError) as ctx:
            SourceFactory.register_provider("invalid", InvalidInstanceClient)
        self.assertIn("@classmethod", str(ctx.exception))

    def test_register_provider_rejects_unimplemented_abstract_methods(self):
        """verify that registering a provider with unimplemented abstract methods raises TypeError."""
        class IncompleteClient(SourceClient):
            provider_id = "incomplete"

        with self.assertRaises(TypeError) as ctx:
            SourceFactory.register_provider("incomplete", IncompleteClient)
        self.assertIn("unimplemented abstract methods", str(ctx.exception))

    def test_detect_source_type_calls_classmethod_without_instantiating(self):
        """verify that detect_source_type calls classmethod directly without creating instances."""
        class ClassMethodOnlyClient(SourceClient):
            provider_id = "cls_only"

            def __init__(self, *args, **kwargs):
                raise AssertionError("constructor must never be called during URL detection")

            @classmethod
            def supports_url(cls, url: str) -> bool:
                return url.startswith("clsonly:")

            def fetch_sources(self, urls: List[str], use_cache_if_available: bool = True) -> List[CanonicalTrack]:
                return []

            def check_health(self) -> bool:
                return True

        SourceFactory.register_provider("cls_only", ClassMethodOnlyClient)
        self.assertEqual(SourceFactory.detect_source_type("clsonly:playlist:123"), "cls_only")
        self.assertEqual(SourceFactory.detect_source_type("other:playlist:123"), "unknown")

    def test_detect_source_type_propagates_unexpected_provider_exception(self):
        """verify that unexpected exceptions during provider URL detection are not silently swallowed."""
        class CrashingClient(SourceClient):
            provider_id = "crashing"

            @classmethod
            def supports_url(cls, url: str) -> bool:
                raise RuntimeError("unexpected parsing failure")

            def fetch_sources(self, urls: List[str], use_cache_if_available: bool = True) -> List[CanonicalTrack]:
                return []

            def check_health(self) -> bool:
                return True

        SourceFactory.register_provider("crashing", CrashingClient)
        with self.assertRaises(RuntimeError) as ctx:
            SourceFactory.detect_source_type("crashing:playlist:123")
        self.assertIn("unexpected parsing failure", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
