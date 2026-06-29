"""PodMate 中文配音模块 — 基于 edge-tts。"""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
from typing import Any

import edge_tts

from .config import get as config_get


def _get_dub_voice() -> str:
    return config_get("dubbing", "voice", "zh-CN-YunyangNeural")


def _get_dub_rate() -> str:
    return config_get("dubbing", "rate", "+0%")


def _get_dub_volume() -> str:
    return config_get("dubbing", "volume", "+0%")

_MAX_CHUNK_CHARS = 3000  # edge-tts max chars per call

SPEAKER_VOICE_MAP = {
    "A": "zh-CN-YunxiNeural",      # 云希，年轻男声，适合主持/采访者
    "B": "zh-CN-YunyangNeural",     # 云扬，沉稳男声，适合被访者/专家
    "C": "zh-CN-XiaoxiaoNeural",    # 晓晓，温柔女声
    "D": "zh-CN-YunjianNeural",     # 云健，活力男声
}

DUB_VOICE = "多人声线模式 (A=云希 B=云扬 C=晓晓 D=云健)"

TONE_SSML_MAP = {
    "calm":     '<prosody rate="-10%" pitch="-5%">{text}</prosody>',
    "excited":  '<prosody rate="+10%" pitch="+10%">{text}</prosody>',
    "serious":  '<prosody rate="-5%" pitch="0%">{text}</prosody>',
    "casual":   '<prosody rate="+5%" pitch="+5%">{text}</prosody>',
    "default":  '<prosody rate="0%" pitch="0%">{text}</prosody>',
}


def get_voice_for_speaker(speaker: str) -> str:
    """Map speaker label to Edge TTS voice."""
    return SPEAKER_VOICE_MAP.get(speaker, "zh-CN-YunyangNeural")


def wrap_with_tone(text: str, tone: str = "default") -> str:
    """Wrap text with SSML prosody based on tone."""
    prosody = TONE_SSML_MAP.get(tone, TONE_SSML_MAP["default"])
    inner = prosody.format(text=text)
    return "<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis'>" + inner + "</speak>"


def _majority_tone(tones: list[str]) -> str:
    """Return the most common tone from a list."""
    from collections import Counter
    if not tones:
        return "default"
    return Counter(tones).most_common(1)[0][0]


# ── 核心函数 ────────────────────────────────────────


async def dub_translation(
    segments: list[dict[str, Any]],
    output_path: str,
    voice: str | None = None,
    rate: str | None = None,
    volume: str | None = None,
    episode_id: int | None = None,
    progress_callback=None,
) -> str:
    """将中文翻译稿转为配音音频。

    Args:
        segments: 翻译段列表，每段含 {"id", "zh", "start", "end", "speaker", "tone"}。
        output_path: 输出 .mp3 路径。
        voice: Edge TTS 语音名称（单声线模式使用）。
        rate: 语速调整（如 "+10%" "-10%"）。
        volume: 音量调整。
        episode_id: 可选，用于更新 DB 状态。
        progress_callback: 可选回调，参数为 (segments_done, total_segments)。

    Returns:
        配音文件路径。
    """
    from .db import update_episode_status

    if episode_id is not None:
        update_episode_status(episode_id, "dubbing", progress=0.0)

    speakers = set(s.get("speaker", "") for s in segments if s.get("speaker"))
    needs_multi_voice = len(speakers) >= 2

    if needs_multi_voice:
        # 按说话人交替批次：同说话人的连续段落合并为一批
        # 这样既保持对话交错节奏，又减少 edge-tts 调用次数
        temp_files = []
        try:
            batches = []
            current_spk = None
            current_texts = []
            current_tone = "default"

            for seg in segments:
                zh = seg.get("zh", "").strip()
                if not zh:
                    continue
                spk = seg.get("speaker", "")
                tone = seg.get("tone", "default")

                if spk != current_spk and current_texts:
                    batches.append((current_spk, current_texts, current_tone))
                    current_texts = []
                current_spk = spk
                current_texts.append(zh)
                if tone != "default":
                    current_tone = tone

            if current_texts:
                batches.append((current_spk, current_texts, current_tone))

            total = len(batches)
            for i, (spk, texts, tone) in enumerate(batches):
                spk_voice = get_voice_for_speaker(spk)
                combined = "。".join(texts)
                ssml_text = wrap_with_tone(combined, tone)

                fd, tmp = tempfile.mkstemp(suffix=f"_{i:04d}.mp3")
                os.close(fd)
                temp_files.append(tmp)

                # 逐段生成，每段之间加延迟避免限流
                for attempt in range(3):
                    try:
                        communicate = edge_tts.Communicate(ssml_text, spk_voice)
                        await communicate.save(tmp)
                        break
                    except Exception as e:
                        if attempt < 2:
                            await asyncio.sleep(5)
                            continue
                        raise

                if episode_id is not None:
                    progress = (i + 1) / total
                    update_episode_status(episode_id, "dubbing", progress=progress)

                # 每段之间加延迟，避免限流
                await asyncio.sleep(1.0)

            _concat_audio(temp_files, output_path)
        finally:
            for tmp in temp_files:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    else:
        # 单 speaker → 原逻辑
        zh_texts = [seg.get("zh", "") for seg in segments if seg.get("zh", "").strip()]
        full_text = "。".join(zh_texts)
        if not full_text.endswith(("。", "！", "？", "...")):
            full_text += "。"

        if not full_text.strip():
            raise ValueError("翻译文本为空，无法配音")

        if voice is None:
            voice = _get_dub_voice()
        if rate is None:
            rate = _get_dub_rate()
        if volume is None:
            volume = _get_dub_volume()

        _dub_text(full_text, output_path, voice, rate, volume)

    if episode_id is not None:
        update_episode_status(episode_id, "dubbed", progress=1.0)

    return output_path


