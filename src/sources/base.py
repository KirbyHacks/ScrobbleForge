"""abstract base class for provider-independent source clients."""
from abc import ABC, abstractmethod
from typing import List

from src.core.models import CanonicalTrack


class SourceClient(ABC):
    """abstract base class for provider-independent music source clients.

    subclasses convert upstream music service payloads into CanonicalTrack models.

    guarantees:
    - module imports do not perform network requests.
    - source and track ordering are preserved.
    - failures raise SourceError subclasses and are never swallowed into empty lists.
    """

    provider_id: str

    @classmethod
    @abstractmethod
    def supports_url(cls, url: str) -> bool:
        """check whether this client supports the given url or uri.

        must be a fast local check without network calls.
        """
        ...

    @abstractmethod
    def fetch_sources(
        self,
        urls: List[str],
        use_cache_if_available: bool = True,
    ) -> List[CanonicalTrack]:
        """fetch and transform tracks from the given source urls into canonical tracks.

        preserves source and track ordering without silently swallowing provider errors.
        """
        ...

    @abstractmethod
    def check_health(self) -> bool:
        """run a lightweight health check to verify upstream provider connectivity."""
        ...
