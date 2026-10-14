"""source contracts used by the playback engine."""
from dataclasses import dataclass
from typing import List, Optional, Protocol, Tuple

from src.models import Track


@dataclass(frozen=True)
class SourceStatus:
    source: str
    provider_id: str
    track_count: Optional[int]
    saved_at: Optional[float] = None
    stale: bool = False
    error: Optional[Exception] = None
    retry_at: Optional[float] = None


@dataclass(frozen=True)
class SourceLoadReport:
    tracks: Tuple[Track, ...] = ()
    sources: Tuple[SourceStatus, ...] = ()

    @property
    def has_failures(self) -> bool:
        return any(item.error is not None for item in self.sources)


class TrackSource(Protocol):
    def fetch_sources_with_report(self, sources: List[str], use_cache_if_available: bool = True) -> SourceLoadReport: ...
    def fetch_sources(self, sources: List[str], use_cache_if_available: bool = True) -> List[Track]: ...
    def refresh_sources(self, sources: List[str], force: bool = False) -> List[Track]: ...
    def next_refresh_delay(self, sources: List[str]) -> float: ...
    def get_status(self) -> Tuple[SourceStatus, ...]: ...
    def enrich_track_metadata(self, track: Track) -> None: ...
