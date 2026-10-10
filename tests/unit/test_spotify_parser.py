import json
import sys
from pathlib import Path
import unittest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.unit._dependency_guard import ensure_dependencies_mocked

ensure_dependencies_mocked()

from src.spotify_client import SpotifyEmbedParser, SpotifyIngestionError
from src.models import Track


def _build_embed_html(entity: dict) -> str:
    """build a Next.js hydration payload enclosed inside HTML markup."""
    payload = {
        "props": {
            "pageProps": {
                "state": {
                    "data": {
                        "entity": entity,
                    }
                }
            }
        }
    }
    return (
        "<!DOCTYPE html><html><head>"
        f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>'
        "</head><body><div id=\"__next\"></div></body></html>"
    )


class TestSpotifyEmbedParser(unittest.TestCase):
    """stateless unit tests for SpotifyEmbedParser without requests mocking."""

    def test_single_track_html_payload(self):
        """verify parsing of single track HTML payload into standardized Track model."""
        entity = {
            "title": "Bohemian Rhapsody",
            "name": "Bohemian Rhapsody",
            "artists": [{"name": "Queen"}],
            "duration": 354320,
            "uri": "spotify:track:4u7EnebtmKWzUH433cf5Qv",
        }
        html = _build_embed_html(entity)

        tracks = SpotifyEmbedParser.parse(html, entity_type="track")

        self.assertEqual(len(tracks), 1)
        track = tracks[0]
        self.assertIsInstance(track, Track)
        self.assertEqual(track.title, "Bohemian Rhapsody")
        self.assertEqual(track.artist, "Queen")
        self.assertEqual(track.album_artist, "Queen")
        self.assertEqual(track.duration_ms, 354320)
        self.assertEqual(track.spotify_id, "4u7EnebtmKWzUH433cf5Qv")
        self.assertEqual(track.track_number, 1)
        self.assertEqual(track.source_name, "Single Track")

    def test_single_track_subtitle_fallback(self):
        """verify fallback to subtitle parsing when artists list is absent."""
        entity = {
            "title": "Under Pressure",
            "subtitle": "Queen,\xa0David Bowie",
            "duration": 248000,
            "uri": "spotify:track:2MS0mK2KjD0p7r6b4aK9jL",
        }
        html = _build_embed_html(entity)

        tracks = SpotifyEmbedParser.parse(html, entity_type="track")

        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0].title, "Under Pressure")
        self.assertEqual(tracks[0].artist, "Queen")
        self.assertEqual(tracks[0].album_artist, "Queen")

    def test_album_html_payload(self):
        """verify parsing of album HTML with multiple tracks in trackList."""
        entity = {
            "name": "A Night at the Opera",
            "type": "album",
            "artists": [{"name": "Queen"}],
            "trackList": [
                {
                    "title": "Death on Two Legs",
                    "artists": [{"name": "Queen"}],
                    "duration": 223000,
                    "uri": "spotify:track:123track",
                },
                {
                    "title": "Lazing on a Sunday Afternoon",
                    "subtitle": "Queen",
                    "duration": 67000,
                    "uri": "spotify:track:456track",
                },
                {
                    "title": "Bohemian Rhapsody",
                    "artists": [{"name": "Queen"}],
                    "duration": 354000,
                    "uri": "spotify:track:789track",
                },
            ],
        }
        html = _build_embed_html(entity)

        tracks = SpotifyEmbedParser.parse(html, entity_type="album")

        self.assertEqual(len(tracks), 3)

        self.assertEqual(tracks[0].title, "Death on Two Legs")
        self.assertEqual(tracks[0].artist, "Queen")
        self.assertEqual(tracks[0].album, "A Night at the Opera")
        self.assertEqual(tracks[0].album_artist, "Queen")
        self.assertEqual(tracks[0].duration_ms, 223000)
        self.assertEqual(tracks[0].track_number, 1)
        self.assertEqual(tracks[0].spotify_id, "123track")
        self.assertEqual(tracks[0].source_name, "A Night at the Opera")

        self.assertEqual(tracks[1].title, "Lazing on a Sunday Afternoon")
        self.assertEqual(tracks[1].artist, "Queen")
        self.assertEqual(tracks[1].album, "A Night at the Opera")
        self.assertEqual(tracks[1].album_artist, "Queen")
        self.assertEqual(tracks[1].track_number, 2)

        self.assertEqual(tracks[2].title, "Bohemian Rhapsody")
        self.assertEqual(tracks[2].track_number, 3)

    def test_playlist_html_payload(self):
        """verify parsing of playlist HTML payload with multiple tracks."""
        entity = {
            "name": "Synthwave Chill",
            "type": "playlist",
            "trackList": [
                {
                    "title": "Resonance",
                    "artists": [{"name": "HOME"}],
                    "duration": 212000,
                    "uri": "spotify:track:1TuopWDI4CuAe502grR32v",
                },
                {
                    "title": "Sunset",
                    "subtitle": "The Midnight",
                    "duration": 326000,
                    "uri": "spotify:track:4y3OI8hC5z0mQv3aF1U91b",
                },
            ],
        }
        html = _build_embed_html(entity)

        tracks = SpotifyEmbedParser.parse(html, entity_type="playlist")

        self.assertEqual(len(tracks), 2)
        self.assertEqual(tracks[0].title, "Resonance")
        self.assertEqual(tracks[0].artist, "HOME")
        self.assertEqual(tracks[0].track_number, 1)
        self.assertEqual(tracks[0].source_name, "Synthwave Chill")

        self.assertEqual(tracks[1].title, "Sunset")
        self.assertEqual(tracks[1].artist, "The Midnight")
        self.assertEqual(tracks[1].track_number, 2)
        self.assertEqual(tracks[1].source_name, "Synthwave Chill")

    def test_playlist_payload_with_pagination_and_nested_tracks(self):
        """verify playlist parsing when tracks are wrapped in tracks.items with pagination."""
        entity = {
            "name": "Weekly Mix",
            "type": "playlist",
            "tracks": {
                "items": [
                    {
                        "track": {
                            "name": "Track One",
                            "artists": [{"name": "Artist 1"}],
                            "duration_ms": 190000,
                            "uri": "spotify:track:alpha1",
                        }
                    },
                    {
                        "track": {
                            "title": "Track Two",
                            "artists": [{"name": "Artist 2"}],
                            "duration": 215000,
                            "id": "beta2",
                        }
                    },
                ],
                "total": 50,
                "limit": 25,
                "offset": 0,
                "next": "https://api.spotify.com/v1/playlists/xyz/tracks?offset=25&limit=25",
            },
        }
        html = _build_embed_html(entity)

        tracks = SpotifyEmbedParser.parse(html)

        self.assertEqual(len(tracks), 2)
        self.assertEqual(tracks[0].title, "Track One")
        self.assertEqual(tracks[0].artist, "Artist 1")
        self.assertEqual(tracks[0].spotify_id, "alpha1")
        self.assertEqual(tracks[0].duration_ms, 190000)

        self.assertEqual(tracks[1].title, "Track Two")
        self.assertEqual(tracks[1].artist, "Artist 2")
        self.assertEqual(tracks[1].spotify_id, "beta2")
        self.assertEqual(tracks[1].duration_ms, 215000)

    def test_direct_json_string_parsing(self):
        """verify direct JSON parsing without requiring HTML script tags."""
        payload = {
            "props": {
                "pageProps": {
                    "state": {
                        "data": {
                            "entity": {
                                "title": "Direct Track",
                                "artists": [{"name": "Direct Artist"}],
                                "duration": 185000,
                                "uri": "spotify:track:direct123",
                            }
                        }
                    }
                }
            }
        }
        raw_json = json.dumps(payload)

        tracks = SpotifyEmbedParser.parse(raw_json)

        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0].title, "Direct Track")
        self.assertEqual(tracks[0].artist, "Direct Artist")
        self.assertEqual(tracks[0].spotify_id, "direct123")

    def test_missing_next_data_raises_ingestion_error(self):
        """verify SpotifyIngestionError when HTML contains no __NEXT_DATA__ tag."""
        html = "<html><head><title>Error</title></head><body><h1>404 Not Found</h1></body></html>"
        with self.assertRaises(SpotifyIngestionError) as ctx:
            SpotifyEmbedParser.parse(html)
        self.assertIn("No metadata found", str(ctx.exception))

    def test_malformed_json_raises_ingestion_error(self):
        """verify SpotifyIngestionError when JSON in __NEXT_DATA__ is malformed."""
        html = '<html><head><script id="__NEXT_DATA__" type="application/json">{ broken: json </script></head></html>'
        with self.assertRaises(SpotifyIngestionError) as ctx:
            SpotifyEmbedParser.parse(html)
        self.assertIn("Failed to parse embed JSON", str(ctx.exception))

    def test_empty_or_whitespace_content_raises_ingestion_error(self):
        """verify SpotifyIngestionError when content is empty or whitespace."""
        with self.assertRaises(SpotifyIngestionError):
            SpotifyEmbedParser.parse("")
        with self.assertRaises(SpotifyIngestionError):
            SpotifyEmbedParser.parse("   \n\t  ")

    def test_empty_entity_payload_returns_empty_list(self):
        """verify returning empty list when entity dictionary is empty."""
        html = _build_embed_html({})
        tracks = SpotifyEmbedParser.parse(html)
        self.assertEqual(tracks, [])

    def test_missing_fields_defaults(self):
        """verify graceful fallback defaults for missing duration, album artist, and artist."""
        entity = {
            "title": "",
            "name": None,
            "artists": [],
            "subtitle": "",
            "duration": None,
            "uri": "",
        }
        html = _build_embed_html(entity)

        tracks = SpotifyEmbedParser.parse(html, entity_type="track", default_id="fallback_id")

        self.assertEqual(len(tracks), 1)
        track = tracks[0]
        self.assertEqual(track.title, "Unknown Track")
        self.assertEqual(track.artist, "Unknown Artist")
        self.assertEqual(track.album_artist, "Unknown Artist")
        self.assertEqual(track.duration_ms, 180000)
        self.assertEqual(track.spotify_id, "fallback_id")

    def test_duration_normalization_variants(self):
        """verify normalize_duration handles various positive, negative, null, and malformed inputs."""
        self.assertEqual(SpotifyEmbedParser.normalize_duration(210000), 210000)
        self.assertEqual(SpotifyEmbedParser.normalize_duration("195000"), 195000)
        self.assertEqual(SpotifyEmbedParser.normalize_duration(None), 180000)
        self.assertEqual(SpotifyEmbedParser.normalize_duration(0), 180000)
        self.assertEqual(SpotifyEmbedParser.normalize_duration(-500), 180000)
        self.assertEqual(SpotifyEmbedParser.normalize_duration("invalid"), 180000)

    def test_artist_normalization_variants(self):
        """verify extract_artist handles lists of dicts, lists of strings, dicts, strings, and subtitles."""
        self.assertEqual(SpotifyEmbedParser.extract_artist([{"name": "Artist A"}]), "Artist A")
        self.assertEqual(SpotifyEmbedParser.extract_artist(["Artist B"]), "Artist B")
        self.assertEqual(SpotifyEmbedParser.extract_artist({"name": "Artist C"}), "Artist C")
        self.assertEqual(SpotifyEmbedParser.extract_artist("Artist D"), "Artist D")
        self.assertEqual(
            SpotifyEmbedParser.extract_artist(None, subtitle="Daft Punk,\xa0Pharrell Williams"),
            "Daft Punk",
        )
        self.assertEqual(SpotifyEmbedParser.extract_artist([], subtitle=""), "Unknown Artist")

    def test_type_inference_without_explicit_entity_type(self):
        """verify automatic detection of entity type when entity_type is omitted."""
        track_html = _build_embed_html({
            "title": "Solo Track",
            "artists": [{"name": "Solo Artist"}],
            "duration": 200000,
            "uri": "spotify:track:abc",
        })
        tracks = SpotifyEmbedParser.parse(track_html)
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0].title, "Solo Track")

        album_html = _build_embed_html({
            "name": "Solo Album",
            "type": "album",
            "trackList": [{"title": "Track 1", "artists": [{"name": "Artist"}]}],
        })
        tracks = SpotifyEmbedParser.parse(album_html)
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0].album, "Solo Album")

        playlist_html = _build_embed_html({
            "name": "Solo Playlist",
            "type": "playlist",
            "trackList": [{"title": "Track A", "artists": [{"name": "Artist"}]}],
        })
        tracks = SpotifyEmbedParser.parse(playlist_html)
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0].source_name, "Solo Playlist")


if __name__ == "__main__":
    unittest.main()
