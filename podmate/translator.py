"""PodMate 翻译与摘要模块 — 多 provider LLM 调用（hermes / deepseek / openai）。"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from .prompts import PromptLoader
from .provider import ProviderConfig, ProviderResolver

_HERMES_ENV_LOADED = False


def _maybe_load_hermes_env() -> None:
    """Load API keys from ~/.hermes/.env if not already loaded.

    This ensures PodMate works even when environment variables aren't
    inherited from the parent shell (e.g., when run via Hermes terminal()).
    """
    global _HERMES_ENV_LOADED
    if _HERMES_ENV_LOADED:
        return

    env_path = Path.home() / ".hermes" / ".env"
    if not env_path.exists():
        _HERMES_ENV_LOADED = True
        return

    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip("'\"")
        if key and val:
            os.environ.setdefault(key, val)

    # Alias: Hermes uses the same API key as DeepSeek
    if "DEEPSEEK_API_KEY" in os.environ and "HERMES_API_KEY" not in os.environ:
        os.environ["HERMES_API_KEY"] = os.environ["DEEPSEEK_API_KEY"]

    _HERMES_ENV_LOADED = True


_BATCH_SIZE = 20  # 每批处理的段落数
_MAX_RETRIES = 3  # API 调用最大重试次数
RETRY_BASE_DELAY = 2
RETRY_MAX_DELAY = 60


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff with jitter: min(base * 2^n, max) * (0.5 + random)."""
    return min(RETRY_BASE_DELAY * (2**attempt), RETRY_MAX_DELAY) * (0.5 + random.random())


# ── 核心函数 ────────────────────────────────────────


