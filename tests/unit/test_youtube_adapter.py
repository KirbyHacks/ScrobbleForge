"""unit tests for youtubeadapter, youtubemetadatanormalizer, and url validation."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import requests

from src.core.models import CanonicalTrack, to_legacy_track
from src.models import Track
from src.queue_manager import QueueManager, _track_identifier
from src.quota_tracker import QuotaTracker
from src.sources.exceptions import (
    SourceAuthError,
    SourceError,
    SourceTemporaryError,
    UnsupportedSourceError,
)
from src.sources.service import SourceIngestionService
from src.sources.source_factory import SourceFactory
from src.sources.youtube_adapter import (
    ItemOmission,
    YouTubeAdapter,
    YouTubeMetadataNormalizer,
    parse_youtube_url,
)


class TestYouTubeUrlParsing(unittest.TestCase):
    """validates strict url parsing, query param handling, and spoofing defense."""

    def test_supported_playlist_urls(self):
        valid_playlists = [
            ("https://www.youtube.com/playlist?list=PLrAl46nJ81c_k3gD-6VnK1d0l6nQx8_i4", "PLrAl46nJ81c_k3gD-6VnK1d0l6nQx8_i4"),
            ("https://youtube.com/playlist?list=PL12345", "PL12345"),
            ("http://youtube.com/playlist?list=PL12345", "PL12345"),
            ("https://music.youtube.com/playlist?list=OLAK5uy_123", "OLAK5uy_123"),
            ("https://m.youtube.com/playlist?list=PLmobile", "PLmobile"),
            ("https://www.youtube.com/playlist?list=PL123&feature=share", "PL123"),
            ("https://www.youtube.com/playlist?foo=bar&list=PL123#frag", "PL123"),
        ]
        for url, expected_id in valid_playlists:
            with self.subTest(url=url):
                parsed = parse_youtube_url(url)
                self.assertIsNotNone(parsed)
                ref_type, ref_id = parsed  # type: ignore
                self.assertEqual(ref_type, "playlist")
                self.assertEqual(ref_id, expected_id)
                self.assertTrue(YouTubeAdapter.supports_url(url))

    def test_supported_track_urls(self):
        valid_tracks = [
            ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
            ("https://youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
            ("https://music.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
            ("https://m.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
            ("https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
            ("http://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
            ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42s", "dQw4w9WgXcQ"),
            ("https://youtu.be/dQw4w9WgXcQ?t=42", "dQw4w9WgXcQ"),
        ]
        for url, expected_id in valid_tracks:
            with self.subTest(url=url):
                parsed = parse_youtube_url(url)
                self.assertIsNotNone(parsed)
                ref_type, ref_id = parsed  # type: ignore
                self.assertEqual(ref_type, "track")
                self.assertEqual(ref_id, expected_id)
                self.assertTrue(YouTubeAdapter.supports_url(url))

    def test_hostname_spoofing_defense(self):
        spoofed = [
            "https://youtube.com.attacker.example/playlist?list=PL123",
            "https://www.youtube.com.attacker.example/watch?v=123",
            "https://notyoutube.com/watch?v=123",
            "https://music.youtube.com.fake.example/playlist?list=PL123",
            "http://evil.com/?url=https://youtube.com",
            "https://youtu.be.attacker.com/123",
        ]
        for url in spoofed:
            with self.subTest(url=url):
                self.assertIsNone(parse_youtube_url(url))
                self.assertFalse(YouTubeAdapter.supports_url(url))

    def test_unsupported_and_malformed_urls(self):
        unsupported = [
            "https://www.youtube.com/@rickastley/videos",  # channels deferred
            "https://www.youtube.com/channel/UCuAXFkgsw1L7xaCfnd5JJOw",
            "https://www.youtube.com/shorts/dQw4w9WgXcQ",  # shorts deferred
            "https://www.youtube.com/playlist",           # missing list param
            "https://www.youtube.com/watch",              # missing v param
            "https://youtu.be/",                          # empty id
            "https://youtu.be/foo/bar",                   # nested path
            "ftp://www.youtube.com/watch?v=123",          # unsupported scheme
            "",
            "   ",
            None,
            ":::",
            "not-a-url",
        ]
        for url in unsupported:
            with self.subTest(url=url):
                self.assertIsNone(parse_youtube_url(url))  # type: ignore
                self.assertFalse(YouTubeAdapter.supports_url(url))  # type: ignore


class TestYouTubeMetadataNormalizer(unittest.TestCase):
    """validates title cleaning, artist extraction, and duration formats."""

    def test_title_cleaning(self):
        cases = [
            ("Song Title (Official Music Video)", "Song Title"),
            ("Song Title [Official Audio]", "Song Title"),
            ("Song Title (Lyric Video)", "Song Title"),
            ("Song Title [HD]", "Song Title"),
            ("Song Title (Remastered 2011)", "Song Title (Remastered 2011)"),
            ("Song Title (Live at Wembley)", "Song Title (Live at Wembley)"),
            ("Song Title (Official Visualizer)", "Song Title"),
            ("Song Title (Clean Version)", "Song Title (Clean Version)"),
            ("Redbone (Live From The Tonight Show Starring Jimmy Fallon)", "Redbone (Live From The Tonight Show Starring Jimmy Fallon)"),
            ("Dear April (Side A - Acoustic)", "Dear April (Side A - Acoustic)"),
            ("Something (Remix)", "Something (Remix)"),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(YouTubeMetadataNormalizer.clean_title(raw), expected)

    def test_artist_and_title_extraction_with_delimiters(self):
        cases = [
            (
                "Dua Lipa - Levitating (Official Video)",
                None,
                ("Levitating", ["Dua Lipa"], "reliable_parsed"),
            ),
            (
                "Calvin Harris feat. Dua Lipa - One Kiss",
                None,
                ("One Kiss", ["Calvin Harris", "Dua Lipa"], "reliable_parsed"),
            ),
            (
                "Queen & David Bowie - Under Pressure",
                None,
                ("Under Pressure", ["Queen", "David Bowie"], "reliable_parsed"),
            ),
            (
                "Artist One, Artist Two - Collaboration Track",
                None,
                ("Collaboration Track", ["Artist One", "Artist Two"], "reliable_parsed"),
            ),
        ]
        for raw, channel, expected in cases:
            with self.subTest(raw=raw):
                res = YouTubeMetadataNormalizer.extract_artist_and_title(raw, channel_title=channel)
                self.assertEqual(res, expected)

    def test_topic_channel_fallback(self):
        title, artists, conf = YouTubeMetadataNormalizer.extract_artist_and_title(
            "Blinding Lights", channel_title="The Weeknd - Topic"
        )
        self.assertEqual(title, "Blinding Lights")
        self.assertEqual(artists, ["The Weeknd"])
        self.assertEqual(conf, "topic_channel")

    def test_ambiguous_title_classification(self):
        title, artists, conf = YouTubeMetadataNormalizer.extract_artist_and_title(
            "My Summer Vacation Vlog", channel_title="GenericVlogger"
        )
        self.assertEqual(title, "My Summer Vacation Vlog")
        self.assertEqual(artists, [])
        self.assertEqual(conf, "ambiguous")

    def test_iso8601_duration_parsing(self):
        self.assertEqual(YouTubeMetadataNormalizer.parse_iso8601_duration("PT3M45S"), 225000)
        self.assertEqual(YouTubeMetadataNormalizer.parse_iso8601_duration("PT1H2M30S"), 3750000)
        self.assertEqual(YouTubeMetadataNormalizer.parse_iso8601_duration("PT45S"), 45000)
        self.assertEqual(YouTubeMetadataNormalizer.parse_iso8601_duration("PT5M"), 300000)
        self.assertIsNone(YouTubeMetadataNormalizer.parse_iso8601_duration("P0D"))
        self.assertIsNone(YouTubeMetadataNormalizer.parse_iso8601_duration(""))

    def test_time_duration_parsing(self):
        self.assertEqual(YouTubeMetadataNormalizer.parse_time_duration("3:45"), 225000)
        self.assertEqual(YouTubeMetadataNormalizer.parse_time_duration("1:02:30"), 3750000)
        self.assertIsNone(YouTubeMetadataNormalizer.parse_time_duration("invalid"))


class TestYouTubeAdapterIngestion(unittest.TestCase):
    """unit tests for youtubeadapter fetch_sources, pagination, and error handling."""

    def _make_mock_response(self, json_data, status_code=200, headers=None):
        resp = MagicMock(spec=requests.Response)
        resp.status_code = status_code
        resp.json.return_value = json_data
        resp.headers = headers or {}
        if status_code >= 400:
            http_err = requests.exceptions.HTTPError(response=resp)
            resp.raise_for_status.side_effect = http_err
        else:
            resp.raise_for_status.return_value = None
        return resp

    def test_youtubei_playlist_ingestion_success(self):
        """verifies parsing of structured youtube music browse response."""
        mock_browse_json = {
            "header": {
                "musicDetailHeaderRenderer": {
                    "title": {"runs": [{"text": "Synthwave Favorites"}]}
                }
            },
            "contents": {
                "singleColumnBrowseResultsRenderer": {
                    "tabs": [
                        {
                            "tabRenderer": {
                                "content": {
                                    "sectionListRenderer": {
                                        "contents": [
                                            {
                                                "musicPlaylistShelfRenderer": {
                                                    "contents": [
                                                        {
                                                            "musicResponsiveListItemRenderer": {
                                                                "playlistItemData": {"videoId": "vid_001"},
                                                                "flexColumns": [
                                                                    {
                                                                        "musicResponsiveListItemFlexColumnRenderer": {
                                                                            "text": {"runs": [{"text": "Nightcall"}]}
                                                                        }
                                                                    },
                                                                    {
                                                                        "musicResponsiveListItemFlexColumnRenderer": {
                                                                            "text": {
                                                                                "runs": [
                                                                                    {"text": "Kavinsky"},
                                                                                    {"text": " • "},
                                                                                    {"text": "OutRun", "navigationEndpoint": {"browseEndpoint": {"browseEndpointContextSupportedConfigs": {"browseEndpointContextMusicConfig": {"pageType": "MUSIC_PAGE_TYPE_ALBUM"}}}}},
                                                                                    {"text": " • "},
                                                                                    {"text": "4:19"},
                                                                                ]
                                                                            }
                                                                        }
                                                                    },
                                                                ],
                                                            }
                                                        },
                                                        {
                                                            "musicResponsiveListItemRenderer": {
                                                                "playlistItemData": {"videoId": "vid_002"},
                                                                "flexColumns": [
                                                                    {
                                                                        "musicResponsiveListItemFlexColumnRenderer": {
                                                                            "text": {"runs": [{"text": "Tech Noir"}]}
                                                                        }
                                                                    },
                                                                    {
                                                                        "musicResponsiveListItemFlexColumnRenderer": {
                                                                            "text": {
                                                                                "runs": [
                                                                                    {"text": "GUNSHIP"},
                                                                                    {"text": " • "},
                                                                                    {"text": "GUNSHIP"},
                                                                                    {"text": " • "},
                                                                                    {"text": "4:57"},
                                                                                ]
                                                                            }
                                                                        }
                                                                    },
                                                                ],
                                                            }
                                                        },
                                                    ]
                                                }
                                            }
                                        ]
                                    }
                                }
                            }
                        }
                    ]
                }
            },
        }

        mock_session = MagicMock(spec=requests.Session)
        mock_session.post.return_value = self._make_mock_response(mock_browse_json)

        adapter = YouTubeAdapter(session=mock_session)
        url = "https://music.youtube.com/playlist?list=PLsynth"
        tracks = adapter.fetch_sources([url], use_cache_if_available=False)

        self.assertEqual(len(tracks), 2)
        t1, t2 = tracks[0], tracks[1]

        self.assertEqual(t1.title, "Nightcall")
        self.assertEqual(t1.artists, ["Kavinsky"])
        self.assertEqual(t1.album, "OutRun")
        self.assertEqual(t1.duration_ms, 259000)
        self.assertEqual(t1.provider_id, "youtube")
        self.assertEqual(t1.external_ids["youtube"], "vid_001")
        self.assertNotIn("spotify", t1.external_ids)

        self.assertEqual(t2.title, "Tech Noir")
        self.assertEqual(t2.artists, ["GUNSHIP"])
        self.assertEqual(t2.duration_ms, 297000)

    def test_youtubei_playlist_pagination(self):
        """verifies multi-page continuation and duplicate token protection."""
        page1_json = {
            "contents": {
                "singleColumnBrowseResultsRenderer": {
                    "tabs": [
                        {
                            "tabRenderer": {
                                "content": {
                                    "sectionListRenderer": {
                                        "contents": [
                                            {
                                                "musicPlaylistShelfRenderer": {
                                                    "contents": [
                                                        {
                                                            "musicResponsiveListItemRenderer": {
                                                                "playlistItemData": {"videoId": "vid_page1"},
                                                                "flexColumns": [
                                                                    {
                                                                        "musicResponsiveListItemFlexColumnRenderer": {
                                                                            "text": {"runs": [{"text": "Page 1 Track"}]}
                                                                        }
                                                                    },
                                                                    {
                                                                        "musicResponsiveListItemFlexColumnRenderer": {
                                                                            "text": {
                                                                                "runs": [
                                                                                    {"text": "Artist One"},
                                                                                    {"text": " • "},
                                                                                    {"text": "3:00"},
                                                                                ]
                                                                            }
                                                                        }
                                                                    },
                                                                ],
                                                            }
                                                        }
                                                    ],
                                                    "continuations": [
                                                        {"nextContinuationData": {"continuation": "token_page_2"}}
                                                    ],
                                                }
                                            }
                                        ]
                                    }
                                }
                            }
                        }
                    ]
                }
            }
        }

        page2_json = {
            "continuationContents": {
                "musicPlaylistShelfContinuation": {
                    "contents": [
                        {
                            "musicResponsiveListItemRenderer": {
                                "playlistItemData": {"videoId": "vid_page2"},
                                "flexColumns": [
                                    {
                                        "musicResponsiveListItemFlexColumnRenderer": {
                                            "text": {"runs": [{"text": "Page 2 Track"}]}
                                        }
                                    },
                                    {
                                        "musicResponsiveListItemFlexColumnRenderer": {
                                            "text": {
                                                "runs": [
                                                    {"text": "Artist Two"},
                                                    {"text": " • "},
                                                    {"text": "3:30"},
                                                ]
                                            }
                                        }
                                    },
                                ],
                            }
                        }
                    ],
                    # playlist exhausted: no more continuations
                    "continuations": [],
                }
            }
        }

        mock_session = MagicMock(spec=requests.Session)
        mock_session.post.side_effect = [
            self._make_mock_response(page1_json),
            self._make_mock_response(page2_json),
        ]

        adapter = YouTubeAdapter(session=mock_session, max_pages=5)
        tracks = adapter.fetch_sources(["https://www.youtube.com/playlist?list=PLmultipage"], use_cache_if_available=False)

        self.assertEqual(len(tracks), 2)
        self.assertEqual(tracks[0].title, "Page 1 Track")
        self.assertEqual(tracks[1].title, "Page 2 Track")

    def test_pagination_repeated_continuation_token_raises_source_error(self):
        """verifies that a repeating continuation token raises sourceerror and halts without partial return."""
        page1_json = {
            "contents": {
                "singleColumnBrowseResultsRenderer": {
                    "tabs": [
                        {
                            "tabRenderer": {
                                "content": {
                                    "sectionListRenderer": {
                                        "contents": [
                                            {
                                                "musicPlaylistShelfRenderer": {
                                                    "contents": [
                                                        {
                                                            "musicResponsiveListItemRenderer": {
                                                                "playlistItemData": {"videoId": "vid_page1"},
                                                                "flexColumns": [
                                                                    {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "Page 1 Track"}]}}},
                                                                    {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "Artist One"}]}}},
                                                                ],
                                                            }
                                                        }
                                                    ],
                                                    "continuations": [
                                                        {"nextContinuationData": {"continuation": "token_loop"}}
                                                    ],
                                                }
                                            }
                                        ]
                                    }
                                }
                            }
                        }
                    ]
                }
            }
        }
        page2_loop_json = {
            "continuationContents": {
                "musicPlaylistShelfContinuation": {
                    "contents": [
                        {
                            "musicResponsiveListItemRenderer": {
                                "playlistItemData": {"videoId": "vid_page2"},
                                "flexColumns": [
                                    {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "Page 2 Track"}]}}},
                                    {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "Artist Two"}]}}},
                                ],
                            }
                        }
                    ],
                    "continuations": [
                        {"nextContinuationData": {"continuation": "token_loop"}}
                    ],
                }
            }
        }

        mock_session = MagicMock(spec=requests.Session)
        mock_session.post.side_effect = [
            self._make_mock_response(page1_json),
            self._make_mock_response(page2_loop_json),
        ]

        adapter = YouTubeAdapter(session=mock_session, max_pages=5)
        with self.assertRaises(SourceError) as ctx:
            adapter.fetch_sources(["https://www.youtube.com/playlist?list=PLcycle"], use_cache_if_available=False)
        self.assertIn("pagination cycle detected", str(ctx.exception))
        self.assertIn("token_loop", str(ctx.exception))

    def test_pagination_max_pages_exhaustion_raises_source_error(self):
        """verifies that exceeding max_pages with pending continuations raises sourceerror."""
        page1_json = {
            "contents": {
                "singleColumnBrowseResultsRenderer": {
                    "tabs": [
                        {
                            "tabRenderer": {
                                "content": {
                                    "sectionListRenderer": {
                                        "contents": [
                                            {
                                                "musicPlaylistShelfRenderer": {
                                                    "contents": [
                                                        {
                                                            "musicResponsiveListItemRenderer": {
                                                                "playlistItemData": {"videoId": "vid_page1"},
                                                                "flexColumns": [
                                                                    {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "Page 1 Track"}]}}},
                                                                    {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "Artist One"}]}}},
                                                                ],
                                                            }
                                                        }
                                                    ],
                                                    "continuations": [
                                                        {"nextContinuationData": {"continuation": "token_page_2"}}
                                                    ],
                                                }
                                            }
                                        ]
                                    }
                                }
                            }
                        }
                    ]
                }
            }
        }
        page2_json = {
            "continuationContents": {
                "musicPlaylistShelfContinuation": {
                    "contents": [
                        {
                            "musicResponsiveListItemRenderer": {
                                "playlistItemData": {"videoId": "vid_page2"},
                                "flexColumns": [
                                    {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "Page 2 Track"}]}}},
                                    {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "Artist Two"}]}}},
                                ],
                            }
                        }
                    ],
                    "continuations": [
                        {"nextContinuationData": {"continuation": "token_page_3"}}
                    ],
                }
            }
        }

        mock_session = MagicMock(spec=requests.Session)
        mock_session.post.side_effect = [
            self._make_mock_response(page1_json),
            self._make_mock_response(page2_json),
        ]

        # max_pages=2 means after page 2 is fetched, token_page_3 cannot be fetched
        adapter = YouTubeAdapter(session=mock_session, max_pages=2)
        with self.assertRaises(SourceError) as ctx:
            adapter.fetch_sources(["https://www.youtube.com/playlist?list=PLexhaust"], use_cache_if_available=False)
        self.assertIn("exceeded maximum page limit (2)", str(ctx.exception))

    def test_pagination_failure_does_not_overwrite_cache(self):
        """verifies that incomplete pagination failure leaves existing healthy cache completely intact."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_file = Path(tmp_dir) / "youtube_tracks.cache.json"
            cached_track = CanonicalTrack(
                title="Healthy Cached Song",
                artists=["Original Artist"],
                album="Original Album",
                duration_ms=180000,
                external_ids={"youtube": "healthy_id"},
                provider_id="youtube",
                source_uri="https://www.youtube.com/playlist?list=PLsafe",
            )
            adapter = YouTubeAdapter(cache_path=cache_file)
            adapter._save_cache([cached_track], ["https://www.youtube.com/playlist?list=PLsafe"])

            # verify cache was written
            initial_cache_content = cache_file.read_text(encoding="utf-8")
            self.assertIn("Healthy Cached Song", initial_cache_content)

            # now attempt fetch where pagination fails due to max_pages exhaustion
            page1_json = {
                "contents": {
                    "singleColumnBrowseResultsRenderer": {
                        "tabs": [
                            {
                                "tabRenderer": {
                                    "content": {
                                        "sectionListRenderer": {
                                            "contents": [
                                                {
                                                    "musicPlaylistShelfRenderer": {
                                                        "contents": [
                                                            {
                                                                "musicResponsiveListItemRenderer": {
                                                                    "playlistItemData": {"videoId": "vid_p1"},
                                                                    "flexColumns": [
                                                                        {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "P1 Partial"}]}}},
                                                                        {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "Artist"}]}}},
                                                                    ],
                                                                }
                                                            }
                                                        ],
                                                        "continuations": [
                                                            {"nextContinuationData": {"continuation": "token_p2"}}
                                                        ],
                                                    }
                                                }
                                            ]
                                        }
                                    }
                                }
                            }
                        ]
                    }
                }
            }

            mock_session = MagicMock(spec=requests.Session)
            mock_session.post.return_value = self._make_mock_response(page1_json)
            adapter._session = mock_session
            adapter.max_pages = 1

            with self.assertRaises(SourceError):
                adapter.fetch_sources(["https://www.youtube.com/playlist?list=PLsafe"], use_cache_if_available=False)

            # ensure cache on disk was not overwritten by the partial result
            final_cache_content = cache_file.read_text(encoding="utf-8")
            self.assertEqual(initial_cache_content, final_cache_content)
            self.assertIn("Healthy Cached Song", final_cache_content)
            self.assertNotIn("P1 Partial", final_cache_content)

    def test_pagination_failure_midway_raises_temporary_error(self):
        """verifies that a failure on a later page does not return a silent partial queue."""
        page1_json = {
            "contents": {
                "singleColumnBrowseResultsRenderer": {
                    "tabs": [
                        {
                            "tabRenderer": {
                                "content": {
                                    "sectionListRenderer": {
                                        "contents": [
                                            {
                                                "musicPlaylistShelfRenderer": {
                                                    "contents": [
                                                        {
                                                            "musicResponsiveListItemRenderer": {
                                                                "playlistItemData": {"videoId": "vid_1"},
                                                                "flexColumns": [
                                                                    {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "Track 1"}]}}},
                                                                    {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "Artist 1"}]}}},
                                                                ],
                                                            }
                                                        }
                                                    ],
                                                    "continuations": [
                                                        {"nextContinuationData": {"continuation": "token_page_2"}}
                                                    ],
                                                }
                                            }
                                        ]
                                    }
                                }
                            }
                        }
                    ]
                }
            }
        }

        mock_session = MagicMock(spec=requests.Session)
        err_resp = self._make_mock_response({}, status_code=500)
        mock_session.post.side_effect = [
            self._make_mock_response(page1_json),
            err_resp,
        ]

        adapter = YouTubeAdapter(session=mock_session)
        with self.assertRaises(SourceTemporaryError):
            adapter.fetch_sources(["https://www.youtube.com/playlist?list=PLfailpage"], use_cache_if_available=False)

    def test_unavailable_and_deleted_item_filtering(self):
        """verifies private and deleted videos are recorded as omissions and filtered out."""
        mock_json = {
            "contents": {
                "singleColumnBrowseResultsRenderer": {
                    "tabs": [
                        {
                            "tabRenderer": {
                                "content": {
                                    "sectionListRenderer": {
                                        "contents": [
                                            {
                                                "musicPlaylistShelfRenderer": {
                                                    "contents": [
                                                        {
                                                            "musicResponsiveListItemRenderer": {
                                                                "playlistItemData": {"videoId": "vid_valid"},
                                                                "flexColumns": [
                                                                    {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "Valid Song"}]}}},
                                                                    {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "Valid Artist"}]}}},
                                                                ],
                                                            }
                                                        },
                                                        {
                                                            "musicResponsiveListItemRenderer": {
                                                                "playlistItemData": {"videoId": "vid_deleted"},
                                                                "flexColumns": [
                                                                    {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "[Deleted video]"}]}}},
                                                                ],
                                                            }
                                                        },
                                                        {
                                                            "musicResponsiveListItemRenderer": {
                                                                "musicItemRendererDisplayPolicy": "MUSIC_ITEM_RENDERER_DISPLAY_POLICY_GREY_OUT",
                                                                "playlistItemData": {"videoId": "vid_grey"},
                                                            }
                                                        },
                                                    ]
                                                }
                                            }
                                        ]
                                    }
                                }
                            }
                        }
                    ]
                }
            }
        }

        mock_session = MagicMock(spec=requests.Session)
        mock_session.post.return_value = self._make_mock_response(mock_json)

        adapter = YouTubeAdapter(session=mock_session)
        tracks = adapter.fetch_sources(["https://www.youtube.com/playlist?list=PLfilter"], use_cache_if_available=False)

        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0].title, "Valid Song")
        self.assertEqual(len(adapter.last_omissions), 2)
        reasons = [o.reason for o in adapter.last_omissions]
        self.assertIn("deleted_video", reasons)
        self.assertIn("unavailable_item", reasons)

    def test_all_items_omitted_raises_source_error(self):
        """verifies that if all items in a playlist cannot be reliably parsed, sourceerror is raised."""
        mock_json = {
            "contents": {
                "singleColumnBrowseResultsRenderer": {
                    "tabs": [
                        {
                            "tabRenderer": {
                                "content": {
                                    "sectionListRenderer": {
                                        "contents": [
                                            {
                                                "musicPlaylistShelfRenderer": {
                                                    "contents": [
                                                        {
                                                            "musicResponsiveListItemRenderer": {
                                                                "playlistItemData": {"videoId": "vid_1"},
                                                                "flexColumns": [
                                                                    {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "[Deleted video]"}]}}},
                                                                ],
                                                            }
                                                        }
                                                    ]
                                                }
                                            }
                                        ]
                                    }
                                }
                            }
                        }
                    ]
                }
            }
        }
        mock_session = MagicMock(spec=requests.Session)
        mock_session.post.return_value = self._make_mock_response(mock_json)

        adapter = YouTubeAdapter(session=mock_session)
        with self.assertRaises(SourceError) as ctx:
            adapter.fetch_sources(["https://www.youtube.com/playlist?list=PLallbad"], use_cache_if_available=False)
        self.assertIn("all 1 items were ambiguous or unavailable", str(ctx.exception))

    def test_genuine_empty_playlist_returns_empty_list(self):
        """verifies genuine empty playlist returns [] without error."""
        mock_json = {
            "contents": {
                "singleColumnBrowseResultsRenderer": {
                    "tabs": [
                        {
                            "tabRenderer": {
                                "content": {
                                    "sectionListRenderer": {
                                        "contents": [
                                            {
                                                "musicPlaylistShelfRenderer": {
                                                    "contents": []
                                                }
                                            }
                                        ]
                                    }
                                }
                            }
                        }
                    ]
                }
            }
        }
        mock_session = MagicMock(spec=requests.Session)
        mock_session.post.return_value = self._make_mock_response(mock_json)

        adapter = YouTubeAdapter(session=mock_session)
        tracks = adapter.fetch_sources(["https://www.youtube.com/playlist?list=PLempty"], use_cache_if_available=False)
        self.assertEqual(tracks, [])

    def test_http_429_rate_limit_classification_and_retry_after(self):
        """verifies http 429 raises sourcetemporaryerror with retry_after populated."""
        mock_session = MagicMock(spec=requests.Session)
        mock_session.post.return_value = self._make_mock_response(
            {}, status_code=429, headers={"Retry-After": "45"}
        )

        adapter = YouTubeAdapter(session=mock_session)
        with self.assertRaises(SourceTemporaryError) as ctx:
            adapter.fetch_sources(["https://www.youtube.com/playlist?list=PL429"], use_cache_if_available=False)
        self.assertEqual(ctx.exception.status_code, 429)
        self.assertEqual(ctx.exception.retry_after, 45)

    def test_http_403_auth_error_classification(self):
        """verifies http 403 raises sourceautherror."""
        mock_session = MagicMock(spec=requests.Session)
        mock_session.post.return_value = self._make_mock_response({}, status_code=403)

        adapter = YouTubeAdapter(session=mock_session)
        with self.assertRaises(SourceAuthError) as ctx:
            adapter.fetch_sources(["https://www.youtube.com/playlist?list=PL403"], use_cache_if_available=False)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_single_track_resolution_via_youtubei_player(self):
        """verifies single watch url resolved via youtubei player endpoint."""
        mock_player_json = {
            "videoDetails": {
                "videoId": "dQw4w9WgXcQ",
                "title": "Rick Astley - Never Gonna Give You Up",
                "author": "Rick Astley",
                "lengthSeconds": "213",
            }
        }
        mock_session = MagicMock(spec=requests.Session)
        mock_session.post.return_value = self._make_mock_response(mock_player_json)

        adapter = YouTubeAdapter(session=mock_session)
        tracks = adapter.fetch_sources(["https://www.youtube.com/watch?v=dQw4w9WgXcQ"], use_cache_if_available=False)

        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0].title, "Never Gonna Give You Up")
        self.assertEqual(tracks[0].artists, ["Rick Astley"])
        self.assertEqual(tracks[0].duration_ms, 213000)
        self.assertEqual(tracks[0].external_ids["youtube"], "dQw4w9WgXcQ")

    def test_single_track_resolution_oembed_fallback(self):
        """verifies single track resolution falls back to oembed when player endpoint fails."""
        mock_oembed_json = {
            "title": "Rick Astley - Never Gonna Give You Up (Official Music Video)",
            "author_name": "Rick Astley",
        }
        mock_session = MagicMock(spec=requests.Session)
        # player endpoint fails with 404
        mock_session.post.return_value = self._make_mock_response({}, status_code=404)
        mock_session.get.return_value = self._make_mock_response(mock_oembed_json)

        adapter = YouTubeAdapter(session=mock_session)
        tracks = adapter.fetch_sources(["https://youtu.be/dQw4w9WgXcQ"], use_cache_if_available=False)

        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0].title, "Never Gonna Give You Up")
        self.assertEqual(tracks[0].artists, ["Rick Astley"])
        self.assertIsNone(tracks[0].duration_ms)

    def test_data_api_v3_ingestion(self):
        """verifies optional youtube data api v3 playlistitems + videos batching."""
        mock_playlist_items = {
            "items": [
                {
                    "snippet": {
                        "title": "Daft Punk - One More Time",
                        "resourceId": {"videoId": "vid_dp"},
                        "videoOwnerChannelTitle": "Daft Punk",
                    },
                    "status": {"privacyStatus": "public"},
                }
            ]
        }
        mock_videos = {
            "items": [
                {
                    "id": "vid_dp",
                    "contentDetails": {"duration": "PT5M20S"},
                }
            ]
        }

        mock_session = MagicMock(spec=requests.Session)
        mock_session.get.side_effect = [
            self._make_mock_response(mock_playlist_items),
            self._make_mock_response(mock_videos),
        ]

        adapter = YouTubeAdapter(api_key="valid_test_key_12345", session=mock_session)
        tracks = adapter.fetch_sources(["https://www.youtube.com/playlist?list=PLdaft"], use_cache_if_available=False)

        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0].title, "One More Time")
        self.assertEqual(tracks[0].artists, ["Daft Punk"])
        self.assertEqual(tracks[0].duration_ms, 320000)
        self.assertEqual(tracks[0].external_ids["youtube"], "vid_dp")

    def test_data_api_v3_pagination_cycle_raises_source_error(self):
        """verifies data api v3 raises sourceerror on repeating pagetoken."""
        p1 = {
            "items": [
                {
                    "snippet": {
                        "title": "Track 1",
                        "resourceId": {"videoId": "v1"},
                        "videoOwnerChannelTitle": "Artist 1",
                    },
                    "status": {"privacyStatus": "public"},
                }
            ],
            "nextPageToken": "token_loop",
        }
        p2_loop = {
            "items": [
                {
                    "snippet": {
                        "title": "Track 2",
                        "resourceId": {"videoId": "v2"},
                        "videoOwnerChannelTitle": "Artist 2",
                    },
                    "status": {"privacyStatus": "public"},
                }
            ],
            "nextPageToken": "token_loop",
        }
        mock_session = MagicMock(spec=requests.Session)
        mock_session.get.side_effect = [
            self._make_mock_response(p1),
            self._make_mock_response(p2_loop),
        ]
        adapter = YouTubeAdapter(api_key="valid_key", session=mock_session, max_pages=5)
        with self.assertRaises(SourceError) as ctx:
            adapter.fetch_sources(["https://www.youtube.com/playlist?list=PLcycle"], use_cache_if_available=False)
        self.assertIn("pagination cycle detected", str(ctx.exception))

    def test_data_api_v3_max_pages_exhaustion_raises_source_error(self):
        """verifies data api v3 raises sourceerror when max_pages reached with nextpagetoken pending."""
        p1 = {
            "items": [
                {
                    "snippet": {
                        "title": "Track 1",
                        "resourceId": {"videoId": "v1"},
                        "videoOwnerChannelTitle": "Artist 1",
                    },
                    "status": {"privacyStatus": "public"},
                }
            ],
            "nextPageToken": "token_page_2",
        }
        mock_session = MagicMock(spec=requests.Session)
        mock_session.get.return_value = self._make_mock_response(p1)

        adapter = YouTubeAdapter(api_key="valid_key", session=mock_session, max_pages=1)
        with self.assertRaises(SourceError) as ctx:
            adapter.fetch_sources(["https://www.youtube.com/playlist?list=PLexhaust"], use_cache_if_available=False)
        self.assertIn("exceeded maximum page limit (1)", str(ctx.exception))

    def test_cache_save_and_load_isolation(self):
        """verifies youtube tracks are cached to dedicated cache file and reload cleanly."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_file = Path(tmp_dir) / "youtube_tracks.cache.json"

            track = CanonicalTrack(
                title="Cached Track",
                artists=["Cached Artist"],
                album="Cached Album",
                duration_ms=200000,
                external_ids={"youtube": "cached_vid"},
                provider_id="youtube",
                source_uri="https://www.youtube.com/playlist?list=PLcache",
            )

            adapter = YouTubeAdapter(cache_path=cache_file, ttl_hours=12.0)
            url = "https://www.youtube.com/playlist?list=PLcache"

            # save cache
            adapter._save_cache([track], [url])
            self.assertTrue(cache_file.is_file())

            # load cache without network calls
            with patch.object(adapter, "_fetch_playlist_youtubei", side_effect=RuntimeError("No network")):
                cached_tracks = adapter.fetch_sources([url], use_cache_if_available=True)
                self.assertEqual(len(cached_tracks), 1)
                self.assertEqual(cached_tracks[0].title, "Cached Track")
                self.assertEqual(cached_tracks[0].external_ids["youtube"], "cached_vid")

    def test_queue_checkpoint_identity_isolation(self):
        """verifies youtube tracks generate youtube:: prefixed queue identifiers."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "test_queue.db"
            tracker = QuotaTracker(db_path=db_path)

            canonical = CanonicalTrack(
                title="Song",
                artists=["Artist"],
                album="Album",
                duration_ms=180000,
                external_ids={"youtube": "yt_123"},
                provider_id="youtube",
                source_uri="https://www.youtube.com/watch?v=yt_123",
            )
            legacy = to_legacy_track(canonical, track_number=1)

            self.assertIsNone(legacy.spotify_id)
            self.assertEqual(legacy.provider_id, "youtube")

            ident = _track_identifier(legacy)
            self.assertTrue(ident.startswith("youtube::"))
            self.assertNotIn("spotify:", ident)

            qm = QueueManager(tracks=[legacy], tracker=tracker, shuffle=False, loop=False)
            self.assertEqual(qm.total_tracks, 1)

    def test_youtube_only_ingestion_does_not_instantiate_spotify(self):
        """verifies that a youtube-only source list does not instantiate spotifyclient."""
        with patch("src.sources.service.SpotifyClient") as mock_spotify_cls:
            service = SourceIngestionService()

            # mock youtubeadapter.fetch_sources
            mock_track = CanonicalTrack(
                title="YT Track",
                artists=["YT Artist"],
                duration_ms=180000,
                external_ids={"youtube": "yt_abc"},
                provider_id="youtube",
                source_uri="https://www.youtube.com/watch?v=yt_abc",
            )

            with patch.object(YouTubeAdapter, "fetch_sources", return_value=[mock_track]):
                tracks = service.fetch_sources(["https://www.youtube.com/watch?v=yt_abc"])

            self.assertEqual(len(tracks), 1)
            self.assertEqual(tracks[0].artist, "YT Artist")
            # spotifyclient must never be instantiated for youtube-only sources
            mock_spotify_cls.assert_not_called()

    def test_mixed_spotify_and_youtube_source_ordering(self):
        """verifies mixed spotify and youtube sources preserve configured interleaving."""
        sources = [
            "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT",
        ]

        spotify_track = CanonicalTrack(
            title="Spotify Song",
            artists=["Spotify Artist"],
            external_ids={"spotify": "4cOdK2wGLETKBW3PvgPWqT"},
            provider_id="spotify",
            source_uri=sources[0],
        )
        yt_track = CanonicalTrack(
            title="YouTube Song",
            artists=["YouTube Artist"],
            external_ids={"youtube": "dQw4w9WgXcQ"},
            provider_id="youtube",
            source_uri=sources[1],
        )

        with patch("src.sources.spotify_adapter.SpotifyAdapter.fetch_sources", return_value=[spotify_track]), \
             patch.object(YouTubeAdapter, "fetch_sources", return_value=[yt_track]):
            service = SourceIngestionService()
            tracks = service.fetch_sources(sources)

        self.assertEqual(len(tracks), 3)
        self.assertEqual(tracks[0].title, "Spotify Song")
        self.assertEqual(tracks[1].title, "YouTube Song")
        self.assertEqual(tracks[2].title, "Spotify Song")

    def test_two_column_desktop_playlist_parsing(self):
        """verifies youtube music twocolumnbrowseresultsrenderer desktop layout is correctly parsed."""
        mock_json = {
            "contents": {
                "twoColumnBrowseResultsRenderer": {
                    "secondaryContents": {
                        "sectionListRenderer": {
                            "contents": [
                                {
                                    "musicPlaylistShelfRenderer": {
                                        "contents": [
                                            {
                                                "musicResponsiveListItemRenderer": {
                                                    "playlistItemData": {"videoId": "vid_2col"},
                                                    "flexColumns": [
                                                        {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "Synthwave Song"}]}}},
                                                        {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "Retro Artist"}]}}},
                                                    ],
                                                }
                                            }
                                        ]
                                    }
                                }
                            ]
                        }
                    }
                }
            }
        }
        mock_session = MagicMock(spec=requests.Session)
        mock_session.post.return_value = self._make_mock_response(mock_json)

        adapter = YouTubeAdapter(session=mock_session)
        tracks = adapter.fetch_sources(["https://music.youtube.com/playlist?list=PLtwocol"], use_cache_if_available=False)

        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0].title, "Synthwave Song")
        self.assertEqual(tracks[0].primary_artist, "Retro Artist")
        self.assertEqual(tracks[0].external_ids.get("youtube"), "vid_2col")

    def test_missing_or_private_playlist_raises_source_error(self):
        """verifies that a private or non-existent playlist without shelves raises a descriptive sourceerror."""
        mock_json = {
            "contents": {},
            "alerts": [
                {
                    "alertRenderer": {
                        "type": "ERROR",
                        "text": {"runs": [{"text": "The playlist does not exist."}]},
                    }
                }
            ],
        }
        mock_session = MagicMock(spec=requests.Session)
        mock_session.post.return_value = self._make_mock_response(mock_json)

        adapter = YouTubeAdapter(session=mock_session)
        with self.assertRaises(SourceError) as ctx:
            adapter.fetch_sources(["https://music.youtube.com/playlist?list=PLprivate"], use_cache_if_available=False)

        self.assertIn("The playlist does not exist", str(ctx.exception))
        self.assertIn("Public or Unlisted", str(ctx.exception))


