"""contract and architecture tests for multi-provider dispatch, queue persistence, and configuration."""
import json
import os
from pathlib import Path
from typing import List
from unittest.mock import MagicMock, patch

import pytest
import requests

from src.config import AppConfig, SourceConfig, SpotifyConfig
from src.core.models import CanonicalTrack, to_canonical_track, to_legacy_track
from src.models import Track
from src.queue_manager import QueueManager, _compute_source_hash, _track_identifier
from src.quota_tracker import QuotaTracker
from src.sources.base import SourceClient
from src.sources.exceptions import (
    SourceAuthError,
    SourceError,
    SourceTemporaryError,
    UnsupportedSourceError,
)
from src.sources.service import SourceIngestionService
from src.sources.source_factory import SourceFactory
from src.sources.spotify_adapter import SpotifyAdapter
from src.spotify_client import SpotifyClient
from tests.contracts.fake_provider import FakeSecondaryProvider


class TestMultiProviderDispatch:
    """tests multi-provider registration, routing, dispatch, and Spotify-free ingestion."""

    def test_multi_provider_detection_and_dispatch(self):
        """SourceFactory detects and dispatches mixed sources preserving source and track ordering."""
        SourceFactory.register_provider("fixture_music", FakeSecondaryProvider)

        mock_spotify = MagicMock(spec=SpotifyClient)
        mock_spotify.fetch_sources.side_effect = lambda urls, use_cache_if_available=True: [
            Track(
                title=f"Spotify Track {u.split(':')[-1]}",
                artist="Spotify Artist",
                album="Spotify Album",
                duration_ms=200000,
                spotify_id=f"spot_{u.split(':')[-1]}",
                source_name=u,
            )
            for u in urls
        ]
        spotify_adapter = SpotifyAdapter(client=mock_spotify)

        sources = [
            "spotify:track:A",
            "fixture:playlist:B",
            "spotify:track:C",
        ]

        # verify detection
        assert SourceFactory.detect_source_type("spotify:track:A") == "spotify"
        assert SourceFactory.detect_source_type("fixture:playlist:B") == "fixture_music"
        assert SourceFactory.detect_source_type("spotify:track:C") == "spotify"

        provider_kwargs = {
            "spotify": {"client": mock_spotify},
            "fixture_music": {"custom_option": "custom_arg"},
        }

        report = SourceFactory.fetch_sources_with_report(
            sources=sources,
            use_cache_if_available=False,
            provider_kwargs=provider_kwargs,
        )

        assert report.has_failures is False
        assert len(report.tracks) == 4  # spotify track A (1) + fixture playlist B (2) + Spotify track C (1)

        # verify exact track and source ordering
        assert report.tracks[0].title == "Spotify Track A"
        assert report.tracks[0].provider_id == "spotify"

        assert report.tracks[1].title == "Track 1 from B"
        assert report.tracks[1].provider_id == "fixture_music"

        assert report.tracks[2].title == "Track 2 from B"
        assert report.tracks[2].provider_id == "fixture_music"

        assert report.tracks[3].title == "Spotify Track C"
        assert report.tracks[3].provider_id == "spotify"

    def test_client_instance_reuse_in_batch(self):
        """verify SourceFactory reuses provider client instances across multiple sources in one batch."""
        init_counts = {"fixture": 0}

        class CountingFixtureProvider(FakeSecondaryProvider):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                init_counts["fixture"] += 1

        SourceFactory.register_provider("fixture_music", CountingFixtureProvider)

        sources = [
            "fixture:playlist:one",
            "fixture:album:two",
            "fixture:track:three",
        ]

        tracks = SourceFactory.fetch_all_sources(sources)
        assert len(tracks) >= 3
        # should create at most one client instance for fixture_music during the batch
        assert init_counts["fixture"] == 1

    def test_no_spotify_client_dependency_passed_to_fake_provider(self):
        """verify Spotify client or credentials are not passed into fake provider constructor."""
        received_args = {}

        class InspectingProvider(FakeSecondaryProvider):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                received_args.update(kwargs)

        SourceFactory.register_provider("fixture_music", InspectingProvider)

        mock_spotify = MagicMock()
        SourceFactory.fetch_all_sources(
            sources=["fixture:playlist:alpha"],
            client=mock_spotify,
            client_id="spotify_client_id_val",
            client_secret="spotify_client_secret_val",
            provider_kwargs={"fixture_music": {"fixture_specific_key": "fixture_val"}},
        )

        assert "client" not in received_args
        assert "client_id" not in received_args
        assert "client_secret" not in received_args
        assert received_args.get("fixture_specific_key") == "fixture_val"

    def test_spotify_free_ingestion_does_not_initialize_spotify_client(self):
        """verifies that fetching only fake provider sources does not initialize SpotifyClient."""
        SourceFactory.register_provider("fixture_music", FakeSecondaryProvider)

        service = SourceIngestionService(client_id=None, client_secret=None)
        # before fetching non-spotify sources, spotify client must not be initialized
        assert getattr(service, "_spotify_client", None) is None

        sources = ["fixture:playlist:alpha", "fixture:track:beta"]
        legacy_tracks = service.fetch_sources(sources)

        assert len(legacy_tracks) >= 2
        # after fetching only non-spotify sources, spotify client still must not be initialized
        assert getattr(service, "_spotify_client", None) is None

    def test_metadata_preservation_during_canonical_to_legacy_conversion(self):
        """verifies metadata fields survive CanonicalTrack -> Track conversion without loss."""
        canonical = CanonicalTrack(
            title="Symphony of Stars",
            artists=["Cosmic Artist", "Featured Guest"],
            album="Cosmic Realm",
            duration_ms=315000,
            isrc="USFIX9999999",
            external_ids={"fixture_music": "star_01"},
            provider_id="fixture_music",
            source_uri="fixture:track:star_01",
            source_name="Fixture Galaxy",
        )

        legacy = to_legacy_track(canonical, track_number=5)

        assert legacy.title == "Symphony of Stars"
        assert legacy.artist == "Cosmic Artist"
        assert legacy.album == "Cosmic Realm"
        assert legacy.album_artist == "Cosmic Artist"
        assert legacy.duration_ms == 315000
        assert legacy.duration_sec == 315
        assert legacy.track_number == 5
        assert legacy.spotify_id is None
        assert legacy.source_name == "Fixture Galaxy"


