"""PodMate 流水线编排器 — 下载 → 转写 → 翻译 → 配音。"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

import yaml

from .config import load as load_config
from .db import (
    get_connection,
    get_episode,
    mark_episode_exported,
    set_episode_path,
    update_episode_status,
)
from .downloader import download_episode
from .dubbing import dub_translation
from .transcriber import _format_time, format_transcript, transcribe_via_deepgram
from .translator import translate_segments

DATA_DIR = os.path.expanduser(load_config()["storage"]["data_dir"])


def _safe_filename(guid: str) -> str:
    """将 guid 中的不安全字符替换为 _，避免 Markdown URL 解析问题。"""
    return guid.replace(":", "_")


def _get_data_path(guid: str, subdir: str) -> str:
    """返回 data/{subdir}/{safe_guid}.json 或 data/{subdir}/{safe_guid}.mp3 的完整路径。"""
    ext = ".mp3" if subdir in ("episodes", "dubs") else ".json"
    return os.path.join(DATA_DIR, subdir, f"{_safe_filename(guid)}{ext}")


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

    cbrain_dir = load_config().get("storage", {}).get("cbrain_dir", "")
    if cbrain_dir:
        cbrain_podcasts = Path(os.path.expanduser(cbrain_dir))
    else:
        cbrain_podcasts = Path.home() / "cbrain" / "docs" / "fuyuans-kb" / "podcasts"

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

        md_path = Path(transcript_path).with_suffix(".md")
        if md_path.exists():
            cbrain_podcasts.mkdir(parents=True, exist_ok=True)
            dest = cbrain_podcasts / md_path.name
            shutil.copy2(md_path, dest)
            exported_to_cbrain = True
            mark_episode_exported(episode_id)
            _emit("exported", 1.0, f"已导出到 cbrain: {dest}")
            try:
                _update_podcasts_index(str(cbrain_podcasts))
            except Exception:
                pass

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

        # ── 生成中文翻译 Markdown ─────────────────
        zh_md_path = Path(translation_path).with_suffix(".zh.md")
        zh_md_content = _build_zh_md(translation, ep.title, ep.feed_title or "")
        zh_md_path.write_text(zh_md_content, encoding="utf-8")

        cbrain_podcasts.mkdir(parents=True, exist_ok=True)
        shutil.copy2(zh_md_path, cbrain_podcasts / zh_md_path.name)

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
            episode_id,
            "error",
            progress=0.0,
            error_message=str(e),
        )
        raise RuntimeError(f"流水线失败 (ep #{episode_id}): {e}")


# ── Podcasts Index ─────────────────────────────────────


def _format_duration(seconds: int) -> str:
    """Format seconds as '1h46min' or '5min'."""
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    if hours > 0:
        return f"{hours}h{minutes}min"
    return f"{minutes}min"


def _truncate_text(text: str, max_len: int) -> str:
    """Strip HTML tags and truncate to max_len characters."""
    clean = re.sub(r"<[^>]+>", "", text).strip()
    if len(clean) <= max_len:
        return clean
    return clean[:max_len] + "…"


def _extract_episode_meta(export_dir: str) -> dict[str, dict[str, Any]]:
    """Scan exported .md files and look up episode metadata from DB.

    Returns dict keyed by base filename (slug) → metadata dict.
    """
    export_path = Path(export_dir)
    md_files = [p for p in export_path.glob("*.md") if p.name != "index.md"]

    base_names: set[str] = set()
    for f in md_files:
        base_names.add(f.stem.removesuffix(".zh"))

    if not base_names:
        return {}

    conn = get_connection()
    rows = conn.execute(
        """SELECT e.guid, e.title, e.pub_date, e.duration_sec, e.description,
                  f.title AS feed_title
           FROM episodes e
           LEFT JOIN feeds f ON e.feed_id = f.id"""
    ).fetchall()

    meta: dict[str, dict[str, Any]] = {}
    for row in rows:
        slug = row["guid"].replace(":", "_")
        if slug in base_names:
            meta[slug] = {
                "title": row["title"],
                "pub_date": row["pub_date"],
                "duration_sec": row["duration_sec"],
                "description": row["description"],
                "feed_title": row["feed_title"],
            }

    return meta


def _extract_title_from_md(md_path: Path) -> str:
    """Extract title from .md file: YAML frontmatter → H1 → filename fallback."""
    text = md_path.read_text(encoding="utf-8")

    if text.startswith("---\n"):
        parts = text.split("---\n", 2)
        if len(parts) >= 3:
            try:
                fm = yaml.safe_load(parts[1])
                if isinstance(fm, dict) and fm.get("title"):
                    return str(fm["title"])
            except yaml.YAMLError:
                pass

    for line in text.split("\n"):
        if line.startswith("# "):
            return line[2:].strip()

    return md_path.stem


def _extract_description_from_md(md_path: Path) -> str | None:
    """Extract description from .md file YAML frontmatter."""
    text = md_path.read_text(encoding="utf-8")

    if text.startswith("---\n"):
        parts = text.split("---\n", 2)
        if len(parts) >= 3:
            try:
                fm = yaml.safe_load(parts[1])
                if isinstance(fm, dict) and fm.get("description"):
                    return str(fm["description"])
            except yaml.YAMLError:
                pass

    return None


def format_translation_md(
    translation: dict[str, Any],
    title: str = "",
) -> str:
    """将翻译结果格式化为中文 Markdown 文稿。"""
    segments = translation.get("segments", [])
    summary_zh = translation.get("summary_zh", "")
    key_points = translation.get("key_points", [])
    episode_title_zh = translation.get("episode_title_zh", "")

    display_title = episode_title_zh or title or "Untitled"

    lines: list[str] = []
    lines.append(f"# {display_title}")
    lines.append("")

    if summary_zh:
        lines.append("## 摘要")
        lines.append("")
        lines.append(summary_zh)
        lines.append("")

    if key_points:
        lines.append("## 关键要点")
        lines.append("")
        for kp in key_points:
            lines.append(f"- {kp}")
        lines.append("")

    if segments:
        lines.append("## 中文翻译稿")
        lines.append("")

        for seg in segments:
            start_str = _format_time(seg.get("start", 0.0))
            end_str = _format_time(seg.get("end", 0.0))
            speaker = seg.get("speaker_name", "") or seg.get("speaker", "")
            zh_text = seg.get("zh", "")

            if not zh_text:
                continue

            lines.append(f"**[{start_str} → {end_str}] {speaker}**")
            lines.append(zh_text)
            lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("*由 PodMate 自动翻译 (DeepSeek)*")

    return "\n".join(lines)


def _build_zh_md(
    translation: dict[str, Any],
    title: str,
    feed_title: str,
) -> str:
    """构建完整的 .zh.md 内容：YAML frontmatter + format_translation_md 输出。"""
    episode_title_zh = translation.get("episode_title_zh", "") or title
    summary_zh = translation.get("summary_zh", "")

    meta_lines = ["---"]
    meta_lines.append(f'title: "{episode_title_zh}"')
    if feed_title:
        meta_lines.append(f'source: "{feed_title}"')
    if summary_zh:
        meta_lines.append(f'description: "{summary_zh}"')
    meta_lines.append("---")
    meta_lines.append("")

    body = format_translation_md(translation, title=title)
    return "\n".join(meta_lines) + body


def _update_podcasts_index(export_dir: str) -> None:
    """扫描 export_dir 中的 .md 转写稿，重建 index.md。

    自动合并同一条目的多语言版本（xxx.md + xxx.zh.md → 同行展示）。
    从数据库查询剧集元数据（日期/时长/来源/简介）。
    只在实际内容变化时写入，避免不必要的 git 变动。
    """
    export_path = Path(export_dir)
    export_path.mkdir(parents=True, exist_ok=True)

    index_path = export_path / "index.md"
    md_files = sorted(p for p in export_path.glob("*.md") if p.name != "index.md")

    if not md_files:
        content = "# 🎙 播客转写稿\n\n暂无转写记录。\n"
    else:
        meta = _extract_episode_meta(export_dir)

        lines = [
            "# 🎙 播客转写稿",
            "",
            "| # | 标题 | 日期 | 时长 | 语言 | 来源 | 简介 |",
            "|---|------|------|------|------|------|------|",
        ]

        # group by base name (strip .zh before extension)
        groups: dict[str, list[Path]] = {}
        for f in md_files:
            stem = f.stem
            base = stem.removesuffix(".zh")
            groups.setdefault(base, []).append(f)

        for i, base in enumerate(sorted(groups), start=1):
            files = groups[base]
            zh_file = next((f for f in files if ".zh." in f.name or f.stem.endswith(".zh")), None)
            en_file = next((f for f in files if f is not zh_file), files[0])

            primary = zh_file or en_file
            title = _extract_title_from_md(primary)
            link_target = primary.name
            title_cell = f"[**{title}**]({link_target})"

            ep_meta = meta.get(base, {})
            pub_date = ep_meta.get("pub_date") or ""
            date_cell = pub_date[:10] if len(pub_date) >= 10 else (pub_date or "—")
            # Try parsing standard HTTP date format → ISO date
            try:
                from email.utils import parsedate_to_datetime

                parsed = parsedate_to_datetime(pub_date.split(" GMT")[0])
                date_cell = parsed.strftime("%Y-%m-%d")
            except Exception:
                pass

            duration_sec = ep_meta.get("duration_sec")
            duration_cell = _format_duration(duration_sec) if duration_sec else "—"

            badges = []
            if zh_file:
                badges.append(f"[🇨🇳 中文]({zh_file.name})")
            if en_file:
                badges.append(f"[🇬🇧 英文]({en_file.name})")
            lang_cell = " · ".join(badges) if badges else "—"

            feed_title = ep_meta.get("feed_title") or ""
            source_cell = feed_title if feed_title else "—"

            # 优先读 .zh.md frontmatter description → .md frontmatter → DB 回退
            desc_cell = "—"
            zh_file_path = zh_file
            if zh_file_path:
                zh_desc = _extract_description_from_md(zh_file_path)
                if zh_desc:
                    desc_cell = _truncate_text(zh_desc, 80)
            if desc_cell == "—" and en_file:
                en_desc = _extract_description_from_md(en_file)
                if en_desc:
                    desc_cell = _truncate_text(en_desc, 80)
            if desc_cell == "—":
                description = ep_meta.get("description") or ""
                desc_clean = description
                for prefix in (
                    "Brought to You By",
                    "This episode is",
                    "This podcast is",
                    "Check out",
                ):
                    if desc_clean.strip().startswith(prefix):
                        for sep in ("If you", "In today", "In this", "Today we", "We cover"):
                            idx = desc_clean.find(sep)
                            if idx >= 0:
                                desc_clean = desc_clean[idx:]
                                break
                        break
                desc_cell = _truncate_text(desc_clean, 80) if desc_clean else "—"

            lines.append(
                f"| {i} | {title_cell} | {date_cell} | {duration_cell} "
                f"| {lang_cell} | {source_cell} | {desc_cell} |"
            )

        content = "\n".join(lines) + "\n"

    if index_path.exists():
        existing = index_path.read_text(encoding="utf-8")
        if existing == content:
            return

    index_path.write_text(content, encoding="utf-8")
