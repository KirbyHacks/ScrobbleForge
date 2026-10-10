"""canonical track representation and legacy model conversion adapters."""
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from src.models import Track


@dataclass(frozen=True)
class CanonicalTrack:
    """canonical, provider-independent representation of a track.

    frozen prevents reassigning top-level fields, but nested lists and dicts
    are not deeply immutable in Python. treat them as read-only.
    """

    title: str
    artists: List[str]
    album: Optional[str] = None
    duration_ms: Optional[int] = None
    isrc: Optional[str] = None
    external_ids: Dict[str, str] = field(default_factory=dict)
    provider_id: str = "unknown"
    source_uri: Optional[str] = None
    source_name: Optional[str] = None

    @property
    def primary_artist(self) -> str:
        """return primary artist preserving original provider ordering, or fallback."""
        return self.artists[0] if self.artists else "Unknown Artist"


def to_canonical_track(
    track: "Track",
    provider_id: str = "spotify",
    source_uri: Optional[str] = None,
) -> CanonicalTrack:
    """convert a legacy Track model into a CanonicalTrack.

    preserves artist ordering and does not invent missing metadata like ISRC.
    """
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

    # prefer explicit provider info supplied on Track or by caller without registry dependency
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
    )


def to_legacy_track(
    canonical: CanonicalTrack,
    source_name: str = "",
    track_number: int = 1,
) -> "Track":
    """convert a CanonicalTrack into a legacy Track model for backward compatibility."""
    from src.models import Track

    resolved_source_name = source_name or canonical.source_name or canonical.source_uri or ""
    spotify_id = canonical.external_ids.get("spotify")
    provider_id = canonical.provider_id if canonical.provider_id != "unknown" else None

    return Track(
        title=canonical.title,
        artist=canonical.primary_artist,
        album=canonical.album or "",
        album_artist=canonical.primary_artist,
        duration_ms=canonical.duration_ms if canonical.duration_ms is not None else 180000,
        track_number=track_number,
        spotify_id=spotify_id,
        source_name=resolved_source_name,
        provider_id=provider_id,
    )