class TestSourceIdentityAndQueueCompatibility:
    """tests identity preservation and queue checkpoint restoration across 7 required scenarios."""

    def test_scenario_1_spotify_track_with_spotify_id(self):
        """Scenario 1: Spotify track with Spotify ID retains spotify_id and produces spotify: identifier."""
        canonical = CanonicalTrack(
            title="Harder Better Faster Stronger",
            artists=["Daft Punk"],
            album="Discovery",
            duration_ms=224000,
            external_ids={"spotify": "5W3cjX2J3tjhG8theqdaAZ"},
            provider_id="spotify",
            source_uri="spotify:track:5W3cjX2J3tjhG8theqdaAZ",
        )
        legacy = to_legacy_track(canonical)

        assert legacy.spotify_id == "5W3cjX2J3tjhG8theqdaAZ"
        assert _track_identifier(legacy) == "spotify:5W3cjX2J3tjhG8theqdaAZ"

    def test_scenario_2_fake_provider_track_with_own_provider_id(self):
        """Scenario 2: Fake provider track retains provider identity and does not fabricate a Spotify ID."""
        canonical = CanonicalTrack(
            title="Fixture Anthem",
            artists=["Fixture Band"],
            album="First Tape",
            duration_ms=185000,
            external_ids={"fixture_music": "fix_track_77"},
            provider_id="fixture_music",
            source_uri="fixture:track:fix_track_77",
            source_name="fixture:track:fix_track_77",
        )
        legacy = to_legacy_track(canonical)

        assert legacy.spotify_id is None
        ident = _track_identifier(legacy)
        assert not ident.startswith("spotify:")
        assert "Fixture Band::Fixture Anthem" in ident

    def test_scenario_3_two_different_providers_exposing_same_local_track_id(self):
        """Scenario 3: Spotify track and non-Spotify track with the same local track ID do not collide."""
        spotify_can = CanonicalTrack(
            title="Common Title",
            artists=["Common Artist"],
            album="Album A",
            duration_ms=180000,
            external_ids={"spotify": "same_local_id_100"},
            provider_id="spotify",
            source_uri="spotify:track:same_local_id_100",
        )
        fixture_can = CanonicalTrack(
            title="Common Title",
            artists=["Common Artist"],
            album="Album A",
            duration_ms=180000,
            external_ids={"fixture_music": "same_local_id_100"},
            provider_id="fixture_music",
            source_uri="fixture:track:same_local_id_100",
        )

        leg_spotify = to_legacy_track(spotify_can)
        leg_fixture = to_legacy_track(fixture_can)

        ident_spotify = _track_identifier(leg_spotify)
        ident_fixture = _track_identifier(leg_fixture)

        assert ident_spotify == "spotify:same_local_id_100"
        assert ident_fixture != ident_spotify
        assert not ident_fixture.startswith("spotify:")

    def test_scenario_4_two_different_tracks_with_same_title_and_primary_artist(self):
        """Scenario 4: Two tracks with same title and artist from different albums/durations have distinct identifiers."""
        track_a = Track(
            title="Aura",
            artist="Lady Gaga",
            album="Artpop",
            duration_ms=235000,
            track_number=1,
            spotify_id=None,
        )
        track_b = Track(
            title="Aura",
            artist="Lady Gaga",
            album="Artpop (Live)",
            duration_ms=250000,
            track_number=2,
            spotify_id=None,
        )

        assert _track_identifier(track_a) != _track_identifier(track_b)

    def test_scenario_5_two_occurrences_of_same_track_in_sources(self, tmp_path):
        """Scenario 5: Repeated occurrences of identical track preserve queue position without collapsing."""
        track = Track(title="Loop Song", artist="Loop Artist", duration_ms=180000, track_number=1)
        tracks = [track, Track(title="Middle", artist="Other", duration_ms=180000, track_number=2), track]

        db_path = tmp_path / "test_dup.db"
        tracker = QuotaTracker(db_path=db_path)
        qm = QueueManager(tracks=tracks, tracker=tracker, shuffle=True, loop=False)

        assert qm.total_tracks == 3
        # permutation must contain all 3 tracks
        perm = json.loads(tracker.get_state("queue_permutation"))
        assert len(perm) == 3

    def test_scenario_6_missing_provider_specific_identifiers(self):
        """Scenario 6: Missing provider identifiers produce valid deterministic identifier without crashing."""
        canonical = CanonicalTrack(
            title="Anonymous Ambient",
            artists=["Anonymous"],
            album=None,
            duration_ms=None,
            isrc=None,
            external_ids={},
            provider_id="fixture_music",
        )
        legacy = to_legacy_track(canonical)
        ident = _track_identifier(legacy)

        assert isinstance(ident, str)
        assert len(ident) > 0
        assert not ident.startswith("spotify:")

    def test_scenario_7_restart_and_checkpoint_restoration_with_mixed_providers(self, tmp_path):
        """Scenario 7: Mixed-provider queue restores exact shuffle permutation and playback index across restart."""
        db_path = tmp_path / "test_mixed_ckpt.db"
        tracker = QuotaTracker(db_path=db_path)

        spotify_track = Track(
            title="Spot Track",
            artist="Spot Artist",
            album="Spot Album",
            duration_ms=200000,
            spotify_id="spot_persisted_1",
            track_number=1,
        )
        fixture_track_1 = Track(
            title="Fix Track 1",
            artist="Fix Artist",
            album="Fix Album",
            duration_ms=180000,
            spotify_id=None,
            track_number=2,
            source_name="fixture:playlist:A",
        )
        fixture_track_2 = Track(
            title="Fix Track 2",
            artist="Fix Artist",
            album="Fix Album",
            duration_ms=240000,
            spotify_id=None,
            track_number=3,
            source_name="fixture:playlist:A",
        )

        original_tracks = [spotify_track, fixture_track_1, fixture_track_2]

        qm1 = QueueManager(tracks=original_tracks, tracker=tracker, shuffle=True, loop=False)
        # advance queue by 1 track and commit
        next_t = qm1.get_next_track()
        assert next_t is not None
        qm1.commit_track_progress()

        saved_perm = tracker.get_state("queue_permutation")
        saved_hash = tracker.get_state("queue_source_hash")
        saved_index = tracker.get_state("queue_index")

        assert saved_perm is not None
        assert saved_index == "1"

        # simulate application restart with same mixed sources
        tracker2 = QuotaTracker(db_path=db_path)
        qm2 = QueueManager(tracks=original_tracks, tracker=tracker2, shuffle=True, loop=False)

        assert qm2.current_index == 1
        assert [t.title for t in qm2.queue] == [t.title for t in qm1.queue]
        assert _compute_source_hash(qm2.original_tracks) == saved_hash


