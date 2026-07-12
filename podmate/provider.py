"""Centralized provider resolution — unified config for transcribe/translate/dub."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import get as config_get
from .config import load as load_config

_DEFAULT_PROVIDERS: dict[str, str] = {
    "transcriber": "deepgram",
    "translator": "hermes",
    "dubbing": "edge-tts",
}


@dataclass
class ProviderConfig:
    """Standardized provider configuration."""

    name: str
    api_key: str = ""
    api_url: str = ""
    model: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


class ProviderResolver:
    """Central provider resolver. All modules get config through this."""

    @staticmethod
    def get_capability(capability: str) -> str:
        """Return the configured provider name for a capability."""
        return config_get(capability, "provider", _DEFAULT_PROVIDERS.get(capability, ""))

    @staticmethod
    def resolve(capability: str) -> list[ProviderConfig]:
        """Return ordered provider config list (primary + fallbacks).

        Caller loops through, tries each, breaks on success.
        Auth errors (401/403) do not fallback — re-raise immediately.
        """
        prov = ProviderResolver.get_capability(capability)
        fallback_names = config_get(capability, "fallback", [])
        all_names = [prov] + list(fallback_names)
        return [ProviderResolver._build_config(capability, n) for n in all_names]

    @staticmethod
    def get_config(capability: str, provider: str | None = None) -> ProviderConfig:
        """Get config for a specific provider under a capability."""
        prov = provider or ProviderResolver.get_capability(capability)
        return ProviderResolver._build_config(capability, prov)

    @staticmethod
    def _build_config(capability: str, provider: str) -> ProviderConfig:
        section = f"{capability}.{provider}"
        cfg = load_config()

        # New hierarchical config first, old flat config as fallback
        new_section = cfg.get(section, {})
        old_section = cfg.get(provider, {})

        api_key = new_section.get("api_key", "") or old_section.get("api_key", "")
        api_url = new_section.get("api_url", "") or old_section.get("api_url", "")
        model = new_section.get("model", "") or old_section.get("model", "")

        # Merge extra keys (new overrides old for non-standard keys)
        extra = dict(old_section)
        extra.update(new_section)
        for k in ("api_key", "api_url", "model"):
            extra.pop(k, None)

        return ProviderConfig(
            name=provider,
            api_key=api_key,
            api_url=api_url,
            model=model,
            extra=extra,
        )
