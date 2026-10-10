"""factory and registry for provider-independent music sources."""
from dataclasses import dataclass, field
import inspect
import logging
from typing import Dict, List, Optional, Tuple, Type
from urllib.parse import urlparse

from src.core.models import CanonicalTrack
from .base import SourceClient
from .exceptions import (
    SourceAuthError,
    SourceError,
    SourceTemporaryError,
    UnsupportedSourceError,
    extract_http_metadata,
)
from .spotify_adapter import SpotifyAdapter

logger = logging.getLogger("scrobbler.sources.factory")


@dataclass
class FetchReport:
    """detailed report for source fetching with partial failure support."""

    tracks: List[CanonicalTrack] = field(default_factory=list)
    failed_sources: List[Tuple[str, Exception]] = field(default_factory=list)

    @property
    def has_failures(self) -> bool:
        return len(self.failed_sources) > 0


def _is_provider_wide_failure(exc: Exception) -> bool:
    """determine if a failure applies provider-wide, making per-source retries wasteful or harmful.

    prefers structured http status codes and exception metadata over fragile substring searches.
    """
    if isinstance(exc, SourceAuthError):
        return True

    status_code, _ = extract_http_metadata(exc)
    if status_code is not None:
        return status_code in (401, 403, 429)

    # fallback when structured metadata is absent
    msg = str(exc).lower()
    if "429" in msg or "rate limit" in msg or "too many requests" in msg:
        return True

    return False


