"""Tests for PodMate CLI commands and underlying functions."""

import hashlib
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from typer.testing import CliRunner

from podmate.cli import app
from podmate.config import load as load_config
from podmate.db import add_episode, add_feed, get_episodes, get_feed, get_feeds
from podmate.feed import PodcastIndexClient, parse_feed, resolve_feed, search_itunes

runner = CliRunner()

# ── Helpers ────────────────────────────────────────────────


def _mock_httpx_client(json_data):
    """Build a mock httpx.AsyncClient context manager returning given JSON."""
    mock_resp = AsyncMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = AsyncMock(return_value=json_data)

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
    mock_ctx = _mock_httpx_client({
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
    })

    with patch("podmate.feed.httpx.AsyncClient", return_value=mock_ctx):
        results = await search_itunes("The Pragmatic Engineer")

    assert len(results) == 1
    assert results[0]["feedUrl"] == "https://feeds.example.com/engineer.xml"
    assert results[0]["trackName"] == "The Pragmatic Engineer"
    assert results[0]["collectionId"] == 123456
    assert results[0]["trackCount"] == 50


async def test_search_itunes_skips_results_without_feed_url():
    """Results missing feedUrl are filtered out."""
    mock_ctx = _mock_httpx_client({
        "resultCount": 2,
        "results": [
            {"trackName": "No Feed", "artistName": "Someone", "feedUrl": ""},
            {
                "trackName": "Has Feed",
                "artistName": "Author",
                "feedUrl": "https://feeds.example.com/real.xml",
            },
        ],
    })

    with patch("podmate.feed.httpx.AsyncClient", return_value=mock_ctx):
        results = await search_itunes("test")

    assert len(results) == 1
    assert results[0]["feedUrl"] == "https://feeds.example.com/real.xml"


async def test_search_itunes_returns_collection_id_zero_when_missing():
    """collectionId defaults to 0 when not in API response."""
    mock_ctx = _mock_httpx_client({
        "resultCount": 1,
        "results": [
            {
                "trackName": "Podcast",
                "artistName": "Author",
                "feedUrl": "https://example.com/feed.xml",
            }
        ],
    })

    with patch("podmate.feed.httpx.AsyncClient", return_value=mock_ctx):
        results = await search_itunes("test")

    assert results[0]["collectionId"] == 0


# ── Feed: parse_feed ───────────────────────────────────────


def test_parse_feed_extracts_metadata_and_episodes():
    """parse_feed returns title, author, episodes with guid/duration/audio."""
    mock_parsed = MagicMock()
    img = MagicMock()
    img.href = "https://example.com/art.jpg"
    mock_parsed.feed = _mock_feed_meta({
        "title": "Test Podcast",
        "link": "https://example.com",
        "author": "Test Author",
        "subtitle": "A test podcast description",
        "image": img,
    })

    mock_parsed.entries = [_mock_feedparser_entry({
        "id": "guid-001",
        "title": "Episode One",
        "summary": "<p>First episode content</p>",
        "published": "2024-01-01T00:00:00Z",
        "itunes_duration": "30:00",
        "enclosures": [{"href": "https://example.com/ep1.mp3", "type": "audio/mpeg"}],
    })]

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
    mock_parsed.feed = _mock_feed_meta({
        "title": "Minimal Podcast",
        "link": "",
        "author": "",
        "subtitle": "",
        "image": img,
    })

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
    mock_ctx = _mock_httpx_client({
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
    })

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
    mock_ctx = _mock_httpx_client({
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
    })

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
    mock_parsed.feed = _mock_feed_meta({
        "title": "RSS Podcast",
        "link": "https://example.com",
        "author": "RSS Author",
        "subtitle": "RSS description",
        "image": img,
    })

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
    mock_parsed.feed = _mock_feed_meta({
        "title": "Podcast",
        "link": "",
        "author": "",
        "subtitle": "",
        "image": img,
    })

    mock_parsed.entries = [
        _mock_feedparser_entry({"id": "rss-1", "title": "RSS Ep"}),
        _mock_feedparser_entry({"id": "rss-only", "title": "RSS Only Ep"}),
    ]

    pi_mock = _mock_httpx_client({
        "items": [
            {"title": "PI Ep 1", "guid": "rss-1", "description": "",
             "datePublishedPretty": "", "enclosureUrl": "", "duration": 0},
            {"title": "PI Ep 2", "guid": "pi-2", "description": "",
             "datePublishedPretty": "", "enclosureUrl": "", "duration": 0},
        ],
    })

    with patch("podmate.feed.feedparser.parse", return_value=mock_parsed), \
         patch("podmate.feed.httpx.AsyncClient", return_value=pi_mock):
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
    mock_parsed.feed = _mock_feed_meta({
        "title": "Safe Podcast",
        "link": "",
        "author": "",
        "subtitle": "",
        "image": img,
    })

    mock_parsed.entries = [_mock_feedparser_entry({"id": "rss-1", "title": "RSS Ep"})]

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=httpx.HTTPError("Network error"))

    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("podmate.feed.feedparser.parse", return_value=mock_parsed), \
         patch("podmate.feed.httpx.AsyncClient", return_value=mock_ctx):
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
    mock_parsed.feed = _mock_feed_meta({
        "title": "Rich RSS",
        "link": "",
        "author": "",
        "subtitle": "",
        "image": img,
    })

    entries = [_mock_feedparser_entry({"id": f"rss-{i}", "title": f"RSS Ep {i}"})
               for i in range(3)]
    mock_parsed.entries = entries

    pi_mock = _mock_httpx_client({
        "items": [
            {"title": "PI Ep 1", "guid": "pi-1", "description": "",
             "datePublishedPretty": "", "enclosureUrl": "", "duration": 0},
        ],
    })

    with patch("podmate.feed.feedparser.parse", return_value=mock_parsed), \
         patch("podmate.feed.httpx.AsyncClient", return_value=pi_mock):
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
            {"title": "E1", "guid": "e1", "description": "", "pub_date": "",
             "audio_url": "", "duration_sec": 0},
            {"title": "E2", "guid": "e2", "description": "", "pub_date": "",
             "audio_url": "", "duration_sec": 0},
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
    result = runner.invoke(app, ["list"])

    assert result.exit_code == 0
    assert "还没有订阅任何播客" in result.stdout


