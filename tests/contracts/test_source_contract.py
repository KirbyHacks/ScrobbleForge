"""reusable contract test suite validating any implementation of SourceClient."""
from abc import ABC
from pathlib import Path
from typing import List, Optional
from unittest.mock import MagicMock, patch

import pytest
import requests

from src.core.models import CanonicalTrack
from src.models import Track
from src.queue_manager import QueueManager
from src.quota_tracker import QuotaTracker
from src.sources.base import SourceClient
from src.sources.exceptions import (
    SourceAuthError,
    SourceError,
    SourceTemporaryError,
    UnsupportedSourceError,
)
from src.sources.source_factory import FetchReport, SourceFactory
from tests.contracts.conftest import ProviderContractSpec
from tests.contracts.fake_provider import FakeSecondaryProvider


class TestProviderRegistrationContract:
    """contract tests for provider registration, validation, and registry isolation."""

    def test_valid_provider_registration(self):
        """register a valid SourceClient subclass and verify registry state and factory creation."""
        SourceFactory.register_provider("fixture_music", FakeSecondaryProvider)
        assert "fixture_music" in SourceFactory.get_registered_providers()

        client = SourceFactory.create_client("fixture_music", custom_option="test_val")
        assert client is not None
        assert isinstance(client, FakeSecondaryProvider)
        assert client.custom_option == "test_val"

    def test_duplicate_provider_registration_behavior(self):
        """registering an existing provider identifier updates the registry without raising."""
        class MockAlternative(FakeSecondaryProvider):
            pass

        SourceFactory.register_provider("fixture_music", FakeSecondaryProvider)
        SourceFactory.register_provider("fixture_music", MockAlternative)

        client = SourceFactory.create_client("fixture_music")
        assert isinstance(client, MockAlternative)

    def test_invalid_provider_class_rejected(self):
        """registering a class that does not subclass SourceClient raises TypeError."""
        class NotAClient:
            pass

        with pytest.raises(TypeError, match="must subclass SourceClient"):
            SourceFactory.register_provider("invalid_cls", NotAClient)  # type: ignore

        with pytest.raises(TypeError, match="must subclass SourceClient"):
            SourceFactory.register_provider("not_a_type", "just_a_string")  # type: ignore

    def test_unimplemented_abstract_methods_rejected(self):
        """subclass with missing abstract methods cannot be registered."""
        class IncompleteClient(SourceClient):
            provider_id = "incomplete"
            # missing supports_url, fetch_sources, check_health

        with pytest.raises(TypeError, match="unimplemented abstract methods"):
            SourceFactory.register_provider("incomplete", IncompleteClient)

    def test_invalid_provider_identifiers_rejected(self):
        """empty string, whitespace, None, and non-strings raise ValueError."""
        for invalid_id in ["", "   ", None, 123]:
            with pytest.raises(ValueError, match="Provider ID must be a non-empty string"):
                SourceFactory.register_provider(invalid_id, FakeSecondaryProvider)  # type: ignore

    def test_registry_isolation_between_tests(self):
        """verifies reset_registry restores registry to defaults without leakage."""
        SourceFactory.register_provider("temp_fixture", FakeSecondaryProvider)
        assert "temp_fixture" in SourceFactory.get_registered_providers()

        SourceFactory.reset_registry()
        assert "temp_fixture" not in SourceFactory.get_registered_providers()
        assert "spotify" in SourceFactory.get_registered_providers()


