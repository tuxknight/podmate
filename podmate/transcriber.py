"""PodMate 语音转写模块 — 支持本地 faster-whisper 和 Deepgram API。"""

from __future__ import annotations

import os
from typing import Any

# 在 RPi 上 HuggingFace Hub 下载模型经常 SSL 错误，使用国内镜像
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_SSL_VERIFY", "1")
os.environ.setdefault("CURL_CA_BUNDLE", "")

import httpx

from .config import get as config_get
from .db import update_episode_status


def _get_deepgram_api_key() -> str:
    return config_get("deepgram", "api_key", "")


def _get_deepgram_api_url() -> str:
    return config_get("deepgram", "api_url", "https://api.deepgram.com/v1/listen")


# ── 本地 faster-whisper（单例） ────────────────────────

_model: Any | None = None
_MODEL_SIZE = "base"


def get_model(model_size: str = _MODEL_SIZE) -> Any:
    """获取 faster-whisper 模型实例（单例，延迟加载）。

    模型文件会在首次调用时自动下载（使用国内镜像 hf-mirror.com）。
    """
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        _model = WhisperModel(model_size, device="cpu", compute_type="int8")
    return _model


def transcribe(audio_path: str) -> dict[str, Any]:
    """使用本地 faster-whisper 转写音频文件为文字。

    Args:
        audio_path: 音频文件路径（支持 .mp3, .wav, .m4a 等格式）。

    Returns:
        dict 包含:
            - text: 完整转写文本
            - segments: 分段列表 [{"id", "start", "end", "text"}, ...]
            - language: 检测到的语言代码（如 "en"）
            - duration_sec: 音频时长（秒）
    """
    if not os.path.isfile(audio_path):
        raise FileNotFoundError(f"音频文件不存在: {audio_path}")

    model = get_model(_MODEL_SIZE)
    segments, info = model.transcribe(audio_path, beam_size=5)

    result: dict[str, Any] = {
        "text": "",
        "segments": [],
        "language": info.language,
        "duration_sec": info.duration,
    }

    full_text_parts: list[str] = []
    for seg in segments:
        full_text_parts.append(seg.text)
        result["segments"].append({
            "id": seg.id,
            "start": seg.start,
            "end": seg.end,
            "text": seg.text.strip(),
        })

    result["text"] = " ".join(full_text_parts)
    return result


# ── Deepgram API 转写 ─────────────────────────────────


def _speaker_label(speaker_num: int) -> str:
    """将 Deepgram 返回的数字 speaker 转为字母标签 (0→A, 1→B, ...)。"""
    return chr(ord("A") + speaker_num)


async def transcribe_via_deepgram(
    audio_path: str,
    episode_id: int | None = None,
) -> dict[str, Any]:
    """使用 Deepgram API 转写音频文件，支持说话人分离（diarization）。

    Args:
        audio_path: 音频文件路径。
        episode_id: 可选，用于更新 DB 状态。

    Returns:
        dict 包含:
            - text: 完整转写文本
            - segments: 分段列表
                        每段: {"id", "start", "end", "text", "speaker"}
            - language: 语言代码
            - duration_sec: 音频时长（秒）
    """
    api_key = _get_deepgram_api_key()
    if not api_key:
        raise RuntimeError(
            "未设置 Deepgram API key。\n"
            "请运行: podmate config set deepgram.api_key 'your_key_here'"
        )

    if not os.path.isfile(audio_path):
        raise FileNotFoundError(f"音频文件不存在: {audio_path}")

    if episode_id is not None:
        update_episode_status(episode_id, "transcribing", progress=0.3)

    # 读取音频文件
    with open(audio_path, "rb") as f:
        audio_data = f.read()

    headers = {
        "Authorization": f"Token {api_key}",
    }

    params = {
        "model": "nova-2",
        "diarize": "true",
        "punctuate": "true",
        "smart_format": "true",
        "paragraphs": "true",
    }

    async with httpx.AsyncClient(timeout=600.0) as client:
        resp = await client.post(
            _get_deepgram_api_url(),
            headers=headers,
            params=params,
            content=audio_data,  # raw audio bytes
        )
        resp.raise_for_status()
        data = resp.json()

    result = _parse_deepgram_response(data)

    if episode_id is not None:
        update_episode_status(episode_id, "transcribing", progress=0.9)

    return result


