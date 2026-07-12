"""Prompt template loader — reads from ~/.config/podmate/prompts.toml."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from .config import CONFIG_DIR


class PromptLoader:
    """Load prompt templates from prompts.toml with {variable} formatting."""

    _prompts: dict[str, Any] | None = None

    @classmethod
    def get(cls, key: str, **kwargs: object) -> dict[str, str]:
        """Get a prompt template by key, with optional variable substitution.

        Returns:
            {"system": "...", "prompt": "..."}
        """
        if cls._prompts is None:
            cls._load()

        template = cls._prompts.get(key, {})
        if not template:
            return {"system": "", "prompt": ""}

        system = template.get("system", "")
        prompt = template.get("prompt", "")
        if kwargs:
            system = system.format(**{k: str(v) for k, v in kwargs.items()})
            prompt = prompt.format(**{k: str(v) for k, v in kwargs.items()})
        return {"system": system, "prompt": prompt}

    @classmethod
    def _load(cls) -> None:
        prompts_path = CONFIG_DIR / "prompts.toml"
        if prompts_path.exists():
            with open(prompts_path, "rb") as f:
                cls._prompts = tomllib.load(f)
        else:
            cls._prompts = cls._get_defaults()
            cls._ensure_defaults(prompts_path)

    @classmethod
    def _ensure_defaults(cls, path: Path) -> None:
        """Write default prompts.toml so user can customize."""
        path.parent.mkdir(parents=True, exist_ok=True)
        defaults = cls._get_defaults()
        lines: list[str] = []
        for section, values in defaults.items():
            lines.append(f"\n[{section}]")
            for k, v in values.items():
                escaped = str(v).replace("\\", "\\\\").replace('"', '\\"')
                escaped = escaped.replace("\n", "\\n")
                lines.append(f'{k} = "{escaped}"')
        content = "\n".join(lines).lstrip("\n") + "\n"
        content_lines: list[str] = []
        for raw_line in content.split("\n"):
            if " = \"" in raw_line and raw_line.strip().endswith('"'):
                key_part, val_part = raw_line.split(" = ", 1)
                val_part = val_part.strip('"')
                val_part = val_part.replace("\\n", "\n")
                raw_line = f'{key_part} = """\n{val_part}\n"""'
            content_lines.append(raw_line)
        path.write_text("\n".join(content_lines))

    @staticmethod
    def _get_defaults() -> dict[str, Any]:
        return {
            "translator.analysis": {
                "system": "你是一个播客分析专家。用中文简洁回复。",
                "prompt": (
                    "As a professional podcast analyst, analyze the following transcript excerpt.\n"
                    "Identify:\n"
                    "1. The main topic(s) being discussed\n"
                    "2. The speaker's tone and speaking style"
                    " (e.g., enthusiastic, academic, conversational, dramatic)\n"
                    "3. Any technical terminology or jargon domains\n"
                    "4. Try to identify the real names of each speaker (e.g., DHH, host name)"
                    " based on contextual clues in the conversation.\n\n"
                    "Transcript:\n"
                    "{text}\n\n"
                    "Return your analysis concisely in Chinese, "
                    "100 characters max for tone/style.\n"
                    "Then return speaker mapping in format:\n"
                    "SPEAKER_MAP: A=Real Name, B=Real Name, ..."
                ),
            },
            "translator.batch_translate": {
                "system": (
                    "你是一个专业的科技播客中英翻译专家。\n"
                    "说话风格: {tone_analysis}\n\n"
                    "规则：\n"
                    "1. 将英文翻译为自然、地道的中文口语\n"
                    "2. 保持技术术语的准确性\n"
                    "3. 保持说话人的语气和表达风格\n"
                    "4. 如果原文有口癖、重复、犹豫（um, uh, like），适当省略\n"
                    "5. 长句拆为短句，更符合中文口语习惯\n"
                    "6. 输出格式: [段ID] 翻译文本 | tone: calm/excited/serious/casual"
                ),
                "prompt": (
                    "将以下英文播客片段翻译成中文。\n\n"
                    "这是第 {batch_num}/{total_batches} 批（段 {start_seg}-{end_seg}）。\n\n"
                    "{batch_text}"
                ),
            },
            "translator.summary": {
                "system": "你是一个播客内容分析专家。用中文回复。",
                "prompt": (
                    "你是一个播客内容分析专家。阅读以下中文翻译稿，生成：\n\n"
                    "1. 中文摘要（200字以内）\n"
                    "2. 3-5个关键话题点\n"
                    "3. 一个吸引人的中文节目标题\n\n"
                    "原文：\n"
                    "{sample}\n\n"
                    "输出格式：\n"
                    "标题: <中文标题>\n"
                    "摘要: <摘要>\n"
                    "要点:\n"
                    "- 要点1\n"
                    "- 要点2\n"
                    "- 要点3\n"
                ),
            },
            "translator.summary_from_batches": {
                "system": "你是一个播客分析专家。用中文回复，简洁有力。",
                "prompt": (
                    "你是一个播客内容摘要专家。根据以下中文翻译稿，生成：\n\n"
                    "1. 中文摘要（150-200字，覆盖核心理念和讨论要点）\n"
                    "2. 3-5个关键话题点\n\n"
                    "说话风格: {tone_analysis}\n\n"
                    "原文：\n"
                    "{sample}\n\n"
                    "输出格式：\n"
                    "摘要: <摘要>\n"
                    "要点:\n"
                    "- 要点1\n"
                    "- 要点2\n"
                ),
            },
        }
