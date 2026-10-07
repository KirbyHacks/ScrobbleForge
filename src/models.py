from dataclasses import dataclass
from typing import Optional


@dataclass
class Track:
    title: str
    artist: str
    album: str = ""
    album_artist: str = ""
    duration_ms: int = 180000  # Default 3 mins if unknown
    track_number: int = 1
    spotify_id: Optional[str] = None
    source_name: str = ""

    @property
    def duration_sec(self) -> int:
        """Returns track duration in seconds (enforcing minimum 30 seconds)."""
        sec = int(self.duration_ms / 1000)
        return max(30, sec)

    @property
    def formatted_duration(self) -> str:
        """Returns human-readable duration, e.g. '3m 42s'."""
        sec = self.duration_sec
        minutes = sec // 60
        seconds = sec % 60
        return f"{minutes}m {seconds:02d}s"

    @property
    def display_name(self) -> str:
        """Returns formatted 'Artist - Title' string."""
        return f"{self.artist} - {self.title}"

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "album_artist": self.album_artist,
            "duration_ms": self.duration_ms,
            "track_number": self.track_number,
            "spotify_id": self.spotify_id,
            "source_name": self.source_name,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Track":
        return cls(
            title=data.get("title", "Unknown Title"),
            artist=data.get("artist", "Unknown Artist"),
            album=data.get("album", ""),
            album_artist=data.get("album_artist", ""),
            duration_ms=data.get("duration_ms", 180000),
            track_number=data.get("track_number", 1),
            spotify_id=data.get("spotify_id"),
            source_name=data.get("source_name", ""),
        )