class TestYouTubeMetadataRemediationRegression(unittest.TestCase):
    """regression suite validating remediation of metadata defects from real youtube playlists."""

    def _make_mock_response(self, json_data, status_code=200):
        resp = MagicMock(spec=requests.Response)
        resp.status_code = status_code
        resp.json.return_value = json_data
        if status_code >= 400:
            resp.raise_for_status.side_effect = requests.exceptions.HTTPError(response=resp)
        else:
            resp.raise_for_status.return_value = None
        return resp

    def test_six_representative_metadata_defects(self):
        """verifies remediation of all six representative real-world defects."""
        # defect 1: structured artist with promotional video label in title
        renderer_1 = {
            "playlistItemData": {"videoId": "vid_d1"},
            "flexColumns": [
                {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "This Is America (Official Video)"}]}}},
                {
                    "musicResponsiveListItemFlexColumnRenderer": {
                        "text": {
                            "runs": [
                                {
                                    "text": "Childish Gambino",
                                    "navigationEndpoint": {
                                        "browseEndpoint": {
                                            "browseEndpointContextSupportedConfigs": {
                                                "browseEndpointContextMusicConfig": {"pageType": "MUSIC_PAGE_TYPE_ARTIST"}
                                            }
                                        }
                                    },
                                }
                            ]
                        }
                    }
                },
            ],
            "fixedColumns": [
                {"musicResponsiveListItemFixedColumnRenderer": {"text": {"runs": [{"text": "4:05"}]}}}
            ],
        }
        # defect 2: uploader channel as artist run, true artist in title
        renderer_2 = {
            "playlistItemData": {"videoId": "vid_d2"},
            "flexColumns": [
                {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "A$AP Rocky - LVL (music video)"}]}}},
                {
                    "musicResponsiveListItemFlexColumnRenderer": {
                        "text": {
                            "runs": [
                                {
                                    "text": "skinnykidmiami",
                                    "navigationEndpoint": {
                                        "browseEndpoint": {
                                            "browseEndpointContextSupportedConfigs": {
                                                "browseEndpointContextMusicConfig": {"pageType": "MUSIC_PAGE_TYPE_USER_CHANNEL"}
                                            }
                                        }
                                    },
                                }
                            ]
                        }
                    }
                },
            ],
            "fixedColumns": [
                {"musicResponsiveListItemFixedColumnRenderer": {"text": {"runs": [{"text": "3:42"}]}}}
            ],
        }
        # defect 3: lyrics tag in title, uploader channel as artist
        renderer_3 = {
            "playlistItemData": {"videoId": "vid_d3"},
            "flexColumns": [
                {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "Drake - One Dance (Lyrics) ft. Wizkid & Kyla"}]}}},
                {
                    "musicResponsiveListItemFlexColumnRenderer": {
                        "text": {
                            "runs": [
                                {
                                    "text": "Billion Stars",
                                    "navigationEndpoint": {
                                        "browseEndpoint": {
                                            "browseEndpointContextSupportedConfigs": {
                                                "browseEndpointContextMusicConfig": {"pageType": "MUSIC_PAGE_TYPE_USER_CHANNEL"}
                                            }
                                        }
                                    },
                                }
                            ]
                        }
                    }
                },
            ],
            "fixedColumns": [
                {"musicResponsiveListItemFixedColumnRenderer": {"text": {"runs": [{"text": "2:55"}]}}}
            ],
        }
        # defect 4: uploader ikisaigo, title frank ocean - ivy (music video)
        renderer_4 = {
            "playlistItemData": {"videoId": "vid_d4"},
            "flexColumns": [
                {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "Frank Ocean - Ivy (Music Video)"}]}}},
                {
                    "musicResponsiveListItemFlexColumnRenderer": {
                        "text": {
                            "runs": [
                                {
                                    "text": "ikisaigo",
                                    "navigationEndpoint": {
                                        "browseEndpoint": {
                                            "browseEndpointContextSupportedConfigs": {
                                                "browseEndpointContextMusicConfig": {"pageType": "MUSIC_PAGE_TYPE_USER_CHANNEL"}
                                            }
                                        }
                                    },
                                }
                            ]
                        }
                    }
                },
            ],
            "fixedColumns": [
                {"musicResponsiveListItemFixedColumnRenderer": {"text": {"runs": [{"text": "4:10"}]}}}
            ],
        }
        # defect 5: uploader thegoodvibe, title frank ocean - lost (lyrics)
        renderer_5 = {
            "playlistItemData": {"videoId": "vid_d5"},
            "flexColumns": [
                {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "Frank Ocean - Lost (Lyrics)"}]}}},
                {
                    "musicResponsiveListItemFlexColumnRenderer": {
                        "text": {
                            "runs": [
                                {
                                    "text": "TheGoodVibe",
                                    "navigationEndpoint": {
                                        "browseEndpoint": {
                                            "browseEndpointContextSupportedConfigs": {
                                                "browseEndpointContextMusicConfig": {"pageType": "MUSIC_PAGE_TYPE_USER_CHANNEL"}
                                            }
                                        }
                                    },
                                }
                            ]
                        }
                    }
                },
            ],
            "fixedColumns": [
                {"musicResponsiveListItemFixedColumnRenderer": {"text": {"runs": [{"text": "3:55"}]}}}
            ],
        }
        # defect 6: unidentified placeholder artist, title drake - it's up ft. young thug, 21 savage
        renderer_6 = {
            "playlistItemData": {"videoId": "vid_d6"},
            "flexColumns": [
                {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "Drake - It's Up ft. Young Thug, 21 Savage"}]}}},
                {
                    "musicResponsiveListItemFlexColumnRenderer": {
                        "text": {
                            "runs": [
                                {
                                    "text": "Unidentified",
                                    "navigationEndpoint": {
                                        "browseEndpoint": {
                                            "browseEndpointContextSupportedConfigs": {
                                                "browseEndpointContextMusicConfig": {"pageType": "MUSIC_PAGE_TYPE_USER_CHANNEL"}
                                            }
                                        }
                                    },
                                }
                            ]
                        }
                    }
                },
            ],
            "fixedColumns": [
                {"musicResponsiveListItemFixedColumnRenderer": {"text": {"runs": [{"text": "4:45"}]}}}
            ],
        }

        mock_shelf_items = [
            {"musicResponsiveListItemRenderer": r}
            for r in (renderer_1, renderer_2, renderer_3, renderer_4, renderer_5, renderer_6)
        ]
        mock_json = {
            "contents": {
                "twoColumnBrowseResultsRenderer": {
                    "secondaryContents": {
                        "sectionListRenderer": {
                            "contents": [{"musicPlaylistShelfRenderer": {"contents": mock_shelf_items}}]
                        }
                    }
                }
            }
        }

        adapter = YouTubeAdapter(session=MagicMock())
        adapter._session.post.return_value = self._make_mock_response(mock_json)
        tracks = adapter.fetch_sources(["https://music.youtube.com/playlist?list=PLdefects"], use_cache_if_available=False)

        self.assertEqual(len(tracks), 6)

        # 1. this is america
        self.assertEqual(tracks[0].title, "This Is America")
        self.assertEqual(tracks[0].artists, ["Childish Gambino"])
        self.assertEqual(tracks[0].duration_ms, 245000)

        # 2. lvl
        self.assertEqual(tracks[1].title, "LVL")
        self.assertEqual(tracks[1].artists, ["A$AP Rocky"])
        self.assertEqual(tracks[1].duration_ms, 222000)

        # 3. one dance ft. wizkid & kyla
        self.assertEqual(tracks[2].title, "One Dance ft. Wizkid & Kyla")
        self.assertEqual(tracks[2].artists, ["Drake"])
        self.assertEqual(tracks[2].duration_ms, 175000)

        # 4. ivy
        self.assertEqual(tracks[3].title, "Ivy")
        self.assertEqual(tracks[3].artists, ["Frank Ocean"])
        self.assertEqual(tracks[3].duration_ms, 250000)

        # 5. lost
        self.assertEqual(tracks[4].title, "Lost")
        self.assertEqual(tracks[4].artists, ["Frank Ocean"])
        self.assertEqual(tracks[4].duration_ms, 235000)

        # 6. it's up ft. young thug, 21 savage
        self.assertEqual(tracks[5].title, "It's Up ft. Young Thug, 21 Savage")
        self.assertEqual(tracks[5].artists, ["Drake"])
        self.assertEqual(tracks[5].duration_ms, 285000)

    def test_legitimate_musical_versions_preserved(self):
        """verifies that live, acoustic, remix, and remastered versions are preserved."""
        cases = [
            ("Redbone (Live From The Tonight Show Starring Jimmy Fallon)", "Redbone (Live From The Tonight Show Starring Jimmy Fallon)"),
            ("Dear April (Side A - Acoustic)", "Dear April (Side A - Acoustic)"),
            ("Track Name (Club Remix)", "Track Name (Club Remix)"),
            ("Track Name (2020 Remaster)", "Track Name (2020 Remaster)"),
            ("Track Name (Remastered)", "Track Name (Remastered)"),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(YouTubeMetadataNormalizer.clean_title(raw), expected)

    def test_duration_recovery_and_preservation(self):
        """verifies that authentic duration is parsed from fixedcolumns and duration_ms=none is preserved if missing."""
        renderer_with_dur = {
            "playlistItemData": {"videoId": "vid_dur"},
            "flexColumns": [
                {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "Song With Dur"}]}}},
                {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "Artist A"}]}}},
            ],
            "fixedColumns": [
                {"musicResponsiveListItemFixedColumnRenderer": {"text": {"runs": [{"text": "3:11"}]}}}
            ],
        }
        renderer_no_dur = {
            "playlistItemData": {"videoId": "vid_nodur"},
            "flexColumns": [
                {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "Song Without Dur"}]}}},
                {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "Artist B"}]}}},
            ],
            "fixedColumns": [],
        }

        mock_json = {
            "contents": {
                "singleColumnBrowseResultsRenderer": {
                    "tabs": [
                        {
                            "tabRenderer": {
                                "content": {
                                    "sectionListRenderer": {
                                        "contents": [
                                            {
                                                "musicPlaylistShelfRenderer": {
                                                    "contents": [
                                                        {"musicResponsiveListItemRenderer": renderer_with_dur},
                                                        {"musicResponsiveListItemRenderer": renderer_no_dur},
                                                    ]
                                                }
                                            }
                                        ]
                                    }
                                }
                            }
                        }
                    ]
                }
            }
        }
        adapter = YouTubeAdapter(session=MagicMock())
        adapter._session.post.return_value = self._make_mock_response(mock_json)
        tracks = adapter.fetch_sources(["https://music.youtube.com/playlist?list=PLdur"], use_cache_if_available=False)

        self.assertEqual(len(tracks), 2)
        self.assertEqual(tracks[0].duration_ms, 191000)
        self.assertIsNone(tracks[1].duration_ms)

    def test_cache_schema_version_invalidation_and_roundtrip(self):
        """old normalization schemas are invalidated and current caches round-trip."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "yt_cache.json"
            adapter = YouTubeAdapter(cache_path=cache_file)

            # 1. stale cache without schema_version
            stale_payload = {
                "saved_at": 9999999999,  # far future to avoid ttl expiry
                "sources": ["https://music.youtube.com/playlist?list=PL1"],
                "sources_hash": "dummy_hash",
                "tracks": [{"title": "Old", "artists": ["Old"], "duration_ms": None}],
            }
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(stale_payload, f)

            loaded = adapter._load_cache(["https://music.youtube.com/playlist?list=PL1"])
            self.assertIsNone(loaded, "Unversioned cache must be invalidated as miss")

            # 2. write a cache with the current normalization schema.
            canonical = [
                CanonicalTrack(
                    title="Clean Song",
                    artists=["Clean Artist"],
                    album="Clean Album",
                    duration_ms=210000,
                    external_ids={"youtube": "v123"},
                    provider_id="youtube",
                    source_uri="https://music.youtube.com/playlist?list=PL1",
                    source_name="Playlist 1",
                ),
                CanonicalTrack(
                    title="Unknown Dur Song",
                    artists=["Clean Artist"],
                    album=None,
                    duration_ms=None,
                    external_ids={"youtube": "v124"},
                    provider_id="youtube",
                    source_uri="https://music.youtube.com/playlist?list=PL1",
                    source_name="Playlist 1",
                ),
            ]
            adapter._save_cache(canonical, ["https://music.youtube.com/playlist?list=PL1"])

            with open(cache_file, "r", encoding="utf-8") as f:
                saved_json = json.load(f)
            self.assertEqual(saved_json.get("schema_version"), adapter.cache_schema_version)

            # 3. read back the current cache.
            reloaded = adapter._load_cache(["https://music.youtube.com/playlist?list=PL1"])
            self.assertIsNotNone(reloaded)
            self.assertEqual(len(reloaded), 2)
            self.assertEqual(reloaded[0].title, "Clean Song")
            self.assertEqual(reloaded[0].duration_ms, 210000)
            self.assertIsNone(reloaded[1].duration_ms)
            self.assertEqual(reloaded[1].external_ids.get("youtube"), "v124")

    def test_duplicate_playlist_entries_preserved(self):
        """verifies that duplicate occurrences in the playlist are preserved in original order."""
        renderer_item = {
            "playlistItemData": {"videoId": "vid_dup"},
            "flexColumns": [
                {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "Mask Off"}]}}},
                {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "Future"}]}}},
            ],
            "fixedColumns": [
                {"musicResponsiveListItemFixedColumnRenderer": {"text": {"runs": [{"text": "3:24"}]}}}
            ],
        }
        mock_json = {
            "contents": {
                "singleColumnBrowseResultsRenderer": {
                    "tabs": [
                        {
                            "tabRenderer": {
                                "content": {
                                    "sectionListRenderer": {
                                        "contents": [
                                            {
                                                "musicPlaylistShelfRenderer": {
                                                    "contents": [
                                                        {"musicResponsiveListItemRenderer": renderer_item},
                                                        {"musicResponsiveListItemRenderer": renderer_item},
                                                    ]
                                                }
                                            }
                                        ]
                                    }
                                }
                            }
                        }
                    ]
                }
            }
        }
        adapter = YouTubeAdapter(session=MagicMock())
        adapter._session.post.return_value = self._make_mock_response(mock_json)
        tracks = adapter.fetch_sources(["https://music.youtube.com/playlist?list=PLdups"], use_cache_if_available=False)

        self.assertEqual(len(tracks), 2)
        self.assertEqual(tracks[0].external_ids["youtube"], "vid_dup")
        self.assertEqual(tracks[1].external_ids["youtube"], "vid_dup")

    def test_ambiguous_artist_omission_and_diagnostics(self):
        """verifies that items without reliable artist information are omitted with diagnostic reason."""
        renderer_ambiguous = {
            "playlistItemData": {"videoId": "vid_vlog"},
            "flexColumns": [
                {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "Vlog Episode 42"}]}}},
                {
                    "musicResponsiveListItemFlexColumnRenderer": {
                        "text": {
                            "runs": [
                                {
                                    "text": "DailyVlogger",
                                    "navigationEndpoint": {
                                        "browseEndpoint": {
                                            "browseEndpointContextSupportedConfigs": {
                                                "browseEndpointContextMusicConfig": {"pageType": "MUSIC_PAGE_TYPE_USER_CHANNEL"}
                                            }
                                        }
                                    },
                                }
                            ]
                        }
                    }
                },
            ],
            "fixedColumns": [],
        }
        mock_json = {
            "contents": {
                "singleColumnBrowseResultsRenderer": {
                    "tabs": [
                        {
                            "tabRenderer": {
                                "content": {
                                    "sectionListRenderer": {
                                        "contents": [
                                            {
                                                "musicPlaylistShelfRenderer": {
                                                    "contents": [
                                                        {"musicResponsiveListItemRenderer": renderer_ambiguous},
                                                    ]
                                                }
                                            }
                                        ]
                                    }
                                }
                            }
                        }
                    ]
                }
            }
        }
        adapter = YouTubeAdapter(session=MagicMock())
        adapter._session.post.return_value = self._make_mock_response(mock_json)

        with self.assertRaises(SourceError) as ctx:
            adapter.fetch_sources(["https://music.youtube.com/playlist?list=PLall_bad"], use_cache_if_available=False)

        self.assertIn("all 1 items were ambiguous or unavailable", str(ctx.exception))
        self.assertEqual(len(adapter.last_omissions), 1)
        self.assertEqual(adapter.last_omissions[0].reason, "ambiguous_metadata")
        self.assertEqual(adapter.last_omissions[0].video_id, "vid_vlog")

    def test_unknown_duration_queue_and_timeline_lifecycle(self):
        """verifies that a track with duration_ms=none converts to legacy track, loads into queue, and operates safely."""
        canonical = CanonicalTrack(
            title="Unknown Duration Track",
            artists=["Ambient Artist"],
            album=None,
            duration_ms=None,
            external_ids={"youtube": "amb_001"},
            provider_id="youtube",
            source_uri="https://youtu.be/amb_001",
            source_name="Single Track",
        )
        self.assertIsNone(canonical.duration_ms)

        # 1. convert to legacy track
        legacy = to_legacy_track(canonical, track_number=1)
        self.assertEqual(legacy.duration_ms, 180000, "Legacy engine pacing defaults to 180000ms")
        self.assertEqual(legacy.duration_sec, 180)
        self.assertEqual(legacy.artist, "Ambient Artist")

        # 2. queuemanager integration
        qm = QueueManager([legacy], tracker=None, shuffle=False, loop=False)
        self.assertEqual(qm.total_tracks, 1)

        t = qm.get_next_track()
        self.assertIsNotNone(t)
        self.assertEqual(t.duration_ms, 180000)
        self.assertEqual(t.provider_id, "youtube")
        qm.commit_track_progress()
        self.assertIsNone(qm.get_next_track())

    def test_artist_name_normalization_encoding(self):
        """verifies normalization of known stylized or encoding-corrupted artists."""
        self.assertEqual(YouTubeMetadataNormalizer.normalize_artist("JAŸ-Z"), "JAY-Z")
        self.assertEqual(YouTubeMetadataNormalizer.normalize_artist("JA\u0178-Z"), "JAY-Z")
        self.assertEqual(YouTubeMetadataNormalizer.normalize_artist("JA\ufffd-Z"), "JAY-Z")
        self.assertEqual(YouTubeMetadataNormalizer.normalize_artist("Kendrick Lamar"), "Kendrick Lamar")

    def test_topic_channel_preserves_artist_when_title_has_hyphen_or_version_suffix(self):
        """scrobbleforge-e8y: topic channel identity is preserved when song title contains hyphens."""
        title, artists, conf = YouTubeMetadataNormalizer.extract_artist_and_title(
            "Love - 2011 Remaster",
            channel_title="Real Artist - Topic",
        )
        self.assertEqual(title, "Love - 2011 Remaster")
        self.assertEqual(artists, ["Real Artist"])
        self.assertEqual(conf, "topic_channel")

    def test_topic_channel_normalizes_explicit_artist_prefixed_title(self):
        """scrobbleforge-e8y: topic channel title explicitly prefixed by artist normalizes title."""
        title, artists, conf = YouTubeMetadataNormalizer.extract_artist_and_title(
            "Real Artist - Love - 2011 Remaster",
            channel_title="Real Artist - Topic",
        )
        self.assertEqual(title, "Love - 2011 Remaster")
        self.assertEqual(artists, ["Real Artist"])
        self.assertEqual(conf, "reliable_parsed")

    def test_topic_channel_preserves_version_suffixes(self):
        """scrobbleforge-e8y: version suffixes and hyphens inside title remain intact."""
        title, artists, conf = YouTubeMetadataNormalizer.extract_artist_and_title(
            "Track Title - Extended Mix",
            channel_title="Electronic Duo - Topic",
        )
        self.assertEqual(title, "Track Title - Extended Mix")
        self.assertEqual(artists, ["Electronic Duo"])
        self.assertEqual(conf, "topic_channel")

    def test_topic_channel_explicit_featuring_prefix(self):
        """scrobbleforge-e8y: topic channel with explicit featuring artists in title prefix."""
        title, artists, conf = YouTubeMetadataNormalizer.extract_artist_and_title(
            "Main Artist feat. Guest - Hit Song",
            channel_title="Main Artist - Topic",
        )
        self.assertEqual(title, "Hit Song")
        self.assertEqual(artists, ["Main Artist", "Guest"])
        self.assertEqual(conf, "reliable_parsed")

    def test_non_topic_channel_splits_artist_delimiter(self):
        """scrobbleforge-e8y: non-topic channel splits on delimiter normally."""
        title, artists, conf = YouTubeMetadataNormalizer.extract_artist_and_title(
            "Some Singer - My Track",
            channel_title="Some Singer Official",
        )
        self.assertEqual(title, "My Track")
        self.assertEqual(artists, ["Some Singer"])
        self.assertEqual(conf, "reliable_parsed")


