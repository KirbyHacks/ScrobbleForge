"""regressions for delivery, source parsing, and authorization failures."""
import json
import sqlite3
from unittest.mock import MagicMock, patch
from xml.dom import minidom

import pylast
import pytest
import requests

from src.config import AppConfig, EngineConfig, LastFMConfig
from src.core.local_tracks import load_local_fallback_tracks
from src.core.models import CanonicalTrack
from src.engine import ScrobblerEngine
from src.lastfm_client import LastFMClient, LastFMDailyLimitError, poll_web_auth
from src.models import Track
from src.queue_manager import QueueManager
from src.quota_tracker import QuotaTracker
from src.sources.cache import SourceCache
from src.sources.exceptions import SourceAuthError, SourceTemporaryError
from src.sources.service import SourceIngestionService
from src.sources.spotify_adapter import SpotifyAdapter
from src.sources.youtube_adapter import YouTubeAdapter, YouTubeMetadataNormalizer
from src.spotify_client import SpotifyClient, SpotifyEmbedParser, SpotifyIngestionError


def response(data, status=200):
    result = requests.Response()
    result.status_code = status
    result.url = "https://example.test/api"
    result._content = json.dumps(data).encode()
    return result


class ClockStop:
    def __init__(self, clock):
        self.clock = clock
        self.stopped = False
        self.waits = []

    def is_set(self):
        return self.stopped

    def wait(self, timeout):
        self.waits.append(timeout)
        self.clock[0] += timeout
        return self.stopped


def engine_fixture(tmp_path, monkeypatch, mode):
    clock = [1700000000.0]
    monkeypatch.setattr("time.time", lambda: clock[0])
    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    tracker = QuotaTracker(tmp_path / "state.db")
    track = Track(title="track", artist="artist")
    queue = QueueManager([track], tracker, shuffle=False, loop=False)
    sink = MagicMock()
    sink.scrobble.return_value = True
    stop = ClockStop(clock)
    cfg = AppConfig(lastfm=LastFMConfig(), engine=EngineConfig(
        mode=mode, loop=False, max_daily_scrobbles=1, custom_interval_seconds=1,
        inter_track_pause_min=0, inter_track_pause_max=0,
    ))
    engine = ScrobblerEngine(cfg, sink, queue=queue, tracker=tracker, stop_event=stop)
    return engine, sink, tracker, stop


@pytest.mark.parametrize("mode,failure", [
    (mode, failure) for mode in ("realistic", "max_limit", "custom_interval")
    for failure in ("ledger", "count", "queue", "timeline")
    if failure != "timeline" or mode == "max_limit"
])
def test_accepted_delivery_is_not_repeated_after_local_failure(tmp_path, monkeypatch, mode, failure):
    engine, sink, tracker, stop = engine_fixture(tmp_path, monkeypatch, mode)
    method = {"ledger": "record_scrobble", "count": "get_rolling_24h_count",
              "queue": "set_state", "timeline": "set_state"}[failure]
    original = getattr(tracker, method)
    failed = False

    def flaky(*args, **kwargs):
        nonlocal failed
        eligible = sink.scrobble.call_count > 0
        if failure in ("queue", "timeline"):
            eligible = eligible and args[0] == ("queue_index" if failure == "queue" else "virtual_timeline_cursor")
        if eligible and not failed:
            failed = True
            raise sqlite3.OperationalError("database is locked")
        return original(*args, **kwargs)

    monkeypatch.setattr(tracker, method, flaky)
    engine.run()
    assert failed
    assert sink.scrobble.call_count == 1
    assert tracker.get_total_scrobbles() == 1
    assert tracker.get_state("queue_index") == "1"
    assert not engine.circuit_broken
    assert 86401 not in stop.waits
    assert sink.update_now_playing.call_count == (1 if mode == "realistic" else 0)


@pytest.mark.parametrize("failure", ["ledger", "queue"])
def test_local_save_recovery_does_not_trigger_a_process_restart(tmp_path, monkeypatch, failure):
    engine, sink, tracker, stop = engine_fixture(tmp_path, monkeypatch, "custom_interval")
    method = "record_scrobble" if failure == "ledger" else "set_state"
    original = getattr(tracker, method)
    failures = 0

    def flaky(*args, **kwargs):
        nonlocal failures
        if failures < 7:
            failures += 1
            raise sqlite3.OperationalError("database is locked")
        return original(*args, **kwargs)

    monkeypatch.setattr(tracker, method, flaky)
    engine.run()
    assert failures == 7 and not engine.circuit_broken
    assert sink.scrobble.call_count == 1
    assert tracker.get_total_scrobbles() == 1
    assert tracker.get_state("queue_index") == "1"


