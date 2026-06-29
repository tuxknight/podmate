"""PodMate 翻译与摘要模块 — 基于 DeepSeek Chat API。"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import httpx

from .config import get as config_get


def _get_api_key() -> str:
    return config_get("deepseek", "api_key", "")


def _get_api_url() -> str:
    return config_get("deepseek", "api_url", "https://api.deepseek.com/v1/chat/completions")

_BATCH_SIZE = 20  # 每批处理的段落数
_MAX_RETRIES = 3  # API 调用最大重试次数
_RETRY_DELAY = 2  # 重试等待秒数


# ── 核心函数 ────────────────────────────────────────


async def translate_segments(
    segments: list[dict[str, Any]],
    batch_size: int = _BATCH_SIZE,
    episode_id: int | None = None,
) -> dict[str, Any]:
    """将英文转写段落翻译为中文，并生成摘要。

    Args:
        segments: 转写结果段落列表，每段含 {"id", "start", "end", "text"}。
        batch_size: 每批处理的段落数。
        episode_id: 可选，用于更新 DB 状态。

    Returns:
        dict 包含:
            - summary_zh: 中文摘要（200 字以内）
            - segments: [{id, start, end, text (en), zh (translated)}], ...]
            - speaker_notes: 语气/说话风格分析
            - duration_sec: 音频时长
    """
    from .db import update_episode_status

    if episode_id is not None:
        update_episode_status(episode_id, "translating", progress=0.0)

    api_key = _get_api_key()
    if not api_key:
        raise RuntimeError(
            "未设置 DeepSeek API key。\n"
            "请运行: podmate config set deepseek.api_key 'your_key_here'"
        )

    if not segments:
        raise ValueError("转写段落为空，无法翻译")

    # 分批处理
    translated_segments: list[dict[str, Any]] = []
    total_batches = (len(segments) + batch_size - 1) // batch_size

    # 先分析整体风格和话题（用第一批作为样本）
    tone_analysis = ""
    first_batch = segments[:min(batch_size, len(segments))]
    first_text = "\n".join(
        f"[{s['id']}] {s['text']}" for s in first_batch
    )

    analysis_prompt = (
        "As a professional podcast analyst, analyze the following transcript excerpt.\n"
        "Identify:\n"
        "1. The main topic(s) being discussed\n"
        "2. The speaker's tone and speaking style (e.g., enthusiastic, academic, conversational, dramatic)\n"
        "3. Any technical terminology or jargon domains\n\n"
        f"Transcript:\n{first_text}\n\n"
        "Return your analysis concisely in Chinese, 100 characters max."
    )

    analysis_result = await _call_deepseek(analysis_prompt, system_role="你是一个播客分析专家。用中文简洁回复。")
    tone_analysis = analysis_result.get("content", "")

    # 逐批翻译
    for batch_idx in range(total_batches):
        start_idx = batch_idx * batch_size
        end_idx = min(start_idx + batch_size, len(segments))
        batch = segments[start_idx:end_idx]

        batch_text = "\n".join(
            f"[{s['id']}] {s['text']}" for s in batch
        )

        # 带上 tone_analysis 保持风格一致
        system_msg = (
            "你是一个专业的科技播客中英翻译专家。\n"
            f"说话风格: {tone_analysis}\n\n"
            "规则：\n"
            "1. 将英文翻译为自然、地道的中文口语\n"
            "2. 保持技术术语的准确性\n"
            "3. 保持说话人的语气和表达风格\n"
            "4. 如果原文有口癖、重复、犹豫（um, uh, like），适当省略\n"
            "5. 长句拆为短句，更符合中文口语习惯\n"
            "6. 输出格式: [段ID] 翻译文本 | tone: calm/excited/serious/casual"
        )

        user_prompt = (
            f"将以下英文播客片段翻译成中文。\n\n"
            f"这是第 {batch_idx + 1}/{total_batches} 批（段 {start_idx + 1}-{end_idx}）。\n\n"
            f"{batch_text}"
        )

        result = await _call_deepseek(user_prompt, system_role=system_msg)

        # 解析返回的翻译结果
        content = result.get("content", "")
        for s in batch:
            seg_id = s["id"]
            zh_text, tone = _extract_translation(content, seg_id)
            translated_segments.append({
                "id": seg_id,
                "start": s.get("start", 0.0),
                "end": s.get("end", 0.0),
                "text": s.get("text", ""),
                "speaker": s.get("speaker", ""),
                "zh": zh_text,
                "tone": tone,
            })

        # 更新进度
        if episode_id is not None:
            progress = (batch_idx + 1) / total_batches
            update_episode_status(episode_id, "translating", progress=progress)

        # 避免 API 限流，稍作延迟
        if batch_idx < total_batches - 1:
            await asyncio.sleep(0.5)

    # 生成摘要
    summary_data = await _generate_summary_from_batches(
        translated_segments, tone_analysis
    )

    return {
        "summary_zh": summary_data.get("summary_zh", ""),
        "key_points": summary_data.get("key_points", []),
        "episode_title_zh": summary_data.get("episode_title_zh", ""),
        "speaker_notes": tone_analysis,
        "segments": translated_segments,
        "duration_sec": segments[-1].get("end", 0) if segments else 0,
    }


async def generate_summary(
    translated_segments: list[dict[str, Any]], full_text: str | None = None
) -> dict[str, Any]:
    """基于已翻译的段落生成摘要。

    Args:
        translated_segments: 翻译后的段落列表。
        full_text: 可选的完整翻译文本。

    Returns:
        dict 包含 summary_zh, key_points, episode_title_zh。
    """
    if full_text:
        sample = full_text[:5000]
    else:
        sample = "\n".join(
            s.get("zh", "") for s in translated_segments if s.get("zh")
        )[:5000]

    prompt = (
        "你是一个播客内容分析专家。阅读以下中文翻译稿，生成：\n\n"
        "1. 中文摘要（200字以内）\n"
        "2. 3-5个关键话题点\n"
        "3. 一个吸引人的中文节目标题\n\n"
        f"原文：\n{sample}\n\n"
        "输出格式：\n"
        "标题: <中文标题>\n"
        "摘要: <摘要>\n"
        "要点:\n"
        "- 要点1\n"
        "- 要点2\n"
        "- 要点3\n"
    )

    result = await _call_deepseek(
        prompt,
        system_role="你是一个播客内容分析专家。用中文回复。"
    )

    content = result.get("content", "")
    return _parse_summary(content)


# ── API 调用 ────────────────────────────────────────


async def _call_deepseek(
    user_prompt: str,
    system_role: str = "",
    temperature: float | None = None,
) -> dict[str, Any]:
    """调用 DeepSeek Chat API。

    Args:
        user_prompt: 用户提示。
        system_role: 系统角色设定。
        temperature: 生成温度（翻译用低温度，0.3 保持准确）。

    Returns:
        dict 含 {"content": "...", "model": "...", "usage": {...}}。
    """
    messages = []
    if system_role:
        messages.append({"role": "system", "content": system_role})
    messages.append({"role": "user", "content": user_prompt})

    headers = {
        "Authorization": f"Bearer {_get_api_key()}",
        "Content-Type": "application/json",
    }

    if temperature is None:
        temperature = config_get("deepseek", "temperature", 0.3)

    payload = {
        "model": "deepseek-chat",
        "messages": messages,
        "temperature": temperature,
        "max_tokens": 4096,
    }

    last_error: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    _get_api_url(),
                    headers=headers,
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()

            choice = data["choices"][0]
            return {
                "content": choice["message"]["content"],
                "model": data.get("model", ""),
                "usage": data.get("usage", {}),
            }

        except httpx.HTTPStatusError as e:
            last_error = e
            if e.response.status_code == 429:
                # 限流，重试
                wait = _RETRY_DELAY * (2 ** attempt)
                await asyncio.sleep(wait)
                continue
            elif e.response.status_code in (400, 401, 403):
                # 认证错误，不重试
                raise RuntimeError(
                    f"DeepSeek API 认证失败 (status={e.response.status_code}): "
                    f"{e.response.text[:200]}"
                )
            else:
                if attempt < _MAX_RETRIES - 1:
                    await asyncio.sleep(_RETRY_DELAY)
                    continue
        except (httpx.RequestError, httpx.TimeoutException) as e:
            last_error = e
            if attempt < _MAX_RETRIES - 1:
                await asyncio.sleep(_RETRY_DELAY * (2 ** attempt))
                continue
        except (KeyError, json.JSONDecodeError) as e:
            last_error = e
            if attempt < _MAX_RETRIES - 1:
                await asyncio.sleep(_RETRY_DELAY)
                continue

    raise RuntimeError(
        f"DeepSeek API 调用失败（已重试 {_MAX_RETRIES} 次）: {last_error}"
    )


# ── 辅助函数 ────────────────────────────────────────


def _extract_translation(content: str, seg_id: int) -> tuple[str, str]:
    """从 API 返回中提取指定段落的翻译和语气。

    Returns:
        (zh_text, tone) — tone is "default" if not specified.
    """
    lines = content.strip().split("\n")
    for line in lines:
        line = line.strip()
        # 匹配多种格式: [5] 翻译文本 | tone: calm
        prefix = None
        text = None
        if line.startswith(f"[{seg_id}] "):
            prefix = f"[{seg_id}] "
        elif line.startswith(f"[{seg_id}]"):
            prefix = f"[{seg_id}]"
        elif line.startswith(f"{seg_id}."):
            prefix = f"{seg_id}."
        elif line.startswith(f"{seg_id}:") or line.startswith(f"{seg_id}："):
            prefix = f"{seg_id}:"

        if prefix is not None:
            text = line[len(prefix):].strip()

        if text is not None:
            tone = "default"
            if "| tone:" in text:
                parts = text.rsplit("| tone:", 1)
                text = parts[0].strip()
                tone = parts[1].strip().lower()
                if tone not in ("calm", "excited", "serious", "casual"):
                    tone = "default"
            return (text, tone)

    return ("", "default")


def _parse_summary(content: str) -> dict[str, Any]:
    """解析摘要生成的返回结果。"""
    result: dict[str, Any] = {
        "summary_zh": "",
        "key_points": [],
        "episode_title_zh": "",
    }

    lines = content.strip().split("\n")
    current_section = None

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # 标题
        if line.startswith("标题:"):
            result["episode_title_zh"] = line[len("标题:"):].strip()
        elif line.startswith("标题：", ):
            result["episode_title_zh"] = line[len("标题："):].strip()

        # 摘要
        elif line.startswith("摘要:"):
            result["summary_zh"] = line[len("摘要:"):].strip()
        elif line.startswith("摘要："):
            result["summary_zh"] = line[len("摘要："):].strip()

        # 要点
        elif line.startswith("- "):
            result["key_points"].append(line[2:].strip())

    # 如果标题没解析到，留空
    return result


async def _generate_summary_from_batches(
    translated_segments: list[dict[str, Any]],
    tone_analysis: str,
) -> dict[str, Any]:
    """基于所有翻译段落生成摘要。"""
    sample_text = "\n".join(
        s.get("zh", "") for s in translated_segments if s.get("zh")
    )

    # 取前中后各一部分作为摘要依据
    total = len(translated_segments)
    if total > 60:
        parts = [
            translated_segments[:20],
            translated_segments[total // 2 - 10:total // 2 + 10],
            translated_segments[-20:],
        ]
        sample_parts = []
        for p in parts:
            sample_parts.extend(p)
        sample_text = "\n".join(
            s.get("zh", "") for s in sample_parts if s.get("zh")
        )

    if len(sample_text) > 6000:
        sample_text = sample_text[:6000]

    prompt = (
        "你是一个播客内容摘要专家。根据以下中文翻译稿，生成：\n\n"
        "1. 中文摘要（150-200字，覆盖核心理念和讨论要点）\n"
        "2. 3-5个关键话题点\n\n"
        f"说话风格: {tone_analysis}\n\n"
        f"原文：\n{sample_text}\n\n"
        "输出格式：\n"
        "摘要: <摘要>\n"
        "要点:\n"
        "- 要点1\n"
        "- 要点2\n"
    )

    result = await _call_deepseek(
        prompt,
        system_role="你是一个播客分析专家。用中文回复，简洁有力。"
    )

    return _parse_summary(result.get("content", ""))