class TestConfigurationCompatibility:
    """tests configuration parsing, backward compatibility, and precedence with fake provider."""

    def test_fake_provider_only_configuration(self):
        """verify generic MUSIC_SOURCES with only fake provider sources parses cleanly."""
        with patch.dict(os.environ, {
            "MUSIC_SOURCES": "fixture:playlist:alpha, fixture:album:beta",
            "SOURCES_REFRESH_INTERVAL_HOURS": "6.0",
        }, clear=False):
            cfg = AppConfig.from_env()

            assert cfg.sources.sources == ["fixture:playlist:alpha", "fixture:album:beta"]
            assert cfg.sources.refresh_interval_hours == 6.0
            # spotify.sources must remain isolated and empty
            assert cfg.spotify.sources == []

    def test_mixed_provider_configuration(self):
        """verify MUSIC_SOURCES supports mixed Spotify and fake provider sources without contamination."""
        with patch.dict(os.environ, {
            "MUSIC_SOURCES": "https://open.spotify.com/playlist/spot1, fixture:playlist:fix2",
        }, clear=False):
            cfg = AppConfig.from_env()

            assert cfg.sources.sources == [
                "https://open.spotify.com/playlist/spot1",
                "fixture:playlist:fix2",
            ]
            assert cfg.spotify.sources == []

    def test_repeated_sources_in_generic_configuration(self):
        """verify repeated source entries in MUSIC_SOURCES are preserved in exact sequence."""
        with patch.dict(os.environ, {
            "MUSIC_SOURCES": "fixture:track:A, fixture:track:B, fixture:track:A",
        }, clear=False):
            cfg = AppConfig.from_env()
            assert cfg.sources.sources == [
                "fixture:track:A",
                "fixture:track:B",
                "fixture:track:A",
            ]

    def test_unsupported_provider_rejected_at_ingestion_boundary(self, tmp_path):
        """unsupported provider in generic source configuration fails during fetch_sources."""
        service = SourceIngestionService(cache_path=tmp_path / "cache.json")
        with pytest.raises(UnsupportedSourceError, match="Unsupported source URL"):
            service.fetch_sources(["https://unsupported.example.com/playlist/123"])