def _parse_deepgram_response(data: dict[str, Any]) -> dict[str, Any]:
    """将 Deepgram API 返回解析为统一格式。

    Deepgram 返回结构 (nova-2 + diarize + paragraphs):
      results.channels[0].alternatives[0]:
        - transcript: 全文
        - paragraphs.transcript: 分段全文（含换行）
        - paragraphs.paragraphs:
          [{sentences: [{text, start, end, speaker}], speaker, num_words, start, end}]
        - words: [{word, start, end, speaker, ...}]
        - language
        - duration
    """
    channel = data.get("results", {}).get("channels", [{}])[0]
    alt = channel.get("alternatives", [{}])[0]

    full_text = alt.get("transcript", "")
    language = alt.get("language", "en")
    duration = alt.get("duration", 0)

    segments: list[dict[str, Any]] = []
    seg_id = 0

    paragraphs = alt.get("paragraphs", {}).get("paragraphs", [])

    if paragraphs:
        # 按 speaker 换段
        for para in paragraphs:
            speaker_num = para.get("speaker", 0)
            speaker_label = _speaker_label(speaker_num)

            sentences = para.get("sentences", [])
            if not sentences:
                continue

            # 合并同一段内的句子
            para_text = " ".join(s.get("text", "") for s in sentences if s.get("text"))
            para_start = sentences[0].get("start", 0)
            para_end = sentences[-1].get("end", 0)

            segments.append({
                "id": seg_id,
                "start": para_start,
                "end": para_end,
                "text": para_text.strip(),
                "speaker": speaker_label,
            })
            seg_id += 1
    else:
        # 如果没有分段，用 words 列表粗分
        words = alt.get("words", [])
        if words:
            # 按 speaker 变化分块
            current_speaker = None
            current_text: list[str] = []
            current_start = 0.0
            current_end = 0.0

            for word in words:
                speaker = _speaker_label(word.get("speaker", 0))
                if speaker != current_speaker and current_speaker is not None:
                    # speaker 切换 → 成段
                    segments.append({
                        "id": seg_id,
                        "start": current_start,
                        "end": current_end,
                        "text": " ".join(current_text).strip(),
                        "speaker": current_speaker,
                    })
                    seg_id += 1
                    current_text = []
                    current_start = 0.0

                if not current_text:
                    current_start = word.get("start", current_start)

                current_text.append(word.get("word", ""))
                current_end = word.get("end", word.get("start", current_end))
                current_speaker = speaker

            # 最后一段
            if current_text:
                segments.append({
                    "id": seg_id,
                    "start": current_start,
                    "end": current_end,
                    "text": " ".join(current_text).strip(),
                    "speaker": current_speaker,
                })

    return {
        "text": full_text.strip(),
        "segments": segments,
        "language": language,
        "duration_sec": duration,
    }


# ── 结构化文字稿格式化 ──────────────────────────────


def _format_time(seconds: float) -> str:
    """将秒数格式化为 HH:MM:SS。"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def format_transcript(result: dict[str, Any], title: str = "") -> str:
    """将转写结果格式化为 Markdown 文字稿。

    合并同一说话人的连续段落，生成带时间轴和说话人标签的可读文稿。
    """
    segments = result.get("segments", [])
    language = result.get("language", "?")
    duration_sec = result.get("duration_sec", 0)

    duration_min = round(duration_sec / 60)
    speakers = {s.get("speaker", "?") for s in segments}
    speaker_count = len(speakers)

    lines: list[str] = []
    display_title = title or "Untitled"

    lines.append(f"# {display_title}")
    lines.append("")
    lines.append(
        f"**语言:** {language} | **时长:** {duration_min} 分钟"
        f" | **说话人:** {speaker_count}"
    )
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 文字稿")
    lines.append("")

    if not segments:
        lines.append("*无转写内容*")
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append("*由 PodMate 自动转写 (Deepgram nova-2)*")
        return "\n".join(lines)

    # 合并同一说话人的连续段落
    merged: list[dict[str, Any]] = []
    for seg in segments:
        speaker = seg.get("speaker", "?")
        text = seg.get("text", "").strip()
        start = seg.get("start", 0.0)
        end = seg.get("end", 0.0)

        if merged and merged[-1]["speaker"] == speaker:
            merged[-1]["text"] += " " + text
            merged[-1]["end"] = end
        else:
            merged.append({
                "speaker": speaker,
                "text": text,
                "start": start,
                "end": end,
            })

    for seg in merged:
        start_str = _format_time(seg["start"])
        end_str = _format_time(seg["end"])
        speaker = seg["speaker"]
        text = seg["text"]

        lines.append(f"**[{start_str} → {end_str}] 说话人 {speaker}**")
        lines.append(text)
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("*由 PodMate 自动转写 (Deepgram nova-2)*")

    return "\n".join(lines)
