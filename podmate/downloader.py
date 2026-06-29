"""PodMate MP3 下载模块 — 通过 httpx 流式下载。"""

from __future__ import annotations

import httpx

from .db import update_episode_status


async def download_episode(
    audio_url: str,
    dest_path: str,
    episode_id: int | None = None,
    progress_callback=None,
) -> str:
    """流式下载 MP3 音频文件。

    Args:
        audio_url: 音频文件的 URL。
        dest_path: 本地保存路径。
        episode_id: 可选，用于更新 DB 中状态。
        progress_callback: 可选回调，参数为 (bytes_downloaded, total_bytes)。

    Returns:
        下载成功后返回 dest_path。

    Raises:
        httpx.HTTPStatusError: 如果服务器返回错误状态码。
        httpx.RequestError: 如果网络请求失败。
    """
    if episode_id is not None:
        update_episode_status(episode_id, "downloading", progress=0.0)

    async with httpx.AsyncClient(timeout=300.0, follow_redirects=True) as client:
        async with client.stream("GET", audio_url) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("content-length", 0))

            with open(dest_path, "wb") as f:
                async for chunk in resp.aiter_bytes():
                    f.write(chunk)
                    if progress_callback:
                        progress_callback(f.tell(), total)

    if episode_id is not None:
        update_episode_status(episode_id, "downloaded", progress=1.0)

    return dest_path