def test_list_feeds_shows_subscribed():
    """Given a feed in db, list command displays it."""
    add_feed(
        url="https://example.com/list-test.xml",
        title="List Test Podcast",
    )

    result = runner.invoke(app, ["list"])

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

    result = runner.invoke(app, ["describe", str(feed.id)])

    assert result.exit_code == 0
    assert "CLI Describe Podcast" in result.stdout
    assert "CLI Author" in result.stdout


def test_cli_describe_nonexistent_feed():
    """Given invalid feed ID, describe shows error."""
    result = runner.invoke(app, ["describe", "9999"])

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
    mock_parsed.feed = _mock_feed_meta({
        "title": "Merge Test",
        "link": "",
        "author": "",
        "subtitle": "",
        "image": img,
    })

    mock_parsed.entries = [
        _mock_feedparser_entry({"id": "rss-exclusive-1", "title": "RSS Only 1"}),
        _mock_feedparser_entry({"id": "rss-exclusive-2", "title": "RSS Only 2"}),
    ]

    pi_mock = _mock_httpx_client({
        "items": [
            {"title": "PI Ep 1", "guid": "pi-1", "description": "",
             "datePublishedPretty": "", "enclosureUrl": "", "duration": 0},
            {"title": "PI Ep 2", "guid": "pi-2", "description": "",
             "datePublishedPretty": "", "enclosureUrl": "", "duration": 0},
            {"title": "PI Ep 3", "guid": "pi-3", "description": "",
             "datePublishedPretty": "", "enclosureUrl": "", "duration": 0},
        ],
    })

    with patch("podmate.feed.feedparser.parse", return_value=mock_parsed), \
         patch("podmate.feed.httpx.AsyncClient", return_value=pi_mock):
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
    mock_parsed.feed = _mock_feed_meta({
        "title": "Dup Test",
        "link": "",
        "author": "",
        "subtitle": "",
        "image": img,
    })

    mock_parsed.entries = [
        _mock_feedparser_entry({"id": "shared-1", "title": "RSS Shared"}),
    ]

    pi_mock = _mock_httpx_client({
        "items": [
            {"title": "PI Shared", "guid": "shared-1", "description": "",
             "datePublishedPretty": "", "enclosureUrl": "", "duration": 0},
        ],
    })

    with patch("podmate.feed.feedparser.parse", return_value=mock_parsed), \
         patch("podmate.feed.httpx.AsyncClient", return_value=pi_mock):
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
    result = runner.invoke(app, ["refresh", str(feed.id)])
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
            {"title": "Existing Episode", "guid": "existing-ep", "description": "",
             "pub_date": "", "audio_url": "", "duration_sec": 0},
            {"title": "New Episode 1", "guid": "new-ep-1", "description": "",
             "pub_date": "", "audio_url": "", "duration_sec": 0},
            {"title": "New Episode 2", "guid": "new-ep-2", "description": "",
             "pub_date": "", "audio_url": "", "duration_sec": 0},
        ],
        "episode_source": "merged",
        "total_episodes": 3,
    }

    test_cfg = load_config().copy()
    test_cfg["podcast_index"]["api_key"] = "pk-test"
    test_cfg["podcast_index"]["api_secret"] = "sk-test"
    monkeypatch.setattr("podmate.cli.load_config", lambda: test_cfg)

    with patch("podmate.cli.resolve_feed", new=AsyncMock(return_value=mock_feed_data)):
        result = runner.invoke(app, ["refresh", str(feed.id)])

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
            {"title": "Keep Me", "guid": "keep-1", "description": "",
             "pub_date": "", "audio_url": "", "duration_sec": 0},
            {"title": "New Only", "guid": "new-only", "description": "",
             "pub_date": "", "audio_url": "", "duration_sec": 0},
        ],
        "episode_source": "merged",
        "total_episodes": 3,
    }

    test_cfg = load_config().copy()
    test_cfg["podcast_index"]["api_key"] = "pk-test"
    test_cfg["podcast_index"]["api_secret"] = "sk-test"
    monkeypatch.setattr("podmate.cli.load_config", lambda: test_cfg)

    with patch("podmate.cli.resolve_feed", new=AsyncMock(return_value=mock_feed_data)):
        result = runner.invoke(app, ["refresh", str(feed.id)])

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