# ── 内部函数 ────────────────────────────────────────


def _dub_text(
    text: str,
    output_path: str,
    voice: str | None = None,
    rate: str | None = None,
    volume: str | None = None,
) -> str:
    """将文本转为配音音频文件。

    如果文本长度超过 _MAX_CHUNK_CHARS，自动分段生成后用 ffmpeg 拼接。
    """
    voice = voice or _get_dub_voice()
    rate = rate or _get_dub_rate()
    volume = volume or _get_dub_volume()
    if len(text) <= _MAX_CHUNK_CHARS:
        # 单段直接生成
        asyncio.run(_generate_audio(text, output_path, voice, rate, volume))
        return output_path

    # 长文本分段生成
    chunks = _split_text(text, _MAX_CHUNK_CHARS)
    temp_files = []

    try:
        for i, chunk in enumerate(chunks):
            temp_fd, temp_path = tempfile.mkstemp(suffix=f".mp3")
            os.close(temp_fd)
            temp_files.append(temp_path)
            asyncio.run(_generate_audio(chunk, temp_path, voice, rate, volume))

        # 用 ffmpeg 拼接
        _concat_audio(temp_files, output_path)
    finally:
        # 清理临时文件
        for tmp in temp_files:
            try:
                os.remove(tmp)
            except OSError:
                pass

    return output_path


async def _generate_audio(
    text: str,
    output_path: str,
    voice: str,
    rate: str,
    volume: str,
) -> None:
    """调用 edge-tts 生成单段音频。"""
    communicate = edge_tts.Communicate(text, voice, rate=rate, volume=volume)
    await communicate.save(output_path)


def _split_text(text: str, max_chars: int = _MAX_CHUNK_CHARS) -> list[str]:
    """按字符数和句子边界拆分长文本。"""
    chunks: list[str] = []
    while len(text) > max_chars:
        # 在 max_chars 处向前寻找最近的句子结束符
        split_at = max_chars
        for sep in ("。", "！", "？", ".\n", "！\n", "？\n"):
            pos = text.rfind(sep, 0, max_chars)
            if pos > split_at * 0.7:  # 不要太靠前
                split_at = pos + len(sep)
                break

        chunks.append(text[:split_at].strip())
        text = text[split_at:].strip()

    if text.strip():
        chunks.append(text.strip())

    return chunks


def _concat_audio(input_files: list[str], output_path: str) -> None:
    """用 ffmpeg 拼接多个音频文件。"""
    # 创建 ffmpeg 输入列表文件
    fd, list_path = tempfile.mkstemp(suffix=".txt", text=True)
    try:
        with os.fdopen(fd, "w") as f:
            for inp in input_files:
                f.write(f"file '{inp}'\n")

        result = subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
             "-i", list_path, "-c", "copy", output_path],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"ffmpeg 拼接失败 (code={result.returncode}): "
                f"{result.stderr[:200]}"
            )
    finally:
        try:
            os.remove(list_path)
        except OSError:
            pass


async def list_voices(locale: str | None = None) -> list[dict[str, str]]:
    """列出可用的 Edge TTS 语音。"""
    voices = await edge_tts.list_voices()
    if locale:
        voices = [v for v in voices if v["Locale"].startswith(locale)]
    return voices