class TestUrlDetectionContract:
    """contract tests for URL detection, case sensitivity, spoofing protection, and offline purity."""

    def test_supported_references(self, provider_spec: ProviderContractSpec):
        """supported references return True on client and detect correct provider identifier."""
        SourceFactory.register_provider(provider_spec.provider_id, provider_spec.client_cls)

        assert provider_spec.client_cls.supports_url(provider_spec.valid_source_single) is True
        assert SourceFactory.detect_source_type(provider_spec.valid_source_single) == provider_spec.provider_id

        for src in provider_spec.valid_source_multiple:
            assert provider_spec.client_cls.supports_url(src) is True
            assert SourceFactory.detect_source_type(src) == provider_spec.provider_id

    def test_unsupported_references(self, provider_spec: ProviderContractSpec):
        """unsupported references return False and detect as unknown."""
        SourceFactory.register_provider(provider_spec.provider_id, provider_spec.client_cls)

        assert provider_spec.client_cls.supports_url(provider_spec.invalid_source) is False
        assert provider_spec.client_cls.supports_url("https://unknown-service.example.org/track/1") is False

    def test_malformed_references(self, provider_spec: ProviderContractSpec):
        """malformed or empty references return False safely without raising."""
        for malformed in ["", "   ", None, "http://", "not-a-url", ":::"]:
            assert provider_spec.client_cls.supports_url(malformed) is False  # type: ignore
            assert SourceFactory.detect_source_type(malformed) == "unknown"  # type: ignore

    def test_hostname_spoofing_rejected(self, provider_spec: ProviderContractSpec):
        """references with spoofed or attacker-controlled subdomains return False."""
        SourceFactory.register_provider(provider_spec.provider_id, provider_spec.client_cls)
        assert provider_spec.client_cls.supports_url(provider_spec.spoofed_source) is False
        assert SourceFactory.detect_source_type(provider_spec.spoofed_source) != provider_spec.provider_id

    def test_case_sensitivity_rules(self, provider_spec: ProviderContractSpec):
        """uppercase scheme and hostname should still be detected correctly."""
        SourceFactory.register_provider(provider_spec.provider_id, provider_spec.client_cls)
        upper_ref = provider_spec.valid_source_single.upper()
        # provider detection should be case-tolerant
        assert provider_spec.client_cls.supports_url(upper_ref) is True

    def test_no_client_initialization_during_detection(self, provider_spec: ProviderContractSpec):
        """supports_url must be callable as a classmethod without creating an instance."""
        # call supports_url directly on client_cls
        result = provider_spec.client_cls.supports_url(provider_spec.valid_source_single)
        assert result is True

    def test_no_network_requests_during_detection(self, provider_spec: ProviderContractSpec):
        """url detection must be entirely local and execute zero network calls."""
        with patch("requests.get", side_effect=RuntimeError("Network forbidden")), \
             patch("requests.head", side_effect=RuntimeError("Network forbidden")):
            detected = SourceFactory.detect_source_type(provider_spec.valid_source_single)
            assert detected == "unknown" or detected == provider_spec.provider_id

    def test_unexpected_provider_detection_exceptions_remain_visible(self):
        """if a provider detector raises an unexpected error, it is not silently swallowed."""
        class ExplodingDetector(SourceClient):
            provider_id = "exploding"

            @classmethod
            def supports_url(cls, url: str) -> bool:
                raise ValueError("Unexpected parser crash in detector")

            def fetch_sources(self, urls, use_cache_if_available=True):
                return []

            def check_health(self):
                return True

        SourceFactory.register_provider("exploding", ExplodingDetector)
        with pytest.raises(ValueError, match="Unexpected parser crash in detector"):
            SourceFactory.detect_source_type("some_url")


