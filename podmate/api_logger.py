"""LLM API call backlog logging. Zero-dependency, append-only JSONL."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path


def get_log_path() -> Path:
    base = os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))
    log_dir = Path(base) / "podmate" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "api_log.jsonl"


def log_api_call(
    provider: str,
    model: str,
    function_call: str,
    duration_sec: float,
    tokens_input: int = 0,
    tokens_output: int = 0,
    success: bool = True,
    error_message: str | None = None,
    episode_id: int | None = None,
) -> None:
    entry = {
        "timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "provider": provider,
        "model": model,
        "function": function_call,
        "duration_sec": round(duration_sec, 3),
        "tokens_input": tokens_input,
        "tokens_output": tokens_output,
        "success": success,
        "error": error_message,
        "episode_id": episode_id,
    }
    with open(get_log_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_logs(episode_id: int | None = None, limit: int = 20, offset: int = 0) -> list[dict]:
    log_path = get_log_path()
    if not log_path.exists():
        return []

    entries: list[dict] = []
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if episode_id is not None and entry.get("episode_id") != episode_id:
                continue
            entries.append(entry)

    entries.reverse()
    return entries[offset : offset + limit]


def get_stats(episode_id: int | None = None) -> dict:
    log_path = get_log_path()
    if not log_path.exists():
        return {
            "total_calls": 0,
            "success_rate": 0.0,
            "avg_duration_sec": 0.0,
            "total_tokens_input": 0,
            "total_tokens_output": 0,
            "failed_calls": [],
        }

    calls: list[dict] = []
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if episode_id is not None and entry.get("episode_id") != episode_id:
                continue
            calls.append(entry)

    total = len(calls)
    if total == 0:
        return {
            "total_calls": 0,
            "success_rate": 0.0,
            "avg_duration_sec": 0.0,
            "total_tokens_input": 0,
            "total_tokens_output": 0,
            "failed_calls": [],
        }

    successful = sum(1 for c in calls if c.get("success", True))
    total_duration = sum(c.get("duration_sec", 0) for c in calls)
    total_input = sum(c.get("tokens_input", 0) for c in calls)
    total_output = sum(c.get("tokens_output", 0) for c in calls)
    failed = [
        {
            "timestamp": c.get("timestamp", ""),
            "function": c.get("function", ""),
            "error": c.get("error", ""),
            "episode_id": c.get("episode_id"),
        }
        for c in calls
        if not c.get("success", True)
    ]

    return {
        "total_calls": total,
        "success_rate": round(successful / total * 100, 1),
        "avg_duration_sec": round(total_duration / total, 3),
        "total_tokens_input": total_input,
        "total_tokens_output": total_output,
        "failed_calls": failed,
    }
