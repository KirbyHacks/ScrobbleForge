"""canonical track representation and legacy model conversion adapters."""
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from src.models import Track


@dataclass(frozen=True)
class CanonicalTrack:
    """track data shared by music sources. nested lists and dictionaries can still change."""

    title: str
    artists: List[str]
    album: Optional[str] = None
    duration_ms: Optional[int] = None
    isrc: Optional[str] = None
    external_ids: Dict[str, str] = field(default_factory=dict)
    provider_id: str = "unknown"
    source_uri: Optional[str] = None
    source_name: Optional[str] = None
    album_artist: Optional[str] = None
    track_number: Optional[int] = None

    @property
    def primary_artist(self) -> str:
        """return primary artist preserving original provider ordering, or fallback."""
        return self.artists[0] if self.artists else "Unknown Artist"


def valid_source_track(track: CanonicalTrack, provider: str) -> bool:
    """check provider output before it reaches the playback queue."""
    if not isinstance(track, CanonicalTrack) or track.provider_id != provider:
        return False
    if not isinstance(track.title, str) or not isinstance(track.artists, list):
        return False
    if not all(isinstance(artist, str) for artist in track.artists):
        return False
    if not isinstance(track.external_ids, dict):
        return False
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in track.external_ids.items()):
        return False
    if any(value is not None and not isinstance(value, str) for value in
           (track.album, track.album_artist, track.source_uri, track.source_name, track.isrc)):
        return False
    if track.duration_ms is not None and (type(track.duration_ms) is not int or track.duration_ms < 0):
        return False
    if track.track_number is not None and (type(track.track_number) is not int or track.track_number <= 0):
        return False
    return True


def to_canonical_track(
    track: "Track",
    provider_id: str = "spotify",
    source_uri: Optional[str] = None,
) -> CanonicalTrack:
    """copy an existing track into the shared source model."""
    artists: List[str] = []
    if track.artist and track.artist.strip():
        artists.append(track.artist.strip())

    external_ids: Dict[str, str] = {}
    if track.spotify_id:
        external_ids["spotify"] = track.spotify_id

    resolved_source_uri = source_uri
    if not resolved_source_uri:
        if track.source_name and (track.source_name.lower().startswith("spotify:") or "://" in track.source_name):
            resolved_source_uri = track.source_name
        elif track.spotify_id:
            resolved_source_uri = f"spotify:track:{track.spotify_id}"
        elif track.source_name and ":" in track.source_name and not track.source_name.startswith("http"):
            parts = track.source_name.split(":", 1)
            if parts[0].isalnum() and " " not in parts[0]:
                resolved_source_uri = track.source_name

    # prefer explicit provider info supplied on track or by caller without registry dependency
    resolved_provider_id = getattr(track, "provider_id", None) or provider_id

    return CanonicalTrack(
        title=track.title,
        artists=artists,
        album=track.album if track.album else None,
        duration_ms=track.duration_ms if track.duration_ms > 0 else None,
        isrc=None,
        external_ids=external_ids,
        provider_id=resolved_provider_id,
        source_uri=resolved_source_uri,
        source_name=track.source_name if track.source_name else None,
        album_artist=getattr(track, "album_artist", None) or None,
        track_number=getattr(track, "track_number", None),
    )


def to_legacy_track(
    canonical: CanonicalTrack,
    source_name: str = "",
    track_number: Optional[int] = None,
) -> "Track":
    """copy source metadata into the track used by the engine."""
    from src.models import Track

    resolved_source_name = source_name or canonical.source_name or canonical.source_uri or ""
    spotify_id = canonical.external_ids.get("spotify")
    provider_id = canonical.provider_id if canonical.provider_id != "unknown" else None

    resolved_track_number = canonical.track_number if canonical.track_number is not None else track_number
    resolved_album_artist = canonical.album_artist if canonical.album_artist else canonical.primary_artist

    return Track(
        title=canonical.title,
        artist=canonical.primary_artist,
        album=canonical.album or "",
        album_artist=resolved_album_artist,
        duration_ms=canonical.duration_ms if canonical.duration_ms is not None else 180000,
        track_number=resolved_track_number,
        spotify_id=spotify_id,
        source_name=resolved_source_name,
        provider_id=provider_id,
    )
