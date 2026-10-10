"""music source clients, adapters, and factory registry for ScrobbleForge."""
from .base import SourceClient
from .exceptions import (
    SourceAuthError,
    SourceError,
    SourceTemporaryError,
    UnsupportedSourceError,
)
from .service import SourceIngestionService
from .source_factory import FetchReport, SourceFactory
from .spotify_adapter import SpotifyAdapter

__all__ = [
    "SourceClient",
    "SourceFactory",
    "SpotifyAdapter",
    "SourceIngestionService",
    "FetchReport",
    "SourceError",
    "UnsupportedSourceError",
    "SourceAuthError",
    "SourceTemporaryError",
]
