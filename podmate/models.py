"""PodMate 数据模型。"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Feed:
    """播客订阅源。"""

    id: int
    title: str
    url: str
    author: Optional[str] = None
    description: Optional[str] = None
    image_url: Optional[str] = None
    added_at: str = ""
    last_fetched_at: Optional[str] = None


@dataclass
class Episode:
    """播客单集。"""

    id: int
    feed_id: int
    guid: str
    title: str
    description: Optional[str] = None
    pub_date: Optional[str] = None
    audio_url: Optional[str] = None
    duration_sec: Optional[int] = None
    local_path: Optional[str] = None
    transcript_path: Optional[str] = None
    translation_path: Optional[str] = None
    dub_path: Optional[str] = None
    status: str = "none"  # none | downloading | downloaded | transcribing | transcribed | translating | translated | dubbing | dubbed | error
    progress: float = 0.0  # 0.0–1.0
    error_message: Optional[str] = None
    created_at: str = ""
    # computed via JOIN
    feed_title: Optional[str] = None