class SourceFactory:
    """registry and orchestrator for discovering and dispatching music sources."""

    _registry: Dict[str, Type[SourceClient]] = {
        "spotify": SpotifyAdapter,
    }

    @classmethod
    def register_provider(cls, provider_id: str, client_cls: Type[SourceClient]) -> None:
        """register a provider client class."""
        if not provider_id or not isinstance(provider_id, str) or not provider_id.strip():
            raise ValueError("Provider ID must be a non-empty string")
        if not isinstance(client_cls, type) or not issubclass(client_cls, SourceClient):
            raise TypeError(f"Class {client_cls} must subclass SourceClient")

        if getattr(client_cls, "__abstractmethods__", None):
            missing = ", ".join(sorted(client_cls.__abstractmethods__))
            raise TypeError(f"Class {client_cls.__name__} has unimplemented abstract methods: {missing}")

        raw_attr = inspect.getattr_static(client_cls, "supports_url", None)
        if not isinstance(raw_attr, (classmethod, staticmethod)):
            raise TypeError(
                f"Class {client_cls.__name__}.supports_url must be a @classmethod"
            )

        cls._registry[provider_id.lower().strip()] = client_cls
        logger.debug(f"Registered source provider: '{provider_id}' -> {client_cls.__name__}")

    @classmethod
    def unregister_provider(cls, provider_id: str) -> None:
        """remove a provider from the registry."""
        cls._registry.pop(provider_id.lower().strip(), None)

    @classmethod
    def reset_registry(cls) -> None:
        """reset the registry to default providers for clean test isolation."""
        cls._registry = {
            "spotify": SpotifyAdapter,
        }

    @classmethod
    def get_registered_providers(cls) -> List[str]:
        """return list of currently registered provider identifiers."""
        return list(cls._registry.keys())

    @classmethod
    def detect_source_type(cls, url: str) -> str:
        """detect the provider identifier for a given url or uri using strict url parsing."""
        if not url or not isinstance(url, str):
            return "unknown"

        raw = url.strip()
        if not raw:
            return "unknown"

        for provider_id, client_cls in cls._registry.items():
            if client_cls.supports_url(raw):
                return provider_id

        return "unknown"

    @classmethod
    def create_client(cls, source_type: str, **kwargs) -> Optional[SourceClient]:
        """instantiate and return a SourceClient for the given provider type."""
        provider_key = (source_type or "").lower().strip()
        client_cls = cls._registry.get(provider_key)
        if client_cls is None:
            return None
        return client_cls(**kwargs)

    @classmethod
    def _resolve_provider_kwargs(
        cls,
        provider_id: str,
        provider_kwargs: Optional[Dict[str, dict]] = None,
        **client_kwargs,
    ) -> dict:
        """resolve kwargs specific to a provider without leaking other provider dependencies."""
        resolved = dict((provider_kwargs or {}).get(provider_id, {}))
        if provider_id == "spotify":
            for k in ("client", "client_id", "client_secret", "cache_path", "ttl_hours"):
                if k in client_kwargs and k not in resolved:
                    resolved[k] = client_kwargs[k]
        else:
            for k, v in client_kwargs.items():
                if k not in ("client", "client_id", "client_secret") and k not in resolved:
                    resolved[k] = v
        return resolved

    @classmethod
    def fetch_all_sources(
        cls,
        sources: List[str],
        use_cache_if_available: bool = True,
        allow_partial: bool = False,
        provider_kwargs: Optional[Dict[str, dict]] = None,
        **client_kwargs,
    ) -> List[CanonicalTrack]:
        """fetch tracks across all given source urls with provider reuse and order preservation.

        preserves source and track ordering without silently swallowing provider errors.
        """
        report = cls.fetch_sources_with_report(
            sources=sources,
            use_cache_if_available=use_cache_if_available,
            provider_kwargs=provider_kwargs,
            **client_kwargs,
        )

        if report.has_failures and not allow_partial:
            # raise the first encountered failure immediately when partial results are disallowed
            first_url, first_exc = report.failed_sources[0]
            raise first_exc

        return report.tracks

    @classmethod
    def fetch_sources_with_report(
        cls,
        sources: List[str],
        use_cache_if_available: bool = True,
        provider_kwargs: Optional[Dict[str, dict]] = None,
        **client_kwargs,
    ) -> FetchReport:
        """fetch tracks across all sources returning both collected tracks and failed sources."""
        report = FetchReport()
        if not sources:
            return report

        # reuse client instances across the batch to avoid repeated construction
        cached_clients: Dict[str, SourceClient] = {}

        source_types = [cls.detect_source_type(s) for s in sources]

        # check for unknown or unsupported providers
        for idx, (source, stype) in enumerate(zip(sources, source_types)):
            if stype == "unknown" or stype not in cls._registry:
                err = UnsupportedSourceError(
                    f"Unsupported source URL or unknown provider: '{source}'"
                )
                report.failed_sources.append((source, err))

        valid_sources_with_types = [
            (s, stype) for s, stype in zip(sources, source_types)
            if stype != "unknown" and stype in cls._registry
        ]

        if not valid_sources_with_types:
            return report

        # when all sources share a single provider, dispatch together for efficient cache hashing
        distinct_providers = {stype for _, stype in valid_sources_with_types}
        if len(distinct_providers) == 1 and len(report.failed_sources) == 0:
            provider_id = distinct_providers.pop()
            client = cached_clients.get(provider_id)
            if client is None:
                p_kwargs = cls._resolve_provider_kwargs(provider_id, provider_kwargs, **client_kwargs)
                client = cls.create_client(provider_id, **p_kwargs)
                if client is None:
                    err = UnsupportedSourceError(f"No client registered for provider '{provider_id}'")
                    for s in sources:
                        report.failed_sources.append((s, err))
                    return report
                cached_clients[provider_id] = client

            try:
                tracks = client.fetch_sources(sources, use_cache_if_available=use_cache_if_available)
                if tracks is None:
                    raise SourceError("Source client violated fetch_sources contract: returned None")
                report.tracks.extend(tracks)
                return report
            except Exception as batch_exc:
                if len(sources) <= 1 or _is_provider_wide_failure(batch_exc):
                    for s in sources:
                        report.failed_sources.append((s, batch_exc))
                    return report
                # fallback to individual source resolution to isolate item-level failures and salvage valid tracks
                for s in sources:
                    try:
                        single_tracks = client.fetch_sources([s], use_cache_if_available=use_cache_if_available)
                        if single_tracks is None:
                            raise SourceError("Source client violated fetch_sources contract: returned None")
                        report.tracks.extend(single_tracks)
                    except Exception as s_exc:
                        report.failed_sources.append((s, s_exc))
                return report

        # for mixed providers, process sources individually to preserve authentic order
        for source, stype in valid_sources_with_types:
            client = cached_clients.get(stype)
            if client is None:
                p_kwargs = cls._resolve_provider_kwargs(stype, provider_kwargs, **client_kwargs)
                client = cls.create_client(stype, **p_kwargs)
                if client is None:
                    err = UnsupportedSourceError(f"No client registered for provider '{stype}'")
                    report.failed_sources.append((source, err))
                    continue
                cached_clients[stype] = client

            try:
                tracks = client.fetch_sources([source], use_cache_if_available=use_cache_if_available)
                if tracks is None:
                    raise SourceError("Source client violated fetch_sources contract: returned None")
                report.tracks.extend(tracks)
            except Exception as exc:
                report.failed_sources.append((source, exc))

        return report