@pytest.mark.parametrize("mode", ["realistic", "max_limit", "custom_interval"])
@pytest.mark.parametrize("shutdown", [False, True])
def test_daily_limit_keeps_pending_track_and_timestamp(tmp_path, monkeypatch, mode, shutdown):
    engine, sink, tracker, stop = engine_fixture(tmp_path, monkeypatch, mode)
    sink.scrobble.side_effect = [LastFMDailyLimitError("daily quota"), True]
    if shutdown:
        wait = stop.wait

        def stop_during_quota(timeout):
            if timeout == 3600:
                stop.stopped = True
            return wait(timeout)

        monkeypatch.setattr(stop, "wait", stop_during_quota)
    engine.run()
    assert 3600 in stop.waits
    assert not engine.circuit_broken
    if shutdown:
        assert engine.pending_track is not None
        assert tracker.get_state("queue_index") is None
        assert tracker.get_total_scrobbles() == 0
    else:
        calls = sink.scrobble.call_args_list
        assert len(calls) == 2
        assert calls[0].kwargs["timestamp"] == calls[1].kwargs["timestamp"]
        assert tracker.get_state("queue_index") == "1"
        assert tracker.get_total_scrobbles() == 1
        assert sink.update_now_playing.call_count == (1 if mode == "realistic" else 0)


def test_lastfm_daily_limit_response_is_recoverable():
    client = LastFMClient.__new__(LastFMClient)
    client.network = MagicMock()
    xml = '<scrobbles accepted="0" ignored="1"><scrobble><ignoredMessage code="5">daily quota</ignoredMessage></scrobble></scrobbles>'
    with patch("src.lastfm_client.pylast._Request") as request:
        request.return_value.execute.return_value = minidom.parseString(xml)
        with pytest.raises(LastFMDailyLimitError):
            client.scrobble(Track(title="track", artist="artist"), timestamp=1000)


@pytest.mark.parametrize("payload", [{}, {"type": "playlist", "name": "list"}, {"type": "playlist", "tracks": {}}])
def test_malformed_spotify_refresh_preserves_existing_queue(tmp_path, monkeypatch, payload):
    source = "spotify:playlist:" + "A" * 22
    good = {"type": "playlist", "name": "list", "trackList": [
        {"title": "song", "subtitle": "artist", "uri": "spotify:track:" + "B" * 22}]}
    monkeypatch.setattr("src.spotify_client.requests.get", lambda *a, **k: response(good))
    service = SourceIngestionService(cache_path=tmp_path / "cache.json")
    tracks = service.fetch_sources([source], False)
    tracker = QuotaTracker(tmp_path / "state.db")
    queue = QueueManager(tracks, tracker, shuffle=False, loop=False)
    monkeypatch.setattr("src.spotify_client.requests.get", lambda *a, **k: response(payload))
    queue.update_tracks(service.fetch_sources([source], False))
    assert queue.total_tracks == 1
    assert service.last_report.has_failures
    assert service.last_report.sources[0].stale
    assert isinstance(service.last_report.sources[0].error, SourceTemporaryError)


def test_valid_empty_spotify_playlist_remains_empty():
    assert SpotifyEmbedParser.parse(json.dumps({"type": "playlist", "trackList": []})) == []


def test_spotify_playlist_preserves_album_artist_and_album_number():
    items = [{"title": "song", "artists": [{"name": "Solo Artist"}],
              "album": {"name": "Compilation", "artists": [{"name": "Various Artists"}]}}]
    for number in (None, 7, "bad", 0):
        items.append(dict(items[0], trackNumber=number))
    tracks = SpotifyEmbedParser.parse(json.dumps({"type": "playlist", "trackList": items}))
    assert [track.track_number for track in tracks] == [None, None, 7, None, None]
    assert all(track.album_artist == "Various Artists" for track in tracks)


@pytest.mark.parametrize("prefix", ["intl-es/", "embed/", "intl-es/embed/", ""])
def test_spotify_accepted_url_fetches_the_correct_entity(monkeypatch, prefix):
    identifier = "C" * 22
    source = f"https://open.spotify.com/{prefix}track/{identifier}"
    assert SpotifyAdapter.supports_url(source)
    assert SpotifyClient.parse_spotify_uri(source) == ("track", identifier)
    with patch("src.spotify_client.requests.get", return_value=response({"type": "track", "title": "song"})) as get:
        SpotifyClient(retries=1).fetch_sources([source], False)
    assert get.call_args.args[0] == f"https://open.spotify.com/embed/track/{identifier}"


