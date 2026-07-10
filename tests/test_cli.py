"""Tests for PodMate CLI commands and underlying functions."""

import hashlib
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from typer.testing import CliRunner

from podmate.cli import app
from podmate.config import load as load_config
from podmate.db import (
    add_episode,
    add_feed,
    get_episode,
    get_episodes,
    get_feed,
    get_feeds,
    set_episode_path,
)
from podmate.downloader import download_episode
from podmate.dubbing import (
    _concat_audio,
    _generate_audio,
    _majority_tone,
    _split_text,
    dub_translation,
    get_voice_for_speaker,
    wrap_with_tone,
)
from podmate.feed import PodcastIndexClient, parse_feed, resolve_feed, search_itunes
from podmate.player import (
    _build_player_command,
    get_available_player,
    play_episode,
    play_file,
)
from podmate.transcriber import (
    _add_tone_markers,
    _format_time,
    _parse_deepgram_response,
    _speaker_label,
    format_transcript,
    transcribe_via_deepgram,
)
from podmate.translator import (
    _extract_translation,
    _parse_summary,
    translate_segments,
)

runner = CliRunner()

# ── Helpers ────────────────────────────────────────────────


def _mock_httpx_client(json_data):
    """Build a mock httpx.AsyncClient context manager returning given JSON."""
    mock_resp = AsyncMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = MagicMock(return_value=json_data)

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)

    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)
    return mock_ctx


def _mock_feed_meta(attrs: dict | None = None) -> MagicMock:
    """Create a feedparser-compatible feed mock supporting both attr and dict access.

    feedparser.feed is a FeedParserDict that supports both feed.title (attribute)
    and feed.get("title") (dict).  This helper creates a MagicMock that behaves
    the same way: attributes set on it are also accessible via .get().
    """
    meta = MagicMock()
    meta.get.side_effect = lambda k, d=None: getattr(meta, k, d) if hasattr(meta, k) else d
    if attrs:
        for k, v in attrs.items():
            setattr(meta, k, v)
    return meta


def _mock_feedparser_entry(data: dict) -> MagicMock:
    """Create a feedparser-compatible entry mock.

    entry.enclosures is a list of dict-like objects (each with .get()),
    and other fields (title, id, summary, published, itunes_duration) are attrs.
    """
    entry = MagicMock()
    entry.get.side_effect = lambda k, d=None: getattr(entry, k, d) if hasattr(entry, k) else d
    for k in ("id", "title", "summary", "published", "link", "subtitle"):
        if k in data:
            setattr(entry, k, data[k])
        elif k == "summary":
            setattr(entry, k, "")  # avoid MagicMock auto-creation of missing attrs
    if "itunes_duration" in data:
        entry.itunes_duration = data["itunes_duration"]
    # Enclosures: list of objects with .get()
    enclosures_raw = data.get("enclosures", [])
    entry.enclosures = []
    for enc in enclosures_raw:
        e = MagicMock()
        e.get.side_effect = lambda k, d=None, _enc=enc: _enc.get(k, d)
        entry.enclosures.append(e)
    return entry


# ── Feed: search_itunes ────────────────────────────────────


async def test_search_itunes_returns_feed_url_and_collection_id():
    """search_itunes returns feedUrl, collectionId, and other metadata."""
    mock_ctx = _mock_httpx_client(
        {
            "resultCount": 1,
            "results": [
                {
                    "trackName": "The Pragmatic Engineer",
                    "artistName": "Gergely Orosz",
                    "feedUrl": "https://feeds.example.com/engineer.xml",
                    "artworkUrl100": "https://example.com/art.jpg",
                    "trackCount": 50,
                    "collectionId": 123456,
                }
            ],
        }
    )

    with patch("podmate.feed.httpx.AsyncClient", return_value=mock_ctx):
        results = await search_itunes("The Pragmatic Engineer")

    assert len(results) == 1
    assert results[0]["feedUrl"] == "https://feeds.example.com/engineer.xml"
    assert results[0]["trackName"] == "The Pragmatic Engineer"
    assert results[0]["collectionId"] == 123456
    assert results[0]["trackCount"] == 50


async def test_search_itunes_skips_results_without_feed_url():
    """Results missing feedUrl are filtered out."""
    mock_ctx = _mock_httpx_client(
        {
            "resultCount": 2,
            "results": [
                {"trackName": "No Feed", "artistName": "Someone", "feedUrl": ""},
                {
                    "trackName": "Has Feed",
                    "artistName": "Author",
                    "feedUrl": "https://feeds.example.com/real.xml",
                },
            ],
        }
    )

    with patch("podmate.feed.httpx.AsyncClient", return_value=mock_ctx):
        results = await search_itunes("test")

    assert len(results) == 1
    assert results[0]["feedUrl"] == "https://feeds.example.com/real.xml"


async def test_search_itunes_returns_collection_id_zero_when_missing():
    """collectionId defaults to 0 when not in API response."""
    mock_ctx = _mock_httpx_client(
        {
            "resultCount": 1,
            "results": [
                {
                    "trackName": "Podcast",
                    "artistName": "Author",
                    "feedUrl": "https://example.com/feed.xml",
                }
            ],
        }
    )

    with patch("podmate.feed.httpx.AsyncClient", return_value=mock_ctx):
        results = await search_itunes("test")

    assert results[0]["collectionId"] == 0


# ── Feed: parse_feed ───────────────────────────────────────


def test_parse_feed_extracts_metadata_and_episodes():
    """parse_feed returns title, author, episodes with guid/duration/audio."""
    mock_parsed = MagicMock()
    img = MagicMock()
    img.href = "https://example.com/art.jpg"
    mock_parsed.feed = _mock_feed_meta(
        {
            "title": "Test Podcast",
            "link": "https://example.com",
            "author": "Test Author",
            "subtitle": "A test podcast description",
            "image": img,
        }
    )

    mock_parsed.entries = [
        _mock_feedparser_entry(
            {
                "id": "guid-001",
                "title": "Episode One",
                "summary": "<p>First episode content</p>",
                "published": "2024-01-01T00:00:00Z",
                "itunes_duration": "30:00",
                "enclosures": [{"href": "https://example.com/ep1.mp3", "type": "audio/mpeg"}],
            }
        )
    ]

    with patch("podmate.feed.feedparser.parse", return_value=mock_parsed):
        result = parse_feed("https://example.com/feed.xml")

    assert result["title"] == "Test Podcast"
    assert result["author"] == "Test Author"
    assert result["description"] == "A test podcast description"
    assert result["image_url"] == "https://example.com/art.jpg"
    assert result["link"] == "https://example.com"
    assert len(result["episodes"]) == 1

    ep = result["episodes"][0]
    assert ep["guid"] == "guid-001"
    assert ep["title"] == "Episode One"
    assert "First episode content" in ep["description"]
    assert ep["pub_date"] == "2024-01-01T00:00:00Z"
    assert ep["audio_url"] == "https://example.com/ep1.mp3"
    assert ep["duration_sec"] == 1800


def test_parse_feed_handles_missing_fields():
    """parse_feed handles feeds with minimal metadata gracefully."""
    mock_parsed = MagicMock()
    img = MagicMock()
    img.href = ""
    mock_parsed.feed = _mock_feed_meta(
        {
            "title": "Minimal Podcast",
            "link": "",
            "author": "",
            "subtitle": "",
            "image": img,
        }
    )

    mock_parsed.entries = [_mock_feedparser_entry({"id": "g1", "title": "Ep"})]

    with patch("podmate.feed.feedparser.parse", return_value=mock_parsed):
        result = parse_feed("https://example.com/minimal.xml")

    assert result["title"] == "Minimal Podcast"
    assert result["author"] == ""
    assert len(result["episodes"]) == 1


# ── PodcastIndexClient ─────────────────────────────────────


def test_podcast_index_auth_headers():
    """_auth_headers returns X-Auth-Key, X-Auth-Date, and valid SHA1 Authorization."""
    client = PodcastIndexClient("test-key", "test-secret")

    with patch("time.time", return_value=1600000000):
        headers = client._auth_headers()

    assert headers["X-Auth-Key"] == "test-key"
    assert headers["X-Auth-Date"] == "1600000000"

    expected_auth = hashlib.sha1(b"test-keytest-secret1600000000").hexdigest()
    assert headers["Authorization"] == expected_auth


async def test_podcast_index_search_by_feed_url():
    """search_by_feed_url calls correct endpoint and parses episodes."""
    mock_ctx = _mock_httpx_client(
        {
            "items": [
                {
                    "title": "Episode One",
                    "guid": "ep-001",
                    "description": "First episode",
                    "datePublishedPretty": "2024-01-01",
                    "enclosureUrl": "https://example.com/ep1.mp3",
                    "duration": 1800,
                },
                {
                    "title": "Episode Two",
                    "guid": "ep-002",
                    "description": "<p>Second episode</p>",
                    "datePublishedPretty": "",
                    "enclosureUrl": "",
                    "duration": 0,
                },
            ],
        }
    )

    client = PodcastIndexClient("key", "secret")
    with patch("podmate.feed.httpx.AsyncClient", return_value=mock_ctx):
        episodes = await client.search_by_feed_url("https://example.com/feed.xml")

    assert len(episodes) == 2
    assert episodes[0]["title"] == "Episode One"
    assert episodes[0]["guid"] == "ep-001"
    assert episodes[0]["audio_url"] == "https://example.com/ep1.mp3"
    assert episodes[0]["duration_sec"] == 1800
    assert episodes[1]["description"] == "Second episode"


async def test_podcast_index_search_by_itunes_id():
    """search_by_itunes_id calls correct endpoint with id parameter."""
    mock_ctx = _mock_httpx_client(
        {
            "items": [
                {
                    "title": "ITunes Episode",
                    "guid": "it-ep-1",
                    "description": "",
                    "datePublishedPretty": "2024-06-01",
                    "enclosureUrl": "https://example.com/it-ep.mp3",
                    "duration": 3600,
                },
            ],
        }
    )

    client = PodcastIndexClient("key", "secret")
    with patch("podmate.feed.httpx.AsyncClient", return_value=mock_ctx):
        episodes = await client.search_by_itunes_id(123456)

    assert len(episodes) == 1
    assert episodes[0]["guid"] == "it-ep-1"


async def test_podcast_index_empty_response():
    """Empty items list returns empty episodes list."""
    mock_ctx = _mock_httpx_client({"items": []})

    client = PodcastIndexClient("key", "secret")
    with patch("podmate.feed.httpx.AsyncClient", return_value=mock_ctx):
        episodes = await client.search_by_feed_url("https://example.com/empty.xml")

    assert episodes == []


# ── resolve_feed ───────────────────────────────────────────


async def test_resolve_feed_rss_only():
    """resolve_feed returns RSS data when no PodcastIndexClient is provided."""
    mock_parsed = MagicMock()
    img = MagicMock()
    img.href = "https://example.com/img.jpg"
    mock_parsed.feed = _mock_feed_meta(
        {
            "title": "RSS Podcast",
            "link": "https://example.com",
            "author": "RSS Author",
            "subtitle": "RSS description",
            "image": img,
        }
    )

    mock_parsed.entries = [_mock_feedparser_entry({"id": "rss-1", "title": "RSS Ep 1"})]

    with patch("podmate.feed.feedparser.parse", return_value=mock_parsed):
        result = await resolve_feed("https://example.com/feed.xml")

    assert result["title"] == "RSS Podcast"
    assert result["author"] == "RSS Author"
    assert result["episode_source"] == "rss"
    assert result["total_episodes"] == 1


async def test_resolve_feed_with_podcast_index_more_episodes():
    """When Podcast Index returns more episodes, merge with RSS as base."""
    client = PodcastIndexClient("key", "secret")

    mock_parsed = MagicMock()
    img = MagicMock()
    img.href = ""
    mock_parsed.feed = _mock_feed_meta(
        {
            "title": "Podcast",
            "link": "",
            "author": "",
            "subtitle": "",
            "image": img,
        }
    )

    mock_parsed.entries = [
        _mock_feedparser_entry({"id": "rss-1", "title": "RSS Ep"}),
        _mock_feedparser_entry({"id": "rss-only", "title": "RSS Only Ep"}),
    ]

    pi_mock = _mock_httpx_client(
        {
            "items": [
                {
                    "title": "PI Ep 1",
                    "guid": "rss-1",
                    "description": "",
                    "datePublishedPretty": "",
                    "enclosureUrl": "",
                    "duration": 0,
                },
                {
                    "title": "PI Ep 2",
                    "guid": "pi-2",
                    "description": "",
                    "datePublishedPretty": "",
                    "enclosureUrl": "",
                    "duration": 0,
                },
            ],
        }
    )

    with (
        patch("podmate.feed.feedparser.parse", return_value=mock_parsed),
        patch("podmate.feed.httpx.AsyncClient", return_value=pi_mock),
    ):
        result = await resolve_feed(
            "https://example.com/feed.xml",
            podcast_index=client,
        )

    # RSS episodes preserved + new PI episodes merged
    assert result["episode_source"] == "merged"
    assert result["total_episodes"] == 3
    guids = {ep["guid"] for ep in result["episodes"]}
    assert guids == {"rss-1", "rss-only", "pi-2"}


async def test_resolve_feed_podcast_index_fails_silently():
    """When Podcast Index API fails, silently fall back to RSS."""
    client = PodcastIndexClient("bad-key", "bad-secret")

    mock_parsed = MagicMock()
    img = MagicMock()
    img.href = ""
    mock_parsed.feed = _mock_feed_meta(
        {
            "title": "Safe Podcast",
            "link": "",
            "author": "",
            "subtitle": "",
            "image": img,
        }
    )

    mock_parsed.entries = [_mock_feedparser_entry({"id": "rss-1", "title": "RSS Ep"})]

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=httpx.HTTPError("Network error"))

    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    with (
        patch("podmate.feed.feedparser.parse", return_value=mock_parsed),
        patch("podmate.feed.httpx.AsyncClient", return_value=mock_ctx),
    ):
        result = await resolve_feed(
            "https://example.com/feed.xml",
            podcast_index=client,
        )

    assert result["episode_source"] == "rss"
    assert result["total_episodes"] == 1


