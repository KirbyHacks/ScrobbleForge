"""source persistence, refresh boundaries, and provider isolation."""
import json
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.config import AppConfig, LastFMConfig, SourceConfig, SpotifyConfig
from src.core.models import CanonicalTrack
from src.core.source_errors import SourceAuthError, SourceError, SourceTemporaryError
from src.engine import ScrobblerEngine, is_transient_error
from src.models import Track
from src.queue_manager import QueueManager
from src.quota_tracker import QuotaTracker
from src.sources.base import SourceClient
from src.sources.cache import SourceCache
from src.sources.service import SourceIngestionService
from src.sources.source_factory import SourceFactory
from src.spotify_client import SpotifyClient, SpotifyIngestionError


class StubSource(SourceClient):
    provider_id = "stub_a"
    cache_schema_version = 1

    def __init__(self, calls, outcomes, **kwargs):
        self.calls = calls
        self.outcomes = outcomes

    @classmethod
    def supports_url(cls, url):
        return url.startswith(cls.provider_id + ":")

    def fetch_sources(self, urls, use_cache_if_available=True):
        assert use_cache_if_available is False
        url = urls[0]
        self.calls.append(url)
        result = self.outcomes.get(url, url)
        if isinstance(result, Exception):
            raise result
        if result == []:
            return []
        return [CanonicalTrack(title=result, artists=["artist"], provider_id=self.provider_id,
                               source_uri=url, duration_ms=180000)]

    def check_health(self):
        return True


class OtherSource(StubSource):
    provider_id = "stub_b"


@pytest.fixture
def sources(tmp_path, monkeypatch):
    registry = dict(SourceFactory._registry)
    SourceFactory.register_provider("stub_a", StubSource)
    SourceFactory.register_provider("stub_b", OtherSource)
    clock = [1000.0]
    monkeypatch.setattr("time.time", lambda: clock[0])
    calls, outcomes = [], {}
    path = tmp_path / "tracks_cache.json"

    def service():
        return SourceIngestionService(cache_path=path, ttl_hours=1,
            provider_options={name: {"calls": calls, "outcomes": outcomes}
                              for name in ("stub_a", "stub_b")})

    yield service, calls, outcomes, clock, path
    SourceFactory._registry = registry


def titles(tracks):
    return [track.title for track in tracks]


def test_restart_reuses_mixed_sources_in_new_order_with_duplicates(sources):
    service, calls, _, _, _ = sources
    service().fetch_sources(["stub_a:A", "stub_b:B", "stub_a:C"])
    calls.clear()
    assert titles(service().fetch_sources(["stub_a:C", "stub_b:B", "stub_a:A", "stub_a:A"])) == [
        "stub_a:C", "stub_b:B", "stub_a:A", "stub_a:A"]
    assert calls == []


def test_duplicate_weight_does_not_repeat_network_requests(sources):
    service, calls, _, _, _ = sources
    assert len(service().fetch_sources(["stub_a:A", "stub_a:A", "stub_b:B"])) == 3
    assert calls == ["stub_a:A", "stub_b:B"]


def test_source_ids_remain_case_sensitive(sources):
    service, calls, _, _, _ = sources
    assert titles(service().fetch_sources(["stub_a:ID", "stub_a:id"])) == ["stub_a:ID", "stub_a:id"]
    calls.clear()
    assert titles(service().fetch_sources(["stub_a:id", "stub_a:ID"])) == ["stub_a:id", "stub_a:ID"]
    assert calls == []


def test_only_expired_sources_refresh(sources):
    service, calls, outcomes, clock, _ = sources
    first = service()
    first.fetch_sources(["stub_a:A"])
    clock[0] += 1800
    first.fetch_sources(["stub_a:A", "stub_b:B"])
    clock[0] += 1800
    outcomes["stub_a:A"] = "fresh a"
    calls.clear()
    assert titles(first.refresh_sources(["stub_a:A", "stub_b:B"])) == ["fresh a", "stub_b:B"]
    assert calls == ["stub_a:A"]
    assert first.next_refresh_delay(["stub_a:A", "stub_b:B"]) == 1800