class TestTrackIngestionContract:
    """contract tests for track ingestion, metadata normalization, and ordering."""

    def test_one_valid_source_returns_canonical_tracks(self, provider_spec: ProviderContractSpec):
        """ingesting a single valid source returns List[CanonicalTrack]."""
        client = provider_spec.create_client()
        tracks = client.fetch_sources([provider_spec.valid_source_single])

        assert isinstance(tracks, list)
        assert len(tracks) >= 1
        for t in tracks:
            assert isinstance(t, CanonicalTrack)
            assert len(t.title) > 0
            assert len(t.artists) > 0
            assert t.primary_artist == t.artists[0]
            assert t.provider_id == provider_spec.provider_id

    def test_multiple_sources_ingestion(self, provider_spec: ProviderContractSpec):
        """ingesting multiple sources aggregates tracks preserving order."""
        client = provider_spec.create_client()
        tracks = client.fetch_sources(provider_spec.valid_source_multiple)

        assert isinstance(tracks, list)
        assert len(tracks) >= len(provider_spec.valid_source_multiple)

    def test_empty_valid_source_returns_empty_list_not_none(self, provider_spec: ProviderContractSpec):
        """empty source returns [] and is distinct from failure."""
        client = provider_spec.create_client()
        tracks = client.fetch_sources([provider_spec.valid_source_empty])

        assert tracks is not None
        assert isinstance(tracks, list)
        assert len(tracks) == 0

    def test_missing_optional_metadata(self, provider_spec: ProviderContractSpec):
        """tracks with missing album, duration, or isrc still produce valid CanonicalTrack."""
        client = provider_spec.create_client()
        tracks = client.fetch_sources([provider_spec.valid_source_missing_meta])

        assert len(tracks) >= 1
        track = tracks[0]
        assert isinstance(track, CanonicalTrack)
        assert track.primary_artist != ""

    def test_multiple_artists_preserved_in_order(self, provider_spec: ProviderContractSpec):
        """multiple artists are preserved in sequence with primary_artist pointing to first artist."""
        client = provider_spec.create_client()
        tracks = client.fetch_sources([provider_spec.valid_source_multi_artist])

        assert len(tracks) >= 1
        track = tracks[0]
        assert isinstance(track.artists, list)
        assert len(track.artists) >= 1
        assert track.primary_artist == track.artists[0]
        if provider_spec.provider_id != "spotify":
            # native providers directly yield multiple artists in canonical model
            assert len(track.artists) >= 2
        else:
            # legacy SpotifyClient bridge collapses artist field to primary artist on legacy Track model
            assert len(track.artists) == 1

    def test_correct_provider_id_and_external_ids(self, provider_spec: ProviderContractSpec):
        """every canonical track has the correct provider_id and external_ids mapping."""
        client = provider_spec.create_client()
        tracks = client.fetch_sources([provider_spec.valid_source_single])

        for track in tracks:
            assert track.provider_id == provider_spec.provider_id
            assert isinstance(track.external_ids, dict)
            assert provider_spec.provider_id in track.external_ids

    def test_track_ordering_preservation(self, provider_spec: ProviderContractSpec):
        """consecutive fetches return tracks in deterministic, authentic sequence."""
        client = provider_spec.create_client()
        run1 = client.fetch_sources(provider_spec.valid_source_multiple)
        run2 = client.fetch_sources(provider_spec.valid_source_multiple)

        assert len(run1) == len(run2)
        for t1, t2 in zip(run1, run2):
            assert t1.title == t2.title
            assert t1.artists == t2.artists
            assert t1.provider_id == t2.provider_id

    def test_repeated_source_references_preserved(self, provider_spec: ProviderContractSpec):
        """repeated source references preserve track count without deduplicating or dropping items."""
        client = provider_spec.create_client()
        single_src = provider_spec.valid_source_single
        repeated_sources = [single_src, single_src, single_src]

        tracks = client.fetch_sources(repeated_sources)
        single_count = len(client.fetch_sources([single_src]))
        assert len(tracks) == single_count * 3


class TestErrorHandlingContract:
    """contract tests for error categorization, failure isolation, and strict failure propagation."""

    def test_auth_error_raised(self, provider_spec: ProviderContractSpec):
        """authentication failures raise SourceAuthError."""
        client = provider_spec.create_client()
        with pytest.raises(SourceAuthError):
            client.fetch_sources([provider_spec.auth_fail_source])

    def test_temporary_network_error_raised(self, provider_spec: ProviderContractSpec):
        """transient network or server failures raise SourceTemporaryError."""
        client = provider_spec.create_client()
        with pytest.raises(SourceTemporaryError):
            client.fetch_sources([provider_spec.temp_fail_source])

    def test_unsupported_source_error_raised(self, provider_spec: ProviderContractSpec):
        """unsupported source reference passed to client raises UnsupportedSourceError."""
        client = provider_spec.create_client()
        with pytest.raises(UnsupportedSourceError):
            client.fetch_sources([provider_spec.invalid_source])

    def test_unexpected_exception_raised(self, provider_spec: ProviderContractSpec):
        """unexpected provider exceptions raise SourceError or subclass."""
        client = provider_spec.create_client()
        with pytest.raises(SourceError):
            client.fetch_sources([provider_spec.unexpected_error_source])

    def test_provider_returning_none_is_contract_violation(self):
        """providers returning None violate contract; SourceFactory raises SourceError."""
        SourceFactory.register_provider("fixture_music", FakeSecondaryProvider)
        with pytest.raises(SourceError, match="violated fetch_sources contract: returned None"):
            SourceFactory.fetch_all_sources(["fixture:playlist:return_none"])

    def test_failing_source_in_middle_of_batch(self, provider_spec: ProviderContractSpec):
        """mixed batch with failure raises immediately when allow_partial=False."""
        SourceFactory.register_provider(provider_spec.provider_id, provider_spec.client_cls)
        batch = [
            provider_spec.valid_source_single,
            provider_spec.temp_fail_source,
            provider_spec.valid_source_single,
        ]

        client = provider_spec.create_client()
        provider_kwargs = {provider_spec.provider_id: {"client": client._client}} if hasattr(client, "_client") else None

        with pytest.raises(SourceTemporaryError):
            SourceFactory.fetch_all_sources(batch, allow_partial=False, provider_kwargs=provider_kwargs)

    def test_explicit_partial_results_via_fetch_report(self, provider_spec: ProviderContractSpec):
        """fetch_sources_with_report collects valid tracks and lists failed sources."""
        SourceFactory.register_provider(provider_spec.provider_id, provider_spec.client_cls)
        batch = [
            provider_spec.valid_source_single,
            provider_spec.temp_fail_source,
        ]

        client = provider_spec.create_client()
        provider_kwargs = {provider_spec.provider_id: {"client": client._client}} if hasattr(client, "_client") else None

        report = SourceFactory.fetch_sources_with_report(batch, provider_kwargs=provider_kwargs)
        assert isinstance(report, FetchReport)
        assert report.has_failures is True
        assert len(report.failed_sources) == 1
        assert report.failed_sources[0][0] == provider_spec.temp_fail_source
        assert isinstance(report.failed_sources[0][1], SourceTemporaryError)
        assert len(report.tracks) >= 1


