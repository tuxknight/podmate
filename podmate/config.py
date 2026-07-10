"""PodMate configuration management — loads from ~/.config/podmate/config.toml."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

CONFIG_DIR = Path.home() / ".config" / "podmate"
CONFIG_PATH = CONFIG_DIR / "config.toml"

DEFAULT_CONFIG: dict[str, Any] = {
    "deepgram": {
        "api_key": "",
        "api_url": "https://api.deepgram.com/v1/listen",
        "model": "nova-2",
        "diarize": True,
    },
    "deepseek": {
        "api_key": "",
        "api_url": "https://api.deepseek.com/v1/chat/completions",
        "model": "deepseek-chat",
        "temperature": 0.3,
    },
    "dubbing": {
        "voice": "zh-CN-YunyangNeural",
        "rate": "+0%",
        "volume": "+0%",
    },
    "podcast_index": {
        "api_key": "",
        "api_secret": "",
    },
    "poll": {
        "interval_hours": 6,
    },
    "storage": {
        "data_dir": str(Path.home() / ".local" / "share" / "podmate"),
        "keep_episodes": 5,
        "cbrain_dir": str(Path.home() / "cbrain" / "docs" / "fuyuans-kb" / "podcasts"),
    },
}

_config: dict[str, Any] | None = None


def _merge(default: dict, override: dict) -> dict:
    """Recursively merge dictionaries, keeping defaults for keys not in override."""
    result = default.copy()
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _merge(result[k], v)
        else:
            result[k] = v
    return result


def load() -> dict[str, Any]:
    """Load configuration. Reads file on first call, returns cached on subsequent."""
    global _config
    if _config is not None:
        return _config

    cfg = DEFAULT_CONFIG.copy()

    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "rb") as f:
            user_cfg = tomllib.load(f)
        cfg = _merge(cfg, user_cfg)

    _config = cfg
    return _config


def get(section: str, key: str, default: Any = None) -> Any:
    """Get a config value, e.g. get('deepgram', 'api_key')."""
    cfg = load()
    return cfg.get(section, {}).get(key, default)


def init() -> bool:
    """Create default config file if it doesn't exist. Returns True if created."""
    if CONFIG_PATH.exists():
        return False

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _write(CONFIG_PATH, DEFAULT_CONFIG)
    global _config
    _config = DEFAULT_CONFIG.copy()
    return True


def set_key(section: str, key: str, value: str) -> None:
    """Set a config key and save. Used by podmate config set."""
    cfg = load()
    if section not in cfg:
        cfg[section] = {}
    cfg[section][key] = value
    global _config
    _config = cfg
    _write(CONFIG_PATH, cfg)


def _write(path: Path, cfg: dict) -> None:
    """Write config dict as TOML file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for section, values in cfg.items():
        lines.append(f"\n[{section}]")
        for k, v in values.items():
            if isinstance(v, bool):
                lines.append(f"{k} = {'true' if v else 'false'}")
            elif isinstance(v, (int, float)):
                lines.append(f"{k} = {v}")
            else:
                escaped = str(v).replace("\\", "\\\\").replace('"', '\\"')
                lines.append(f'{k} = "{escaped}"')
    path.write_text("\n".join(lines).lstrip("\n") + "\n")


def mask(value: str, visible: int = 4) -> str:
    """Mask a key showing first `visible` chars, rest as *."""
    if not value:
        return "(not set)"
    if len(value) <= visible + 4:
        return value[:visible] + "..." + value[-4:]
    return value[:visible] + "*" * (len(value) - visible - 4) + value[-4:]


def show() -> dict[str, Any]:
    """Return masked config for display."""
    cfg = load()
    masked: dict[str, Any] = {}
    for section, values in cfg.items():
        masked[section] = {}
        for k, v in values.items():
            if "key" in k.lower() or "token" in k.lower() or "secret" in k.lower():
                masked[section][k] = mask(str(v))
            else:
                masked[section][k] = v
    return masked
