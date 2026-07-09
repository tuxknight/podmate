"""Tests for PodMate CLI commands and underlying functions."""


from unittest.mock import AsyncMock, MagicMock, patch

from typer.testing import CliRunner

from podmate.cli import app
from podmate.db import add_episode, add_feed, get_episodes, get_feed, get_feeds
from podmate.feed import search_itunes

runner = CliRunner()


# ── Feed search ──────────────────────────────────────────


async def test_search_itunes_returns_feed_url():
    """Given keyword, search_itunes returns results containing feedUrl."""
    mock_resp = AsyncMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = AsyncMock(return_value={
        "resultCount": 1,
        "results": [
            {
                "trackName": "The Pragmatic Engineer",
                "artistName": "Gergely Orosz",
                "feedUrl": "https://feeds.example.com/engineer.xml",
                "artworkUrl100": "https://example.com/art.jpg",
                "trackCount": 50,
            }
        ],
    })

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)

    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("podmate.feed.httpx.AsyncClient", return_value=mock_ctx):
        results = await search_itunes("The Pragmatic Engineer")

    assert len(results) == 1
    assert results[0]["feedUrl"] == "https://feeds.example.com/engineer.xml"
    assert results[0]["trackName"] == "The Pragmatic Engineer"


async def test_search_itunes_skips_results_without_feed_url():
    """Results missing feedUrl are filtered out."""
    mock_resp = AsyncMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = AsyncMock(return_value={
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

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)

    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("podmate.feed.httpx.AsyncClient", return_value=mock_ctx):
        results = await search_itunes("test")

    assert len(results) == 1
    assert results[0]["feedUrl"] == "https://feeds.example.com/real.xml"


# ── CLI: sub (URL mode) ─────────────────────────────────


def test_sub_by_url_subscribes_successfully():
    """Given RSS URL, sub command parses feed and stores it."""
    mock_feed = {
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
    }

    with patch("podmate.cli.parse_feed", return_value=mock_feed):
        result = runner.invoke(app, ["sub", "https://example.com/feed.xml"])

    assert result.exit_code == 0
    assert "订阅成功" in result.stdout
    assert "CLI Test Podcast" in result.stdout

    feeds = get_feeds()
    assert any(f.url == "https://example.com/feed.xml" for f in feeds)


# ── CLI: list feeds ──────────────────────────────────────


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


# ── DB: describe feed flow ───────────────────────────────


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


# ── CLI: describe command ────────────────────────────────


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


# ── CLI: status command ──────────────────────────────────


def test_status_shows_stats():
    """Status command shows statistics."""
    add_feed(url="https://example.com/status-test.xml", title="Status Podcast")

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    assert "Status Podcast" in result.stdout or "已订阅播客" in result.stdout