class TestFailureIsolation:
    """tests batch failure isolation, non-replacement of valid running queue, and absence of scrobbles."""

    def test_mixed_batch_failure_isolation(self):
        """in a mixed batch where second provider fails, report isolates failures and successful tracks."""
        SourceFactory.register_provider("fixture_music", FakeSecondaryProvider)

        mock_spotify = MagicMock(spec=SpotifyClient)
        mock_spotify.fetch_sources.side_effect = lambda urls, use_cache_if_available=True: [
            Track(
                title=f"Spotify Track {u.split(':')[-1]}",
                artist="Artist",
                duration_ms=180000,
                spotify_id=f"spot_{u.split(':')[-1]}",
                source_name=u,
            )
            for u in urls
        ]
        spotify_adapter = SpotifyAdapter(client=mock_spotify)

        sources = [
            "spotify:track:A",
            "fixture:playlist:temp_fail",
            "spotify:track:C",
        ]

        provider_kwargs = {
            "spotify": {"client": mock_spotify},
        }

        # 1. fetch_sources_with_report records failure explicitly and retains successful tracks
        report = SourceFactory.fetch_sources_with_report(
            sources=sources,
            use_cache_if_available=False,
            provider_kwargs=provider_kwargs,
        )

        assert report.has_failures is True
        assert len(report.failed_sources) == 1
        assert report.failed_sources[0][0] == "fixture:playlist:temp_fail"
        assert isinstance(report.failed_sources[0][1], SourceTemporaryError)
        # successful tracks from first and third sources are preserved
        assert len(report.tracks) == 2
        assert report.tracks[0].title == "Spotify Track A"
        assert report.tracks[1].title == "Spotify Track C"

        # 2. strict mode raises the failure immediately without returning partial list
        with pytest.raises(SourceTemporaryError):
            SourceFactory.fetch_all_sources(
                sources=sources,
                allow_partial=False,
                provider_kwargs=provider_kwargs,
            )

    def test_failed_ingestion_does_not_replace_running_queue_or_scrobble(self, tmp_path):
        """a failing source refresh does not mutate running queue, checkpoints, or send scrobbles."""
        db_path = tmp_path / "test_safe.db"
        tracker = QuotaTracker(db_path=db_path)
        existing_track = Track(title="Running Song", artist="Running Artist", duration_ms=180000)
        qm = QueueManager(tracks=[existing_track], tracker=tracker, shuffle=False, loop=False)

        SourceFactory.register_provider("fixture_music", FakeSecondaryProvider)

        sources = ["fixture:playlist:temp_fail"]

        with patch("src.lastfm_client.LastFMClient.scrobble", side_effect=RuntimeError("Must not scrobble")):
            with pytest.raises(SourceTemporaryError):
                SourceFactory.fetch_all_sources(sources, allow_partial=False)

        # queue state remains untouched
        assert qm.total_tracks == 1
        assert qm.queue[0].title == "Running Song"
        assert tracker.get_rolling_24h_count() == 0