def test_partial_refresh_preserves_failed_source_and_its_age_across_restart(sources):
    service, calls, outcomes, clock, _ = sources
    first = service()
    first.fetch_sources(["stub_a:A", "stub_b:B"])
    clock[0] += 3600
    outcomes.update({"stub_a:A": SourceTemporaryError("offline"), "stub_b:B": "new b"})
    assert titles(first.refresh_sources(["stub_a:A", "stub_b:B"])) == ["stub_a:A", "new b"]
    state_a, state_b = first.get_status()
    assert state_a.stale and state_a.saved_at == 1000
    assert state_b.saved_at == 4600 and state_b.error is None
    calls.clear()
    second = service()
    assert titles(second.fetch_sources(["stub_b:B", "stub_a:A"])) == ["new b", "stub_a:A"]
    assert calls == ["stub_a:A"]
    assert second.get_status()[1].saved_at == 1000


def test_retry_does_not_refresh_healthy_sources(sources):
    service, calls, outcomes, clock, _ = sources
    first = service()
    first.fetch_sources(["stub_a:A", "stub_b:B"])
    clock[0] += 3600
    outcomes["stub_a:A"] = SourceTemporaryError("offline")
    first.refresh_sources(["stub_a:A", "stub_b:B"])
    assert first.next_refresh_delay(["stub_a:A", "stub_b:B"]) == 900
    calls.clear()
    clock[0] += 900
    outcomes["stub_a:A"] = "recovered"
    assert titles(first.refresh_sources(["stub_a:A", "stub_b:B"])) == ["recovered", "stub_b:B"]
    assert calls == ["stub_a:A"]


def test_initial_failure_keeps_healthy_sources_and_reports_failure(sources):
    service, _, outcomes, _, _ = sources
    outcomes["stub_a:A"] = SourceTemporaryError("offline")
    first = service()
    assert titles(first.fetch_sources(["stub_a:A", "stub_b:B"])) == ["stub_b:B"]
    assert first.last_report.has_failures
    assert isinstance(first.get_status()[0].error, SourceTemporaryError)
    with pytest.raises(SourceTemporaryError):
        first.fetch_sources(["stub_a:A", "stub_b:B"], allow_partial=False)


def test_all_failed_sources_without_cache_raise(sources):
    service, _, outcomes, _, _ = sources
    outcomes["stub_a:A"] = SourceTemporaryError("offline")
    with pytest.raises(SourceTemporaryError):
        service().fetch_sources(["stub_a:A"])


def test_rate_limit_is_scoped_to_provider_and_honors_retry_after(sources):
    service, calls, outcomes, clock, _ = sources
    first = service()
    urls = ["stub_a:A", "stub_a:C", "stub_b:B"]
    first.fetch_sources(urls)
    clock[0] += 3600
    outcomes["stub_a:A"] = SourceTemporaryError("rate limit", status_code=429, retry_after=3600)
    calls.clear()
    assert titles(first.refresh_sources(urls)) == urls
    assert calls == ["stub_a:A", "stub_b:B"]
    assert all(state.stale for state in first.get_status()[:2])
    retry_at = first.get_status()[0].retry_at
    clock[0] += 100
    calls.clear()
    first.refresh_sources(urls, force=True)
    assert calls == ["stub_b:B"]
    assert first.get_status()[0].retry_at == retry_at


def test_rate_limited_provider_still_uses_other_fresh_source(sources):
    service, calls, outcomes, clock, _ = sources
    first = service()
    first.fetch_sources(["stub_a:A"])
    clock[0] += 1800
    first.fetch_sources(["stub_a:C"])
    clock[0] += 1800
    outcomes["stub_a:A"] = SourceTemporaryError("rate limit", status_code=429)
    calls.clear()
    first.refresh_sources(["stub_a:A", "stub_a:C"])
    assert calls == ["stub_a:A"]
    assert first.get_status()[1].error is None
    assert not first.get_status()[1].stale


def test_item_failure_does_not_block_other_sources_of_same_provider(sources):
    service, calls, outcomes, _, _ = sources
    outcomes["stub_a:A"] = SourceTemporaryError("one playlist offline")
    assert titles(service().fetch_sources(["stub_a:A", "stub_a:C"])) == ["stub_a:C"]
    assert calls == ["stub_a:A", "stub_a:C"]