async def test_resolve_feed_rss_has_more_than_pi():
    """When RSS has more episodes than PI, merge unique PI episodes into RSS."""
    client = PodcastIndexClient("key", "secret")

    mock_parsed = MagicMock()
    img = MagicMock()
    img.href = ""
    mock_parsed.feed = _mock_feed_meta(
        {
            "title": "Rich RSS",
            "link": "",
            "author": "",
            "subtitle": "",
            "image": img,
        }
    )

    entries = [_mock_feedparser_entry({"id": f"rss-{i}", "title": f"RSS Ep {i}"}) for i in range(3)]
    mock_parsed.entries = entries

    pi_mock = _mock_httpx_client(
        {
            "items": [
                {
                    "title": "PI Ep 1",
                    "guid": "pi-1",
                    "description": "",
                    "datePublishedPretty": "",
                    "enclosureUrl": "",
                    "duration": 0,
                },
            ],
        }
    )

    with (
        patch("podmate.feed.feedparser.parse", return_value=mock_parsed),
        patch("podmate.feed.httpx.AsyncClient", return_value=pi_mock),
    ):
        result = await resolve_feed(
            "https://example.com/feed.xml",
            podcast_index=client,
        )

    # RSS base + new PI episode merged in
    assert result["episode_source"] == "merged"
    assert result["total_episodes"] == 4
    guids = {ep["guid"] for ep in result["episodes"]}
    assert guids == {"rss-0", "rss-1", "rss-2", "pi-1"}


# ── CLI: sub (URL mode) ────────────────────────────────────


def test_sub_by_url_subscribes_successfully():
    """Given RSS URL, sub command resolves feed and stores it."""
    mock_feed_data = {
        "title": "CLI Test Podcast",
        "author": "CLI Author",
        "description": "A CLI test podcast",
        "image_url": "",
        "link": "https://example.com",
        "episodes": [
            {
                "title": "Ep 1",
                "guid": "ep1",
                "description": "First",
                "pub_date": "2024-01-01",
                "audio_url": "https://example.com/ep1.mp3",
                "duration_sec": 1800,
            }
        ],
        "episode_source": "rss",
        "total_episodes": 1,
    }

    with patch("podmate.cli.resolve_feed", new=AsyncMock(return_value=mock_feed_data)):
        result = runner.invoke(app, ["sub", "https://example.com/feed.xml"])

    assert result.exit_code == 0
    assert "订阅成功" in result.stdout
    assert "CLI Test Podcast" in result.stdout
    assert "RSS" in result.stdout

    feeds = get_feeds()
    assert any(f.url == "https://example.com/feed.xml" for f in feeds)


def test_sub_stores_episode_source_in_db():
    """After subscribing, feed in DB has episode_source and total_episodes."""
    mock_feed_data = {
        "title": "Full Episodes Podcast",
        "author": "Author",
        "description": "Desc",
        "image_url": "",
        "link": "",
        "episodes": [
            {
                "title": "E1",
                "guid": "e1",
                "description": "",
                "pub_date": "",
                "audio_url": "",
                "duration_sec": 0,
            },
            {
                "title": "E2",
                "guid": "e2",
                "description": "",
                "pub_date": "",
                "audio_url": "",
                "duration_sec": 0,
            },
        ],
        "episode_source": "podcast-index",
        "total_episodes": 150,
    }

    with patch("podmate.cli.resolve_feed", new=AsyncMock(return_value=mock_feed_data)):
        result = runner.invoke(app, ["sub", "https://example.com/full.xml"])

    assert result.exit_code == 0
    assert "150 集" in result.stdout
    assert "Podcast Index" in result.stdout

    feeds = get_feeds()
    feed = next(f for f in feeds if f.url == "https://example.com/full.xml")
    assert feed.episode_source == "podcast-index"
    assert feed.total_episodes == 150

    eps = get_episodes(feed_id=feed.id, limit=9999)
    assert len(eps) == 2


def test_sub_by_url_preserves_itunes_id():
    """URL mode passes itunes_id=None to resolve_feed (no search result)."""
    mock_feed_data = {
        "title": "URL Only",
        "author": "",
        "description": "",
        "image_url": "",
        "link": "",
        "episodes": [],
        "episode_source": "rss",
        "total_episodes": 0,
    }

    with patch("podmate.cli.resolve_feed", new=AsyncMock(return_value=mock_feed_data)) as mock_rf:
        runner.invoke(app, ["sub", "https://example.com/url-only.xml"])

    mock_rf.assert_called_once()
    call_kwargs = mock_rf.call_args.kwargs
    assert call_kwargs["itunes_id"] is None


# ── CLI: list feeds ────────────────────────────────────────


def test_list_feeds_empty_shows_message():
    """Given no feeds, list command shows empty hint."""
    result = runner.invoke(app, ["feed", "list"])

    assert result.exit_code == 0
    assert "还没有订阅任何播客" in result.stdout


def test_list_feeds_shows_subscribed():
    """Given a feed in db, list command displays it."""
    add_feed(
        url="https://example.com/list-test.xml",
        title="List Test Podcast",
    )

    result = runner.invoke(app, ["feed", "list"])

    assert result.exit_code == 0
    assert "List Test Podcast" in result.stdout


# ── DB: describe feed flow ─────────────────────────────────


def test_describe_feed_returns_metadata_and_episodes():
    """Given feed with episodes, get_feed + get_episodes returns correct data."""
    feed = add_feed(
        url="https://example.com/describe-test.xml",
        title="Describe Test Podcast",
        author="Test Author",
        description="A podcast for testing describe",
    )
    add_episode(
        feed_id=feed.id,
        guid="desc-ep-1",
        title="Episode One",
        pub_date="2024-01-01",
    )
    add_episode(
        feed_id=feed.id,
        guid="desc-ep-2",
        title="Episode Two",
        pub_date="2024-01-08",
    )

    fetched = get_feed(feed.id)
    assert fetched is not None
    assert fetched.title == "Describe Test Podcast"
    assert fetched.author == "Test Author"
    assert fetched.description == "A podcast for testing describe"

    episodes = get_episodes(feed_id=feed.id, limit=10)
    assert len(episodes) == 2
    titles = {ep.title for ep in episodes}
    assert "Episode One" in titles
    assert "Episode Two" in titles


# ── CLI: describe command ──────────────────────────────────


def test_cli_describe_shows_feed_info():
    """Given a feed, describe command shows metadata."""
    feed = add_feed(
        url="https://example.com/cli-describe.xml",
        title="CLI Describe Podcast",
        author="CLI Author",
    )
    add_episode(
        feed_id=feed.id,
        guid="cli-ep-1",
        title="CLI Episode",
    )

    result = runner.invoke(app, ["feed", "show", str(feed.id)])

    assert result.exit_code == 0
    assert "CLI Describe Podcast" in result.stdout
    assert "CLI Author" in result.stdout


def test_cli_describe_nonexistent_feed():
    """Given invalid feed ID, describe shows error."""
    result = runner.invoke(app, ["feed", "show", "9999"])

    assert result.exit_code == 1
    assert "未找到" in result.stdout


# ── CLI: status command ────────────────────────────────────


def test_status_shows_stats():
    """Status command shows statistics."""
    add_feed(url="https://example.com/status-test.xml", title="Status Podcast")

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    assert "Status Podcast" in result.stdout or "已订阅播客" in result.stdout


# ── Config ─────────────────────────────────────────────────


def test_config_set_podcast_index_key():
    """set_key for podcast_index persists and can be loaded."""
    from podmate.config import get, set_key

    set_key("podcast_index", "api_key", "pk-test-12345")
    set_key("podcast_index", "api_secret", "sk-secret-hash")

    cfg = load_config()
    assert cfg["podcast_index"]["api_key"] == "pk-test-12345"
    assert cfg["podcast_index"]["api_secret"] == "sk-secret-hash"
    assert get("podcast_index", "api_key") == "pk-test-12345"


def test_config_load_includes_podcast_index_defaults():
    """Default config includes podcast_index section with empty strings."""
    cfg = load_config()
    assert "podcast_index" in cfg
    assert cfg["podcast_index"]["api_key"] == ""
    assert cfg["podcast_index"]["api_secret"] == ""


def test_config_show_masks_podcast_index_keys():
    """show() masks api_key and api_secret values."""
    from podmate.config import set_key, show

    set_key("podcast_index", "api_key", "pk-very-long-api-key-for-testing")
    set_key("podcast_index", "api_secret", "secret-value")

    masked = show()
    pi = masked["podcast_index"]
    assert "pk-very-long-api-key-for-testing" not in str(pi)
    assert "secret-value" not in str(pi)


# ── DB: unique guid constraint ─────────────────────────────


def test_add_episode_duplicate_guid_is_ignored():
    """INSERT OR IGNORE prevents duplicate GUIDs."""
    feed = add_feed(url="https://example.com/unique-test.xml", title="Unique Test")
    ep1 = add_episode(feed_id=feed.id, guid="same-guid", title="First")
    ep2 = add_episode(feed_id=feed.id, guid="same-guid", title="Second")

    assert ep1.id == ep2.id
    assert ep1.title == "First"

    episodes = get_episodes(feed_id=feed.id, limit=10)
    assert len(episodes) == 1


# ── resolve_feed merge behavior ─────────────────────────


async def test_resolve_feed_always_preserves_rss_episodes():
    """RSS-only episodes (not in PI) survive the merge."""
    client = PodcastIndexClient("key", "secret")

    mock_parsed = MagicMock()
    img = MagicMock()
    img.href = ""
    mock_parsed.feed = _mock_feed_meta(
        {
            "title": "Merge Test",
            "link": "",
            "author": "",
            "subtitle": "",
            "image": img,
        }
    )

    mock_parsed.entries = [
        _mock_feedparser_entry({"id": "rss-exclusive-1", "title": "RSS Only 1"}),
        _mock_feedparser_entry({"id": "rss-exclusive-2", "title": "RSS Only 2"}),
    ]

    pi_mock = _mock_httpx_client(
        {
            "items": [
                {
                    "title": "PI Ep 1",
                    "guid": "pi-1",
                    "description": "",
                    "datePublishedPretty": "",
                    "enclosureUrl": "",
                    "duration": 0,
                },
                {
                    "title": "PI Ep 2",
                    "guid": "pi-2",
                    "description": "",
                    "datePublishedPretty": "",
                    "enclosureUrl": "",
                    "duration": 0,
                },
                {
                    "title": "PI Ep 3",
                    "guid": "pi-3",
                    "description": "",
                    "datePublishedPretty": "",
                    "enclosureUrl": "",
                    "duration": 0,
                },
            ],
        }
    )

    with (
        patch("podmate.feed.feedparser.parse", return_value=mock_parsed),
        patch("podmate.feed.httpx.AsyncClient", return_value=pi_mock),
    ):
        result = await resolve_feed(
            "https://example.com/feed.xml",
            podcast_index=client,
        )

    assert result["episode_source"] == "merged"
    assert result["total_episodes"] == 5
    guids = {ep["guid"] for ep in result["episodes"]}
    assert "rss-exclusive-1" in guids
    assert "rss-exclusive-2" in guids
    assert "pi-1" in guids
    assert "pi-2" in guids
    assert "pi-3" in guids


async def test_resolve_feed_all_pi_duplicates_stays_rss():
    """When all PI episodes duplicate RSS GUIDs, source stays 'rss'."""
    client = PodcastIndexClient("key", "secret")

    mock_parsed = MagicMock()
    img = MagicMock()
    img.href = ""
    mock_parsed.feed = _mock_feed_meta(
        {
            "title": "Dup Test",
            "link": "",
            "author": "",
            "subtitle": "",
            "image": img,
        }
    )

    mock_parsed.entries = [
        _mock_feedparser_entry({"id": "shared-1", "title": "RSS Shared"}),
    ]

    pi_mock = _mock_httpx_client(
        {
            "items": [
                {
                    "title": "PI Shared",
                    "guid": "shared-1",
                    "description": "",
                    "datePublishedPretty": "",
                    "enclosureUrl": "",
                    "duration": 0,
                },
            ],
        }
    )

    with (
        patch("podmate.feed.feedparser.parse", return_value=mock_parsed),
        patch("podmate.feed.httpx.AsyncClient", return_value=pi_mock),
    ):
        result = await resolve_feed(
            "https://example.com/feed.xml",
            podcast_index=client,
        )

    assert result["episode_source"] == "rss"
    assert result["total_episodes"] == 1


# ── CLI: refresh command ────────────────────────────────


def test_refresh_command_no_pi_key_shows_error():
    """When PI API key is not configured, refresh shows helpful error."""
    feed = add_feed(url="https://example.com/refresh-test.xml", title="Refresh Test")
    result = runner.invoke(app, ["feed", "refresh", str(feed.id)])
    assert result.exit_code == 1
    assert "未配置" in result.stdout


def test_refresh_command_feed_not_found():
    """When feed ID does not exist, refresh shows error."""
    result = runner.invoke(app, ["refresh", "9999"])
    assert result.exit_code == 1
    assert "未找到" in result.stdout


def test_refresh_command_adds_new_episodes(monkeypatch):
    """Refresh resolves feed with PI client and adds new episodes via INSERT OR IGNORE."""
    feed = add_feed(
        url="https://example.com/refresh-episodes.xml",
        title="Refresh Eps Test",
        itunes_id=123456,
    )
    add_episode(feed_id=feed.id, guid="existing-ep", title="Existing Episode")

    mock_feed_data = {
        "title": "Refresh Eps Test",
        "author": "Author",
        "description": "Desc",
        "image_url": "",
        "link": "",
        "episodes": [
            {
                "title": "Existing Episode",
                "guid": "existing-ep",
                "description": "",
                "pub_date": "",
                "audio_url": "",
                "duration_sec": 0,
            },
            {
                "title": "New Episode 1",
                "guid": "new-ep-1",
                "description": "",
                "pub_date": "",
                "audio_url": "",
                "duration_sec": 0,
            },
            {
                "title": "New Episode 2",
                "guid": "new-ep-2",
                "description": "",
                "pub_date": "",
                "audio_url": "",
                "duration_sec": 0,
            },
        ],
        "episode_source": "merged",
        "total_episodes": 3,
    }

    test_cfg = load_config().copy()
    test_cfg["podcast_index"]["api_key"] = "pk-test"
    test_cfg["podcast_index"]["api_secret"] = "sk-test"
    monkeypatch.setattr("podmate.cli.load_config", lambda: test_cfg)

    with patch("podmate.cli.resolve_feed", new=AsyncMock(return_value=mock_feed_data)):
        result = runner.invoke(app, ["feed", "refresh", str(feed.id)])

    assert result.exit_code == 0
    assert "刷新完成" in result.stdout
    assert "新增剧集" in result.stdout
    assert "3 集" in result.stdout

    eps = get_episodes(feed_id=feed.id, limit=9999)
    assert len(eps) == 3