def youtube_service(tmp_path, session):
    source = "https://music.youtube.com/playlist?list=PLreview"
    path = tmp_path / "cache.json"
    old_tracks = [CanonicalTrack(title=title, artists=["Radiohead"], provider_id="youtube",
                                source_uri=source, duration_ms=180000) for title in ("one", "two")]
    SourceCache(path).write("youtube", source, YouTubeAdapter.cache_schema_version, old_tracks, 1000)
    service = SourceIngestionService(cache_path=path, provider_options={"youtube": {"session": session}})
    return service, source


@pytest.mark.parametrize("continuation", [{}, {"continuationContents": {"musicPlaylistShelfContinuation": {}}}])
def test_missing_youtube_continuation_preserves_full_cache(tmp_path, continuation):
    item = {"playlistItemData": {"videoId": "abcdefghijk"}, "flexColumns": [
        {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "song"}]}}},
        {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "Radiohead"}]}}}]}
    shelf = {"contents": [{"musicResponsiveListItemRenderer": item}],
             "continuations": [{"nextContinuationData": {"continuation": "next"}}]}
    first = {"contents": {"singleColumnBrowseResultsRenderer": {"tabs": [{"tabRenderer": {
        "content": {"sectionListRenderer": {"contents": [{"musicPlaylistShelfRenderer": shelf}]}}}}]}}}
    session = MagicMock(spec=requests.Session)
    session.post.side_effect = [response(first), response(continuation)]
    service, source = youtube_service(tmp_path, session)
    tracks = service.fetch_sources([source], False)
    assert [track.title for track in tracks] == ["one", "two"]
    assert service.last_report.sources[0].stale
    assert isinstance(service.last_report.sources[0].error, SourceTemporaryError)
    assert len(SourceCache(service.cache_path).read("youtube", source, YouTubeAdapter.cache_schema_version)[0]) == 2


def test_youtube_quota_preserves_cache_and_cools_down_provider(tmp_path):
    session = MagicMock(spec=requests.Session)
    session.get.return_value = response({"error": {"errors": [{"reason": "quotaExceeded"}]}}, 403)
    service, source = youtube_service(tmp_path, session)
    service.youtube_api_key = "test-key"
    tracks = service.fetch_sources([source], False)
    assert len(tracks) == 2
    state = service.last_report.sources[0]
    assert state.stale and isinstance(state.error, SourceTemporaryError)
    assert state.error.status_code == 403
    assert state.retry_at is not None
    service.fetch_sources([source, source + "Other"], False)
    assert session.get.call_count == 1


def test_youtube_invalid_key_still_raises_auth_error():
    error = requests.HTTPError(response=response({"error": {"errors": [{"reason": "keyInvalid"}]}}, 403))
    with pytest.raises(SourceAuthError):
        YouTubeAdapter()._translate_request_error(error, "request failed")


@pytest.mark.parametrize("artist", ["Tyler, The Creator", "Earth, Wind & Fire", "Simon & Garfunkel"])
def test_matching_youtube_channel_preserves_full_artist(artist):
    title, artists, confidence = YouTubeMetadataNormalizer.extract_artist_and_title(
        artist + " - Song", channel_title=artist)
    assert title == "Song" and artists == [artist]
    assert confidence == "reliable_parsed"


def test_null_local_json_duration_uses_playback_estimate(tmp_path):
    path = tmp_path / "tracks.json"
    path.write_text(json.dumps([{"artist": "artist", "title": "track", "duration_ms": None}]), encoding="utf-8")
    tracks = load_local_fallback_tracks(path)
    assert len(tracks) == 1 and tracks[0].duration_sec == 180
    assert tracks[0].formatted_duration == "3m 00s"


@pytest.mark.parametrize("status", ["11", "16", "29"])
def test_web_auth_retries_temporary_api_error_with_same_url(status):
    generator = MagicMock()
    generator.get_web_auth_session_key_username.side_effect = [
        pylast.WSError(None, status, "temporarily unavailable"), ("session", "user")]
    stop = MagicMock()
    stop.is_set.return_value = False
    stop.wait.return_value = False
    assert poll_web_auth(generator, "auth-url", stop) == ("session", "user")
    assert generator.get_web_auth_session_key_username.call_count == 2
    assert all(call.args == ("auth-url",) for call in generator.get_web_auth_session_key_username.call_args_list)