class TestCrossProviderTrackIdentityRegressions:
    """regression tests for cross-provider track collisions and restart checkpoint recovery."""

    def test_cross_provider_identical_metadata_does_not_collide_identifiers(self):
        """tracks with identical artist, title, album, track_number from different providers have distinct IDs."""
        track_fixture = Track(
            title="Parallel Song",
            artist="Common Artist",
            album="Shared Album",
            duration_ms=180000,
            track_number=1,
            provider_id="fixture_music",
        )
        track_other = Track(
            title="Parallel Song",
            artist="Common Artist",
            album="Shared Album",
            duration_ms=180000,
            track_number=1,
            provider_id="other_service",
        )

        ident_fixture = _track_identifier(track_fixture)
        ident_other = _track_identifier(track_other)

        assert ident_fixture.startswith("fixture_music::")
        assert ident_other.startswith("other_service::")
        assert ident_fixture != ident_other

    def test_provider_switch_prevents_stale_checkpoint_reuse(self, tmp_path):
        """switching a source provider with identical track metadata prevents reusing old checkpoint."""
        db_path = tmp_path / "test_switch.db"
        tracker = QuotaTracker(db_path=db_path)

        tracks_fixture = [
            Track(
                title="Song A",
                artist="Artist A",
                album="Album",
                duration_ms=180000,
                track_number=1,
                provider_id="fixture_music",
            ),
        ]
        qm_fixture = QueueManager(tracks=tracks_fixture, tracker=tracker, shuffle=True, loop=False)
        qm_fixture.get_next_track()
        qm_fixture.commit_track_progress()

        saved_fixture_hash = tracker.get_state("queue_source_hash")
        assert tracker.get_state("queue_index") == "1"

        # user switches source provider to another provider with identical metadata
        tracks_other = [
            Track(
                title="Song A",
                artist="Artist A",
                album="Album",
                duration_ms=180000,
                track_number=1,
                provider_id="other_music",
            ),
        ]
        other_hash = _compute_source_hash(tracks_other)
        assert other_hash != saved_fixture_hash

        qm_other = QueueManager(tracks=tracks_other, tracker=tracker, shuffle=True, loop=False)
        # because the hash differs, checkpoint must NOT be reused and playback starts at 0
        assert qm_other.current_index == 0

    def test_restart_and_recovery_with_repeated_tracks_same_provider(self, tmp_path):
        """repeated tracks from the same provider preserve distinct positions and resume correctly."""
        db_path = tmp_path / "test_rep_recovery.db"
        tracker = QuotaTracker(db_path=db_path)

        track_a = Track(
            title="Loop Hit",
            artist="Loop Artist",
            album="Album",
            duration_ms=180000,
            track_number=1,
            provider_id="fixture_music",
        )
        track_b = Track(
            title="Interlude",
            artist="Loop Artist",
            album="Album",
            duration_ms=60000,
            track_number=2,
            provider_id="fixture_music",
        )
        tracks = [track_a, track_b, track_a]

        qm = QueueManager(tracks=tracks, tracker=tracker, shuffle=False, loop=False)
        qm.get_next_track()  # plays track_a (1st occurrence)
        qm.commit_track_progress()
        assert tracker.get_state("queue_index") == "1"

        # simulate restart
        qm_restored = QueueManager(tracks=tracks, tracker=tracker, shuffle=False, loop=False)
        assert qm_restored.current_index == 1
        next_track = qm_restored.get_next_track()
        assert next_track.title == "Interlude"


