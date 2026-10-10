from dataclasses import dataclass
from typing import Optional


@dataclass
class Track:
    title: str
    artist: str
    album: str = ""
    album_artist: str = ""
    duration_ms: int = 180000  # default to 3 minutes if duration is not known
    track_number: int = 1
    spotify_id: Optional[str] = None
    source_name: str = ""

    @property
    def duration_sec(self) -> int:
        """returns track duration in seconds."""
        return int(self.duration_ms / 1000)

    @property
    def formatted_duration(self) -> str:
        """returns formatted duration like 3m 42s."""
        sec = self.duration_sec
        minutes = sec // 60
        seconds = sec % 60
        return f"{minutes}m {seconds:02d}s"

    @property
    def display_name(self) -> str:
        """returns artist and title formatted for display."""
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