class TestHealthCheckContract:
    """contract tests for provider health checks."""

    def test_healthy_provider_returns_true(self):
        """healthy provider check_health returns True."""
        client = FakeSecondaryProvider(is_healthy=True)
        assert client.check_health() is True

    def test_unavailable_provider_returns_false(self):
        """unavailable provider check_health returns False."""
        client = FakeSecondaryProvider(is_healthy=False)
        assert client.check_health() is False

    def test_health_check_does_not_mutate_queue_state(self, tmp_path):
        """health check does not modify queue cursor, tracks, or quota tracker."""
        db_path = tmp_path / "test_health.db"
        tracker = QuotaTracker(db_path=db_path)
        track = Track(title="Song", artist="Artist", duration_ms=180000)
        qm = QueueManager(tracks=[track], tracker=tracker, shuffle=False, loop=False)

        client = FakeSecondaryProvider()
        assert client.check_health() is True

        assert qm.current_index == 0
        assert qm.total_tracks == 1
        assert tracker.get_rolling_24h_count() == 0

    def test_health_check_network_policy_fake_provider(self):
        """document and verify fake provider executes zero network requests during health check."""
        client = FakeSecondaryProvider()
        with patch("requests.head", side_effect=RuntimeError("No network allowed")), \
             patch("requests.get", side_effect=RuntimeError("No network allowed")):
            assert client.check_health() is True


class TestIsolationContract:
    """contract tests verifying isolation across providers, Last.fm, queue checkpoints, and quota."""

    def test_no_lastfm_requests_during_ingestion(self, provider_spec: ProviderContractSpec):
        """no Last.fm network activity occurs during source ingestion."""
        client = provider_spec.create_client()
        with patch("src.lastfm_client.LastFMClient.scrobble", side_effect=RuntimeError("Last.fm scrobble called")), \
             patch("src.lastfm_client.LastFMClient.update_now_playing", side_effect=RuntimeError("Last.fm now playing called")):
            tracks = client.fetch_sources([provider_spec.valid_source_single])
            assert len(tracks) >= 1

    def test_no_queue_checkpoint_advancement_during_ingestion(self, tmp_path):
        """ingesting sources does not mutate existing queue checkpoints."""
        db_path = tmp_path / "test_iso.db"
        tracker = QuotaTracker(db_path=db_path)
        tracker.set_state("queue_index", "42")

        SourceFactory.register_provider("fixture_music", FakeSecondaryProvider)
        tracks = SourceFactory.fetch_all_sources(["fixture:playlist:test"])

        assert len(tracks) >= 1
        assert tracker.get_state("queue_index") == "42"

    def test_no_quota_modifications_during_ingestion(self, tmp_path):
        """source ingestion does not add scrobble entries to quota tracker."""
        db_path = tmp_path / "test_iso.db"
        tracker = QuotaTracker(db_path=db_path)
        assert tracker.get_rolling_24h_count() == 0

        SourceFactory.register_provider("fixture_music", FakeSecondaryProvider)
        SourceFactory.fetch_all_sources(["fixture:playlist:test"])

        assert tracker.get_rolling_24h_count() == 0
