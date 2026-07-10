"""PodMate RSS 订阅发现与解析模块 — iTunes 搜索、RSS 解析、Podcast Index 集成。"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from typing import Any
from urllib.parse import quote

import feedparser
import httpx

logger = logging.getLogger(__name__)

# ── iTunes Search ─────────────────────────────────────


async def search_itunes(keyword: str, limit: int = 10) -> list[dict[str, Any]]:
    """搜索 iTunes Podcast API，返回播客列表。

    返回的每个 dict 包含:
        trackName, artistName, feedUrl, artworkUrl100, trackCount, collectionId
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
            "collectionId": item.get("collectionId", 0),
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


# ── Podcast Index API ──────────────────────────────────


class PodcastIndexClient:
    """Podcast Index API 客户端，用于查询播客完整剧集列表。

    Podcast Index (https://podcastindex.org) 是一个开放的播客搜索索引。
    提供按 feed URL 或 iTunes ID 查询剧集的 API，通常比 RSS feed 包含更完整的剧集列表。

    API 认证需要注册获取 api_key 和 api_secret。
    认证方式：HTTP 头 X-Auth-Key + X-Auth-Date + Authorization (SHA1 签名)。
    """

    BASE_URL = "https://api.podcastindex.org/api/1.0"

    def __init__(self, api_key: str, api_secret: str) -> None:
        """初始化 Podcast Index 客户端。

        Args:
            api_key: Podcast Index API key。
            api_secret: Podcast Index API secret。
        """
        self.api_key = api_key
        self.api_secret = api_secret

    def _auth_headers(self) -> dict[str, str]:
        """生成 Podcast Index API 认证头。

        签名算法：SHA1(api_key + api_secret + unix_timestamp)。
        """
        ts = str(int(time.time()))
        auth_hash = hashlib.sha1(
            (self.api_key + self.api_secret + ts).encode()
        ).hexdigest()
        return {
            "X-Auth-Date": ts,
            "X-Auth-Key": self.api_key,
            "Authorization": auth_hash,
        }

    async def search_by_feed_url(self, feed_url: str) -> list[dict[str, Any]]:
        """按 feed URL 查询剧集列表。

        Args:
            feed_url: 播客 RSS feed URL。

        Returns:
            剧集列表，每项包含 title, guid, description, pub_date, audio_url, duration_sec。
        """
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{self.BASE_URL}/episodes/byfeedurl",
                params={"url": feed_url},
                headers=self._auth_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
        return self._parse_episodes(data)

    async def search_by_itunes_id(self, itunes_id: int) -> list[dict[str, Any]]:
        """按 iTunes 播客 ID 查询剧集列表。

        Args:
            itunes_id: iTunes 播客 collection ID。

        Returns:
            剧集列表，每项包含 title, guid, description, pub_date, audio_url, duration_sec。
        """
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{self.BASE_URL}/episodes/byitunesid",
                params={"id": itunes_id},
                headers=self._auth_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
        return self._parse_episodes(data)

    @staticmethod
    def _parse_episodes(data: dict[str, Any]) -> list[dict[str, Any]]:
        """解析 Podcast Index API 返回的剧集数据。"""
        items = data.get("items", [])
        episodes: list[dict[str, Any]] = []
        for item in items:
            duration = item.get("duration", 0)
            if isinstance(duration, str):
                duration = _parse_duration(duration)
            episodes.append({
                "title": item.get("title", ""),
                "guid": item.get("guid", ""),
                "description": _strip_html(item.get("description", "")),
                "pub_date": item.get("datePublishedPretty", ""),
                "audio_url": item.get("enclosureUrl", ""),
                "duration_sec": duration or 0,
            })
        return episodes


# ── Resolve feed (RSS + optional Podcast Index) ────────


async def resolve_feed(
    feed_url: str,
    itunes_id: int | None = None,
    podcast_index: PodcastIndexClient | None = None,
) -> dict[str, Any]:
    """解析播客 RSS，尽可能通过 Podcast Index 获取完整剧集列表。

    策略：
    1. 先用 RSS 解析获取基础剧集列表。
    2. 如果配置了 PodcastIndexClient，尝试通过 Podcast Index API 查询更多剧集。
    3. 如果 Podcast Index 返回的剧集数更多，优先使用 Podcast Index 数据（更完整）。
    4. 按 guid 去重。
    5. API 调用失败时静默回退到 RSS 模式。

    Args:
        feed_url: 播客 RSS feed URL。
        itunes_id: iTunes 播客 collection ID（可选，用于 Podcast Index 备选查询）。
        podcast_index: PodcastIndexClient 实例（可选，未提供则仅使用 RSS）。

    Returns:
        {
            "title": str, "author": str, "description": str,
            "image_url": str, "link": str, "episodes": [...],
            "episode_source": "rss" | "podcast-index" | "merged",
            "total_episodes": int,
        }
    """
    rss_data = parse_feed(feed_url)
    rss_episodes = rss_data.get("episodes", [])
    source = "rss"
    all_episodes = rss_episodes

    if podcast_index is not None:
        try:
            pi_episodes = await podcast_index.search_by_feed_url(feed_url)
            if not pi_episodes and itunes_id:
                pi_episodes = await podcast_index.search_by_itunes_id(itunes_id)

            if pi_episodes:
                rss_guids = {ep.get("guid") for ep in rss_episodes}
                new_from_pi = [
                    ep for ep in pi_episodes if ep.get("guid") not in rss_guids
                ]
                if new_from_pi:
                    all_episodes = rss_episodes + new_from_pi
                    source = "merged"
        except Exception:
            logger.debug("Podcast Index API failed, falling back to RSS", exc_info=True)

    return {
        "title": rss_data.get("title", ""),
        "author": rss_data.get("author", ""),
        "description": rss_data.get("description", ""),
        "image_url": rss_data.get("image_url", ""),
        "link": rss_data.get("link", ""),
        "episodes": all_episodes,
        "episode_source": source,
        "total_episodes": len(all_episodes),
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
    clean = clean.replace("&quot;", '"').replace("&#39;", "'")
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
