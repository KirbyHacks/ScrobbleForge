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
from src.sources.youtube_adapter import YouTubeAdapter
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
    """ensure sourcefactory registry is completely reset before and after every test."""
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
    """contract spec for the deterministic fakesecondaryprovider."""
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
    """contract spec for spotifyadapter using isolated mock client without network access."""
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


@pytest.fixture
def youtube_provider_spec() -> ProviderContractSpec:
    """contract spec for youtubeadapter using isolated mock session without network access."""
    def _create_mock_youtube_adapter(**kwargs):
        mock_session = MagicMock(spec=requests.Session)

        def mock_post(url, **call_kwargs):
            resp = MagicMock(spec=requests.Response)
            json_body = call_kwargs.get("json", {})
            browse_id = json_body.get("browseId", "")
            video_id = json_body.get("videoId", "")
            full_req = f"{url}:{browse_id}:{video_id}"

            if "auth_fail" in full_req:
                resp.status_code = 401
                resp.raise_for_status.side_effect = requests.exceptions.HTTPError(response=resp)
                return resp
            if "temp_fail" in full_req:
                resp.status_code = 503
                resp.raise_for_status.side_effect = requests.exceptions.HTTPError(response=resp)
                return resp
            if "unexpected" in full_req:
                resp.status_code = 500
                resp.raise_for_status.side_effect = RuntimeError("Internal crash in YouTube client")
                return resp
            if "empty" in full_req:
                resp.status_code = 200
                resp.raise_for_status.return_value = None
                resp.json.return_value = {
                    "contents": {
                        "singleColumnBrowseResultsRenderer": {
                            "tabs": [{"tabRenderer": {"content": {"sectionListRenderer": {"contents": [{"musicPlaylistShelfRenderer": {"contents": []}}]}}}}]
                        }
                    }
                }
                return resp

            resp.status_code = 200
            resp.raise_for_status.return_value = None

            if "/player" in url:
                if "multi" in full_req:
                    resp.json.return_value = {
                        "videoDetails": {
                            "videoId": video_id or "vid_multi",
                            "title": "Artist One feat. Artist Two - Collaboration",
                            "author": "Artist One",
                            "lengthSeconds": "210",
                        }
                    }
                elif "missing" in full_req:
                    resp.json.return_value = {
                        "videoDetails": {
                            "videoId": video_id or "vid_missing",
                            "title": "Solo Track",
                            "author": "Solo Artist - Topic",
                            "lengthSeconds": "0",
                        }
                    }
                else:
                    resp.json.return_value = {
                        "videoDetails": {
                            "videoId": video_id or "dQw4w9WgXcQ",
                            "title": "Single YouTube Track",
                            "author": "Artist Solo - Topic",
                            "lengthSeconds": "195",
                        }
                    }
                return resp

            if "multi" in full_req:
                items = [
                    {
                        "musicResponsiveListItemRenderer": {
                            "playlistItemData": {"videoId": "yt_multi_01"},
                            "flexColumns": [
                                {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "Collab Song"}]}}},
                                {
                                    "musicResponsiveListItemFlexColumnRenderer": {
                                        "text": {
                                            "runs": [
                                                {"text": "Artist One"},
                                                {"text": " & "},
                                                {"text": "Artist Two"},
                                                {"text": " • "},
                                                {"text": "Collab Album"},
                                                {"text": " • "},
                                                {"text": "3:30"},
                                            ]
                                        }
                                    }
                                },
                            ],
                        }
                    }
                ]
            elif "missing" in full_req:
                items = [
                    {
                        "musicResponsiveListItemRenderer": {
                            "playlistItemData": {"videoId": "yt_missing_01"},
                            "flexColumns": [
                                {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "Minimal Track"}]}}},
                                {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "Solo Artist"}]}}},
                            ],
                        }
                    }
                ]
            else:
                items = [
                    {
                        "musicResponsiveListItemRenderer": {
                            "playlistItemData": {"videoId": f"yt_{abs(hash(browse_id)) % 10000}_1"},
                            "flexColumns": [
                                {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "Track 1"}]}}},
                                {
                                    "musicResponsiveListItemFlexColumnRenderer": {
                                        "text": {
                                            "runs": [
                                                {"text": "Artist One"},
                                                {"text": " • "},
                                                {"text": "Album One"},
                                                {"text": " • "},
                                                {"text": "3:00"},
                                            ]
                                        }
                                    }
                                },
                            ],
                        }
                    },
                    {
                        "musicResponsiveListItemRenderer": {
                            "playlistItemData": {"videoId": f"yt_{abs(hash(browse_id)) % 10000}_2"},
                            "flexColumns": [
                                {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "Track 2"}]}}},
                                {
                                    "musicResponsiveListItemFlexColumnRenderer": {
                                        "text": {
                                            "runs": [
                                                {"text": "Artist Two"},
                                                {"text": " • "},
                                                {"text": "Album One"},
                                                {"text": " • "},
                                                {"text": "4:00"},
                                            ]
                                        }
                                    }
                                },
                            ],
                        }
                    },
                ]

            resp.json.return_value = {
                "contents": {
                    "singleColumnBrowseResultsRenderer": {
                        "tabs": [
                            {
                                "tabRenderer": {
                                    "content": {
                                        "sectionListRenderer": {
                                            "contents": [{"musicPlaylistShelfRenderer": {"contents": items}}]
                                        }
                                    }
                                }
                            }
                        ]
                    }
                }
            }
            return resp

        mock_session.post.side_effect = mock_post
        return YouTubeAdapter(session=mock_session)

    return ProviderContractSpec(
        provider_id="youtube",
        client_cls=YouTubeAdapter,
        create_client=_create_mock_youtube_adapter,
        valid_source_single="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        valid_source_multiple=[
            "https://www.youtube.com/playlist?list=PLalpha123",
            "https://music.youtube.com/playlist?list=OLAK5uy_beta",
        ],
        valid_source_empty="https://www.youtube.com/playlist?list=PLempty",
        valid_source_multi_artist="https://www.youtube.com/playlist?list=PLmulti",
        valid_source_missing_meta="https://www.youtube.com/playlist?list=PLmissing",
        invalid_source="https://www.youtube.com/shorts/dQw4w9WgXcQ",
        spoofed_source="https://youtube.com.attacker.example/playlist?list=PL123",
        auth_fail_source="https://www.youtube.com/playlist?list=PLauth_fail",
        temp_fail_source="https://www.youtube.com/playlist?list=PLtemp_fail",
        unexpected_error_source="https://www.youtube.com/playlist?list=PLunexpected",
        supports_return_none_sim=False,
    )


@pytest.fixture(params=["fake_provider", "spotify", "youtube"])
def provider_spec(request, fake_provider_spec, spotify_provider_spec, youtube_provider_spec) -> ProviderContractSpec:
    """parameterized fixture supplying fake_provider, spotify, and youtube specs for contract suite."""
    if request.param == "fake_provider":
        return fake_provider_spec
    if request.param == "spotify":
        return spotify_provider_spec
    return youtube_provider_spec
