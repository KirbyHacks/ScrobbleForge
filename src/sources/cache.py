"""persist each source without changing another source's age."""
import hashlib
import json
import logging
import math
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src.core.models import CanonicalTrack, valid_source_track

logger = logging.getLogger("scrobbler.sources.cache")


class SourceCache:
    schema_version = 1

    def __init__(self, path: Optional[Path]):
        self.path = path
        self.entries: Dict[str, dict] = {}
        if path and path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("schema_version") == self.schema_version and isinstance(data.get("entries"), dict):
                    self.entries = data["entries"]
            except (OSError, ValueError, AttributeError) as exc:
                logger.warning("Could not read source cache: %s", exc)

    @staticmethod
    def key(provider: str, source: str) -> str:
        return hashlib.sha256(json.dumps([provider, source], ensure_ascii=False).encode("utf-8")).hexdigest()

    def read(self, provider: str, source: str, version: int) -> Optional[Tuple[List[CanonicalTrack], float]]:
        entry = self.entries.get(self.key(provider, source))
        if not isinstance(entry, dict):
            return None
        try:
            if (entry.get("provider"), entry.get("source"), entry.get("version")) != (provider, source, version):
                return None
            saved_at = float(entry["saved_at"])
            if not math.isfinite(saved_at) or saved_at < 0 or saved_at > time.time():
                return None
            tracks = []
            if not isinstance(entry["tracks"], list):
                return None
            for item in entry["tracks"]:
                track = CanonicalTrack(**item)
                if not valid_source_track(track, provider):
                    return None
                tracks.append(track)
            return tracks, saved_at
        except (KeyError, ValueError, TypeError):
            return None

    def write(self, provider: str, source: str, version: int, tracks: List[CanonicalTrack], saved_at: float,
              persist: bool = True) -> None:
        self.entries[self.key(provider, source)] = {
            "provider": provider, "source": source, "version": version,
            "saved_at": saved_at, "tracks": [asdict(track) for track in tracks],
        }
        if persist:
            self._save()

    def write_many(self, entries) -> None:
        for entry in entries:
            self.write(*entry, persist=False)
        if entries:
            self._save()

    def _save(self) -> None:
        if not self.path:
            return
        tmp = self.path.with_name(f"{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tmp.open("w", encoding="utf-8") as stream:
                json.dump({"schema_version": self.schema_version, "entries": self.entries}, stream, ensure_ascii=False)
            tmp.replace(self.path)
        except (OSError, ValueError, TypeError) as exc:
            logger.warning("Could not save source cache: %s", exc)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