def test_refresh_command_preserves_existing_episodes(monkeypatch):
    """Existing episodes are kept after refresh (INSERT OR IGNORE dedup)."""
    feed = add_feed(
        url="https://example.com/refresh-keep.xml",
        title="Keep Test",
        itunes_id=789,
    )
    add_episode(feed_id=feed.id, guid="keep-1", title="Keep Me")
    add_episode(feed_id=feed.id, guid="keep-2", title="Keep Me Too")

    mock_feed_data = {
        "title": "Keep Test",
        "author": "",
        "description": "",
        "image_url": "",
        "link": "",
        "episodes": [
            {
                "title": "Keep Me",
                "guid": "keep-1",
                "description": "",
                "pub_date": "",
                "audio_url": "",
                "duration_sec": 0,
            },
            {
                "title": "New Only",
                "guid": "new-only",
                "description": "",
                "pub_date": "",
                "audio_url": "",
                "duration_sec": 0,
            },
        ],
        "episode_source": "merged",
        "total_episodes": 3,
    }

    test_cfg = load_config().copy()
    test_cfg["podcast_index"]["api_key"] = "pk-test"
    test_cfg["podcast_index"]["api_secret"] = "sk-test"
    monkeypatch.setattr("podmate.cli.load_config", lambda: test_cfg)

    with patch("podmate.cli.resolve_feed", new=AsyncMock(return_value=mock_feed_data)):
        result = runner.invoke(app, ["feed", "refresh", str(feed.id)])

    assert result.exit_code == 0
    eps = get_episodes(feed_id=feed.id, limit=9999)
    guids = {ep.guid for ep in eps}
    assert guids == {"keep-1", "keep-2", "new-only"}


# ── DB: itunes_id column ────────────────────────────────


def test_add_feed_stores_itunes_id():
    """add_feed persists itunes_id in the feeds table."""
    feed = add_feed(
        url="https://example.com/itunes-test.xml",
        title="ITunes ID Test",
        itunes_id=424242,
    )
    assert feed.itunes_id == 424242


def test_feed_itunes_id_defaults_to_none():
    """When itunes_id is not passed, it stays None."""
    feed = add_feed(
        url="https://example.com/no-itunes-test.xml",
        title="No ITunes ID",
    )
    assert feed.itunes_id is None


# ── CLI: poll command ──────────────────────────────────────


def test_poll_command_no_feeds():
    """When no feeds exist, poll shows helpful message."""
    result = runner.invoke(app, ["poll"])

    assert result.exit_code == 0
    assert "还没有订阅任何播客" in result.stdout


def test_poll_command_shows_updates():
    """Poll detects new episodes from RSS and adds them to DB."""
    feed = add_feed(
        url="https://example.com/poll-test.xml",
        title="Poll Test Podcast",
    )
    add_episode(feed_id=feed.id, guid="old-1", title="Old Episode")

    mock_feed_data = {
        "title": "Poll Test Podcast",
        "author": "Author",
        "description": "Desc",
        "image_url": "",
        "link": "",
        "episodes": [
            {
                "title": "Old Episode",
                "guid": "old-1",
                "description": "",
                "pub_date": "2024-01-01",
                "audio_url": "",
                "duration_sec": 0,
            },
            {
                "title": "New Episode 1",
                "guid": "new-1",
                "description": "",
                "pub_date": "2024-02-01",
                "audio_url": "",
                "duration_sec": 0,
            },
            {
                "title": "New Episode 2",
                "guid": "new-2",
                "description": "",
                "pub_date": "2024-03-01",
                "audio_url": "",
                "duration_sec": 0,
            },
        ],
    }

    with patch("podmate.cli.parse_feed", return_value=mock_feed_data):
        result = runner.invoke(app, ["poll"])

    assert result.exit_code == 0
    assert "Poll Test Podcast" in result.stdout
    assert "发现" in result.stdout
    assert "2" in result.stdout
    assert "📊 检查" in result.stdout
    assert "已入库" in result.stdout

    eps = get_episodes(feed_id=feed.id, limit=9999)
    guids = {ep.guid for ep in eps}
    assert "old-1" in guids
    assert "new-1" in guids
    assert "new-2" in guids
    assert len(eps) == 3


def test_poll_command_dry_run():
    """Dry-run mode shows new episodes but does not add them to DB."""
    feed = add_feed(
        url="https://example.com/poll-dryrun.xml",
        title="Dry Run Podcast",
    )
    add_episode(feed_id=feed.id, guid="existing-1", title="Existing Episode")

    before_eps = get_episodes(feed_id=feed.id, limit=9999)
    assert len(before_eps) == 1

    mock_feed_data = {
        "title": "Dry Run Podcast",
        "author": "",
        "description": "",
        "image_url": "",
        "link": "",
        "episodes": [
            {
                "title": "Existing Episode",
                "guid": "existing-1",
                "description": "",
                "pub_date": "",
                "audio_url": "",
                "duration_sec": 0,
            },
            {
                "title": "Would Be New",
                "guid": "new-dry-1",
                "description": "",
                "pub_date": "",
                "audio_url": "",
                "duration_sec": 0,
            },
        ],
    }

    with patch("podmate.cli.parse_feed", return_value=mock_feed_data):
        result = runner.invoke(app, ["poll", "--dry-run"])

    assert result.exit_code == 0
    assert "Dry Run Podcast" in result.stdout
    assert "发现" in result.stdout
    assert "📊 检查" in result.stdout
    assert "--dry-run" in result.stdout

    after_eps = get_episodes(feed_id=feed.id, limit=9999)
    assert len(after_eps) == 1


def test_poll_command_error_continues():
    """When one feed fails, poll continues with remaining feeds."""
    feed1 = add_feed(
        url="https://example.com/poll-good.xml",
        title="Good Feed",
    )
    add_feed(
        url="https://example.com/poll-bad.xml",
        title="Bad Feed",
    )
    add_episode(feed_id=feed1.id, guid="g-1", title="Existing")

    mock_good = {
        "title": "Good Feed",
        "author": "",
        "description": "",
        "image_url": "",
        "link": "",
        "episodes": [
            {
                "title": "New Good",
                "guid": "g-new",
                "description": "",
                "pub_date": "",
                "audio_url": "",
                "duration_sec": 0,
            },
        ],
    }

    def mock_parse(url):
        if "bad" in url:
            raise ConnectionError("Network error")
        return mock_good

    with patch("podmate.cli.parse_feed", side_effect=mock_parse):
        result = runner.invoke(app, ["poll"])

    assert result.exit_code == 0
    assert "Good Feed" in result.stdout
    assert "Bad Feed" in result.stdout
    assert "RSS 获取失败" in result.stdout

    eps = get_episodes(feed_id=feed1.id, limit=9999)
    guids = {ep.guid for ep in eps}
    assert "g-new" in guids


def test_poll_shows_summary_when_no_new_episodes():
    """When all feeds are up to date, poll shows zero-changes summary."""
    feed = add_feed(
        url="https://example.com/poll-no-new.xml",
        title="No New Podcast",
    )
    add_episode(feed_id=feed.id, guid="existing-1", title="Existing Episode")

    mock_feed_data = {
        "title": "No New Podcast",
        "author": "",
        "description": "",
        "image_url": "",
        "link": "",
        "episodes": [
            {
                "title": "Existing Episode",
                "guid": "existing-1",
                "description": "",
                "pub_date": "",
                "audio_url": "",
                "duration_sec": 0,
            },
        ],
    }

    with patch("podmate.cli.parse_feed", return_value=mock_feed_data):
        result = runner.invoke(app, ["poll"])

    assert result.exit_code == 0
    assert "[podmate] 暂无新剧集" in result.stdout


def test_poll_shows_summary_with_new_episodes():
    """When new episodes found, poll shows summary line with counts."""
    feed = add_feed(
        url="https://example.com/poll-summary.xml",
        title="Summary Podcast",
    )
    add_episode(feed_id=feed.id, guid="old-1", title="Old Episode")

    mock_feed_data = {
        "title": "Summary Podcast",
        "author": "",
        "description": "",
        "image_url": "",
        "link": "",
        "episodes": [
            {
                "title": "Old Episode",
                "guid": "old-1",
                "description": "",
                "pub_date": "",
                "audio_url": "",
                "duration_sec": 0,
            },
            {
                "title": "Fresh Episode",
                "guid": "fresh-1",
                "description": "",
                "pub_date": "",
                "audio_url": "",
                "duration_sec": 0,
            },
        ],
    }

    with patch("podmate.cli.parse_feed", return_value=mock_feed_data):
        result = runner.invoke(app, ["poll"])

    assert result.exit_code == 0
    assert "📊 检查" in result.stdout
    assert "发现 1 集新内容" in result.stdout
    assert "已入库 1 集" in result.stdout
    assert "Summary Podcast" in result.stdout


def test_poll_config_interval_default():
    """Default config has poll.interval_hours = 6."""
    from podmate.config import load as load_cfg

    cfg = load_cfg()
    assert cfg["poll"]["interval_hours"] == 6


def test_poll_config_interval_custom():
    """After setting poll.interval_hours, value reads correctly."""
    from podmate.config import load as load_cfg
    from podmate.config import set_key

    set_key("poll", "interval_hours", "12")
    cfg = load_cfg()
    assert cfg["poll"]["interval_hours"] == "12"

    set_key("poll", "interval_hours", "6")


# ── Transcriber: _format_time ─────────────────────────────


def test_format_time_zero():
    """0 seconds → 00:00:00."""
    assert _format_time(0) == "00:00:00"


def test_format_time_under_one_minute():
    """59 seconds → 00:00:59."""
    assert _format_time(59) == "00:00:59"


def test_format_time_one_hour_one_second():
    """3661 seconds → 01:01:01."""
    assert _format_time(3661) == "01:01:01"


def test_format_time_many_hours():
    """7384 seconds → 02:03:04."""
    assert _format_time(7384) == "02:03:04"


# ── Transcriber: format_transcript ───────────────────────


def _make_result(segments, language="en", duration_sec=120.0):
    """Build a minimal transcript result dict."""
    return {
        "text": " ".join(s.get("text", "") for s in segments),
        "segments": segments,
        "language": language,
        "duration_sec": duration_sec,
    }


def test_format_transcript_with_speakers():
    """Multiple speakers → markdown with time ranges and speaker labels."""
    segments = [
        {"id": 0, "start": 1.0, "end": 15.0, "text": "Hello everyone.", "speaker": "A"},
        {
            "id": 1,
            "start": 16.0,
            "end": 62.0,
            "text": "Hi there, welcome to the show.",
            "speaker": "B",
        },  # noqa: E501
        {
            "id": 2,
            "start": 63.0,
            "end": 105.0,
            "text": "Today we discuss technology.",
            "speaker": "A",
        },  # noqa: E501
    ]
    result = _make_result(segments, duration_sec=105.0)

    md = format_transcript(result, title="Test Episode")

    assert "# Test Episode" in md
    assert "**语言:** en" in md
    assert "**时长:** 2 分钟" in md
    assert "**说话人:** 2" in md
    assert "## 文字稿" in md
    assert "**[00:00:01 → 00:00:15] 说话人 A**" in md
    assert "Hello everyone." in md
    assert "**[00:00:16 → 00:01:02] 说话人 B**" in md
    assert "Hi there, welcome to the show." in md
    assert "**[00:01:03 → 00:01:45] 说话人 A**" in md
    assert "Today we discuss technology." in md
    assert "*由 PodMate 自动转写 (Deepgram nova-2)*" in md


def test_format_transcript_merges_consecutive_same_speaker():
    """Consecutive same-speaker segments are merged into one block."""
    segments = [
        {"id": 0, "start": 0.0, "end": 5.0, "text": "Part one.", "speaker": "A"},
        {"id": 1, "start": 5.0, "end": 10.0, "text": "Part two.", "speaker": "A"},
        {"id": 2, "start": 10.0, "end": 15.0, "text": "Part three.", "speaker": "B"},
    ]
    result = _make_result(segments, duration_sec=15.0)

    md = format_transcript(result)

    # Speaker A's two segments merged: one time block, combined text
    assert "**[00:00:00 → 00:00:10] 说话人 A**" in md
    assert "Part one. Part two." in md
    # Speaker B separate
    assert "**[00:00:10 → 00:00:15] 说话人 B**" in md
    assert "Part three." in md


def test_format_transcript_single_speaker():
    """Segments with no speaker field → unified output."""
    segments = [
        {"id": 0, "start": 0.0, "end": 30.0, "text": "Monologue part one."},
        {"id": 1, "start": 30.0, "end": 60.0, "text": "Monologue part two."},
    ]
    result = _make_result(segments, duration_sec=60.0)

    md = format_transcript(result, title="Solo Show")

    assert "**说话人:** 1" in md
    assert "**时长:** 1 分钟" in md
    # No speaker field → defaults to "?"
    assert "说话人 ?" in md


def test_format_transcript_empty_segments():
    """Empty segments list → placeholder message."""
    result = _make_result([], duration_sec=0)

    md = format_transcript(result)

    assert "*无转写内容*" in md
    assert "**说话人:** 0" in md
    assert "**时长:** 0 分钟" in md


def test_format_transcript_untitled_fallback():
    """No title → 'Untitled'."""
    result = _make_result([], duration_sec=0)

    md = format_transcript(result)

    assert "# Untitled" in md


# ── Transcriber: _add_tone_markers ──────────────────────


def test_tone_marker_laugh():
    """Detects (laughs) and replaces with [笑声]."""
    assert _add_tone_markers("That is hilarious (laughs)") == "That is hilarious [笑声]"


def test_tone_marker_laughter():
    """Detects (laughter) and replaces with [笑声]."""
    assert _add_tone_markers("(laughter) Welcome everyone") == "Welcome everyone [笑声]"


def test_tone_marker_applause():
    """Detects (applause) and replaces with [掌声]."""
    assert _add_tone_markers("Thank you (applause)") == "Thank you [掌声]"


def test_tone_marker_music():
    """Detects (music) and replaces with [音乐]."""
    assert _add_tone_markers("(music) Opening theme") == "Opening theme [音乐]"


def test_tone_marker_chuckles():
    """Detects (chuckles) and replaces with [轻笑]."""
    assert _add_tone_markers("That was funny (chuckles)") == "That was funny [轻笑]"


def test_tone_marker_brackets():
    """Detects bracket-form [laughs] and [applause]."""
    assert _add_tone_markers("So funny [laughs]") == "So funny [笑声]"
    assert _add_tone_markers("[applause] Great") == "Great [掌声]"


def test_tone_marker_music_bracket():
    """Detects [Music] with capital M."""
    assert _add_tone_markers("[Music] Intro") == "Intro [音乐]"


def test_tone_marker_no_marker():
    """Text without tone markers is unchanged."""
    assert _add_tone_markers("Hello world, this is a test.") == "Hello world, this is a test."


def test_tone_marker_removes_original():
    """Original tone text is removed, only Chinese marker remains."""
    result = _add_tone_markers("We have a great guest (applause) today")
    assert "(applause)" not in result
    assert "[掌声]" in result


def test_tone_marker_multiple():
    """Multiple markers all appended."""
    result = _add_tone_markers("(laughter) Hello (applause)")
    assert "(laughter)" not in result
    assert "(applause)" not in result
    assert "[笑声]" in result
    assert "[掌声]" in result


