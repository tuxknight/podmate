"""PodMate 数据模型。"""

from dataclasses import dataclass


@dataclass
class Feed:
    """播客订阅源。"""

    id: int
    title: str
    url: str
    author: str | None = None
    description: str | None = None
    image_url: str | None = None
    added_at: str = ""
    last_fetched_at: str | None = None
    episode_source: str = "rss"  # "rss" | "podcast-index" | "merged"
    total_episodes: int = 0


@dataclass
class Episode:
    """播客单集。"""

    id: int
    feed_id: int
    guid: str
    title: str
    description: str | None = None
    pub_date: str | None = None
    audio_url: str | None = None
    duration_sec: int | None = None
    local_path: str | None = None
    transcript_path: str | None = None
    translation_path: str | None = None
    dub_path: str | None = None
    status: str = "none"  # none | downloading | downloaded | transcribing  # noqa: E501
    # | transcribed | translating | translated | dubbing | dubbed | error
    progress: float = 0.0  # 0.0–1.0
    error_message: str | None = None
    created_at: str = ""
    # computed via JOIN
    feed_title: str | None = None
