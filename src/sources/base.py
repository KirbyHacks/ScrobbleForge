"""abstract base class for provider-independent source clients."""
from abc import ABC, abstractmethod
from typing import List

from src.core.models import CanonicalTrack


class SourceClient(ABC):
    """read music sources into shared tracks. keep order and raise errors on failure."""

    provider_id: str

    @classmethod
    @abstractmethod
    def supports_url(cls, url: str) -> bool:
        """check a reference locally without network calls."""
        ...

    @abstractmethod
    def fetch_sources(
        self,
        urls: List[str],
        use_cache_if_available: bool = True,
    ) -> List[CanonicalTrack]:
        """read source tracks in order. raise a source error if loading fails."""
        ...

    @abstractmethod
    def check_health(self) -> bool:
        """run a lightweight health check to verify upstream provider connectivity."""
        ...

    def enrich_track_metadata(self, track) -> None:
        """leave optional metadata unchanged when no lookup is available."""
        return None

    def load_legacy_cache(self, urls: List[str]):
        """return an older cache and its source breakdown when supported."""
        return None
