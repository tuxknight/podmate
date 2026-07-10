#!/usr/bin/env python3
"""直接翻译 episode 16 已有的 transcript JSON，不跑 pipeline。"""
import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Load API keys before any podmate imports
_env = Path.home() / ".hermes" / ".env"
if _env.exists():
    for _line in _env.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line and not _line.startswith("WE_UPLOAD"):
            _k, _v = _line.split("=", 1)
            _v = _v.strip().strip("'\"")
            os.environ.setdefault(_k, _v)
# Alias: Hermes uses same API key as DeepSeek
if "DEEPSEEK_API_KEY" in os.environ and "HERMES_API_KEY" not in os.environ:
    os.environ["HERMES_API_KEY"] = os.environ["DEEPSEEK_API_KEY"]

from podmate.translator import translate_segments
from podmate.config import load, get as config_get
from podmate.db import get_episode, update_episode_status, set_episode_path, mark_episode_exported


async def main():
    ep = get_episode(16)
    if not ep:
        print("❌ Episode 16 not found")
        return

    print(f"🎙 {ep.title}")
    data_dir = config_get("storage", "data_dir", str(Path.home() / ".local/share/podmate"))
    guid = ep.guid
    safe_guid = guid.replace(":", "_")
    transcript_path = Path(data_dir) / "transcripts" / f"{safe_guid}.json"

    if not transcript_path.exists():
        print(f"❌ Transcript not found: {transcript_path}")
        return

    with open(transcript_path) as f:
        data = json.load(f)
    segments = data.get("segments", [])
    print(f"📄 {len(segments)} segments loaded")

    # Run translation
    print("🔄 Translating via Hermes API...")
    translation = await translate_segments(segments, batch_size=20, episode_id=16)

    # Save translation
    translation_dir = Path(data_dir) / "translations"
    translation_dir.mkdir(parents=True, exist_ok=True)
    translation_path = translation_dir / f"{safe_guid}.json"
    with open(translation_path, "w", encoding="utf-8") as f:
        json.dump(translation, f, ensure_ascii=False, indent=2)

    set_episode_path(16, "translation_path", str(translation_path))
    update_episode_status(16, "translated", progress=1.0)
    print(f"✅ Translation saved to: {translation_path}")

    # Generate markdown translation with ZH segments
    zh_lines = [f"# {ep.title}\n"]
    segs = translation.get("segments", [])
    for s in segs:
        t = s.get("start", 0)
        mins, secs = int(t // 60), int(t % 60)
        speaker = s.get("speaker_name", s.get("speaker", ""))
        speaker_tag = f"**{speaker}**: " if speaker else ""
        zh_lines.append(f"\n[{mins}:{secs:02d}] {speaker_tag}{s.get('zh', '')}")
    zh_content = "".join(zh_lines)

    zh_path = Path(data_dir) / "transcripts" / f"{safe_guid}.zh.md"
    with open(zh_path, "w", encoding="utf-8") as f:
        f.write(zh_content)
    print(f"✅ Chinese transcript saved to: {zh_path}")

    # Copy EN transcript to cbrain
    cbrain_dir = Path(config_get("storage", "cbrain_dir", str(Path.home() / "cbrain" / "docs" / "fuyuans-kb" / "podcasts")))
    cbrain_dir.mkdir(parents=True, exist_ok=True)

    en_md_path = Path(data_dir) / "transcripts" / f"{safe_guid}.md"
    if en_md_path.exists():
        shutil.copy2(en_md_path, cbrain_dir / en_md_path.name)
        print(f"✅ EN -> cbrain: {en_md_path.name}")

    shutil.copy2(zh_path, cbrain_dir / zh_path.name)
    print(f"✅ ZH -> cbrain: {zh_path.name}")

    mark_episode_exported(16)
    print("✅ Export complete")

    summary = translation.get("summary_zh", "")
    title_zh = translation.get("episode_title_zh", "")
    if title_zh:
        print(f"\n📝 中文标题: {title_zh}")
    print(f"📝 摘要: {summary[:200]}...")

    # Print a preview
    print(f"\n📋 前 3 段翻译预览:")
    for s in segs[:3]:
        t = s.get("start", 0)
        mins, secs = int(t // 60), int(t % 60)
        speaker = s.get("speaker_name", s.get("speaker", ""))
        print(f"  [{mins}:{secs:02d}] {speaker}: {s.get('zh', '')[:80]}")


if __name__ == "__main__":
    asyncio.run(main())