def test_auth_failure_is_not_hidden_by_stale_cache(sources):
    service, _, outcomes, _, _ = sources
    first = service()
    urls = ["stub_a:A", "stub_b:B"]
    first.fetch_sources(urls)
    outcomes["stub_a:A"] = SourceAuthError("unauthorized", status_code=401)
    assert titles(first.refresh_sources(urls, force=True)) == ["stub_b:B"]
    assert first.get_status()[0].stale is False
    with pytest.raises(SourceAuthError):
        first.refresh_sources(["stub_a:A"], force=True)


def test_unexpected_provider_failure_is_reported_without_harming_other_provider(sources):
    service, _, outcomes, _, _ = sources
    outcomes["stub_a:A"] = RuntimeError("parser bug")
    first = service()
    assert titles(first.fetch_sources(["stub_a:A", "stub_b:B"])) == ["stub_b:B"]
    assert isinstance(first.get_status()[0].error, RuntimeError)


def test_empty_source_is_cached_and_can_replace_old_tracks(sources):
    service, calls, outcomes, _, _ = sources
    first = service()
    first.fetch_sources(["stub_a:A"])
    outcomes["stub_a:A"] = []
    assert first.refresh_sources(["stub_a:A"], force=True) == []
    calls.clear()
    assert service().fetch_sources(["stub_a:A"]) == []
    assert calls == []


def test_corrupt_entry_does_not_invalidate_healthy_source(sources):
    service, calls, _, _, path = sources
    service().fetch_sources(["stub_a:A", "stub_b:B"])
    data = json.loads(path.read_text())
    entry = data["entries"][SourceCache.key("stub_a", "stub_a:A")]
    entry["tracks"][0]["duration_ms"] = "not a duration"
    path.write_text(json.dumps(data))
    calls.clear()
    service().fetch_sources(["stub_a:A", "stub_b:B"])
    assert calls == ["stub_a:A"]


def test_provider_cache_revision_invalidates_only_that_provider(sources, monkeypatch):
    service, calls, _, _, _ = sources
    service().fetch_sources(["stub_a:A", "stub_b:B"])
    monkeypatch.setattr(OtherSource, "cache_schema_version", 2)
    calls.clear()
    service().fetch_sources(["stub_a:A", "stub_b:B"])
    assert calls == ["stub_b:B"]


def test_failed_atomic_write_keeps_previous_disk_cache(sources):
    service, _, outcomes, _, path = sources
    first = service()
    first.fetch_sources(["stub_a:A", "stub_b:B"])
    before = path.read_bytes()
    outcomes["stub_a:A"] = "new a"
    with patch("src.sources.cache.json.dump", side_effect=OSError("disk full")):
        first.refresh_sources(["stub_a:A"], force=True)
    assert path.read_bytes() == before
    assert list(path.parent.glob("*.tmp")) == []
    assert titles(service().fetch_sources(["stub_a:A", "stub_b:B"])) == ["stub_a:A", "stub_b:B"]


def test_old_spotify_source_breakdown_migrates_without_resetting_age(sources):
    service, _, _, clock, path = sources
    urls = ["spotify:playlist:Alpha", "spotify:playlist:Beta"]
    tracks = [Track(title="a", artist="artist", spotify_id="a"), Track(title="b", artist="artist", spotify_id="b")]
    clock[0] = 900
    SpotifyClient(cache_path=path).save_cache(tracks, urls,
        source_tracks={url.lower(): [track] for url, track in zip(urls, tracks)})
    clock[0] = 1000
    first = service()
    with patch("src.spotify_client.requests.get") as get:
        assert titles(first.fetch_sources(urls[::-1])) == ["b", "a"]
        get.assert_not_called()
    assert all(state.saved_at == 900 for state in first.get_status())


def test_ambiguous_old_batch_survives_partial_refresh_and_restart(sources):
    service, _, _, _, path = sources
    urls = ["spotify:playlist:Alpha", "spotify:playlist:Beta"]
    old = [Track(title="a", artist="artist", spotify_id="a"), Track(title="b", artist="artist", spotify_id="b")]
    SpotifyClient(cache_path=path).save_cache(old, urls)
    before = path.read_bytes()
    first = service()
    assert titles(first.fetch_sources(urls)) == ["a", "b"]

    def load(client, refs, **kwargs):
        if refs[0] == urls[1]:
            raise SpotifyIngestionError("offline")
        return [Track(title="new a", artist="artist", spotify_id="a")]

    with patch.object(SpotifyClient, "fetch_sources", autospec=True, side_effect=load):
        assert titles(first.refresh_sources(urls, force=True)) == ["a", "b"]
    assert path.read_bytes() == before
    with patch("src.spotify_client.requests.get") as get:
        assert titles(service().fetch_sources(urls)) == ["a", "b"]
        get.assert_not_called()