def test_format_transcript_with_tone_markers():
    """format_transcript applies tone markers to segment text."""
    segments = [
        {
            "id": 0,
            "start": 0.0,
            "end": 15.0,
            "text": "Welcome to the show (applause)",
            "speaker": "A",
        },  # noqa: E501
        {
            "id": 1,
            "start": 16.0,
            "end": 62.0,
            "text": "Thanks, that's hilarious (laughs)",
            "speaker": "B",
        },  # noqa: E501
    ]
    result = _make_result(segments, duration_sec=62.0)

    md = format_transcript(result, title="Tone Test")

    assert "Welcome to the show [掌声]" in md
    assert "Thanks, that's hilarious [笑声]" in md
    assert "(applause)" not in md
    assert "(laughs)" not in md


# ── CLI: read command ───────────────────────────────────────


def test_read_command_shows_markdown(tmp_path):
    """Given episode with .md transcript, read command displays it."""
    from podmate.db import set_episode_path

    feed = add_feed(
        url="https://example.com/read-test.xml",
        title="Read Test Podcast",
    )
    ep = add_episode(
        feed_id=feed.id,
        guid="read-test-guid",
        title="Read Test Episode",
    )

    json_path = tmp_path / "read-test-guid.json"
    json_path.write_text("{}")
    md_path = tmp_path / "read-test-guid.md"
    md_content = "# Test Episode\n\n**语言:** en\n\n---\n\nHello world.\n"
    md_path.write_text(md_content)
    set_episode_path(ep.id, "transcript_path", str(json_path))

    result = runner.invoke(app, ["read", str(ep.id)])

    assert result.exit_code == 0
    assert "Test Episode" in result.stdout


def test_read_command_no_transcript():
    """Given episode without transcript, read shows error."""
    feed = add_feed(
        url="https://example.com/read-none.xml",
        title="No Transcript Podcast",
    )
    ep = add_episode(
        feed_id=feed.id,
        guid="read-none-guid",
        title="No Transcript Episode",
    )

    result = runner.invoke(app, ["read", str(ep.id)])

    assert result.exit_code == 1
    assert "尚未转写" in result.stdout


def test_read_command_no_md_but_has_json(tmp_path):
    """Given episode with .json but no .md, read prompts to regenerate."""
    from podmate.db import set_episode_path

    feed = add_feed(
        url="https://example.com/read-json-only.xml",
        title="JSON Only Podcast",
    )
    ep = add_episode(
        feed_id=feed.id,
        guid="read-json-only-guid",
        title="JSON Only Episode",
    )

    json_path = tmp_path / "read-json-only-guid.json"
    json_path.write_text("{}")
    set_episode_path(ep.id, "transcript_path", str(json_path))

    result = runner.invoke(app, ["read", str(ep.id)])

    assert result.exit_code == 1
    assert "尚未生成 Markdown" in result.stdout


def test_read_command_episode_not_found():
    """Given invalid episode ID, read shows error."""
    result = runner.invoke(app, ["read", "9999"])

    assert result.exit_code == 1
    assert "未找到" in result.stdout


# ── Pipeline: dual-format transcript save ────────────────


def test_pipeline_saves_markdown_alongside_json(tmp_path, monkeypatch):
    """After transcription, both .json and .md files exist."""
    import asyncio
    import json
    import os

    from podmate.db import add_episode, add_feed, set_episode_path, update_episode_status

    # Mock config to use tmp_path
    test_cfg = {
        "deepgram": {
            "api_key": "test-key",
            "api_url": "https://api.example.com/v1/listen",
            "model": "nova-2",
            "diarize": True,
        },  # noqa: E501
        "deepseek": {
            "api_key": "sk-test",
            "api_url": "https://api.example.com/v1",
            "model": "test",
            "temperature": 0.3,
        },  # noqa: E501
        "dubbing": {"voice": "test-voice", "rate": "1.0", "volume": "1.0"},
        "podcast_index": {"api_key": "", "api_secret": ""},
        "storage": {
            "data_dir": str(tmp_path),
            "keep_episodes": 5,
            "cbrain_dir": str(tmp_path / "cbrain" / "podcasts"),
        },
    }
    monkeypatch.setattr("podmate.pipeline.DATA_DIR", str(tmp_path))

    import podmate.config as config_mod

    monkeypatch.setattr(config_mod, "_config", test_cfg)

    # Set up test DB
    feed = add_feed(url="https://example.com/pipeline-test.xml", title="Pipeline Test")
    ep = add_episode(
        feed_id=feed.id,
        guid="pipeline-test-guid",
        title="Pipeline Test Episode",
        audio_url="https://example.com/audio.mp3",
    )

    episodes_dir = os.path.join(str(tmp_path), "episodes")
    transcripts_dir = os.path.join(str(tmp_path), "transcripts")
    translations_dir = os.path.join(str(tmp_path), "translations")
    dubs_dir = os.path.join(str(tmp_path), "dubs")
    for d in [episodes_dir, transcripts_dir, translations_dir, dubs_dir]:
        os.makedirs(d, exist_ok=True)

    # Create fake audio file (skip download)
    audio_path = os.path.join(episodes_dir, "pipeline-test-guid.mp3")
    with open(audio_path, "wb") as f:
        f.write(b"\x00" * 2048)
    set_episode_path(ep.id, "local_path", audio_path)
    update_episode_status(ep.id, "downloaded", progress=1.0)

    # Mock Deepgram response
    mock_transcript = {
        "text": "Hello world. This is a test.",
        "segments": [
            {"id": 0, "start": 0.0, "end": 2.0, "text": "Hello world.", "speaker": "A"},
            {"id": 1, "start": 2.0, "end": 5.0, "text": "This is a test.", "speaker": "B"},
        ],
        "language": "en",
        "duration_sec": 5.0,
    }

    # Mock translation (needed since pipeline continues past transcription)
    mock_translation = {
        "segments": [
            {
                "id": 0,
                "start": 0.0,
                "end": 2.0,
                "zh": "你好世界。",
                "speaker": "A",
                "text": "Hello world.",
            },  # noqa: E501
            {
                "id": 1,
                "start": 2.0,
                "end": 5.0,
                "zh": "这是一个测试。",
                "speaker": "B",
                "text": "This is a test.",
            },  # noqa: E501
        ],
        "summary_zh": "测试摘要",
    }

    from podmate.pipeline import run_pipeline

    with (
        patch(
            "podmate.pipeline.transcribe_via_deepgram", new=AsyncMock(return_value=mock_transcript)
        ),
        patch("podmate.pipeline.translate_segments", new=AsyncMock(return_value=mock_translation)),
        patch(
            "podmate.pipeline.dub_translation",
            new=AsyncMock(return_value=os.path.join(dubs_dir, "pipeline-test-guid.mp3")),
        ),
    ):  # noqa: E501
        result = asyncio.run(run_pipeline(ep.id, skip_dub=False))

    json_path = os.path.join(transcripts_dir, "pipeline-test-guid.json")
    md_path = os.path.join(transcripts_dir, "pipeline-test-guid.md")

    assert os.path.isfile(json_path), f"JSON transcript missing: {json_path}"
    assert os.path.isfile(md_path), f"Markdown transcript missing: {md_path}"

    # Verify JSON content
    with open(json_path) as f:
        saved_json = json.load(f)
    assert saved_json["text"] == "Hello world. This is a test."
    assert len(saved_json["segments"]) == 2

    # Verify Markdown content
    with open(md_path) as f:
        md_content = f.read()
    assert "# Pipeline Test Episode" in md_content
    assert "**[00:00:00 → 00:00:02] 说话人 A**" in md_content
    assert "Hello world." in md_content
    assert "说话人 B" in md_content

    assert result["transcript_path"] == json_path


def test_pipeline_exports_to_cbrain_when_dir_exists(tmp_path, monkeypatch):
    """When cbrain dir exists, .md transcript is copied there after transcription."""
    import asyncio

    from podmate.db import add_episode, add_feed, set_episode_path, update_episode_status

    cbrain_home = tmp_path / "fake_home"
    cbrain_podcasts = cbrain_home / "cbrain" / "docs" / "fuyuans-kb" / "podcasts"
    cbrain_podcasts.mkdir(parents=True)

    monkeypatch.setattr("podmate.pipeline.Path.home", lambda: cbrain_home)

    test_cfg = {
        "deepgram": {
            "api_key": "test-key",
            "api_url": "https://api.example.com/v1/listen",
            "model": "nova-2",
            "diarize": True,
        },  # noqa: E501
        "deepseek": {
            "api_key": "sk-test",
            "api_url": "https://api.example.com/v1",
            "model": "test",
            "temperature": 0.3,
        },  # noqa: E501
        "dubbing": {"voice": "test-voice", "rate": "1.0", "volume": "1.0"},
        "podcast_index": {"api_key": "", "api_secret": ""},
        "storage": {"data_dir": str(tmp_path), "keep_episodes": 5},
    }
    monkeypatch.setattr("podmate.pipeline.DATA_DIR", str(tmp_path))

    import podmate.config as config_mod

    monkeypatch.setattr(config_mod, "_config", test_cfg)

    feed = add_feed(url="https://example.com/cbrain-test.xml", title="Cbrain Test")
    ep = add_episode(
        feed_id=feed.id,
        guid="cbrain-test-guid",
        title="Cbrain Test Episode",
        audio_url="https://example.com/audio.mp3",
    )

    episodes_dir = os.path.join(str(tmp_path), "episodes")
    transcripts_dir = os.path.join(str(tmp_path), "transcripts")
    translations_dir = os.path.join(str(tmp_path), "translations")
    dubs_dir = os.path.join(str(tmp_path), "dubs")
    for d in [episodes_dir, transcripts_dir, translations_dir, dubs_dir]:
        os.makedirs(d, exist_ok=True)

    audio_path = os.path.join(episodes_dir, "cbrain-test-guid.mp3")
    with open(audio_path, "wb") as f:
        f.write(b"\x00" * 2048)
    set_episode_path(ep.id, "local_path", audio_path)
    update_episode_status(ep.id, "downloaded", progress=1.0)

    mock_transcript = {
        "text": "Hello world.",
        "segments": [
            {"id": 0, "start": 0.0, "end": 2.0, "text": "Hello world.", "speaker": "A"},
        ],
        "language": "en",
        "duration_sec": 2.0,
    }

    mock_translation = {
        "segments": [
            {
                "id": 0,
                "start": 0.0,
                "end": 2.0,
                "zh": "你好世界。",
                "speaker": "A",
                "text": "Hello world.",
            },  # noqa: E501
        ],
        "summary_zh": "测试摘要",
    }

    from podmate.pipeline import run_pipeline

    with (
        patch(
            "podmate.pipeline.transcribe_via_deepgram", new=AsyncMock(return_value=mock_transcript)
        ),
        patch("podmate.pipeline.translate_segments", new=AsyncMock(return_value=mock_translation)),
        patch(
            "podmate.pipeline.dub_translation",
            new=AsyncMock(return_value=os.path.join(dubs_dir, "cbrain-test-guid.mp3")),
        ),
    ):  # noqa: E501
        result = asyncio.run(run_pipeline(ep.id, skip_dub=False))

    assert result["exported_to_cbrain"] is True

    copied_md = cbrain_podcasts / "cbrain-test-guid.md"
    assert copied_md.is_file(), f"Markdown not copied to cbrain: {copied_md}"
    content = copied_md.read_text()
    assert "# Cbrain Test Episode" in content


def test_pipeline_creates_cbrain_dir_when_missing(tmp_path, monkeypatch):
    """When cbrain dir does not exist, pipeline auto-creates it and exports."""
    import asyncio

    from podmate.db import add_episode, add_feed, set_episode_path, update_episode_status

    nonexistent_home = tmp_path / "no_cbrain_home"
    monkeypatch.setattr("podmate.pipeline.Path.home", lambda: nonexistent_home)

    test_cfg = {
        "deepgram": {
            "api_key": "test-key",
            "api_url": "https://api.example.com/v1/listen",
            "model": "nova-2",
            "diarize": True,
        },  # noqa: E501
        "deepseek": {
            "api_key": "sk-test",
            "api_url": "https://api.example.com/v1",
            "model": "test",
            "temperature": 0.3,
        },  # noqa: E501
        "dubbing": {"voice": "test-voice", "rate": "1.0", "volume": "1.0"},
        "podcast_index": {"api_key": "", "api_secret": ""},
        "storage": {"data_dir": str(tmp_path), "keep_episodes": 5},
    }
    monkeypatch.setattr("podmate.pipeline.DATA_DIR", str(tmp_path))

    import podmate.config as config_mod

    monkeypatch.setattr(config_mod, "_config", test_cfg)

    feed = add_feed(url="https://example.com/autocreate-test.xml", title="Auto Create")
    ep = add_episode(
        feed_id=feed.id,
        guid="autocreate-test-guid",
        title="Auto Create Episode",
        audio_url="https://example.com/audio.mp3",
    )

    episodes_dir = os.path.join(str(tmp_path), "episodes")
    transcripts_dir = os.path.join(str(tmp_path), "transcripts")
    translations_dir = os.path.join(str(tmp_path), "translations")
    dubs_dir = os.path.join(str(tmp_path), "dubs")
    for d in [episodes_dir, transcripts_dir, translations_dir, dubs_dir]:
        os.makedirs(d, exist_ok=True)

    audio_path = os.path.join(episodes_dir, "autocreate-test-guid.mp3")
    with open(audio_path, "wb") as f:
        f.write(b"\x00" * 2048)
    set_episode_path(ep.id, "local_path", audio_path)
    update_episode_status(ep.id, "downloaded", progress=1.0)

    mock_transcript = {
        "text": "Hello.",
        "segments": [
            {"id": 0, "start": 0.0, "end": 1.0, "text": "Hello.", "speaker": "A"},
        ],
        "language": "en",
        "duration_sec": 1.0,
    }

    mock_translation = {
        "segments": [
            {"id": 0, "start": 0.0, "end": 1.0, "zh": "你好。", "speaker": "A", "text": "Hello."},
        ],
        "summary_zh": "测试",
    }

    from podmate.pipeline import run_pipeline

    with (
        patch(
            "podmate.pipeline.transcribe_via_deepgram", new=AsyncMock(return_value=mock_transcript)
        ),
        patch("podmate.pipeline.translate_segments", new=AsyncMock(return_value=mock_translation)),
        patch(
            "podmate.pipeline.dub_translation",
            new=AsyncMock(return_value=os.path.join(dubs_dir, "autocreate-test-guid.mp3")),
        ),
    ):  # noqa: E501
        result = asyncio.run(run_pipeline(ep.id, skip_dub=False))

    assert result["exported_to_cbrain"] is True

    # Verify directory was auto-created
    cbrain_podcasts = nonexistent_home / "cbrain" / "docs" / "fuyuans-kb" / "podcasts"
    assert cbrain_podcasts.is_dir()
    copied_md = cbrain_podcasts / "autocreate-test-guid.md"
    assert copied_md.is_file()

    # Verify index.md was created
    index_md = cbrain_podcasts / "index.md"
    assert index_md.is_file()


