"""unit tests for CanonicalTrack and conversion adapters."""
from dataclasses import FrozenInstanceError
import sys
from pathlib import Path
import unittest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.unit._dependency_guard import ensure_dependencies_mocked

ensure_dependencies_mocked()

from src.core.models import CanonicalTrack, to_canonical_track, to_legacy_track
from src.models import Track


class TestCanonicalTrack(unittest.TestCase):
    def test_construction_and_defaults(self):
        track = CanonicalTrack(
            title="Starman",
            artists=["David Bowie"],
        )
        self.assertEqual(track.title, "Starman")
        self.assertEqual(track.artists, ["David Bowie"])
        self.assertIsNone(track.album)
        self.assertIsNone(track.duration_ms)
        self.assertIsNone(track.isrc)
        self.assertEqual(track.external_ids, {})
        self.assertEqual(track.provider_id, "unknown")
        self.assertIsNone(track.source_uri)
        self.assertEqual(track.primary_artist, "David Bowie")

    def test_artist_ordering_preserved(self):
        track = CanonicalTrack(
            title="Under Pressure",
            artists=["Queen", "David Bowie"],
            album="Hot Space",
            duration_ms=248000,
            provider_id="spotify",
        )
        # primary artist is the first artist in the list
        self.assertEqual(track.primary_artist, "Queen")
        self.assertEqual(track.artists, ["Queen", "David Bowie"])
        self.assertEqual(len(track.artists), 2)

    def test_empty_artists_fallback(self):
        track = CanonicalTrack(
            title="Unknown Track",
            artists=[],
        )
        self.assertEqual(track.primary_artist, "Unknown Artist")
        self.assertEqual(track.artists, [])

    def test_frozen_immutability_and_nested_caveat(self):
        track = CanonicalTrack(
            title="Solar Power",
            artists=["Lorde"],
            external_ids={"spotify": "abc123xyz"},
        )
        # attributes cannot be reassigned directly
        with self.assertRaises(FrozenInstanceError):
            track.title = "New Title"  # type: ignore

        with self.assertRaises(FrozenInstanceError):
            track.album = "New Album"  # type: ignore

        # nested collections remain mutable in Python unless explicitly defended
        track.artists.append("Jack Antonoff")
        self.assertEqual(track.artists, ["Lorde", "Jack Antonoff"])

    def test_to_canonical_track_conversion(self):
        legacy = Track(
            title="Midnight City",
            artist="M83",
            album="Hurry Up, We're Dreaming",
            duration_ms=243000,
            spotify_id="6GyFP1nfCDB87D2E0RJJ97",
            source_name="Synthwave Mix",
        )
        canonical = to_canonical_track(legacy, provider_id="spotify")

        self.assertEqual(canonical.title, "Midnight City")
        self.assertEqual(canonical.artists, ["M83"])
        self.assertEqual(canonical.primary_artist, "M83")
        self.assertEqual(canonical.album, "Hurry Up, We're Dreaming")
        self.assertEqual(canonical.duration_ms, 243000)
        self.assertIsNone(canonical.isrc)  # do not invent missing ISRC
        self.assertEqual(canonical.external_ids, {"spotify": "6GyFP1nfCDB87D2E0RJJ97"})
        self.assertEqual(canonical.provider_id, "spotify")
        self.assertEqual(canonical.source_uri, "spotify:track:6GyFP1nfCDB87D2E0RJJ97")

    def test_to_canonical_track_missing_metadata(self):
        legacy = Track(
            title="Minimal Track",
            artist="",
            album="",
            duration_ms=0,
        )
        canonical = to_canonical_track(legacy)

        self.assertEqual(canonical.title, "Minimal Track")
        self.assertEqual(canonical.artists, [])
        self.assertEqual(canonical.primary_artist, "Unknown Artist")
        self.assertIsNone(canonical.album)
        self.assertIsNone(canonical.duration_ms)
        self.assertIsNone(canonical.isrc)
        self.assertEqual(canonical.external_ids, {})

    def test_to_legacy_track_conversion(self):
        canonical = CanonicalTrack(
            title="Get Lucky",
            artists=["Daft Punk", "Pharrell Williams"],
            album="Random Access Memories",
            duration_ms=248000,
            external_ids={"spotify": "69kOkLUCkxIZYexIgSG8rq"},
            provider_id="spotify",
            source_uri="spotify:playlist:37i9dQZF1DXcBWIGoYBM5M",
        )
        legacy = to_legacy_track(canonical, track_number=5)

        self.assertEqual(legacy.title, "Get Lucky")
        self.assertEqual(legacy.artist, "Daft Punk")
        self.assertEqual(legacy.album, "Random Access Memories")
        self.assertEqual(legacy.album_artist, "Daft Punk")
        self.assertEqual(legacy.duration_ms, 248000)
        self.assertEqual(legacy.track_number, 5)
        self.assertEqual(legacy.spotify_id, "69kOkLUCkxIZYexIgSG8rq")
        self.assertEqual(legacy.source_name, "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M")

    def test_multi_artist_and_isrc_intentional_information_loss(self):
        """verify intentional information loss when converting to legacy Track."""
        canonical = CanonicalTrack(
            title="Under Pressure",
            artists=["Queen", "David Bowie"],
            album="Hot Space",
            duration_ms=248000,
            isrc="GBUM71106512",
            external_ids={"spotify": "11LmqTE2naFULdEP94AUBa"},
            provider_id="spotify",
        )
        legacy = to_legacy_track(canonical)

        # secondary artists are intentionally omitted in legacy single-artist model
        self.assertEqual(legacy.artist, "Queen")
        self.assertEqual(legacy.album_artist, "Queen")
        # legacy Track model does not carry ISRC
        self.assertFalse(hasattr(legacy, "isrc"))

    def test_metadata_roundtrip_fidelity(self):
        """verify round-trip fidelity between legacy and canonical representations."""
        original = Track(
            title="Instant Crush",
            artist="Daft Punk",
            album="Random Access Memories",
            duration_ms=337000,
            spotify_id="2cGx047T2F7Mn2O2b00Ilv",
            source_name="spotify:album:4m2880jivSbbyEGAKfITCa",
        )
        canonical = to_canonical_track(original)
        restored = to_legacy_track(canonical)

        self.assertEqual(restored.title, original.title)
        self.assertEqual(restored.artist, original.artist)
        self.assertEqual(restored.album, original.album)
        self.assertEqual(restored.duration_ms, original.duration_ms)
        self.assertEqual(restored.spotify_id, original.spotify_id)
        self.assertEqual(restored.source_name, original.source_name)


if __name__ == "__main__":
    unittest.main()