class TestSafeBatchFailureIsolationRegressions:
    """regression tests for HTTP 429, timeouts, and item-level failures during batch ingestion."""

    def test_batch_failure_rate_limit_http_429_skips_per_source_retries(self):
        """HTTP 429 rate limit aborts immediately without attempting per-source retries."""
        call_count = {"calls": 0}

        class RateLimitedProvider(FakeSecondaryProvider):
            def fetch_sources(self, urls, use_cache_if_available=True):
                call_count["calls"] += 1
                raise SourceTemporaryError("HTTP failure (429): Too Many Requests")

        SourceFactory.register_provider("fixture_music", RateLimitedProvider)

        sources = ["fixture:playlist:one", "fixture:playlist:two", "fixture:playlist:three"]
        report = SourceFactory.fetch_sources_with_report(sources)

        assert report.has_failures is True
        assert len(report.failed_sources) == 3
        # critical requirement: exactly 1 batch call, zero individual retries
        assert call_count["calls"] == 1

    def test_batch_failure_auth_error_skips_per_source_retries(self):
        """authentication failures abort immediately without attempting individual retries."""
        call_count = {"calls": 0}

        class AuthFailProvider(FakeSecondaryProvider):
            def fetch_sources(self, urls, use_cache_if_available=True):
                call_count["calls"] += 1
                raise SourceAuthError("Invalid credentials (401)")

        SourceFactory.register_provider("fixture_music", AuthFailProvider)

        sources = ["fixture:playlist:one", "fixture:playlist:two"]
        report = SourceFactory.fetch_sources_with_report(sources)

        assert report.has_failures is True
        assert len(report.failed_sources) == 2
        # exactly 1 call
        assert call_count["calls"] == 1

    def test_batch_failure_timeout_isolates_failing_source_and_salvages_valid(self):
        """a timeout on one source triggers fallback isolating the timed-out source."""
        class TimeoutItemProvider(FakeSecondaryProvider):
            def fetch_sources(self, urls, use_cache_if_available=True):
                if len(urls) > 1:
                    raise SourceTemporaryError("Batch operation timed out")
                if "timeout" in urls[0]:
                    raise SourceTemporaryError("Connection timed out for single source")
                return [
                    CanonicalTrack(
                        title=f"Valid Track from {urls[0]}",
                        artists=["Artist"],
                        provider_id=self.provider_id,
                    )
                ]

        SourceFactory.register_provider("fixture_music", TimeoutItemProvider)

        sources = ["fixture:playlist:valid", "fixture:playlist:timeout"]
        report = SourceFactory.fetch_sources_with_report(sources)

        assert report.has_failures is True
        assert len(report.failed_sources) == 1
        assert report.failed_sources[0][0] == "fixture:playlist:timeout"
        assert len(report.tracks) == 1
        assert report.tracks[0].title == "Valid Track from fixture:playlist:valid"

    def test_batch_failure_wrapped_http_429_skips_per_source_retries(self):
        """wrapped HTTP 429 via __cause__ or structured status_code aborts without per-source retries."""
        call_count = {"calls": 0}

        class Wrapped429Provider(FakeSecondaryProvider):
            def fetch_sources(self, urls, use_cache_if_available=True):
                call_count["calls"] += 1
                mock_resp = MagicMock()
                mock_resp.status_code = 429
                upstream = requests.exceptions.HTTPError(response=mock_resp)
                raise SourceTemporaryError(
                    "Upstream gateway reported rate limit",
                    status_code=429,
                ) from upstream

        SourceFactory.register_provider("fixture_music", Wrapped429Provider)

        sources = ["fixture:playlist:one", "fixture:playlist:two", "fixture:playlist:three"]
        report = SourceFactory.fetch_sources_with_report(sources)

        assert report.has_failures is True
        assert len(report.failed_sources) == 3
        # critical requirement: exactly 1 batch call, zero individual retries
        assert call_count["calls"] == 1
        failed_exc = report.failed_sources[0][1]
        assert isinstance(failed_exc, SourceTemporaryError)
        assert failed_exc.status_code == 429
        assert getattr(failed_exc, "__cause__", None) is not None

    def test_batch_failure_missing_status_metadata_fallback_rate_limit(self):
        """when structured status metadata is missing (None), message fallback detects rate limiting."""
        call_count = {"calls": 0}

        class MissingMetadataRateLimitProvider(FakeSecondaryProvider):
            def fetch_sources(self, urls, use_cache_if_available=True):
                call_count["calls"] += 1
                # status_code is None and no response object, only rate limit in message text
                raise SourceTemporaryError("rate limit exceeded: back off", status_code=None)

        SourceFactory.register_provider("fixture_music", MissingMetadataRateLimitProvider)

        sources = ["fixture:playlist:one", "fixture:playlist:two"]
        report = SourceFactory.fetch_sources_with_report(sources)

        assert report.has_failures is True
        assert len(report.failed_sources) == 2
        assert call_count["calls"] == 1

    def test_batch_failure_misleading_exception_message_preserves_per_source_isolation(self):
        """an item-level 404 whose message contains '429' does not falsely trigger provider-wide abort."""
        class MisleadingMessageProvider(FakeSecondaryProvider):
            def fetch_sources(self, urls, use_cache_if_available=True):
                if len(urls) > 1:
                    # batch failed with status 404, but message contains misleading '429' (e.g. playlist ID)
                    raise SourceTemporaryError(
                        "Playlist 429 item not found in catalog",
                        status_code=404,
                    )
                if "broken" in urls[0]:
                    raise SourceTemporaryError("Track 429 missing from remote server", status_code=404)
                return [
                    CanonicalTrack(
                        title=f"Valid Track from {urls[0]}",
                        artists=["Artist"],
                        provider_id=self.provider_id,
                    )
                ]

        SourceFactory.register_provider("fixture_music", MisleadingMessageProvider)

        sources = ["fixture:playlist:valid", "fixture:playlist:broken_429"]
        report = SourceFactory.fetch_sources_with_report(sources)

        # per-source isolation must NOT be skipped: valid source salvaged, broken source isolated
        assert report.has_failures is True
        assert len(report.failed_sources) == 1
        assert report.failed_sources[0][0] == "fixture:playlist:broken_429"
        assert len(report.tracks) == 1
        assert report.tracks[0].title == "Valid Track from fixture:playlist:valid"

    def test_batch_failure_ordinary_timeout_preserves_per_source_isolation(self):
        """ordinary network timeouts without status code preserve per-source isolation."""
        class OrdinaryTimeoutProvider(FakeSecondaryProvider):
            def fetch_sources(self, urls, use_cache_if_available=True):
                if len(urls) > 1:
                    raise TimeoutError("Read timed out on socket")
                if "slow" in urls[0]:
                    raise TimeoutError("Read timed out on socket for slow source")
                return [
                    CanonicalTrack(
                        title=f"Valid Track from {urls[0]}",
                        artists=["Artist"],
                        provider_id=self.provider_id,
                    )
                ]

        SourceFactory.register_provider("fixture_music", OrdinaryTimeoutProvider)

        sources = ["fixture:playlist:fast", "fixture:playlist:slow"]
        report = SourceFactory.fetch_sources_with_report(sources)

        assert report.has_failures is True
        assert len(report.failed_sources) == 1
        assert report.failed_sources[0][0] == "fixture:playlist:slow"
        assert len(report.tracks) == 1
        assert report.tracks[0].title == "Valid Track from fixture:playlist:fast"

    def test_successful_batch_ingestion_single_call_no_fallback(self):
        """when batch succeeds, client is called exactly once without fallback overhead."""
        call_count = {"calls": 0}

        class SuccessfulProvider(FakeSecondaryProvider):
            def fetch_sources(self, urls, use_cache_if_available=True):
                call_count["calls"] += 1
                return [
                    CanonicalTrack(title=f"Track {u}", artists=["Artist"], provider_id=self.provider_id)
                    for u in urls
                ]

        SourceFactory.register_provider("fixture_music", SuccessfulProvider)

        sources = ["fixture:playlist:a", "fixture:playlist:b", "fixture:playlist:c"]
        report = SourceFactory.fetch_sources_with_report(sources)

        assert report.has_failures is False
        assert len(report.tracks) == 3
        assert call_count["calls"] == 1


