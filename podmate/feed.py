"""PodMate RSS 订阅发现与解析模块。"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

import feedparser
import httpx

# ── iTunes Search ─────────────────────────────────────


async def search_itunes(keyword: str, limit: int = 10) -> list[dict[str, Any]]:
    """搜索 iTunes Podcast API，返回播客列表。

    返回的每个 dict 包含:
        trackName, artistName, feedUrl, artworkUrl100, trackCount
    """
    url = f"https://itunes.apple.com/search?term={quote(keyword)}&media=podcast&limit={limit}"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()

    results: list[dict[str, Any]] = []
    for item in data.get("results", []):
        feed_url = item.get("feedUrl")
        if not feed_url:
            continue
        results.append({
            "trackName": item.get("trackName", ""),
            "artistName": item.get("artistName", ""),
            "feedUrl": feed_url,
            "artworkUrl100": item.get("artworkUrl100", ""),
            "trackCount": item.get("trackCount", 0),
        })
    return results


# ── Feed Parsing ──────────────────────────────────────


def parse_feed(url: str) -> dict[str, Any]:
    """解析 RSS/Atom 播客订阅源。

    返回:
        {
            "title": "...",
            "author": "...",
            "description": "...",
            "image_url": "...",
            "link": "...",
            "episodes": [
                {
                    "title": "...",
                    "guid": "...",
                    "description": "...",
                    "pub_date": "...",        # ISO 8601 或 RSS 原始格式
                    "audio_url": "...",
                    "duration_sec": 1234,
                },
                ...
            ],
        }
    """
    parsed = feedparser.parse(url)

    feed_meta = parsed.feed

    # 提取图片 URL
    image_url = ""
    if hasattr(feed_meta, "image") and hasattr(feed_meta.image, "href"):
        image_url = feed_meta.image.href
    elif hasattr(feed_meta, "itunes_image"):
        image_url = feed_meta.itunes_image.get("href", "")
    elif hasattr(feed_meta, "logo"):
        image_url = feed_meta.logo

    # 提取作者
    author = ""
    if hasattr(feed_meta, "author"):
        author = feed_meta.author
    elif hasattr(feed_meta, "itunes_author"):
        author = feed_meta.itunes_author

    # 提取描述
    description = ""
    if hasattr(feed_meta, "subtitle"):
        description = feed_meta.subtitle
    elif hasattr(feed_meta, "description"):
        description = feed_meta.description

    episodes: list[dict[str, Any]] = []
    for entry in parsed.entries:
        guid = entry.get("id", "")
        # iTunes GUID (for podcasts that don't have standard <guid>)
        if not guid and hasattr(entry, "itunes_id"):
            guid = entry.itunes_id
        # Fallback: use link as GUID
        if not guid and hasattr(entry, "link"):
            guid = entry.link
        # Last resort
        if not guid:
            guid = entry.get("title", "")

        title = entry.get("title", "")

        # 描述 (strip HTML tags)
        desc = ""
        if hasattr(entry, "summary"):
            desc = _strip_html(entry.summary)
        elif hasattr(entry, "description"):
            desc = _strip_html(entry.description)
        elif hasattr(entry, "subtitle"):
            desc = entry.subtitle

        # 发布时间
        pub_date = ""
        if hasattr(entry, "published"):
            pub_date = entry.published
        elif hasattr(entry, "updated"):
            pub_date = entry.updated

        # 音频 URL (enclosure)
        audio_url = ""
        if hasattr(entry, "enclosures") and entry.enclosures:
            for enc in entry.enclosures:
                href = enc.get("href", "")
                mime = enc.get("type", "")
                if "audio" in mime or href.endswith((".mp3", ".m4a", ".wav", ".ogg")):
                    audio_url = href
                    break
            if not audio_url:
                audio_url = entry.enclosures[0].get("href", "")

        # 时长
        duration_sec = 0
        if hasattr(entry, "itunes_duration"):
            duration_sec = _parse_duration(entry.itunes_duration)

        episodes.append({
            "title": title,
            "guid": guid,
            "description": desc,
            "pub_date": pub_date,
            "audio_url": audio_url,
            "duration_sec": duration_sec,
        })

    return {
        "title": feed_meta.get("title", ""),
        "author": author,
        "description": description,
        "image_url": image_url,
        "link": feed_meta.get("link", ""),
        "episodes": episodes,
    }


# ── Fetch recent episodes ─────────────────────────────


async def fetch_recent_episodes(feed_url: str, limit: int = 5) -> list[dict[str, Any]]:
    """异步获取指定订阅源的最新剧集列表。"""
    parsed = parse_feed(feed_url)
    return parsed.get("episodes", [])[:limit]


# ── Internal helpers ──────────────────────────────────


def _strip_html(text: str) -> str:
    """去除 HTML 标签。"""
    clean = re.sub(r"<[^>]+>", "", text)
    clean = clean.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    clean = clean.replace("&quot;", "\"").replace("&#39;", "'")
    return clean.strip()


def _parse_duration(duration: str | int) -> int:
    """将 iTunes 时长字符串/数字转为秒数。

    支持格式: "HH:MM:SS", "MM:SS", 或纯数字字符串。
    """
    if isinstance(duration, int):
        return duration
    if isinstance(duration, (float,)):
        return int(duration)

    duration_str = str(duration).strip()
    # 纯数字
    if duration_str.isdigit():
        return int(duration_str)

    # HH:MM:SS 或 MM:SS
    parts = duration_str.split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    elif len(parts) == 2:
        return int(parts[0]) * 60 + int(parts[1])
    return 0