def test_legacy_cache_does_not_reuse_a_different_case_sensitive_source(sources):
    service, _, _, _, path = sources
    SpotifyClient(cache_path=path).save_cache([Track(title="wrong", artist="artist")], ["spotify:playlist:ID"])
    with patch.object(SpotifyClient, "fetch_sources", return_value=[Track(title="correct", artist="artist")]) as fetch:
        assert titles(service().fetch_sources(["spotify:playlist:id"])) == ["correct"]
        fetch.assert_called_once()


def make_engine(tmp_path, source):
    tracker = QuotaTracker(tmp_path / "state.db")
    cfg = AppConfig(lastfm=LastFMConfig(), spotify=SpotifyConfig(),
                    sources=SourceConfig(sources=["stub_a:A", "stub_b:B"], refresh_interval_hours=1))
    queue = QueueManager([Track(title="old", artist="artist")], tracker=tracker, shuffle=False)
    return ScrobblerEngine(cfg, MagicMock(), queue=queue, tracker=tracker,
                           stop_event=threading.Event(), source_service=source)


def test_engine_refresh_uses_each_source_deadline(sources, tmp_path):
    service, calls, outcomes, clock, _ = sources
    first = service()
    first.fetch_sources(["stub_a:A"])
    clock[0] += 1800
    first.fetch_sources(["stub_a:A", "stub_b:B"])
    engine = make_engine(tmp_path, first)
    clock[0] += 1800
    outcomes["stub_a:A"] = "fresh a"
    calls.clear()
    engine._check_periodic_refresh()
    assert calls == ["stub_a:A"]
    assert titles(engine.queue.queue) == ["fresh a", "stub_b:B"]


@pytest.mark.parametrize("shuffle", [False, True])
def test_unchanged_refresh_preserves_queue_progress(sources, tmp_path, shuffle):
    service, _, _, _, _ = sources
    first = service()
    tracks = first.fetch_sources(["stub_a:A", "stub_b:B"])
    engine = make_engine(tmp_path, first)
    engine.queue = QueueManager(tracks, tracker=engine.tracker, shuffle=shuffle, loop=False)
    engine.queue.get_next_track()
    engine.queue.commit_track_progress()
    order = titles(engine.queue.queue)
    assert engine.refresh_sources(force=True)
    assert engine.queue.current_index == 1
    assert titles(engine.queue.queue) == order


def test_pending_track_prevents_public_refresh(sources, tmp_path):
    service, calls, _, _, _ = sources
    engine = make_engine(tmp_path, service())
    engine.pending_track = engine.queue.get_next_track()
    index = engine.queue.current_index
    assert engine.refresh_sources(force=True) is False
    assert engine.get_status()["queue_index"] == index
    assert engine.get_status()["pending_track"]["title"] == "old"
    assert calls == []


def test_successful_empty_refresh_clears_queue(sources, tmp_path):
    service, _, outcomes, _, _ = sources
    outcomes.update({"stub_a:A": [], "stub_b:B": []})
    engine = make_engine(tmp_path, service())
    assert engine.refresh_sources(force=True)
    assert engine.queue.total_tracks == 0
    assert engine.tracker.get_state("queue_index") == "0"


def test_core_import_does_not_load_provider_clients():
    script = "import sys; import src.engine; import src.core.local_tracks; assert 'src.spotify_client' not in sys.modules; assert 'src.sources.spotify_adapter' not in sys.modules; assert 'src.sources.youtube_adapter' not in sys.modules"
    subprocess.run([sys.executable, "-c", script], check=True, cwd=Path(__file__).resolve().parents[2])


def test_core_error_classification_is_provider_independent():
    assert is_transient_error(SourceTemporaryError("offline"))
    assert not is_transient_error(SourceAuthError("unauthorized"))
    assert not is_transient_error(SourceError("invalid source"))


@pytest.mark.parametrize("invalid", [None, {"bad": "value"},
    [CanonicalTrack(title="wrong provider", artists=[], provider_id="other")],
    [CanonicalTrack(title="bad duration", artists=[], provider_id="stub_a", duration_ms="nan")]])
