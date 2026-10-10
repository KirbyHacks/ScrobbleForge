"""pytest configuration and reusable provider contract fixtures."""
from dataclasses import dataclass
from typing import Callable, List, Type
from unittest.mock import MagicMock, patch

import pytest
import requests

from src.models import Track
from src.sources.base import SourceClient
from src.sources.source_factory import SourceFactory
from src.sources.spotify_adapter import SpotifyAdapter
from src.spotify_client import SpotifyClient
from tests.contracts.fake_provider import FakeSecondaryProvider


@dataclass
class ProviderContractSpec:
    """specification defining sample sources and behavior for a provider contract run."""

    provider_id: str
    client_cls: Type[SourceClient]
    create_client: Callable[..., SourceClient]
    valid_source_single: str
    valid_source_multiple: List[str]
    valid_source_empty: str
    valid_source_multi_artist: str
    valid_source_missing_meta: str
    invalid_source: str
    spoofed_source: str
    auth_fail_source: str
    temp_fail_source: str
    unexpected_error_source: str
    supports_return_none_sim: bool = False


@pytest.fixture(autouse=True)
def clean_registry():
    """ensure SourceFactory registry is completely reset before and after every test."""
    SourceFactory.reset_registry()
    yield
    SourceFactory.reset_registry()


@pytest.fixture(autouse=True)
def guard_network():
    """prevent any accidental live network calls during contract testing."""
    def block_socket(*args, **kwargs):
        raise RuntimeError("Blocked real network access during contract testing")

    with patch("socket.socket.connect", side_effect=block_socket):
        yield


@pytest.fixture
def fake_provider_spec() -> ProviderContractSpec:
    """contract spec for the deterministic FakeSecondaryProvider."""
    return ProviderContractSpec(
        provider_id="fixture_music",
        client_cls=FakeSecondaryProvider,
        create_client=lambda **kwargs: FakeSecondaryProvider(**kwargs),
        valid_source_single="fixture:track:track_42",
        valid_source_multiple=["fixture:playlist:alpha", "fixture:album:beta"],
        valid_source_empty="fixture:playlist:empty",
        valid_source_multi_artist="fixture:playlist:multi_artist",
        valid_source_missing_meta="fixture:playlist:missing_meta",
        invalid_source="fixture:unsupported:item",
        spoofed_source="https://fixture.music.local.attacker.com/playlist/fake",
        auth_fail_source="fixture:playlist:auth_fail",
        temp_fail_source="fixture:playlist:temp_fail",
        unexpected_error_source="fixture:playlist:unexpected_error",
        supports_return_none_sim=True,
    )


@pytest.fixture
def spotify_provider_spec() -> ProviderContractSpec:
    """contract spec for SpotifyAdapter using isolated mock client without network access."""
    def _create_mock_spotify_adapter(**kwargs):
        mock_client = MagicMock(spec=SpotifyClient)

        def mock_fetch(urls, use_cache_if_available=True):
            tracks = []
            for url in urls:
                if "auth_fail" in url:
                    resp = MagicMock()
                    resp.status_code = 401
                    raise requests.exceptions.HTTPError(response=resp)
                if "temp_fail" in url:
                    resp = MagicMock()
                    resp.status_code = 503
                    raise requests.exceptions.HTTPError(response=resp)
                if "unexpected" in url:
                    raise RuntimeError("Unexpected internal crash in spotify client")
                if "empty" in url:
                    continue
                if "multi" in url:
                    tracks.append(
                        Track(
                            title="Spotify Collab",
                            artist="Primary Artist, Secondary Artist",
                            album="Collab Album",
                            duration_ms=210000,
                            spotify_id="spot_collab_01",
                            source_name="Spotify Collab",
                        )
                    )
                    continue
                if "missing" in url:
                    tracks.append(
                        Track(
                            title="Minimal Spotify",
                            artist="Solo Artist",
                            album="",
                            duration_ms=180000,
                            spotify_id="spot_min_01",
                            source_name="Spotify Minimal",
                        )
                    )
                    continue
                if "4cOdK2wGLETKBW3PvgPWqT" in url:
                    tracks.append(
                        Track(
                            title="Single Spotify Track",
                            artist="Artist Solo",
                            album="Single Album",
                            duration_ms=195000,
                            spotify_id="4cOdK2wGLETKBW3PvgPWqT",
                            source_name="Single Track",
                        )
                    )
                    continue

                # default playlist / album returns 2 tracks
                tracks.extend([
                    Track(
                        title=f"Track 1 from {url}",
                        artist="Artist One",
                        album="Album One",
                        duration_ms=180000,
                        spotify_id=f"spot_{abs(hash(url)) % 10000}_1",
                        source_name="Spotify Playlist",
                    ),
                    Track(
                        title=f"Track 2 from {url}",
                        artist="Artist Two",
                        album="Album One",
                        duration_ms=240000,
                        spotify_id=f"spot_{abs(hash(url)) % 10000}_2",
                        source_name="Spotify Playlist",
                    ),
                ])
            return tracks

        mock_client.fetch_sources.side_effect = mock_fetch
        return SpotifyAdapter(client=mock_client)

    return ProviderContractSpec(
        provider_id="spotify",
        client_cls=SpotifyAdapter,
        create_client=_create_mock_spotify_adapter,
        valid_source_single="https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT",
        valid_source_multiple=[
            "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M",
            "spotify:album:4m2880jivSbbyEGAKfITCa",
        ],
        valid_source_empty="https://open.spotify.com/playlist/empty",
        valid_source_multi_artist="https://open.spotify.com/track/multi",
        valid_source_missing_meta="https://open.spotify.com/track/missing",
        invalid_source="spotify:unknown_entity:123",
        spoofed_source="https://open.spotify.com.attacker.example/playlist/123",
        auth_fail_source="https://open.spotify.com/playlist/auth_fail",
        temp_fail_source="https://open.spotify.com/playlist/temp_fail",
        unexpected_error_source="https://open.spotify.com/playlist/unexpected",
        supports_return_none_sim=False,
    )


@pytest.fixture(params=["fake_provider", "spotify"])
def provider_spec(request, fake_provider_spec, spotify_provider_spec) -> ProviderContractSpec:
    """parameterized fixture supplying both fake_provider and spotify specs for contract suite."""
    if request.param == "fake_provider":
        return fake_provider_spec
    return spotify_provider_spec