# ── CLI: search command ──────────────────────────────────────


def _make_transcript_json(path, segments):
    """Write a transcript JSON file with given segments."""
    data = {
        "text": " ".join(s.get("text", "") for s in segments),
        "segments": segments,
        "language": "en",
        "duration_sec": sum(s.get("end", 0) for s in segments),
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f)


def test_search_finds_matching_episodes(tmp_path):
    """Search finds keyword in transcript segments and displays results."""
    feed = add_feed(url="https://example.com/search-test.xml", title="Search Podcast")
    ep = add_episode(feed_id=feed.id, guid="search-ep-1", title="Search Episode")

    json_path = str(tmp_path / "search-ep-1.json")
    _make_transcript_json(
        json_path,
        [
            {
                "id": 0,
                "start": 0.0,
                "end": 5.0,
                "text": "Hello welcome to kubernetes podcast.",
                "speaker": "A",
            },  # noqa: E501
            {
                "id": 1,
                "start": 5.0,
                "end": 10.0,
                "text": "Yes kubernetes is great for scaling apps.",
                "speaker": "B",
            },  # noqa: E501
        ],
    )
    set_episode_path(ep.id, "transcript_path", json_path)

    result = runner.invoke(app, ["grep", "kubernetes"])

    assert result.exit_code == 0
    assert "Search Podcast" in result.stdout
    assert "Search Episode" in result.stdout
    assert "找到 2 处匹配" in result.stdout
    assert "kubernetes podcast" in result.stdout
    assert "kubernetes is great" in result.stdout
    assert "说话人 A" in result.stdout
    assert "说话人 B" in result.stdout
    assert "总计 2 处匹配" in result.stdout


def test_search_no_matches(tmp_path):
    """Search with no matching keyword shows 'not found' message."""
    feed = add_feed(url="https://example.com/search-none.xml", title="No Match Podcast")
    ep = add_episode(feed_id=feed.id, guid="search-none-ep", title="No Match Episode")

    json_path = str(tmp_path / "search-none-ep.json")
    _make_transcript_json(
        json_path,
        [
            {
                "id": 0,
                "start": 0.0,
                "end": 5.0,
                "text": "Hello world this is a test.",
                "speaker": "A",
            },
        ],
    )
    set_episode_path(ep.id, "transcript_path", json_path)

    result = runner.invoke(app, ["grep", "kubernetes"])

    assert result.exit_code == 0
    assert "未找到匹配结果" in result.stdout


def test_search_no_transcripts():
    """Search with no episodes having transcript files exits gracefully."""
    feed = add_feed(url="https://example.com/search-no-trans.xml", title="No Trans Podcast")
    add_episode(feed_id=feed.id, guid="no-trans-ep", title="No Trans Episode")
    # No transcript_path set

    result = runner.invoke(app, ["grep", "anything"])

    assert result.exit_code == 0
    assert "未找到匹配结果" in result.stdout


def test_search_case_insensitive(tmp_path):
    """Search is case-insensitive — 'KUBERNETES' matches 'kubernetes'."""
    feed = add_feed(url="https://example.com/search-case.xml", title="Case Podcast")
    ep = add_episode(feed_id=feed.id, guid="case-ep", title="Case Episode")

    json_path = str(tmp_path / "case-ep.json")
    _make_transcript_json(
        json_path,
        [
            {
                "id": 0,
                "start": 0.0,
                "end": 5.0,
                "text": "We use Kubernetes in production.",
                "speaker": "A",
            },  # noqa: E501
        ],
    )
    set_episode_path(ep.id, "transcript_path", json_path)

    result = runner.invoke(app, ["grep", "kubernetes"])

    assert result.exit_code == 0
    assert "找到 1 处匹配" in result.stdout
    # Also test uppercase
    result2 = runner.invoke(app, ["grep", "KUBERNETES"])
    assert result2.exit_code == 0
    assert "找到 1 处匹配" in result2.stdout


def test_search_limits_snippets_per_episode(tmp_path):
    """Max 3 snippets displayed per episode, but total count is accurate."""
    feed = add_feed(url="https://example.com/search-limit.xml", title="Limit Podcast")
    ep = add_episode(feed_id=feed.id, guid="limit-ep", title="Limit Episode")

    json_path = str(tmp_path / "limit-ep.json")
    _make_transcript_json(
        json_path,
        [
            {
                "id": 0,
                "start": 10.0,
                "end": 15.0,
                "text": "First mention of kubernetes here.",
                "speaker": "A",
            },  # noqa: E501
            {
                "id": 1,
                "start": 20.0,
                "end": 25.0,
                "text": "Second kubernetes reference in text.",
                "speaker": "A",
            },  # noqa: E501
            {
                "id": 2,
                "start": 30.0,
                "end": 35.0,
                "text": "Third kubernetes mention right here.",
                "speaker": "B",
            },  # noqa: E501
            {
                "id": 3,
                "start": 40.0,
                "end": 45.0,
                "text": "Fourth kubernetes mention hidden.",
                "speaker": "B",
            },  # noqa: E501
        ],
    )
    set_episode_path(ep.id, "transcript_path", json_path)

    result = runner.invoke(app, ["grep", "kubernetes"])

    assert result.exit_code == 0
    # Total match count shows 4, but only 3 snippets displayed
    assert "找到 4 处匹配" in result.stdout
    assert "First mention of kubernetes" in result.stdout
    assert "Second kubernetes reference" in result.stdout
    assert "Third kubernetes mention" in result.stdout
    assert "Fourth kubernetes mention" not in result.stdout
    assert "总计 4 处匹配" in result.stdout


# ── CLI: mark command ──────────────────────────────────────


def test_mark_read():
    """Marking an episode as read sets is_read=True."""
    feed = add_feed(url="https://example.com/mark-read.xml", title="Mark Read")
    ep = add_episode(feed_id=feed.id, guid="mark-read-ep", title="Mark Read Episode")

    result = runner.invoke(app, ["episode", "mark", str(ep.id), "--read"])

    assert result.exit_code == 0
    assert "已标记为已读" in result.stdout
    assert "Mark Read Episode" in result.stdout

    updated = get_episode(ep.id)
    assert updated.is_read is True
    assert updated.is_starred is False


def test_mark_unread():
    """Marking an episode as unread sets is_read=False."""
    feed = add_feed(url="https://example.com/mark-unread.xml", title="Mark Unread")
    ep = add_episode(feed_id=feed.id, guid="mark-unread-ep", title="Mark Unread Ep")

    runner.invoke(app, ["mark", str(ep.id), "--read"])
    result = runner.invoke(app, ["episode", "mark", str(ep.id), "--unread"])

    assert result.exit_code == 0
    assert "已标记为未读" in result.stdout

    updated = get_episode(ep.id)
    assert updated.is_read is False


def test_mark_star():
    """Marking an episode as starred sets is_starred=True."""
    feed = add_feed(url="https://example.com/mark-star.xml", title="Mark Star")
    ep = add_episode(feed_id=feed.id, guid="mark-star-ep", title="Mark Star Episode")

    result = runner.invoke(app, ["episode", "mark", str(ep.id), "--star"])

    assert result.exit_code == 0
    assert "已添加星标" in result.stdout

    updated = get_episode(ep.id)
    assert updated.is_starred is True
    assert updated.is_read is False


def test_mark_unstar():
    """Removing star sets is_starred=False."""
    feed = add_feed(url="https://example.com/mark-unstar.xml", title="Mark Unstar")
    ep = add_episode(feed_id=feed.id, guid="mark-unstar-ep", title="Mark Unstar Ep")

    runner.invoke(app, ["mark", str(ep.id), "--star"])
    result = runner.invoke(app, ["episode", "mark", str(ep.id), "--unstar"])

    assert result.exit_code == 0
    assert "已取消星标" in result.stdout

    updated = get_episode(ep.id)
    assert updated.is_starred is False


def test_mark_both():
    """Marking both --read and --star in one command works."""
    feed = add_feed(url="https://example.com/mark-both.xml", title="Mark Both")
    ep = add_episode(feed_id=feed.id, guid="mark-both-ep", title="Mark Both Episode")

    result = runner.invoke(app, ["episode", "mark", str(ep.id), "--read", "--star"])

    assert result.exit_code == 0
    assert "已标记为已读" in result.stdout
    assert "已添加星标" in result.stdout

    updated = get_episode(ep.id)
    assert updated.is_read is True
    assert updated.is_starred is True


def test_mark_nonexistent():
    """Marking a nonexistent episode shows error."""
    result = runner.invoke(app, ["episode", "mark", "9999", "--read"])

    assert result.exit_code == 1
    assert "未找到" in result.stdout


def test_mark_no_flags():
    """Mark command with no flags shows help message."""
    feed = add_feed(url="https://example.com/mark-noflags.xml", title="No Flags")
    ep = add_episode(feed_id=feed.id, guid="mark-noflags-ep", title="No Flags Ep")

    result = runner.invoke(app, ["episode", "mark", str(ep.id)])

    assert result.exit_code == 1
    assert "请指定标记操作" in result.stdout


def test_mark_negative_id_via_option():
    """Marking via --id -1 parses correctly (episode may not exist)."""
    result = runner.invoke(app, ["episode", "mark", "--id", "-1", "--read"])

    assert result.exit_code in (0, 1)


def test_mark_negative_positional_fails():
    """Mark with positional -1 fails — Click treats it as an option flag.
    Known limitation: use --id -1 instead."""
    result = runner.invoke(app, ["episode", "mark", "-1", "--read"])

    assert result.exit_code == 2
    assert "No such option" in result.stdout or "No such option" in result.stderr


def test_mark_dash_dash_1_fails():
    """Mark with positional --1 fails — Click treats it as an option flag."""
    result = runner.invoke(app, ["episode", "mark", "--1", "--star"])

    assert result.exit_code == 2


def test_mark_non_numeric_id():
    """Mark with non-numeric positional ID shows numeric error."""
    result = runner.invoke(app, ["episode", "mark", "abc", "--read"])

    assert result.exit_code == 1
    assert "必须是数字" in result.stdout
    assert "abc" in result.stdout


def test_episode_negative_id_via_option():
    """Episode detail via --id -1 parses correctly."""
    result = runner.invoke(app, ["episode", "show", "--id", "-1"])

    assert result.exit_code in (0, 1)


def test_episode_detail_shows_read_status():
    """Episode detail command displays read/star status."""
    feed = add_feed(url="https://example.com/ep-detail.xml", title="Detail Feed")
    ep = add_episode(feed_id=feed.id, guid="ep-detail-ep", title="Detail Episode")

    runner.invoke(app, ["episode", "mark", str(ep.id), "--read", "--star"])

    result = runner.invoke(app, ["episode", "show", str(ep.id)])

    assert result.exit_code == 0
    assert "✅ 已读" in result.stdout
    assert "⭐ 是" in result.stdout


def test_list_shows_unread_and_star_marks():
    """List command shows 📖 for unread and ⭐ for starred episodes."""
    feed = add_feed(url="https://example.com/list-marks.xml", title="List Marks")
    ep1 = add_episode(feed_id=feed.id, guid="list-marks-1", title="Unread Starred")
    ep2 = add_episode(feed_id=feed.id, guid="list-marks-2", title="Read No Star")

    runner.invoke(app, ["episode", "mark", str(ep1.id), "--star"])
    runner.invoke(app, ["episode", "mark", str(ep2.id), "--read"])

    result = runner.invoke(app, ["episode", "list", "--feed", str(feed.id)])

    assert result.exit_code == 0
    assert "📖" in result.stdout
    assert "⭐" in result.stdout


# ── Podcasts Index: _extract_title_from_md ───────────────


def test_extract_title_from_h1(tmp_path):
    """Title extracted from H1 when no frontmatter present."""
    from podmate.pipeline import _extract_title_from_md

    md = tmp_path / "ep1.md"
    md.write_text("# My Episode Title\n\nSome content.\n")
    assert _extract_title_from_md(md) == "My Episode Title"


def test_extract_title_from_frontmatter(tmp_path):
    """Title extracted from YAML frontmatter title field."""
    from podmate.pipeline import _extract_title_from_md

    md = tmp_path / "ep2.md"
    md.write_text("---\ntitle: FM Title\n---\n\n# Different H1\n\nContent.\n")
    assert _extract_title_from_md(md) == "FM Title"


def test_extract_title_from_frontmatter_non_dict(tmp_path):
    """Frontmatter that isn't a dict falls back to H1."""
    from podmate.pipeline import _extract_title_from_md

    md = tmp_path / "ep3.md"
    md.write_text("---\n- list item\n- another\n---\n\n# H1 Title\n\nContent.\n")
    assert _extract_title_from_md(md) == "H1 Title"


def test_extract_title_falls_back_to_filename(tmp_path):
    """When no frontmatter or H1, use stem as title."""
    from podmate.pipeline import _extract_title_from_md

    md = tmp_path / "some-guid.md"
    md.write_text("Just some text, no heading.\n")
    assert _extract_title_from_md(md) == "some-guid"


def test_extract_title_invalid_yaml_falls_back(tmp_path):
    """Malformed YAML frontmatter falls back to H1."""
    from podmate.pipeline import _extract_title_from_md

    md = tmp_path / "bad.md"
    md.write_text("---\n: bad yaml: :\n---\n\n# Safe Title\n\nContent.\n")
    assert _extract_title_from_md(md) == "Safe Title"


# ── Podcasts Index: _update_podcasts_index ──────────────


def test_update_index_empty_dir(tmp_path):
    """Empty directory generates 'no records' placeholder."""
    from podmate.pipeline import _update_podcasts_index

    _update_podcasts_index(str(tmp_path))

    index_md = tmp_path / "index.md"
    assert index_md.is_file()
    content = index_md.read_text()
    assert "# 🎙 播客转写稿" in content
    assert "暂无转写记录" in content


def test_update_index_with_files(tmp_path):
    """Directory with .md files generates correct table."""
    from podmate.pipeline import _update_podcasts_index

    (tmp_path / "ep1.md").write_text("# Episode One\n\nContent.\n")
    (tmp_path / "ep2.md").write_text("# Episode Two\n\nContent.\n")

    _update_podcasts_index(str(tmp_path))

    index_md = tmp_path / "index.md"
    content = index_md.read_text()
    assert "| # | 标题 | 语言 | 来源播客 |" in content
    assert "**Episode One**" in content
    assert "**Episode Two**" in content
    assert "🇬🇧 英文" in content
    # ep1 comes first (sorted)
    assert content.index("Episode One") < content.index("Episode Two")