async def translate_segments(
    segments: list[dict[str, Any]],
    batch_size: int = _BATCH_SIZE,
    episode_id: int | None = None,
    *,
    skip_summary: bool = False,
    progress_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """将英文转写段落翻译为中文，并生成摘要。

    Args:
        segments: 转写结果段落列表，每段含 {"id", "start", "end", "text"}。
        batch_size: 每批处理的段落数。
        episode_id: 可选，用于更新 DB 状态。
        skip_summary: 跳过摘要生成，返回空的 summary_zh/key_points。
        progress_callback: 可选进度回调，接收中文进度消息。

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

    if not segments:
        raise ValueError("转写段落为空，无法翻译")

    # 分批处理
    translated_segments: list[dict[str, Any]] = []
    total_batches = (len(segments) + batch_size - 1) // batch_size

    # 先分析整体风格、话题和说话人身份（用第一批作为样本）
    tone_analysis = ""
    speaker_mapping: dict[str, str] = {}
    first_batch = segments[: min(batch_size, len(segments))]
    first_text = "\n".join(
        f"[{s['id']}][Speaker {s.get('speaker', '?')}] {s['text']}" for s in first_batch
    )

    total_start = time.monotonic()

    if progress_callback:
        progress_callback("[Translation] 正在分析说话人风格...")

    analysis = PromptLoader.get("translator.analysis", text=first_text)
    analysis_result = await _call_llm(
        analysis["prompt"],
        system_role=analysis["system"],
        function_call="analysis",
        episode_id=episode_id,
        progress_callback=progress_callback,
    )

    analysis_text = analysis_result.get("content", "")
    tone_analysis = analysis_text

    # 提取说话人映射
    import re as _re

    map_match = _re.search(r"SPEAKER_MAP:\s*(.+)", analysis_text, _re.IGNORECASE)
    if map_match:
        map_part = map_match.group(1)
        for pair in map_part.split(","):
            pair = pair.strip()
            if "=" in pair:
                key, val = pair.split("=", 1)
                speaker_mapping[key.strip().upper()] = val.strip()

    # 逐批翻译
    for batch_idx in range(total_batches):
        start_idx = batch_idx * batch_size
        end_idx = min(start_idx + batch_size, len(segments))
        batch = segments[start_idx:end_idx]

        batch_text = "\n".join(f"[{s['id']}] {s['text']}" for s in batch)

        tmpl = PromptLoader.get(
            "translator.batch_translate",
            tone_analysis=tone_analysis,
            batch_num=str(batch_idx + 1),
            total_batches=str(total_batches),
            start_seg=str(start_idx + 1),
            end_seg=str(end_idx),
            batch_text=batch_text,
        )

        batch_msg = (
            f"[Translation] 第 {batch_idx + 1}/{total_batches} 批 "
            f"(段落 {start_idx + 1}-{end_idx})..."
        )
        if progress_callback:
            progress_callback(batch_msg)

        batch_start = time.monotonic()
        result = await _call_llm(
            tmpl["prompt"],
            system_role=tmpl["system"],
            function_call="batch_translate",
            episode_id=episode_id,
            progress_callback=progress_callback,
        )
        batch_elapsed = time.monotonic() - batch_start

        if progress_callback:
            progress_callback(f"{batch_msg} 完成 ({batch_elapsed:.1f}s)")

        # 解析返回的翻译结果
        content = result.get("content", "")
        for s in batch:
            seg_id = s["id"]
            zh_text, tone = _extract_translation(content, seg_id)
            raw_speaker = s.get("speaker", "")
            display_speaker = (
                speaker_mapping.get(raw_speaker.upper(), raw_speaker) if raw_speaker else ""
            )
            translated_segments.append(
                {
                    "id": seg_id,
                    "start": s.get("start", 0.0),
                    "end": s.get("end", 0.0),
                    "text": s.get("text", ""),
                    "speaker": raw_speaker,
                    "speaker_name": display_speaker,
                    "zh": zh_text,
                    "tone": tone,
                }
            )

        # 更新进度
        if episode_id is not None:
            progress = (batch_idx + 1) / total_batches
            update_episode_status(episode_id, "translating", progress=progress)

        # 避免 API 限流，稍作延迟
        if batch_idx < total_batches - 1:
            await asyncio.sleep(0.5)

    # 生成摘要
    if skip_summary:
        summary_data: dict[str, Any] = {
            "summary_zh": "",
            "key_points": [],
            "episode_title_zh": "",
        }
        if progress_callback:
            progress_callback("[Translation] 跳过摘要生成")
    else:
        if progress_callback:
            progress_callback("[Translation] 正在生成摘要...")
        summary_data = await _generate_summary_from_batches(
            translated_segments, tone_analysis, progress_callback=progress_callback
        )

    total_elapsed = time.monotonic() - total_start
    if progress_callback:
        progress_callback(f"[Translation] 翻译完成 (总耗时 {total_elapsed:.1f}s)")

    return {
        "summary_zh": summary_data.get("summary_zh", ""),
        "key_points": summary_data.get("key_points", []),
        "episode_title_zh": summary_data.get("episode_title_zh", ""),
        "speaker_notes": tone_analysis,
        "speaker_mapping": speaker_mapping,
        "segments": translated_segments,
        "duration_sec": segments[-1].get("end", 0) if segments else 0,
    }


async def generate_summary(
    translated_segments: list[dict[str, Any]],
    full_text: str | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """基于已翻译的段落生成摘要。

    Args:
        translated_segments: 翻译后的段落列表。
        full_text: 可选的完整翻译文本。
        progress_callback: 可选进度回调。

    Returns:
        dict 包含 summary_zh, key_points, episode_title_zh。
    """
    if full_text:
        sample = full_text[:5000]
    else:
        sample = "\n".join(s.get("zh", "") for s in translated_segments if s.get("zh"))[:5000]

    tmpl = PromptLoader.get("translator.summary", sample=sample)
    result = await _call_llm(
        tmpl["prompt"],
        system_role=tmpl["system"],
        function_call="summary",
        progress_callback=progress_callback,
    )

    content = result.get("content", "")
    return _parse_summary(content)


# ── API 调用 ────────────────────────────────────────


_DEFAULT_API_URLS: dict[str, str] = {
    "hermes": "https://api.hermes.ai/v1/chat/completions",
    "deepseek": "https://api.deepseek.com/v1/chat/completions",
    "openai": "https://api.openai.com/v1/chat/completions",
}

_DEFAULT_MODELS: dict[str, str] = {
    "hermes": "deepseek-chat",
    "deepseek": "deepseek-chat",
    "openai": "gpt-4o-mini",
}


async def _call_llm(
    user_prompt: str,
    system_role: str = "",
    temperature: float | None = None,
    *,
    function_call: str = "unknown",
    episode_id: int | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """通用 LLM API 调用，通过 ProviderResolver 路由到不同后端。

    自动在主 provider 和 fallback 列表间切换：
    - 认证错误（401/403）直接抛出，不降级
    - 限流（429）、超时、服务端错误（5xx）→ 试下一个 provider
    """
    _maybe_load_hermes_env()

    for cfg in ProviderResolver.resolve("translator"):
        try:
            return await _call_llm_with_config(
                user_prompt,
                system_role,
                temperature,
                cfg,
                function_call=function_call,
                episode_id=episode_id,
                progress_callback=progress_callback,
            )
        except (httpx.HTTPStatusError, httpx.TimeoutException) as e:
            if isinstance(e, httpx.HTTPStatusError) and e.response.status_code in (401, 403):
                raise
            continue

    raise RuntimeError("翻译失败：所有 provider 都不可用")


async def _call_llm_with_config(
    user_prompt: str,
    system_role: str,
    temperature: float | None,
    cfg: ProviderConfig,
    *,
    function_call: str = "unknown",
    episode_id: int | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Use a specific ProviderConfig to call the LLM API with retries."""
    provider = cfg.name
    api_key = cfg.api_key or os.environ.get(f"{provider.upper()}_API_KEY", "")
    api_url = (
        cfg.api_url
        or os.environ.get(f"{provider.upper()}_BASE_URL", "")
        or _DEFAULT_API_URLS.get(provider, "")
    )
    model = cfg.model or _DEFAULT_MODELS.get(provider, "")
    if temperature is None:
        temperature = float(cfg.extra.get("temperature", 0.3))
    timeout = float(cfg.extra.get("timeout", 300.0))

    if not api_key:
        raise RuntimeError(
            f"未设置 {provider} API key。\n"
            f"请运行: podmate config set {provider}.api_key 'your_key_here'"
        )
    if not api_url:
        raise RuntimeError(f"未设置 {provider} API URL。")

    messages: list[dict[str, str]] = []
    if system_role:
        messages.append({"role": "system", "content": system_role})
    messages.append({"role": "user", "content": user_prompt})

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": 4096,
    }

    last_error: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    api_url,
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
            if e.response.status_code in (400, 401, 403):
                raise RuntimeError(
                    f"{provider} API 认证失败 (status={e.response.status_code}): "
                    f"{e.response.text[:200]}"
                )
            # 429, 5xx: retryable
        except (httpx.RequestError, httpx.TimeoutException) as e:
            last_error = e
        except (KeyError, json.JSONDecodeError) as e:
            last_error = e

        if attempt < _MAX_RETRIES - 1:
            wait = _backoff_delay(attempt)
            if progress_callback:
                progress_callback(f"[Translation] API 调用失败，第 {attempt + 1} 次重试...")
            await asyncio.sleep(wait)
            continue

    raise RuntimeError(f"{provider} API 调用失败（已重试 {_MAX_RETRIES} 次）: {last_error}")


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
            text = line[len(prefix) :].strip()

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

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # 标题
        if line.startswith("标题:"):
            result["episode_title_zh"] = line[len("标题:") :].strip()
        elif line.startswith(
            "标题：",
        ):
            result["episode_title_zh"] = line[len("标题：") :].strip()

        # 摘要
        elif line.startswith("摘要:"):
            result["summary_zh"] = line[len("摘要:") :].strip()
        elif line.startswith("摘要："):
            result["summary_zh"] = line[len("摘要：") :].strip()

        # 要点
        elif line.startswith("- "):
            result["key_points"].append(line[2:].strip())

    # 如果标题没解析到，留空
    return result


async def _generate_summary_from_batches(
    translated_segments: list[dict[str, Any]],
    tone_analysis: str,
    progress_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """基于所有翻译段落生成摘要。"""
    sample_text = "\n".join(s.get("zh", "") for s in translated_segments if s.get("zh"))

    # 取前中后各一部分作为摘要依据
    total = len(translated_segments)
    if total > 60:
        parts = [
            translated_segments[:20],
            translated_segments[total // 2 - 10 : total // 2 + 10],
            translated_segments[-20:],
        ]
        sample_parts = []
        for p in parts:
            sample_parts.extend(p)
        sample_text = "\n".join(s.get("zh", "") for s in sample_parts if s.get("zh"))

    if len(sample_text) > 6000:
        sample_text = sample_text[:6000]

    tmpl = PromptLoader.get(
        "translator.summary_from_batches",
        tone_analysis=tone_analysis,
        sample=sample_text,
    )
    result = await _call_llm(
        tmpl["prompt"],
        system_role=tmpl["system"],
        function_call="summary",
        progress_callback=progress_callback,
    )

    return _parse_summary(result.get("content", ""))
