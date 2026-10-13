"""integration regressions for the v1.1 delivery and provider fixes."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from xml.dom import minidom

import requests

from src.config import AppConfig, ConfigError, EngineConfig, LastFMConfig, SpotifyConfig
from src.engine import ScrobblerEngine
from src.lastfm_client import LastFMClient, LastFMTemporaryError
from src.models import Track
from src.queue_manager import QueueManager
from src.quota_tracker import QuotaTracker
from src.sources.exceptions import SourceAuthError, SourceError, SourceTemporaryError, extract_http_metadata
from src.sources.source_factory import SourceFactory
from src.sources.spotify_adapter import SpotifyAdapter
from src.core.models import CanonicalTrack
from src.sources.youtube_adapter import YouTubeAdapter, YouTubeMetadataNormalizer
from src.spotify_client import SpotifyClient, SpotifyIngestionError


def acknowledgement(accepted="1", ignored="0", code="0"):
    return (
        f'<lfm status="ok"><scrobbles accepted="{accepted}" ignored="{ignored}">'
        f'<scrobble><ignoredMessage code="{code}"/></scrobble></scrobbles></lfm>'
    )


class TestLastFMAcknowledgements(unittest.TestCase):
    def setUp(self):
        self.client = LastFMClient.__new__(LastFMClient)
        self.client.network = MagicMock()
        self.track = Track(title="Track", artist="Artist")

    def submit_xml(self, xml):
        with patch("src.lastfm_client.pylast._Request") as request:
            request.return_value.execute.return_value = minidom.parseString(xml)
            return self.client.scrobble(self.track, timestamp=1000)

    def test_acceptance_and_explicit_ignore(self):
        self.assertTrue(self.submit_xml(acknowledgement()))
        self.assertFalse(self.submit_xml(acknowledgement("0", "1", "3")))

    def test_compilation_metadata_reaches_lastfm_and_unknown_number_is_omitted(self):
        for number in (7, None):
            with self.subTest(number=number), patch("src.lastfm_client.pylast._Request") as request:
                request.return_value.execute.return_value = minidom.parseString(acknowledgement())
                self.client.scrobble(Track(
                    title="Compilation Track", artist="Solo Artist", album="Compilation",
                    album_artist="Various Artists", track_number=number,
                ), timestamp=1000)
                params = request.call_args.args[2]
                self.assertEqual(params["albumArtist[0]"], "Various Artists")
                if number is None:
                    self.assertNotIn("trackNumber[0]", params)
                else:
                    self.assertEqual(params["trackNumber[0]"], "7")

    def test_incomplete_or_inconsistent_response_is_retryable(self):
        malformed = [
            '<lfm status="ok"><scrobbles/></lfm>',
            '<lfm status="ok"><scrobbles accepted="0" ignored="0"/></lfm>',
            '<lfm status="ok"><scrobbles accepted="1" ignored="0"/></lfm>',
            '<lfm status="ok"><scrobbles accepted="1" ignored="0"><scrobble/></scrobbles></lfm>',
            acknowledgement("", "0", "0"),
            acknowledgement("invalid", "0", "0"),
            acknowledgement("1", "0", ""),
            acknowledgement("1", "0", "3"),
            acknowledgement("0", "1", "0"),
            acknowledgement("0", "0", "0"),
            acknowledgement("1", "1", "0"),
            acknowledgement("-1", "2", "3"),
            acknowledgement("2", "0", "0"),
            acknowledgement().replace('</scrobbles>', '<scrobble><ignoredMessage code="0"/></scrobble></scrobbles>'),
        ]
        for xml in malformed:
            with self.subTest(xml=xml), self.assertRaises(LastFMTemporaryError):
                self.submit_xml(xml)

    def test_incomplete_acknowledgement_never_commits_queue_or_quota(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = QuotaTracker(Path(directory) / "state.db")
            queue = QueueManager([self.track], tracker, shuffle=False, loop=False)
            tracker.set_state("queue_index", "0")
            config = AppConfig(
                lastfm=LastFMConfig(api_key="mock", api_secret="mock"),
                spotify=SpotifyConfig(),
                engine=EngineConfig(mode="custom_interval", loop=False),
            )
            engine = ScrobblerEngine(
                config, self.client, queue=queue, tracker=tracker,
                stop_event=threading.Event(), circuit_breaker_threshold=1,
            )
            with patch("src.lastfm_client.pylast._Request") as request:
                request.return_value.execute.return_value = minidom.parseString(
                    '<lfm status="ok"><scrobbles accepted="0" ignored="0"/></lfm>'
                )
                engine.run()
            self.assertIs(engine.pending_track, self.track)
            self.assertEqual(tracker.get_state("queue_index"), "0")
            self.assertEqual(tracker.get_total_scrobbles(), 0)


class TestSpotifyTransportAndCache(unittest.TestCase):
    @staticmethod
    def response(status):
        response = requests.Response()
        response.status_code = status
        response.url = "https://open.spotify.com/embed/track/mock"
        response.headers["Retry-After"] = "45"
        response._content = b"<html>Request rejected</html>"
        return response

    def test_actual_http_errors_reach_adapter_without_parsing_error_bodies(self):
        for status, expected_type in ((403, SourceAuthError), (429, SourceTemporaryError), (503, SourceTemporaryError)):
            with self.subTest(status=status):
                client = SpotifyClient(retries=2)
                with patch("src.spotify_client.requests.get", return_value=self.response(status)) as get, patch(
                    "src.spotify_client.time.sleep"
                ), patch("src.spotify_client.SpotifyEmbedParser.parse") as parse:
                    with self.assertRaises(expected_type) as caught:
                        SpotifyAdapter(client=client).fetch_sources(["spotify:track:mock"], False)
                self.assertEqual(extract_http_metadata(caught.exception), (status, 45))
                self.assertEqual(get.call_count, 2 if status == 503 else 1)
                parse.assert_not_called()

    def test_provider_wide_http_failure_avoids_per_source_fallback(self):
        for status in (403, 429):
            with self.subTest(status=status), patch(
                "src.spotify_client.requests.get", return_value=self.response(status)
            ) as get:
                report = SourceFactory.fetch_sources_with_report(
                    ["spotify:track:one", "spotify:track:two"],
                    use_cache_if_available=False,
                )
                self.assertEqual(len(report.failed_sources), 2)
                self.assertEqual(report.tracks, [])
                self.assertEqual(extract_http_metadata(report.failed_sources[0][1]), (status, 45))
                get.assert_called_once()

    def test_transient_retry_can_recover_with_valid_response(self):
        success = self.response(200)
        success._content = b'{"title":"Song","artists":[{"name":"Artist"}],"duration":180000}'
        with patch("src.spotify_client.requests.get", side_effect=[self.response(503), success]) as get, patch(
            "src.spotify_client.time.sleep"
        ):
            tracks = SpotifyClient(retries=2).fetch_embed_tracks("track", "mock")
        self.assertEqual([track.title for track in tracks], ["Song"])
        self.assertEqual(get.call_count, 2)

    def test_failed_retry_after_http_response_never_parses_previous_body(self):
        with patch("src.spotify_client.requests.get", side_effect=[self.response(503), requests.Timeout("timeout")]), patch(
            "src.spotify_client.time.sleep"
        ), patch("src.spotify_client.SpotifyEmbedParser.parse") as parse:
            with self.assertRaises(SpotifyIngestionError) as caught:
                SpotifyClient(retries=2).fetch_embed_tracks("track", "mock")
        self.assertIsInstance(caught.exception.__cause__, requests.Timeout)
        parse.assert_not_called()

    def test_cache_ordering_respects_ttl_and_explicit_stale_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            client = SpotifyClient(cache_path=Path(directory) / "cache.json", ttl_hours=1)
            sources = ["spotify:playlist:A", "spotify:playlist:B"]
            tracks = [Track(title=t, artist="Artist") for t in ("A", "B")]
            with patch("src.spotify_client.time.time", return_value=1000):
                client.save_cache(tracks, sources, {sources[0].lower(): tracks[:1], sources[1].lower(): tracks[1:]})
            with patch("src.spotify_client.time.time", return_value=1500):
                self.assertEqual([t.title for t in client.load_cache(sources[::-1])], ["B", "A"])
            with patch("src.spotify_client.time.time", return_value=4600):
                self.assertEqual(client.load_cache(sources), [])
                self.assertEqual(client.load_cache(sources[::-1]), [])
            with patch("src.spotify_client.time.time", return_value=10000), patch.object(
                client, "fetch_embed_tracks", side_effect=requests.ConnectionError("offline")
            ):
                self.assertEqual([t.title for t in client.fetch_sources(sources[::-1], False)], ["B", "A"])


class TestFiniteEnvironmentConfiguration(unittest.TestCase):
    def test_nonfinite_values_rejected_for_every_float_setting(self):
        names = (
            "SPOTIFY_REFRESH_INTERVAL_HOURS", "SOURCES_REFRESH_INTERVAL_HOURS",
            "INTER_TRACK_PAUSE_MIN", "INTER_TRACK_PAUSE_MAX",
        )
        for name in names:
            for value in ("nan", "NaN", "inf", "-inf", "Infinity", "1e999"):
                with self.subTest(name=name, value=value), patch.dict(
                    "os.environ", {name: value}, clear=True
                ), patch("src.config.load_dotenv"):
                    with self.assertRaises(ConfigError) as caught:
                        AppConfig.from_env()
                    self.assertIn(name, str(caught.exception))
                    self.assertIn("finite", str(caught.exception))


class TestIntegratedTopicIdentity(unittest.TestCase):
    def test_explicit_topic_artist_with_punctuation_remains_one_artist(self):
        for artist in ("Tyler, The Creator", "Earth, Wind & Fire", "Simon & Garfunkel"):
            with self.subTest(artist=artist):
                title, artists, confidence = YouTubeMetadataNormalizer.extract_artist_and_title(
                    f"{artist} - Song - Extended Mix", channel_title=f"{artist} - Topic"
                )
                self.assertEqual(title, "Song - Extended Mix")
                self.assertEqual(artists, [artist])
                self.assertEqual(confidence, "reliable_parsed")


class TestYouTubeReleaseQuality(unittest.TestCase):
    def test_ambiguous_single_video_does_not_use_uploader_as_artist(self):
        for method in ("player", "oembed", "data_api"):
            with self.subTest(method=method):
                session = MagicMock(spec=requests.Session)
                response = MagicMock()
                response.raise_for_status.return_value = None
                session.post.return_value = response
                session.get.return_value = response
                adapter = YouTubeAdapter(session=session)
                if method == "player":
                    response.json.return_value = {"videoDetails": {
                        "title": "My Summer Vacation", "author": "GenericVlogger", "lengthSeconds": "200"
                    }}
                    fetch = adapter._fetch_track_youtubei
                elif method == "oembed":
                    response.json.return_value = {"title": "My Summer Vacation", "author_name": "GenericVlogger"}
                    fetch = adapter._fetch_track_oembed
                else:
                    response.json.return_value = {"items": [{"snippet": {
                        "title": "My Summer Vacation", "channelTitle": "GenericVlogger"
                    }, "contentDetails": {"duration": "PT3M20S"}}]}
                    fetch = adapter._fetch_track_data_api_v3
                with self.assertRaises(SourceError):
                    fetch("mock", "https://youtu.be/mock")

    def test_schema_two_artist_mappings_are_invalidated(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "cache.json"
            adapter = YouTubeAdapter(cache_path=cache)
            urls = ["https://youtu.be/mock"]
            adapter._save_cache([CanonicalTrack(title="Old Mapping", artists=["Wrong Artist"])], urls)
            payload = json.loads(cache.read_text(encoding="utf-8"))
            payload["schema_version"] = 2
            cache.write_text(json.dumps(payload), encoding="utf-8")
            self.assertIsNone(adapter._load_cache(urls))

    def test_forced_refresh_persists_metadata_and_never_reads_stale_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = YouTubeAdapter(cache_path=Path(directory) / "cache.json")
            urls = ["https://youtu.be/mock"]
            old = CanonicalTrack(title="Old", artists=["Artist"])
            fresh = CanonicalTrack(
                title="Fresh", artists=["Artist"], album_artist="Various Artists", track_number=7
            )
            adapter._save_cache([old], urls)
            with patch.object(adapter, "_load_cache", side_effect=AssertionError("read stale cache")), patch.object(
                adapter, "_fetch_track_youtubei", return_value=[fresh]
            ):
                self.assertEqual(adapter.fetch_sources(urls, False), [fresh])
            self.assertEqual(adapter._load_cache(urls), [fresh])


if __name__ == "__main__":
    unittest.main()