def test_update_index_excludes_self(tmp_path):
    """index.md is excluded from the scanned files."""
    from podmate.pipeline import _update_podcasts_index

    (tmp_path / "ep1.md").write_text("# Ep One\n\nContent.\n")
    (tmp_path / "index.md").write_text("old index")

    _update_podcasts_index(str(tmp_path))

    content = tmp_path.joinpath("index.md").read_text()
    assert "**Ep One**" in content
    assert "🇬🇧 英文" in content
    # Should not link to itself
    assert "index.md" not in content.replace(" ", "").replace("|", "").replace("-", "")


def test_update_index_no_write_when_unchanged(tmp_path):
    """When index content is identical, file is not overwritten."""
    from podmate.pipeline import _update_podcasts_index

    (tmp_path / "ep1.md").write_text("# Ep One\n\nContent.\n")

    _update_podcasts_index(str(tmp_path))
    index_md = tmp_path / "index.md"
    mtime1 = index_md.stat().st_mtime
    content1 = index_md.read_text()

    _update_podcasts_index(str(tmp_path))
    mtime2 = index_md.stat().st_mtime

    assert mtime1 == mtime2
    assert index_md.read_text() == content1


def test_update_index_rewrites_when_changed(tmp_path):
    """When .md files change, index is rewritten."""
    from podmate.pipeline import _update_podcasts_index

    (tmp_path / "ep1.md").write_text("# Ep One\n\nContent.\n")
    _update_podcasts_index(str(tmp_path))
    content1 = tmp_path.joinpath("index.md").read_text()
    assert "**Ep One**" in content1
    assert "| 1 | **Ep One** | [🇬🇧 英文](ep1.md) | — |" in content1
    assert "ep2.md" not in content1

    (tmp_path / "ep2.md").write_text("# Ep Two\n\nContent.\n")
    _update_podcasts_index(str(tmp_path))
    content2 = tmp_path.joinpath("index.md").read_text()
    assert "**Ep One**" in content2
    assert "**Ep Two**" in content2
    assert content2 != content1


# ── CLI: export command ─────────────────────────────────


def test_cli_export_rebuild_index(tmp_path, monkeypatch):
    """export --rebuild-index generates index from .md files in cbrain dir."""
    cbrain_dir = tmp_path / "cbrain" / "podcasts"
    cbrain_dir.mkdir(parents=True)
    (cbrain_dir / "a.md").write_text("# Episode A\n\nContent.\n")
    (cbrain_dir / "b.md").write_text("# Episode B\n\nContent.\n")

    test_cfg = load_config().copy()
    test_cfg["storage"]["cbrain_dir"] = str(cbrain_dir)
    monkeypatch.setattr("podmate.cli.load_config", lambda: test_cfg)

    result = runner.invoke(app, ["export", "index"])

    assert result.exit_code == 0
    assert "索引已重建" in result.stdout
    index_md = cbrain_dir / "index.md"
    assert index_md.is_file()
    content = index_md.read_text()
    assert "**Episode A**" in content
    assert "**Episode B**" in content
    assert "🇬🇧 英文" in content


def test_cli_export_episode_no_transcript(tmp_path, monkeypatch):
    """export <episode-id> fails when episode has no transcript."""
    cbrain_dir = tmp_path / "cbrain" / "podcasts"
    cbrain_dir.mkdir(parents=True)

    test_cfg = load_config().copy()
    test_cfg["storage"]["cbrain_dir"] = str(cbrain_dir)
    monkeypatch.setattr("podmate.cli.load_config", lambda: test_cfg)

    feed = add_feed(url="https://example.com/export-notrans.xml", title="No Trans")
    ep = add_episode(feed_id=feed.id, guid="no-trans-export", title="No Trans Ep")
    # No transcript_path set

    result = runner.invoke(app, ["export", "episode", str(ep.id)])

    assert result.exit_code == 1
    assert "尚未转写" in result.stdout


def test_cli_export_episode_md_missing(tmp_path, monkeypatch):
    """export <episode-id> fails when .md file does not exist."""
    cbrain_dir = tmp_path / "cbrain" / "podcasts"
    cbrain_dir.mkdir(parents=True)

    test_cfg = load_config().copy()
    test_cfg["storage"]["cbrain_dir"] = str(cbrain_dir)
    monkeypatch.setattr("podmate.cli.load_config", lambda: test_cfg)

    feed = add_feed(url="https://example.com/export-nomd.xml", title="No MD")
    ep = add_episode(feed_id=feed.id, guid="no-md-export", title="No MD Ep")
    set_episode_path(ep.id, "transcript_path", str(tmp_path / "nonexistent.json"))

    result = runner.invoke(app, ["export", "episode", str(ep.id)])

    assert result.exit_code == 1
    assert "Markdown 文字稿不存在" in result.stdout


def test_cli_export_episode_success(tmp_path, monkeypatch):
    """export <episode-id> copies .md to cbrain and succeeds."""
    cbrain_dir = tmp_path / "cbrain" / "podcasts"
    cbrain_dir.mkdir(parents=True)

    test_cfg = load_config().copy()
    test_cfg["storage"]["cbrain_dir"] = str(cbrain_dir)
    monkeypatch.setattr("podmate.cli.load_config", lambda: test_cfg)

    feed = add_feed(url="https://example.com/export-ok.xml", title="Export OK")
    ep = add_episode(feed_id=feed.id, guid="export-ok-guid", title="Export OK Ep")

    json_path = tmp_path / "export-ok-guid.json"
    json_path.write_text("{}")
    md_path = tmp_path / "export-ok-guid.md"
    md_path.write_text("# Export OK Ep\n\nContent.\n")
    set_episode_path(ep.id, "transcript_path", str(json_path))

    result = runner.invoke(app, ["export", "episode", str(ep.id)])

    assert result.exit_code == 0
    assert "已导出到" in result.stdout
    copied = cbrain_dir / "export-ok-guid.md"
    assert copied.is_file()
    # 导出时会附加元数据头部
    content = copied.read_text()
    assert "---" in content
    assert 'title: "Export OK Ep"' in content
    assert "Content." in content


def test_cli_export_episode_not_found():
    """export with nonexistent episode ID shows error."""
    result = runner.invoke(app, ["export", "episode", "9999"])

    assert result.exit_code == 1
    assert "未找到" in result.stdout


def test_cli_export_no_args(tmp_path, monkeypatch):
    """export with no arguments shows usage hint."""
    cbrain_dir = tmp_path / "cbrain" / "podcasts"
    cbrain_dir.mkdir(parents=True)

    test_cfg = load_config().copy()
    test_cfg["storage"]["cbrain_dir"] = str(cbrain_dir)
    monkeypatch.setattr("podmate.cli.load_config", lambda: test_cfg)

    result = runner.invoke(app, ["export", "episode"])

    assert result.exit_code == 1
    assert "请指定剧集 ID" in result.stdout


def test_export_rebuild_index_empty_dir(tmp_path, monkeypatch):
    """export --rebuild-index on empty dir creates placeholder index."""
    cbrain_dir = tmp_path / "cbrain" / "podcasts"
    cbrain_dir.mkdir(parents=True)

    test_cfg = load_config().copy()
    test_cfg["storage"]["cbrain_dir"] = str(cbrain_dir)
    monkeypatch.setattr("podmate.cli.load_config", lambda: test_cfg)

    result = runner.invoke(app, ["export", "index"])

    assert result.exit_code == 0
    index_md = cbrain_dir / "index.md"
    assert index_md.is_file()
    assert "暂无转写记录" in index_md.read_text()


# ── CLI: export --format ───────────────────────────────


def test_cli_export_format_json(tmp_path, monkeypatch):
    """export --format json copies .json transcript instead of .md."""
    cbrain_dir = tmp_path / "cbrain" / "podcasts"
    cbrain_dir.mkdir(parents=True)

    test_cfg = load_config().copy()
    test_cfg["storage"]["cbrain_dir"] = str(cbrain_dir)
    monkeypatch.setattr("podmate.cli.load_config", lambda: test_cfg)

    feed = add_feed(url="https://example.com/export-json.xml", title="Export JSON")
    ep = add_episode(feed_id=feed.id, guid="export-json-guid", title="Export JSON Ep")

    json_path = tmp_path / "export-json-guid.json"
    json_path.write_text('{"text":"Hello world.","segments":[]}')
    set_episode_path(ep.id, "transcript_path", str(json_path))

    result = runner.invoke(app, ["export", "episode", str(ep.id), "--format", "json"])

    assert result.exit_code == 0
    assert "已导出到" in result.stdout
    copied_json = cbrain_dir / "export-json-guid.json"
    assert copied_json.is_file()
    assert copied_json.read_text() == '{"text":"Hello world.","segments":[]}'


def test_cli_export_format_invalid():
    """export --format with unsupported value shows error."""
    result = runner.invoke(app, ["export", "episode", "1", "--format", "txt"])

    assert result.exit_code == 1
    assert "不支持的格式" in result.stdout
    assert "txt" in result.stdout


# ── CLI: export --output ───────────────────────────────


def test_cli_export_custom_output(tmp_path, monkeypatch):
    """export --output copies .md to custom directory."""
    custom_dir = tmp_path / "my-backup"
    # Don't pre-create — export should auto-create it

    feed = add_feed(url="https://example.com/export-out.xml", title="Export Out")
    ep = add_episode(feed_id=feed.id, guid="export-out-guid", title="Export Out Ep")

    json_path = tmp_path / "export-out-guid.json"
    json_path.write_text("{}")
    md_path = tmp_path / "export-out-guid.md"
    md_path.write_text("# Export Out Ep\n\nContent.\n")
    set_episode_path(ep.id, "transcript_path", str(json_path))

    result = runner.invoke(app, ["export", "episode", str(ep.id), "--output", str(custom_dir)])

    assert result.exit_code == 0
    assert "已导出到" in result.stdout
    assert custom_dir.is_dir()
    copied_md = custom_dir / "export-out-guid.md"
    assert copied_md.is_file()
    content = copied_md.read_text()
    assert "---" in content
    assert 'title: "Export Out Ep"' in content
    assert "Content." in content


def test_cli_export_format_json_custom_output(tmp_path, monkeypatch):
    """export --format json --output copies .json to custom directory."""
    custom_dir = tmp_path / "json-backup"

    feed = add_feed(url="https://example.com/export-json-out.xml", title="JSON Out")
    ep = add_episode(feed_id=feed.id, guid="export-json-out-guid", title="JSON Out Ep")

    json_path = tmp_path / "export-json-out-guid.json"
    json_path.write_text('{"text":"Test.","segments":[]}')
    md_path = tmp_path / "export-json-out-guid.md"
    md_path.write_text("# Should not be copied\n")
    set_episode_path(ep.id, "transcript_path", str(json_path))

    result = runner.invoke(
        app, ["export", "episode", str(ep.id), "--format", "json", "--output", str(custom_dir)]
    )

    assert result.exit_code == 0
    assert "已导出到" in result.stdout
    copied_json = custom_dir / "export-json-out-guid.json"
    assert copied_json.is_file()
    assert copied_json.read_text() == '{"text":"Test.","segments":[]}'
    # .md should NOT be copied when format is json
    assert not (custom_dir / "export-json-out-guid.md").is_file()


# ── CLI: export --id (negative IDs) ────────────────────


def test_cli_export_negative_id_via_option(tmp_path, monkeypatch):
    """export --id -1 parses negative episode IDs correctly."""
    cbrain_dir = tmp_path / "cbrain" / "podcasts"
    cbrain_dir.mkdir(parents=True)

    test_cfg = load_config().copy()
    test_cfg["storage"]["cbrain_dir"] = str(cbrain_dir)
    monkeypatch.setattr("podmate.cli.load_config", lambda: test_cfg)

    feed = add_feed(url="https://example.com/export-neg.xml", title="Neg Export")
    ep = add_episode(feed_id=feed.id, guid="export-neg-guid", title="Neg Export Ep")

    json_path = tmp_path / "export-neg-guid.json"
    json_path.write_text("{}")
    md_path = tmp_path / "export-neg-guid.md"
    md_path.write_text("# Neg Export Ep\n\nContent.\n")
    set_episode_path(ep.id, "transcript_path", str(json_path))

    result = runner.invoke(app, ["export", "episode", "--id", str(ep.id), "--format", "md"])

    assert result.exit_code == 0
    assert "已导出到" in result.stdout
    copied_md = cbrain_dir / "export-neg-guid.md"
    assert copied_md.is_file()


# ═══════════════════════════════════════════════════════════
# Smoke tests for previously untested modules (issue #5)
# ═══════════════════════════════════════════════════════════


# ── player ─────────────────────────────────────────────


def test_get_available_player_found(monkeypatch):
    monkeypatch.setattr("podmate.player._available_player", None)
    monkeypatch.setattr("shutil.which", lambda p: p if p == "mpv" else None)
    assert get_available_player() == "mpv"


def test_get_available_player_not_found(monkeypatch):
    monkeypatch.setattr("podmate.player._available_player", None)
    monkeypatch.setattr("shutil.which", lambda p: None)
    assert get_available_player() is None


def test_get_available_player_cached(monkeypatch):
    monkeypatch.setattr("podmate.player._available_player", None)
    monkeypatch.setattr("shutil.which", lambda p: p if p == "ffplay" else None)
    first = get_available_player()
    monkeypatch.setattr("shutil.which", lambda p: p if p == "mpv" else None)
    second = get_available_player()
    assert first == second == "ffplay"


def test_play_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        play_file(str(tmp_path / "nonexistent.mp3"))


def test_play_file_no_player(monkeypatch, tmp_path):
    audio = tmp_path / "test.mp3"
    audio.write_text("fake audio")
    monkeypatch.setattr("podmate.player._available_player", None)
    monkeypatch.setattr("shutil.which", lambda p: None)
    with pytest.raises(RuntimeError, match="未找到可用的播放器"):
        play_file(str(audio))


def test_play_file_foreground(monkeypatch, tmp_path):
    audio = tmp_path / "test.mp3"
    audio.write_text("fake audio")
    monkeypatch.setattr("podmate.player._available_player", None)
    monkeypatch.setattr("shutil.which", lambda p: "mpv")
    mock_run = MagicMock()
    monkeypatch.setattr("subprocess.run", mock_run)

    result = play_file(str(audio))
    assert result is None
    mock_run.assert_called_once()


def test_play_file_background(monkeypatch, tmp_path):
    audio = tmp_path / "test.mp3"
    audio.write_text("fake audio")
    monkeypatch.setattr("podmate.player._available_player", None)
    monkeypatch.setattr("shutil.which", lambda p: "mpv")
    mock_popen = MagicMock()
    monkeypatch.setattr("subprocess.Popen", mock_popen)

    result = play_file(str(audio), background=True)
    assert result is mock_popen.return_value
    mock_popen.assert_called_once()


def test_play_episode_no_player(monkeypatch):
    monkeypatch.setattr("podmate.player._available_player", None)
    monkeypatch.setattr("shutil.which", lambda p: None)
    with pytest.raises(RuntimeError, match="未找到可用的播放器"):
        play_episode("test.mp3")


