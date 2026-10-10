"""deterministic fake provider implementation for contract and multi-provider testing."""
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from src.core.models import CanonicalTrack
from src.sources.base import SourceClient
from src.sources.exceptions import (
    SourceAuthError,
    SourceError,
    SourceTemporaryError,
    UnsupportedSourceError,
)


class FakeSecondaryProvider(SourceClient):
    """deterministic fake music provider for testing SourceClient contracts and factory routing."""

    provider_id: str = "fixture_music"

    def __init__(
        self,
        api_key: Optional[str] = None,
        custom_option: str = "default_val",
        is_healthy: bool = True,
        **kwargs: Any,
    ):
        self.api_key = api_key
        self.custom_option = custom_option
        self.is_healthy = is_healthy
        self.extra_kwargs = kwargs

    @classmethod
    def supports_url(cls, url: str) -> bool:
        """check whether the given url or uri matches fixture_music format."""
        if not url or not isinstance(url, str):
            return False

        raw = url.strip()
        if not raw:
            return False

        # uri format: fixture:playlist:<id>, fixture:album:<id>, fixture:track:<id>
        if raw.lower().startswith("fixture:"):
            parts = raw.split(":")
            if len(parts) >= 3 and parts[1].lower() in ("playlist", "album", "track"):
                item_id = parts[2].split("?")[0].strip()
                return len(item_id) > 0
            return False

        try:
            parsed = urlparse(raw)
        except Exception:
            return False

        if parsed.scheme in ("http", "https"):
            hostname = (parsed.hostname or "").lower()
            if hostname == "fixture.music.local":
                path_parts = [p for p in parsed.path.split("/") if p]
                if path_parts and path_parts[0].lower() in ("playlist", "album", "track"):
                    return True
        return False

    def fetch_sources(
        self,
        urls: List[str],
        use_cache_if_available: bool = True,
    ) -> List[CanonicalTrack]:
        """fetch and transform deterministic tracks without real network calls."""
        if self.api_key == "invalid_key":
            raise SourceAuthError("Authentication failed: invalid api_key for fixture_music")

        tracks: List[CanonicalTrack] = []

        for url in urls:
            if not self.supports_url(url):
                raise UnsupportedSourceError(f"Unsupported fixture source: '{url}'")

            raw = url.strip()

            # simulated failure triggers
            if "auth_fail" in raw.lower():
                raise SourceAuthError(f"Authentication failed for fixture source '{raw}'")
            if "temp_fail" in raw.lower():
                raise SourceTemporaryError(f"Temporary network failure for fixture source '{raw}'")
            if "unexpected_error" in raw.lower():
                raise SourceError(f"Unexpected provider crash for fixture source '{raw}'")
            if "return_none" in raw.lower():
                return None  # type: ignore # contract violation simulation

            # simulated empty playlist
            if "empty" in raw.lower():
                continue

            # simulated multi-artist metadata
            if "multi_artist" in raw.lower():
                tracks.append(
                    CanonicalTrack(
                        title="Collaborative Horizon",
                        artists=["Primary Artist", "Featured Artist", "Guest Star"],
                        album="Collaborations",
                        duration_ms=215000,
                        isrc="USFIX1234567",
                        external_ids={"fixture_music": "collab_01"},
                        provider_id=self.provider_id,
                        source_uri=raw,
                        source_name="Fixture Collaborations",
                    )
                )
                continue

            # simulated missing optional metadata
            if "missing_meta" in raw.lower():
                tracks.append(
                    CanonicalTrack(
                        title="Minimalist Drone",
                        artists=["Solo Minimalist"],
                        album=None,
                        duration_ms=None,
                        isrc=None,
                        external_ids={"fixture_music": "minimal_01"},
                        provider_id=self.provider_id,
                        source_uri=raw,
                        source_name="Fixture Minimal",
                    )
                )
                continue

            # single track format
            if ":track:" in raw.lower() or "/track/" in raw.lower() or "single" in raw.lower():
                item_id = raw.split(":")[-1].split("/")[-1].split("?")[0]
                tracks.append(
                    CanonicalTrack(
                        title=f"Deterministic Track {item_id}",
                        artists=[f"Artist {item_id}"],
                        album=f"Single Album {item_id}",
                        duration_ms=195000,
                        isrc=f"USFIX{item_id:>07}",
                        external_ids={"fixture_music": str(item_id)},
                        provider_id=self.provider_id,
                        source_uri=raw,
                        source_name=f"Fixture Track {item_id}",
                    )
                )
            else:
                # playlist or album returns 2 deterministic tracks preserving authentic order
                item_id = raw.split(":")[-1].split("/")[-1].split("?")[0]
                tracks.append(
                    CanonicalTrack(
                        title=f"Track 1 from {item_id}",
                        artists=[f"Primary Artist {item_id}"],
                        album=f"Album {item_id}",
                        duration_ms=180000,
                        isrc=f"USFIX{item_id[:3]:>05}01",
                        external_ids={"fixture_music": f"{item_id}_track_1"},
                        provider_id=self.provider_id,
                        source_uri=raw,
                        source_name=f"Fixture Source {item_id}",
                    )
                )
                tracks.append(
                    CanonicalTrack(
                        title=f"Track 2 from {item_id}",
                        artists=[f"Secondary Artist {item_id}"],
                        album=f"Album {item_id}",
                        duration_ms=240000,
                        isrc=f"USFIX{item_id[:3]:>05}02",
                        external_ids={"fixture_music": f"{item_id}_track_2"},
                        provider_id=self.provider_id,
                        source_uri=raw,
                        source_name=f"Fixture Source {item_id}",
                    )
                )

        return tracks

    def check_health(self) -> bool:
        """deterministic health check without any network access."""
        return self.is_healthy
