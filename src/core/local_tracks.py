"""read local track lists without a provider client."""
import csv
import json
import logging
from pathlib import Path
from typing import List
from src.models import Track

logger = logging.getLogger("scrobbler.local_tracks")


def load_local_fallback_tracks(file_path: Path) -> List[Track]:
    """load fallback tracks from txt, csv, or json files."""
    path = Path(file_path)
    if not path.is_file():
        return []

    tracks: List[Track] = []
    suffix = path.suffix.lower()

    if suffix == ".json":
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and "artist" in item and "title" in item:
                            tracks.append(Track.from_dict(item))
        except Exception as e:
            logger.warning(f"Could not parse JSON track list: {e}")

    elif suffix == ".csv":
        try:
            with open(path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    title = row.get("Track Name") or row.get("title") or row.get("Track")
                    artist = row.get("Artist Name(s)") or row.get("artist") or row.get("Artist")
                    album = row.get("Album Name") or row.get("album") or ""
                    duration_ms = row.get("Duration (ms)") or row.get("duration_ms")

                    if title and artist:
                        ms = int(duration_ms) if duration_ms and str(duration_ms).isdigit() else 180000
                        tracks.append(Track(
                            title=title.strip(),
                            artist=artist.strip(),
                            album=album.strip(),
                            duration_ms=ms,
                            source_name="Local CSV",
                        ))
        except Exception as e:
            logger.warning(f"Could not parse CSV track list: {e}")

    else:
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if " - " in line:
                        parts = line.split(" - ", 1)
                        tracks.append(Track(artist=parts[0].strip(), title=parts[1].strip(), source_name="Local List"))
        except Exception as e:
            logger.warning(f"Could not parse text track list: {e}")

    return tracks