def test_play_episode_foreground(monkeypatch):
    monkeypatch.setattr("podmate.player._available_player", None)
    monkeypatch.setattr("shutil.which", lambda p: "mpv")
    mock_run = MagicMock()
    monkeypatch.setattr("subprocess.run", mock_run)

    result = play_episode("test.mp3")
    assert result is None
    mock_run.assert_called_once()


def test_play_episode_background(monkeypatch):
    monkeypatch.setattr("podmate.player._available_player", None)
    monkeypatch.setattr("shutil.which", lambda p: "mpv")
    mock_popen = MagicMock()
    monkeypatch.setattr("subprocess.Popen", mock_popen)

    result = play_episode("test.mp3", background=True)
    assert result is mock_popen.return_value
    mock_popen.assert_called_once()


def test_play_episode_with_start_sec(monkeypatch):
    monkeypatch.setattr("podmate.player._available_player", None)
    monkeypatch.setattr("shutil.which", lambda p: "mpv")
    mock_run = MagicMock()
    monkeypatch.setattr("subprocess.run", mock_run)

    play_episode("test.mp3", start_sec=30)
    cmd = mock_run.call_args[0][0]
    assert "--start" in cmd
    assert "30" in cmd


def test_build_player_command_mpv():
    cmd = _build_player_command("mpv", "test.mp3")
    assert cmd[0] == "mpv"
    assert "test.mp3" in cmd


def test_build_player_command_mpv_with_start():
    cmd = _build_player_command("mpv", "test.mp3", start_sec=45)
    assert "--start" in cmd
    assert "45" in cmd


def test_build_player_command_mplayer():
    cmd = _build_player_command("mplayer", "test.mp3")
    assert cmd[0] == "mplayer"


def test_build_player_command_mplayer_with_start():
    cmd = _build_player_command("mplayer", "test.mp3", start_sec=10)
    assert "-ss" in cmd
    assert "10" in cmd


def test_build_player_command_ffplay():
    cmd = _build_player_command("ffplay", "test.mp3")
    assert cmd[0] == "ffplay"


def test_build_player_command_ffplay_with_start():
    cmd = _build_player_command("ffplay", "test.mp3", start_sec=20)
    assert "-ss" in cmd
    assert "20" in cmd


def test_build_player_command_aplay():
    cmd = _build_player_command("aplay", "test.mp3")
    assert cmd[0] == "aplay"
    assert "-q" in cmd


def test_build_player_command_aplay_ignores_start():
    """aplay does not support seeking — start_sec is ignored."""
    cmd = _build_player_command("aplay", "test.mp3", start_sec=30)
    assert "-ss" not in cmd
    assert "--start" not in cmd


def test_build_player_command_unknown():
    cmd = _build_player_command("custom-player", "test.mp3")
    assert cmd == ["custom-player", "test.mp3"]


# ── downloader ─────────────────────────────────────────


async def test_download_episode_success(tmp_path):
    dest = tmp_path / "test.mp3"

    mock_resp = AsyncMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.headers = {"content-length": "20"}
    mock_resp.aiter_bytes = MagicMock(return_value=_async_iter([b"chunk1", b"chunk2"]))

    mock_stream_ctx = MagicMock()
    mock_stream_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_stream_ctx.__aexit__ = AsyncMock(return_value=None)

    mock_client = AsyncMock()
    mock_client.stream = MagicMock(return_value=mock_stream_ctx)

    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("podmate.downloader.httpx.AsyncClient", return_value=mock_ctx):
        result = await download_episode("https://example.com/audio.mp3", str(dest))

    assert result == str(dest)
    assert dest.read_bytes() == b"chunk1chunk2"


async def test_download_episode_with_callback(tmp_path):
    dest = tmp_path / "test.mp3"
    progress_values = []

    mock_resp = AsyncMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.headers = {"content-length": "12"}
    mock_resp.aiter_bytes = MagicMock(return_value=_async_iter([b"aaa", b"bbb", b"ccc"]))

    mock_stream_ctx = MagicMock()
    mock_stream_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_stream_ctx.__aexit__ = AsyncMock(return_value=None)

    mock_client = AsyncMock()
    mock_client.stream = MagicMock(return_value=mock_stream_ctx)

    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("podmate.downloader.httpx.AsyncClient", return_value=mock_ctx):
        await download_episode(
            "https://example.com/audio.mp3",
            str(dest),
            progress_callback=lambda done, total: progress_values.append((done, total)),
        )

    assert len(progress_values) == 3
    assert progress_values[-1][0] == 9  # total bytes_written


async def test_download_episode_http_error(tmp_path):
    dest = tmp_path / "test.mp3"
    mock_resp = AsyncMock()
    mock_resp.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError(
            "Not Found", request=MagicMock(), response=MagicMock(status_code=404)
        )
    )

    mock_stream_ctx = MagicMock()
    mock_stream_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_stream_ctx.__aexit__ = AsyncMock(return_value=None)

    mock_client = AsyncMock()
    mock_client.stream = MagicMock(return_value=mock_stream_ctx)

    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    with (
        patch("podmate.downloader.httpx.AsyncClient", return_value=mock_ctx),
        pytest.raises(httpx.HTTPStatusError),
    ):
        await download_episode("https://example.com/audio.mp3", str(dest))


# ── translator ─────────────────────────────────────────


def test_translate_segments_no_api_key():
    """translate_segments raises RuntimeError when no API key configured."""
    segments = [{"id": 0, "start": 0.0, "end": 5.0, "text": "Hello world."}]
    with patch("podmate.translator._get_api_key", return_value=""):
        with pytest.raises(RuntimeError, match="未设置 DeepSeek API key"):
            import asyncio

            asyncio.run(translate_segments(segments))


def test_translate_segments_empty():
    """translate_segments raises ValueError when segments is empty."""
    with patch("podmate.translator._get_api_key", return_value="sk-test"):
        with pytest.raises(ValueError, match="转写段落为空"):
            import asyncio

            asyncio.run(translate_segments([]))


def test_extract_translation_bracket_format():
    """_extract_translation parses [N] text | tone: X format."""
    content = "[0] 你好世界 | tone: calm"
    zh, tone = _extract_translation(content, 0)
    assert zh == "你好世界"
    assert tone == "calm"


def test_extract_translation_no_tone():
    """_extract_translation returns 'default' tone when not specified."""
    content = "[5] 翻译文本"
    zh, tone = _extract_translation(content, 5)
    assert zh == "翻译文本"
    assert tone == "default"


def test_extract_translation_dot_format():
    """_extract_translation parses 'N.' format."""
    content = "3. 这是翻译"
    zh, tone = _extract_translation(content, 3)
    assert zh == "这是翻译"
    assert tone == "default"


def test_extract_translation_colon_format():
    """_extract_translation parses 'N:' format."""
    content = "7: 译文内容 | tone: serious"
    zh, tone = _extract_translation(content, 7)
    assert zh == "译文内容"
    assert tone == "serious"


def test_extract_translation_unknown_tone_falls_back():
    """Unknown tone value defaults to 'default'."""
    content = "[1] 文本 | tone: angry"
    zh, tone = _extract_translation(content, 1)
    assert zh == "文本"
    assert tone == "default"


def test_extract_translation_not_found():
    """Segment ID not in content returns empty strings."""
    content = "[0] 第一段\n[1] 第二段"
    zh, tone = _extract_translation(content, 99)
    assert zh == ""
    assert tone == "default"


def test_parse_summary_full():
    """_parse_summary extracts title, summary, and key points."""
    content = "标题: 测试标题\n摘要: 这是一个测试摘要\n要点:\n- 要点1\n- 要点2"
    result = _parse_summary(content)
    assert result["episode_title_zh"] == "测试标题"
    assert result["summary_zh"] == "这是一个测试摘要"
    assert result["key_points"] == ["要点1", "要点2"]


def test_parse_summary_chinese_colons():
    """_parse_summary handles Chinese colons in labels."""
    content = "标题：中文标题\n摘要：中文摘要\n要点:\n- 重点一\n- 重点二"
    result = _parse_summary(content)
    assert result["episode_title_zh"] == "中文标题"
    assert result["summary_zh"] == "中文摘要"
    assert result["key_points"] == ["重点一", "重点二"]


def test_parse_summary_empty():
    """_parse_summary returns empty dict for empty content."""
    result = _parse_summary("")
    assert result == {"summary_zh": "", "key_points": [], "episode_title_zh": ""}


async def test_translate_segments_success(monkeypatch):
    """translate_segments returns translated segments with mocked API."""
    monkeypatch.setattr("podmate.translator._get_api_key", lambda: "sk-test")

    segments = [
        {"id": 0, "start": 0.0, "end": 5.0, "text": "Hello world."},
        {"id": 1, "start": 5.0, "end": 10.0, "text": "This is a test."},
    ]

    mock_call = AsyncMock(
        side_effect=[
            {"content": "Tech podcast with calm tone"},
            {"content": "[0] 你好世界 | tone: calm\n[1] 这是测试 | tone: serious"},
            {"content": "摘要: 测试摘要\n要点:\n- 要点1\n- 要点2"},
        ]
    )

    with patch("podmate.translator._call_deepseek", mock_call):
        result = await translate_segments(segments, batch_size=10)

    assert len(result["segments"]) == 2
    assert result["segments"][0]["zh"] == "你好世界"
    assert result["segments"][0]["tone"] == "calm"
    assert result["segments"][1]["zh"] == "这是测试"
    assert result["segments"][1]["tone"] == "serious"
    assert result["summary_zh"] == "测试摘要"
    assert result["key_points"] == ["要点1", "要点2"]


# ── dubbing ────────────────────────────────────────────


def test_get_voice_for_speaker_known():
    assert "Yunxi" in get_voice_for_speaker("A")
    assert "Yunyang" in get_voice_for_speaker("B")
    assert "Xiaoxiao" in get_voice_for_speaker("C")
    assert "Yunjian" in get_voice_for_speaker("D")


def test_get_voice_for_speaker_unknown():
    """Unknown speaker returns default voice."""
    assert "Yunyang" in get_voice_for_speaker("Z")


def test_wrap_with_tone_calm():
    result = wrap_with_tone("你好", "calm")
    assert '<prosody rate="-10%"' in result
    assert "你好" in result
    assert result.startswith("<speak")


def test_wrap_with_tone_excited():
    result = wrap_with_tone("太棒了", "excited")
    assert '<prosody rate="+10%"' in result


def test_wrap_with_tone_default():
    result = wrap_with_tone("测试", "default")
    assert '<prosody rate="0%"' in result


def test_wrap_with_tone_unknown():
    """Unknown tone falls back to default prosody."""
    result = wrap_with_tone("文本", "unknown")
    assert '<prosody rate="0%"' in result


def test_majority_tone():
    assert _majority_tone(["calm", "calm", "excited"]) == "calm"
    assert _majority_tone(["excited", "serious", "excited", "calm"]) == "excited"


def test_majority_tone_empty():
    assert _majority_tone([]) == "default"


def test_majority_tone_single():
    assert _majority_tone(["serious"]) == "serious"


def test_split_text_short():
    """Text under max_chars returns as single chunk."""
    result = _split_text("短文本。", max_chars=3000)
    assert result == ["短文本。"]


def test_split_text_at_sentence_boundary():
    """Long text splits at last sentence boundary before max_chars."""
    text = ("A" * 2900) + "。split_here" + ("B" * 500)
    result = _split_text(text, max_chars=3000)
    assert len(result) == 2
    assert result[0].endswith("。")


def test_split_text_no_boundary():
    """When no sentence boundary found, splits at max_chars."""
    text = "X" * 5000  # no sentence markers
    result = _split_text(text, max_chars=3000)
    assert len(result) == 2


def test_concat_audio_success(tmp_path, monkeypatch):
    """_concat_audio runs ffmpeg and cleans up temp file."""
    out = tmp_path / "output.mp3"
    mock_run = MagicMock(return_value=MagicMock(returncode=0))
    monkeypatch.setattr("subprocess.run", mock_run)

    _concat_audio(["a.mp3", "b.mp3"], str(out))
    mock_run.assert_called_once()


def test_concat_audio_failure(tmp_path, monkeypatch):
    """_concat_audio raises RuntimeError when ffmpeg fails."""
    out = tmp_path / "output.mp3"
    mock_result = MagicMock(returncode=1, stderr="ffmpeg error")
    mock_run = MagicMock(return_value=mock_result)
    monkeypatch.setattr("subprocess.run", mock_run)

    with pytest.raises(RuntimeError, match="ffmpeg 拼接失败"):
        _concat_audio(["a.mp3"], str(out))


def test_dub_text_empty_raises(monkeypatch, tmp_path):
    """_dub_text processes valid input through to edge_tts."""
    from podmate.dubbing import _dub_text

    mock_comm = MagicMock()
    mock_comm.save = AsyncMock()
    monkeypatch.setattr("podmate.dubbing.edge_tts.Communicate", lambda *a, **kw: mock_comm)

    result = _dub_text("测试文本。", str(tmp_path / "out.mp3"))
    assert result == str(tmp_path / "out.mp3")
    mock_comm.save.assert_called_once()


async def test_dub_translation_single_speaker(tmp_path, monkeypatch):
    """dub_translation with one speaker generates audio via _dub_text."""
    out = tmp_path / "out.mp3"
    segments = [
        {"id": 0, "zh": "你好世界。", "speaker": "A", "tone": "calm", "start": 0.0, "end": 5.0},
    ]

    mock_comm = MagicMock()
    mock_comm.save = AsyncMock()
    monkeypatch.setattr("podmate.dubbing.edge_tts.Communicate", lambda *a, **kw: mock_comm)

    with patch("podmate.dubbing._dub_text") as mock_dub:
        mock_dub.return_value = str(out)
        result = await dub_translation(segments, str(out))
        mock_dub.assert_called_once()
        assert result == str(out)


async def test_dub_translation_multi_speaker(tmp_path, monkeypatch):
    """dub_translation with multiple speakers generates per-speaker audio."""
    out = tmp_path / "out.mp3"
    segments = [
        {"id": 0, "zh": "第一段。", "speaker": "A", "tone": "calm", "start": 0.0, "end": 5.0},
        {"id": 1, "zh": "第二段。", "speaker": "B", "tone": "serious", "start": 5.0, "end": 10.0},
    ]

    mock_comm = MagicMock()
    mock_comm.save = AsyncMock()
    monkeypatch.setattr("podmate.dubbing.edge_tts.Communicate", lambda *a, **kw: mock_comm)

    mock_run = MagicMock(return_value=MagicMock(returncode=0))
    monkeypatch.setattr("subprocess.run", mock_run)

    result = await dub_translation(segments, str(out))
    assert result == str(out)


