"""PodMate 音频播放模块 — 自动检测系统播放器。"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Literal

# ── 播放器优先级 ─────────────────────────────────────

PLAYER_PRIORITY = ["mpv", "mplayer", "ffplay", "aplay"]

# 检测到的播放器缓存
_available_player: str | None = None


def get_available_player() -> str | None:
    """检测可用的系统播放器。"""
    global _available_player
    if _available_player is not None:
        return _available_player

    for player in PLAYER_PRIORITY:
        if shutil.which(player):
            _available_player = player
            return player
    return None


def play_file(
    file_path: str,
    player: str | None = None,
    background: bool = False,
) -> subprocess.Popen | None:
    """播放音频文件。

    Args:
        file_path: 音频文件路径。
        player: 指定播放器，为 None 则自动检测。
        background: 是否后台播放（不阻塞终端）。

    Returns:
        如果 background=True 返回 Popen 对象，否则返回 None。

    Raises:
        FileNotFoundError: 如果音频文件不存在。
        RuntimeError: 如果找不到播放器。
    """
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"音频文件不存在: {file_path}")

    if player is None:
        player = get_available_player()

    if player is None:
        raise RuntimeError("未找到可用的播放器。请安装 mpv: sudo apt install mpv")

    cmd = _build_player_command(player, file_path)

    if background:
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.run(cmd, check=True)
        return None


def play_episode(
    file_path: str,
    mode: Literal["original", "dub"] = "original",
    start_sec: int = 0,
    background: bool = False,
) -> subprocess.Popen | None:
    """播放剧集音频，支持从指定秒数开始播放。

    Args:
        file_path: 音频文件路径。
        mode: 播放模式（仅用于提示标签）。
        start_sec: 从第几秒开始播放。
        background: 是否后台播放。

    Returns:
        如果 background=True 返回 Popen 对象。
    """
    player = get_available_player()
    if player is None:
        raise RuntimeError("未找到可用的播放器。请安装 mpv: sudo apt install mpv")

    cmd = _build_player_command(player, file_path, start_sec=start_sec)

    if background:
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.run(cmd, check=True)
        return None


# ── 内部函数 ────────────────────────────────────────


def _build_player_command(player: str, file_path: str, start_sec: int = 0) -> list[str]:
    """根据播放器构建命令。"""
    if player == "mpv":
        cmd = ["mpv", "--no-terminal", "--quiet"]
        if start_sec > 0:
            cmd.extend(["--start", str(start_sec)])
        cmd.append(file_path)
        return cmd
    elif player == "mplayer":
        cmd = ["mplayer", "-really-quiet", "-noautosub"]
        if start_sec > 0:
            cmd.extend(["-ss", str(start_sec)])
        cmd.append(file_path)
        return cmd
    elif player == "ffplay":
        cmd = ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"]
        if start_sec > 0:
            cmd.extend(["-ss", str(start_sec)])
        cmd.append(file_path)
        return cmd
    elif player == "aplay":
        # aplay 不支持跳转
        return ["aplay", "-q", file_path]
    else:
        return [player, file_path]
