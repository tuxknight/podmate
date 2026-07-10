"""PodMate 流水线编排器 — 下载 → 转写 → 翻译 → 配音。"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from .config import load as load_config
from .db import (
    get_episode,
    set_episode_path,
    update_episode_status,
)
from .downloader import download_episode
from .dubbing import dub_translation
from .transcriber import format_transcript, transcribe_via_deepgram
from .translator import translate_segments

DATA_DIR = os.path.expanduser(load_config()["storage"]["data_dir"])


def _get_data_path(guid: str, subdir: str) -> str:
    """返回 data/{subdir}/{guid}.json 或 data/{subdir}/{guid}.mp3 的完整路径。"""
    ext = ".mp3" if subdir in ("episodes", "dubs") else ".json"
    return os.path.join(DATA_DIR, subdir, f"{guid}{ext}")


# ── 进度回调 ────────────────────────────────────────


class PipelineProgress:
    """进度跟踪器，供 CLI 层订阅进度更新。"""

    def __init__(self) -> None:
        self.step: str = ""  # 当前步骤名
        self.progress: float = 0.0  # 0.0-1.0
        self.status_text: str = ""  # 状态描述

    def update(self, step: str, progress: float, status_text: str = "") -> None:
        self.step = step
        self.progress = progress
        self.status_text = status_text


# ── Pipeline 编排器 ────────────────────────────────


async def run_pipeline(
    episode_id: int,
    *,
    skip_dub: bool = False,
    progress_callback: object | None = None,
) -> dict[str, Any]:
    """运行一集的完整流水线：下载 → 转写 → 翻译 → 配音。

    Args:
        episode_id: 剧集 ID
        skip_dub: 跳过配音步骤
        progress_callback: 可选回调，接收 PipelineProgress 实例

    Returns:
        dict: {episode_id, status, audio_path, transcript_path, translation_path, dub_path}

    Raises:
        RuntimeError: 任何步骤失败时抛异常，DB 状态设为 error
    """
    pp = PipelineProgress()

    def _emit(step: str, progress: float, text: str = "") -> None:
        pp.update(step, progress, text)
        if progress_callback:
            progress_callback(pp)

    # 1. 获取剧集
    ep = get_episode(episode_id)
    if not ep:
        raise RuntimeError(f"未找到剧集 ID: {episode_id}")
    if not ep.audio_url:
        raise RuntimeError(f"剧集 #{episode_id} 没有音频链接")

    guid = ep.guid
    audio_path = _get_data_path(guid, "episodes")
    transcript_path = _get_data_path(guid, "transcripts")
    translation_path = _get_data_path(guid, "translations")
    dub_path = _get_data_path(guid, "dubs")

    for p in [audio_path, transcript_path, translation_path, dub_path]:
        os.makedirs(os.path.dirname(p), exist_ok=True)

    try:
        # ── 下载（跳过已存在的文件） ────────────────────
        if os.path.exists(audio_path) and os.path.getsize(audio_path) > 1024:
            _emit("downloaded", 1.0, "音频已存在，跳过下载")
        else:
            _emit("downloading", 0.0, f"正在下载: {ep.title}")
            update_episode_status(episode_id, "downloading", progress=0.0)

            def _dl_cb(done: int, total: int) -> None:
                progress = done / total if total > 0 else 0
                update_episode_status(episode_id, "downloading", progress=progress)

            await download_episode(ep.audio_url, audio_path, progress_callback=_dl_cb)

            update_episode_status(episode_id, "downloaded", progress=1.0)
            set_episode_path(episode_id, "local_path", audio_path)
            _emit("downloaded", 1.0, "下载完成")

        # ── 转写（Deepgram API） ──────────────────────
        _emit("transcribing", 0.0, "正在通过 Deepgram API 转写音频...")
        update_episode_status(episode_id, "transcribing", progress=0.0)

        result = await transcribe_via_deepgram(audio_path, episode_id=episode_id)

        # 保存转写结果（JSON + Markdown 双格式）
        with open(transcript_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

        readable_path = str(Path(transcript_path).with_suffix(".md"))
        readable_content = format_transcript(result, title=ep.title)
        with open(readable_path, "w", encoding="utf-8") as f:
            f.write(readable_content)

        set_episode_path(episode_id, "transcript_path", transcript_path)
        update_episode_status(episode_id, "transcribed", progress=1.0)

        lang = result.get("language", "?")
        seg_count = len(result.get("segments", []))
        speakers = set(s.get("speaker", "?") for s in result["segments"])
        _emit("transcribed", 1.0, f"转写完成: {lang}, {seg_count} 段, {len(speakers)} 位说话人")

        # ── 导出到 cbrain ────────────────────────────
        exported_to_cbrain = False
        cbrain_podcasts = Path.home() / "cbrain" / "docs" / "fuyuans-kb" / "podcasts"
        if cbrain_podcasts.exists():
            md_path = Path(transcript_path).with_suffix(".md")
            if md_path.exists():
                dest = cbrain_podcasts / md_path.name
                shutil.copy2(md_path, dest)
                exported_to_cbrain = True
                _emit("exported", 1.0, f"已导出到 cbrain: {dest}")

        # ── 翻译 ─────────────────────────────────────
        _emit("translating", 0.0, "正在调用 DeepSeek 翻译...")
        update_episode_status(episode_id, "translating", progress=0.0)

        translation = await translate_segments(
            result.get("segments", []),
            batch_size=20,
            episode_id=episode_id,
        )

        # 保存翻译结果
        with open(translation_path, "w", encoding="utf-8") as f:
            json.dump(translation, f, ensure_ascii=False, indent=2)

        set_episode_path(episode_id, "translation_path", translation_path)
        update_episode_status(episode_id, "translated", progress=1.0)

        summary = translation.get("summary_zh", "")
        _emit("translated", 1.0, f"翻译完成: {summary}")

        # ── 配音 ─────────────────────────────────────
        if not skip_dub:
            _emit("dubbing", 0.0, "正在生成中文配音...")
            update_episode_status(episode_id, "dubbing", progress=0.0)

            dub_path_result = await dub_translation(
                translation.get("segments", []),
                dub_path,
                episode_id=episode_id,
            )

            set_episode_path(episode_id, "dub_path", dub_path_result)
            update_episode_status(episode_id, "dubbed", progress=1.0)
            _emit("dubbed", 1.0, "配音完成 (Yunyang 云扬)")
        else:
            update_episode_status(episode_id, "dubbed", progress=1.0)
            _emit("dubbed", 1.0, "跳过配音")

        return {
            "episode_id": episode_id,
            "status": "dubbed",
            "audio_path": audio_path,
            "transcript_path": transcript_path,
            "translation_path": translation_path,
            "dub_path": dub_path,
            "exported_to_cbrain": exported_to_cbrain,
        }

    except Exception as e:
        update_episode_status(
            episode_id, "error",
            progress=0.0,
            error_message=str(e),
        )
        raise RuntimeError(f"流水线失败 (ep #{episode_id}): {e}")