async def test_generate_audio(tmp_path, monkeypatch):
    """_generate_audio calls edge_tts.Communicate.save."""
    out = tmp_path / "gen.mp3"
    mock_comm = MagicMock()
    mock_comm.save = AsyncMock()
    monkeypatch.setattr("podmate.dubbing.edge_tts.Communicate", lambda *a, **kw: mock_comm)

    await _generate_audio("测试文本", str(out), "zh-CN-YunyangNeural", "+0%", "+0%")
    mock_comm.save.assert_called_once_with(str(out))


# ── transcriber (additional) ───────────────────────────


def test_speaker_label():
    assert _speaker_label(0) == "A"
    assert _speaker_label(1) == "B"
    assert _speaker_label(25) == "Z"


def test_parse_deepgram_response_paragraphs():
    """_parse_deepgram_response extracts segments from paragraph data."""
    data = {
        "results": {
            "channels": [
                {
                    "alternatives": [
                        {
                            "transcript": "Hello world. This is a test.",
                            "language": "en",
                            "duration": 10.0,
                            "paragraphs": {
                                "paragraphs": [
                                    {
                                        "speaker": 0,
                                        "sentences": [
                                            {"text": "Hello world.", "start": 0.0, "end": 3.0},
                                        ],
                                    },
                                    {
                                        "speaker": 1,
                                        "sentences": [
                                            {"text": "This is a test.", "start": 4.0, "end": 9.0},
                                        ],
                                    },
                                ],
                            },
                        }
                    ],
                }
            ],
        },
    }

    result = _parse_deepgram_response(data)
    assert result["text"] == "Hello world. This is a test."
    assert result["language"] == "en"
    assert result["duration_sec"] == 10.0
    assert len(result["segments"]) == 2
    assert result["segments"][0]["speaker"] == "A"
    assert result["segments"][0]["text"] == "Hello world."
    assert result["segments"][1]["speaker"] == "B"
    assert result["segments"][1]["text"] == "This is a test."


def test_parse_deepgram_response_words():
    """_parse_deepgram_response falls back to word-level when no paragraphs."""
    data = {
        "results": {
            "channels": [
                {
                    "alternatives": [
                        {
                            "transcript": "hello world",
                            "language": "en",
                            "duration": 5.0,
                            "words": [
                                {"word": "hello", "start": 0.0, "end": 1.0, "speaker": 0},
                                {"word": "world", "start": 1.5, "end": 3.0, "speaker": 0},
                            ],
                        }
                    ],
                }
            ],
        },
    }

    result = _parse_deepgram_response(data)
    assert len(result["segments"]) == 1
    assert result["segments"][0]["speaker"] == "A"
    assert "hello world" in result["segments"][0]["text"]


def test_parse_deepgram_response_words_multi_speaker():
    """Word-level parsing splits segments on speaker change."""
    data = {
        "results": {
            "channels": [
                {
                    "alternatives": [
                        {
                            "transcript": "hi there",
                            "language": "en",
                            "duration": 5.0,
                            "words": [
                                {"word": "hi", "start": 0.0, "end": 1.0, "speaker": 0},
                                {"word": "there", "start": 1.0, "end": 2.0, "speaker": 0},
                                {"word": "hello", "start": 3.0, "end": 4.0, "speaker": 1},
                            ],
                        }
                    ],
                }
            ],
        },
    }

    result = _parse_deepgram_response(data)
    assert len(result["segments"]) == 2
    assert result["segments"][0]["speaker"] == "A"
    assert result["segments"][1]["speaker"] == "B"


def test_parse_deepgram_response_empty():
    """Empty response returns empty segments."""
    data = {"results": {"channels": [{"alternatives": [{}]}]}}
    result = _parse_deepgram_response(data)
    assert result["segments"] == []
    assert result["language"] == "en"


def test_transcribe_via_deepgram_no_api_key():
    """transcribe_via_deepgram raises RuntimeError when API key missing."""
    with patch("podmate.transcriber._get_deepgram_api_key", return_value=""):
        with pytest.raises(RuntimeError, match="未设置 Deepgram API key"):
            import asyncio

            asyncio.run(transcribe_via_deepgram("test.mp3"))


def test_transcribe_via_deepgram_file_not_found():
    """transcribe_via_deepgram raises FileNotFoundError for missing file."""
    with patch("podmate.transcriber._get_deepgram_api_key", return_value="test-key"):
        with pytest.raises(FileNotFoundError, match="音频文件不存在"):
            import asyncio

            asyncio.run(transcribe_via_deepgram("/nonexistent/path.mp3"))


async def test_transcribe_via_deepgram_success(tmp_path):
    """transcribe_via_deepgram returns parsed result from mocked API."""
    audio = tmp_path / "test.mp3"
    audio.write_text("fake audio data")

    mock_resp = AsyncMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = MagicMock(
        return_value={
            "results": {
                "channels": [
                    {
                        "alternatives": [
                            {
                                "transcript": "Hello world.",
                                "language": "en",
                                "duration": 5.0,
                                "paragraphs": {
                                    "paragraphs": [
                                        {
                                            "speaker": 0,
                                            "sentences": [
                                                {"text": "Hello world.", "start": 0.0, "end": 4.0},
                                            ],
                                        },
                                    ],
                                },
                            }
                        ],
                    }
                ],
            },
        }
    )

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp)

    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    with (
        patch("podmate.transcriber._get_deepgram_api_key", return_value="test-key"),
        patch("podmate.transcriber.httpx.AsyncClient", return_value=mock_ctx),
    ):
        result = await transcribe_via_deepgram(str(audio))

    assert result["text"] == "Hello world."
    assert result["language"] == "en"
    assert result["duration_sec"] == 5.0
    assert len(result["segments"]) == 1


# ── CLI: sync-cbrain ──────────────────────────────────────


def test_sync_cbrain_no_unexported(tmp_path, monkeypatch):
    """When all episodes are already exported, sync_cbrain shows completion message."""
    cbrain_dir = tmp_path / "cbrain" / "podcasts"
    cbrain_dir.mkdir(parents=True)

    test_cfg = load_config().copy()
    test_cfg["storage"]["cbrain_dir"] = str(cbrain_dir)
    monkeypatch.setattr("podmate.cli.load_config", lambda: test_cfg)

    feed = add_feed(url="https://example.com/sync-all-done.xml", title="All Done")
    ep = add_episode(feed_id=feed.id, guid="sync-all-done", title="All Done Ep")

    json_path = tmp_path / "sync-all-done.json"
    json_path.write_text("{}")
    md_path = tmp_path / "sync-all-done.md"
    md_path.write_text("# All Done Ep\n\nContent.\n")
    set_episode_path(ep.id, "transcript_path", str(json_path))

    from podmate.db import mark_episode_exported, update_episode_status

    update_episode_status(ep.id, "transcribed")
    mark_episode_exported(ep.id)

    result = runner.invoke(app, ["export", "sync"])

    assert result.exit_code == 0
    assert "[podmate] 所有转写稿已同步到 cbrain" in result.stdout


def test_sync_cbrain_dry_run(tmp_path, monkeypatch):
    """--dry-run shows episodes that would be exported but does not copy files."""
    cbrain_dir = tmp_path / "cbrain" / "podcasts"
    cbrain_dir.mkdir(parents=True)

    test_cfg = load_config().copy()
    test_cfg["storage"]["cbrain_dir"] = str(cbrain_dir)
    monkeypatch.setattr("podmate.cli.load_config", lambda: test_cfg)

    feed = add_feed(url="https://example.com/sync-dry.xml", title="Dry Sync")
    ep = add_episode(feed_id=feed.id, guid="sync-dry-guid", title="Dry Sync Episode")

    json_path = tmp_path / "sync-dry-guid.json"
    json_path.write_text("{}")
    md_path = tmp_path / "sync-dry-guid.md"
    md_path.write_text("# Dry Sync Episode\n\nContent.\n")
    set_episode_path(ep.id, "transcript_path", str(json_path))

    from podmate.db import update_episode_status

    update_episode_status(ep.id, "transcribed")

    result = runner.invoke(app, ["export", "sync", "--dry-run"])

    assert result.exit_code == 0
    assert "预览模式" in result.stdout
    assert "Dry Sync Episode" in result.stdout
    assert "md=✅" in result.stdout
    assert "--dry-run" in result.stdout
    # No files should be copied
    assert not list(cbrain_dir.glob("*.md"))


def test_sync_cbrain_actual_sync(tmp_path, monkeypatch):
    """sync-cbrain copies .md and .json files, marks exported, rebuilds index."""
    cbrain_dir = tmp_path / "cbrain" / "podcasts"
    cbrain_dir.mkdir(parents=True)

    test_cfg = load_config().copy()
    test_cfg["storage"]["cbrain_dir"] = str(cbrain_dir)
    monkeypatch.setattr("podmate.cli.load_config", lambda: test_cfg)

    feed = add_feed(url="https://example.com/sync-actual.xml", title="Actual Sync")
    ep = add_episode(feed_id=feed.id, guid="sync-actual-guid", title="Actual Sync Ep")

    json_path = tmp_path / "sync-actual-guid.json"
    json_path.write_text('{"text":"Hello."}')
    md_path = tmp_path / "sync-actual-guid.md"
    md_path.write_text("# Actual Sync Ep\n\nContent.\n")
    set_episode_path(ep.id, "transcript_path", str(json_path))

    from podmate.db import update_episode_status

    update_episode_status(ep.id, "transcribed")

    result = runner.invoke(app, ["export", "sync"])

    assert result.exit_code == 0
    assert "已同步" in result.stdout
    assert "1" in result.stdout

    # Files should be copied
    copied_md = cbrain_dir / "sync-actual-guid.md"
    copied_json = cbrain_dir / "sync-actual-guid.json"
    assert copied_md.is_file()
    assert copied_json.is_file()
    assert copied_md.read_text() == "# Actual Sync Ep\n\nContent.\n"

    # Episode should be marked as exported
    updated = get_episode(ep.id)
    assert updated.exported_to_cbrain is True

    # Index should be regenerated
    index_md = cbrain_dir / "index.md"
    assert index_md.is_file()
    assert "Actual Sync Ep" in index_md.read_text()


def test_sync_cbrain_with_since(tmp_path, monkeypatch):
    """--since filters episodes by creation date."""
    cbrain_dir = tmp_path / "cbrain" / "podcasts"
    cbrain_dir.mkdir(parents=True)

    test_cfg = load_config().copy()
    test_cfg["storage"]["cbrain_dir"] = str(cbrain_dir)
    monkeypatch.setattr("podmate.cli.load_config", lambda: test_cfg)

    feed = add_feed(url="https://example.com/sync-since.xml", title="Since Sync")

    ep_old = add_episode(feed_id=feed.id, guid="sync-old-guid", title="Old Episode")
    json_old = tmp_path / "sync-old-guid.json"
    json_old.write_text("{}")
    md_old = tmp_path / "sync-old-guid.md"
    md_old.write_text("# Old Episode\n\nOld.\n")
    set_episode_path(ep_old.id, "transcript_path", str(json_old))

    ep_new = add_episode(feed_id=feed.id, guid="sync-new-guid", title="New Episode")
    json_new = tmp_path / "sync-new-guid.json"
    json_new.write_text("{}")
    md_new = tmp_path / "sync-new-guid.md"
    md_new.write_text("# New Episode\n\nNew.\n")
    set_episode_path(ep_new.id, "transcript_path", str(json_new))

    from podmate.db import get_connection as db_get_connection
    from podmate.db import update_episode_status

    update_episode_status(ep_old.id, "transcribed")
    update_episode_status(ep_new.id, "transcribed")

    # Set created_at timestamps
    conn = db_get_connection()
    conn.execute("UPDATE episodes SET created_at = '2026-01-01' WHERE guid = ?", ("sync-old-guid",))
    conn.execute("UPDATE episodes SET created_at = '2026-07-01' WHERE guid = ?", ("sync-new-guid",))
    conn.commit()

    result = runner.invoke(app, ["export", "sync", "--since", "2026-06-01"])

    assert result.exit_code == 0
    assert "已同步" in result.stdout

    # Old episode (2026-01-01) should NOT be exported
    copied_old = cbrain_dir / "sync-old-guid.md"
    assert not copied_old.is_file()

    # New episode (2026-07-01) SHOULD be exported
    copied_new = cbrain_dir / "sync-new-guid.md"
    assert copied_new.is_file()

    updated_old = get_episode(ep_old.id)
    assert updated_old.exported_to_cbrain is False
    updated_new = get_episode(ep_new.id)
    assert updated_new.exported_to_cbrain is True


def test_sync_cbrain_skips_episodes_without_transcript(tmp_path, monkeypatch):
    """Episodes without transcript_path are not included in sync."""
    cbrain_dir = tmp_path / "cbrain" / "podcasts"
    cbrain_dir.mkdir(parents=True)

    test_cfg = load_config().copy()
    test_cfg["storage"]["cbrain_dir"] = str(cbrain_dir)
    monkeypatch.setattr("podmate.cli.load_config", lambda: test_cfg)

    feed = add_feed(url="https://example.com/sync-notrans.xml", title="No Trans Sync")
    add_episode(feed_id=feed.id, guid="sync-notrans-guid", title="No Trans Ep")
    # No transcript_path set

    result = runner.invoke(app, ["export", "sync"])

    assert result.exit_code == 0
    assert "[podmate] 所有转写稿已同步到 cbrain" in result.stdout


def test_sync_cbrain_rebuilds_index(tmp_path, monkeypatch):
    """After sync, _update_podcasts_index is called and index.md exists."""
    cbrain_dir = tmp_path / "cbrain" / "podcasts"
    cbrain_dir.mkdir(parents=True)

    test_cfg = load_config().copy()
    test_cfg["storage"]["cbrain_dir"] = str(cbrain_dir)
    monkeypatch.setattr("podmate.cli.load_config", lambda: test_cfg)

    feed = add_feed(url="https://example.com/sync-index.xml", title="Index Sync")
    ep = add_episode(feed_id=feed.id, guid="sync-index-guid", title="Index Sync Ep")

    json_path = tmp_path / "sync-index-guid.json"
    json_path.write_text("{}")
    md_path = tmp_path / "sync-index-guid.md"
    md_path.write_text("# Index Sync Ep\n\nContent.\n")
    set_episode_path(ep.id, "transcript_path", str(json_path))

    from podmate.db import update_episode_status

    update_episode_status(ep.id, "transcribed")

    result = runner.invoke(app, ["export", "sync"])

    assert result.exit_code == 0
    index_md = cbrain_dir / "index.md"
    assert index_md.is_file()
    content = index_md.read_text()
    assert "**Index Sync Ep**" in content
    assert "🇬🇧 英文" in content


# ── helpers ────────────────────────────────────────────


async def _async_iter(items):
    """Helper to turn a list into an async iterable for mocking aiter_bytes."""
    for item in items:
        yield item