class TestDomainModelDependencyDirectionRegressions:
    """regression tests ensuring domain models remain decoupled from provider registries."""

    def test_domain_model_does_not_import_source_factory(self):
        """src/core/models.py must have zero references or imports to SourceFactory."""
        import src.core.models as core_models
        assert "SourceFactory" not in core_models.__dict__

        with open(core_models.__file__, "r", encoding="utf-8") as f:
            code = f.read()
        assert "SourceFactory" not in code
        assert "detect_source_type" not in code

    def test_to_canonical_track_human_readable_source_label(self):
        """human-readable playlist titles do not populate source_uri as pseudo-URIs."""
        legacy = Track(
            title="Solaris",
            artist="Photay",
            album="Onism",
            duration_ms=230000,
            spotify_id=None,
            source_name="Chill Evening Rotation",
        )
        canonical = to_canonical_track(legacy)

        assert canonical.title == "Solaris"
        assert canonical.source_name == "Chill Evening Rotation"
        # human readable label must not be coerced into a URI
        assert canonical.source_uri is None
        # legacy fallback without explicit provider_id defaults to spotify
        assert canonical.provider_id == "spotify"

    def test_to_canonical_track_preserves_explicit_provider_id(self):
        """track with explicit provider_id is mapped cleanly to CanonicalTrack."""
        legacy = Track(
            title="Native Track",
            artist="Native Artist",
            provider_id="fixture_music",
            source_name="fixture:track:native_99",
        )
        canonical = to_canonical_track(legacy)

        assert canonical.provider_id == "fixture_music"
        assert canonical.source_uri == "fixture:track:native_99"
        assert canonical.source_name == "fixture:track:native_99"