def test_invalid_provider_output_does_not_reach_the_queue(sources, monkeypatch, invalid):
    service, _, _, _, _ = sources
    original = StubSource.fetch_sources

    def fetch(self, urls, **kwargs):
        return invalid if self.provider_id == "stub_a" else original(self, urls, **kwargs)

    monkeypatch.setattr(StubSource, "fetch_sources", fetch)
    first = service()
    assert titles(first.fetch_sources(["stub_a:A", "stub_b:B"])) == ["stub_b:B"]
    assert isinstance(first.get_status()[0].error, SourceError)


def test_generic_config_does_not_require_spotify_settings():
    cfg = AppConfig(lastfm=LastFMConfig(), sources=SourceConfig(sources=["stub_a:A"]))
    assert cfg.sources.sources == ["stub_a:A"]


def test_engine_partial_refresh_keeps_old_source_and_updates_healthy_source(sources, tmp_path):
    service, _, outcomes, clock, _ = sources
    first = service()
    first.fetch_sources(["stub_a:A", "stub_b:B"])
    engine = make_engine(tmp_path, first)
    outcomes.update({"stub_a:A": SourceTemporaryError("offline"), "stub_b:B": "updated b"})
    clock[0] += 3600
    engine._check_periodic_refresh()
    assert titles(engine.queue.queue) == ["stub_a:A", "updated b"]
    assert engine.get_status()["sources"][0].stale
    assert engine.tracker.get_rolling_24h_count() == 0


def test_old_cache_migrates_when_adding_another_provider(sources):
    service, calls, _, _, path = sources
    urls = ["spotify:playlist:Alpha", "spotify:playlist:Beta"]
    tracks = [Track(title="a", artist="artist", spotify_id="a"), Track(title="b", artist="artist", spotify_id="b")]
    SpotifyClient(cache_path=path).save_cache(tracks, urls,
        source_tracks={url.lower(): [track] for url, track in zip(urls, tracks)})
    with patch("src.spotify_client.requests.get") as get:
        assert titles(service().fetch_sources([urls[0], "stub_b:B"])) == ["a", "stub_b:B"]
        assert titles(service().fetch_sources([urls[1], "stub_b:B"])) == ["b", "stub_b:B"]
        get.assert_not_called()
    assert calls == ["stub_b:B"]


def test_unpartitioned_old_cache_is_not_reordered_by_guessing(sources):
    service, _, _, _, path = sources
    urls = ["spotify:playlist:Alpha", "spotify:playlist:Beta"]
    SpotifyClient(cache_path=path).save_cache([Track(title="old batch", artist="artist")], urls)
    def fetch(client, refs, **kwargs):
        return [Track(title=refs[0], artist="artist")]
    with patch.object(SpotifyClient, "fetch_sources", autospec=True, side_effect=fetch):
        assert titles(service().fetch_sources(urls[::-1])) == urls[::-1]


def test_real_provider_adapters_do_not_replace_each_others_source_cache(tmp_path):
    from src.sources.youtube_adapter import YouTubeAdapter
    path = tmp_path / "tracks_cache.json"
    refs = ["spotify:playlist:Alpha", "https://music.youtube.com/watch?v=abcdefghijk", "spotify:playlist:Beta"]

    def spotify_fetch(client, urls, **kwargs):
        assert client.cache_path is None
        return [Track(title=urls[0], artist="artist", spotify_id=urls[0].split(":")[-1])]

    def youtube_fetch(client, urls, **kwargs):
        assert client.cache_path is None
        return [CanonicalTrack(title=urls[0], artists=["artist"], provider_id="youtube")]

    with patch.object(SpotifyClient, "fetch_sources", autospec=True, side_effect=spotify_fetch) as sp, \
         patch.object(YouTubeAdapter, "fetch_sources", autospec=True, side_effect=youtube_fetch) as yt:
        assert titles(SourceIngestionService(cache_path=path).fetch_sources(refs)) == refs
        sp.reset_mock()
        yt.reset_mock()
        reordered = [refs[2], refs[0], refs[1], refs[0]]
        assert titles(SourceIngestionService(cache_path=path).fetch_sources(reordered)) == reordered
        sp.assert_not_called()
        yt.assert_not_called()
