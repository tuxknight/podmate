# PodMate — CLI 原型 TASKS.md

> 终端里的播客伴侣。订阅英文科技播客 → 自动转写+翻译+中文配音。

## 项目结构

```
~/hermes-workspace/podmate/
├── podmate/                  # Python package
│   ├── __init__.py
│   ├── __main__.py           # entry: python -m podmate
│   ├── cli.py                # click/typer command definitions
│   ├── db.py                 # SQLite data layer
│   ├── feed.py               # RSS feed parsing + discovery
│   ├── downloader.py         # MP3 download via httpx
│   ├── transcriber.py        # faster-whisper transcription
│   ├── translator.py         # DeepSeek API translation + summary
│   ├── dubbing.py            # edge-tts Chinese dubbing
│   ├── player.py             # audio playback
│   ├── pipeline.py           # async pipeline orchestrator
│   └── models.py             # dataclasses/types
├── data/
│   ├── feeds.db              # SQLite database
│   ├── episodes/             # {guid}.mp3 (original)
│   ├── transcripts/          # {guid}.json (whisper output)
│   ├── translations/         # {guid}.json (translation + summary)
│   └── dubs/                 # {guid}.mp3 (chinese dub)
├── preset-feeds.json         # initial podcast list
├── requirements.txt
├── setup.py
└── README.md
```

## 通用规则

1. **所有代码都用 Claude Code 实现。** 我 (ProMan) 只写 spec 和 TASKS.md，不手动写代码。
2. 每个 Task 单独跑一次 `claude -p "..." --allowedTools "Read,Write,Edit,Bash" --max-turns 20`
3. 每个 Task 完成后验证：`cd ~/hermes-workspace/podmate && python3 -c "..."` 或跑一遍测试
4. DeepSeek API key 从环境变量 `DEEPSEEK_API_KEY` 读取
5. 使用 `pip3 install --user --break-system-packages <pkg>` 装依赖（RPi 有 PEP 668 限制）
6. 中文配音语音用 `zh-CN-YunyangNeural`（云扬男声，沉稳适合科技内容）

## Task 1: 项目骨架 + 数据模型 + CLI 框架

**描述：** 创建项目目录结构、SQLite 数据模型、CLI 入口框架

### 数据结构

SQLite tables:

```sql
CREATE TABLE feeds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    url TEXT UNIQUE NOT NULL,          -- RSS URL
    author TEXT,
    description TEXT,
    image_url TEXT,
    added_at TEXT DEFAULT (datetime('now')),
    last_fetched_at TEXT
);

CREATE TABLE episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    feed_id INTEGER NOT NULL REFERENCES feeds(id),
    guid TEXT NOT NULL,                -- unique per episode (RSS GUID)
    title TEXT NOT NULL,
    description TEXT,
    pub_date TEXT,
    audio_url TEXT,                    -- original download URL
    duration_sec INTEGER,
    -- local file paths (relative to data/ dir)
    local_path TEXT,                   -- data/episodes/{guid}.mp3
    status TEXT DEFAULT 'none',        -- none | downloading | downloaded | transcribing | transcribed | translating | translated | dubbing | dubbed | error
    progress REAL DEFAULT 0,           -- 0.0-1.0
    error_message TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
```

### 数据层 (db.py)

- `init_db()` — create tables if not exist
- `add_feed(url, title, ...)` — insert or ignore (on conflict url)
- `get_feeds()` — list all subscribed feeds
- `get_episodes(feed_id=None, status=None, limit=20)` — list episodes
- `get_episode(id)` — single episode
- `add_episode(feed_id, guid, title, ...)` — insert or ignore (on conflict guid)
- `update_episode_status(id, status, progress=None, error_message=None)`
- `set_episode_path(id, field, path)` — set local_path / transcript_path / translation_path / dub_path
- `search_feeds(keyword)` — search feeds by title
- `search_episodes(keyword)` — search episodes by title
- `delete_episode(id)` — remove episode + cleanup file paths
- `count_stats()` — total feeds, episodes, by status
- `auto_vacuum()` — optional

### 模型 (models.py)

```python
@dataclass
class Feed:
    id: int
    title: str
    url: str
    author: str | None
    description: str | None
    image_url: str | None
    added_at: str
    last_fetched_at: str | None

@dataclass
class Episode:
    id: int
    feed_id: int
    guid: str
    title: str
    description: str | None
    pub_date: str | None
    audio_url: str | None
    duration_sec: int | None
    local_path: str | None
    status: str        # none | downloading | downloaded | transcribing | transcribed | translating | translated | dubbing | dubbed | error
    progress: float
    error_message: str | None
    created_at: str
    # computed: feed_title from JOIN
    feed_title: str | None = None
```

### CLI 入口 (cli.py + __main__.py)

使用 **typer** (已装) 定义命令：

```python
app = typer.Typer(name="podmate", help="Podcast companion — download, transcribe, translate, dub")

@app.callback()
def main():
    """Initialize data directory and DB"""
    ensure_data_dirs()
    init_db()

# Subcommands (stubs first, implement in later tasks)
@app.command()
def discover(keyword: str = typer.Argument(..., help="Search podcast feeds")):
    """Search and discover podcasts"""
    ...

@app.command()
def sub(url: str = typer.Argument(..., help="RSS feed URL to subscribe")):
    """Subscribe to a podcast feed"""
    ...

@app.command()
def unsubscribe(feed_id: int = typer.Argument(..., help="Feed ID to unsubscribe")):
    """Unsubscribe from a podcast feed"""
    ...

@app.command()
def list(
    subscribed: bool = typer.Option(False, "--subscribed", "-s", help="Show subscribed feeds instead of episodes"),
    feed_id: int = typer.Option(None, "--feed", "-f", help="Filter by feed ID"),
    limit: int = typer.Option(20, "--limit", "-n", help="Max results"),
):
    """List episodes or subscribed feeds"""
    ...

@app.command()
def show(episode_id: int = typer.Argument(..., help="Episode ID")):
    """Show episode details and summary"""
    ...

@app.command()
def download(
    episode_id: int = typer.Argument(..., help="Episode ID to download and process"),
    skip_dub: bool = typer.Option(False, "--skip-dub", help="Skip Chinese dubbing step"),
):
    """Download episode, transcribe, translate, and dub"""
    ...

@app.command()
def play(episode_id: int = typer.Argument(..., help="Episode ID to play")):
    """Play original audio and/or Chinese dub"""
    ...

@app.command()
def clean(keep: int = typer.Option(5, "--keep", "-k", help="Number of recent episodes to keep")):
    """Remove old episodes to free space"""
    ...

@app.command()
def status():
    """Show overall stats"""
    ...
```

`__main__.py`:
```python
from .cli import app
app()
```

### 验证
```bash
cd ~/hermes-workspace/podmate && python3 -m podmate --help
python3 -m podmate status
# Should show: subcommands listed, "No feeds yet" or similar
```

---

## Task 2: RSS 发现 + 订阅 (feed.py)

### 功能

**`podmate discover <keyword>`**
- 用现有的播客搜索引擎搜索：首选 **iTunes Search API**（https://itunes.apple.com/search?term=...&media=podcast）
- 返回结果列表：标题、作者、episode 数量、RSS URL
- 用 rich 表格展示

**`podmate sub <url>`**
- 解析 RSS feed 获取 feed 元信息
- 存入数据库
- 自动 fetch 最近 5 集存入 episodes 表
- 下载镜像到 data/episodes/（按需，可以先只存元信息，下载留给 `podmate dl`）

**`podmate sub <id>`**
- 如果传的是数字 ID，从已发现的结果查找并订阅

### feed.py

```python
async def search_itunes(keyword: str, limit: int = 10) -> list[dict]:
    """Search iTunes Podcast API for feeds"""
    url = f"https://itunes.apple.com/search?term={quote(keyword)}&media=podcast&limit={limit}"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url)
        data = resp.json()
        # results contain: trackName, artistName, feedUrl, artworkUrl100, primaryGenreName
        ...

def parse_feed(url: str) -> dict:
    """Parse RSS feed using feedparser, return feed metadata + episode list"""
    import feedparser
    feed = feedparser.parse(url)
    # feed.feed: title, subtitle, author, image, link
    # feed.entries: title, description, published, links (enclosure), itunes_duration
    ...

async def fetch_recent_episodes(feed_url: str, limit: int = 5) -> list[dict]:
    """Get recent episodes from a feed, return structured data"""
    ...
```

### 验证
```bash
python3 -m podmate discover "lex fridman"
# Should show a rich table with results

python3 -m podmate sub "https://lexfridman.com/feed/podcast/"
# Should show "Subscribed to Lex Fridman Podcast"

python3 -m podmate list --subscribed
# Should show the feed
```

---

## Task 3: 下载 + 转写 (downloader.py + transcriber.py)

### 下载 (downloader.py)

```python
async def download_episode(audio_url: str, dest_path: str, progress_callback=None) -> str:
    """
    Stream download MP3 via httpx.
    Save to dest_path.
    Call progress_callback(bytes_downloaded, total_bytes) if provided.
    Returns dest_path on success.
    """
    async with httpx.AsyncClient(timeout=300.0) as client:
        async with client.stream("GET", audio_url) as resp:
            total = int(resp.headers.get("content-length", 0))
            with open(dest_path, "wb") as f:
                async for chunk in resp.aiter_bytes():
                    f.write(chunk)
                    if progress_callback:
                        progress_callback(f.tell(), total)
```

### 转写 (transcriber.py)

使用 faster-whisper，本地 CPU 跑。因为 RPi 没 GPU，选 `base` 或 `small` 模型（large 太慢）。

```python
from faster_whisper import WhisperModel

model = None  # lazy singleton

def get_model(model_size: str = "base"):
    global model
    if model is None:
        model = WhisperModel(model_size, device="cpu", compute_type="int8")
    return model

def transcribe(audio_path: str) -> dict:
    """
    Transcribe audio file using faster-whisper.
    Returns dict: {
        "text": "...",           # full transcript
        "segments": [            # per-segment with timestamps
            {
                "id": 0,
                "start": 0.0,
                "end": 5.2,
                "text": "...",
            },
            ...
        ],
        "language": "en",
        "duration_sec": 1234.5,
    }
    """
    model = get_model("base")  # use "small" if speed is acceptable
    segments, info = model.transcribe(audio_path, beam_size=5)
    result = {
        "text": "",
        "segments": [],
        "language": info.language,
        "duration_sec": info.duration,
    }
    full_text_parts = []
    for seg in segments:
        full_text_parts.append(seg.text)
        result["segments"].append({
            "id": seg.id,
            "start": seg.start,
            "end": seg.end,
            "text": seg.text.strip(),
        })
    result["text"] = " ".join(full_text_parts)
    return result
```

保存路径格式：`data/transcripts/{episode_guid}.json`

### 验证
```bash
# Pick a short podcast episode URL, download and transcribe
# python3 -c "from podmate.downloader import download_episode; ..."
# python3 -c "from podmate.transcriber import transcribe; ..."
```

---

## Task 4: 翻译 + 摘要 (translator.py)

使用 **DeepSeek Chat API** 做翻译和摘要。

### API 配置

```python
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
```

### 翻译策略

Whisper 输出的 transcript 可能很长（一期 1h 的播客可能有 200+ 条 segment）。需要分批处理：

```python
async def translate_segments(segments: list[dict], batch_size: int = 20) -> dict:
    """
    Send segments to DeepSeek in batches.
    
    System prompt:
    You are a professional Chinese-English translator specializing in tech podcasts.
    Translate the following English podcast transcript segments to natural Chinese.
    Preserve the speaker's tone and technical accuracy.
    For each segment, output: [segment_id] translated_text
    
    Return dict: {
        "summary_zh": "中文摘要（200字以内）",
        "segments": [
            {"id": 0, "en": "original", "zh": "translated"},
            ...
        ],
        "speaker_notes": "语气/说话风格分析（可选）",
    }
    """
    ...
```

**Prompt 策略：**
1. 先发一个较长的 segment 做采样，让 LLM 了解播客主题和说话风格
2. 第一个请求同时让 LLM 生成「讨论话题要点拆解」
3. 后续每个 batch 带上「上下文提示」保持翻译一致性
4. 全部 batch 完成后汇总生成最终摘要

### 输出格式

保存到 `data/translations/{guid}.json`：

```json
{
  "summary_zh": "这期节目讨论了...",
  "summary_en": "...",
  "key_points": ["要点1", "要点2"],
  "recommended_voice_style": "知性沉稳，科技播客语调",
  "episode_title_zh": "译制标题",
  "segments": [
    {"id": 0, "start": 0.0, "end": 12.5, "en": "Welcome...", "zh": "欢迎来到..."},
    ...
  ]
}
```

### 验证
```bash
# Take a short transcript file, translate it
# python3 -c "from podmate.translator import translate_segments; ..."
```

---

## Task 5: 中文配音 (dubbing.py)

使用 **edge-tts** 将中文翻译稿转为音频。

### 设计

```python
DUB_VOICE = "zh-CN-YunyangNeural"  # 沉稳男声，适合科技内容

async def dub_translation(
    segments: list[dict],       # [{"id": 0, "zh": "翻译文本", "start": 0.0, ...}]
    output_path: str,
    voice: str = DUB_VOICE,
    rate: str = "+0%",          # speech rate adjustment
    volume: str = "+0%",
) -> str:
    """
    Generate Chinese speech from translated segments.
    
    Strategy: Concatenate all "zh" texts from segments, 
    generate one long audio via edge-tts.
    
    Returns path to generated .mp3.
    
    Edge case: If text is very long (>5000 chars), split into chunks
    and concatenate with ffmpeg.
    """
    ...
```

**为什么要衔接所有段**：逐段配音会有不自然的停顿。整段生成更流畅。
**但如果需要段级别的对齐**（为后续"点击字幕跳到对应位置"做准备），可以生成完整配音后在处理的副产物中记录时间偏移。

### 验证
```bash
# Create a small test text and dub it
# python3 -c "from podmate.dubbing import dub_translation; ..."
# Should produce an .mp3 file
```

---

## Task 6: 播放 (player.py)

### 设计

使用 `subprocess` 调用系统播放器。

```python
def play_original(episode_id_or_path: str):
    """
    Play original English audio using system player.
    Auto-detect: mpv > mplayer > ffplay > aplay
    """
    ...

def play_dub(episode_id_or_path: str):
    """Play Chinese dub audio"""
    ...

def play_both(episode_id_or_path: str):
    """Play original and dub side-by-side? No — just switch between them."""
    ...

def get_available_player() -> str | None:
    """Find first available player on system"""
    for player in ["mpv", "mplayer", "ffplay", "aplay"]:
        if shutil.which(player):
            return player
    return None
```

### Edge cases
- 如果没播放器，提示用户安装 mpv
- 如果音频文件不存在，报错提示"请先下载"

---

## Task 7: 流水线编排 (pipeline.py)

**重要：这不是一个新模块，而是从 cli.py 的 `download` 命令中抽离 pipeline 逻辑。**

cli.py 的 `download` 命令现在有 870 行（L385-L637），包含了下载→转写→翻译→配音的全部业务逻辑。
目标是：把业务逻辑抽到 `pipeline.py`，cli.py 只保留 CLI 胶水代码。

### 新文件: `podmate/pipeline.py`

```python
from __future__ import annotations

import asyncio
import json
import os
import threading
from typing import Any, Optional

from .db import (
    get_episode,
    set_episode_path,
    update_episode_status,
)
from .downloader import download_episode
from .transcriber import transcribe
from .translator import translate_segments
from .dubbing import dub_translation

# ── 路径辅助 ────────────────────────────────────────

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

def _get_data_path(guid: str, subdir: str) -> str:
    """返回 data/{subdir}/{guid}.json 或 data/{subdir}/{guid}.mp3 的完整路径。"""
    ext = ".mp3" if subdir in ("episodes", "dubs") else ".json"
    return os.path.join(DATA_DIR, subdir, f"{guid}{ext}")

# ── 进度回调 ────────────────────────────────────────

class PipelineProgress:
    """进度跟踪器，供 CLI 层订阅进度更新。"""
    def __init__(self):
        self.step: str = ""          # 当前步骤名
        self.progress: float = 0.0   # 0.0-1.0
        self.status_text: str = ""   # 状态描述
        
    def update(self, step: str, progress: float, status_text: str = ""):
        self.step = step
        self.progress = progress
        self.status_text = status_text

# ── Pipeline 编排器 ────────────────────────────────

async def run_pipeline(
    episode_id: int,
    *,
    skip_dub: bool = False,
    progress_callback: Optional[callable] = None,
) -> dict[str, Any]:
    """
    运行一集的完整流水线：下载 → 转写 → 翻译 → 配音。
    
    Args:
        episode_id: 剧集 ID
        skip_dub: 跳过配音步骤
        progress_callback: 可选回调，接收 PipelineProgress 实例
        
    Returns:
        dict: {episode_id, status, audio_path, transcript_path, translation_path, dub_path}
        
    Raises:
        RuntimeError: 任何步骤失败时抛异常，DB 状态设为 error
    """
    pp = PipelineProgress()
    def _emit(step, progress, text=""):
        pp.update(step, progress, text)
        if progress_callback:
            progress_callback(pp)
    
    # 1. 获取剧集
    ep = get_episode(episode_id)
    if not ep:
        raise RuntimeError(f"未找到剧集 ID: {episode_id}")
    if not ep.audio_url:
        raise RuntimeError(f"剧集 #{episode_id} 没有音频链接")
    
    guid = ep.guid
    audio_path = _get_data_path(guid, "episodes")
    transcript_path = _get_data_path(guid, "transcripts")
    translation_path = _get_data_path(guid, "translations")
    dub_path = _get_data_path(guid, "dubs")
    
    for p in [audio_path, transcript_path, translation_path, dub_path]:
        os.makedirs(os.path.dirname(p), exist_ok=True)
    
    try:
        # ── 下载 ─────────────────────────────────────
        _emit("downloading", 0.0, f"正在下载: {ep.title}")
        update_episode_status(episode_id, "downloading", progress=0.0)
        
        def _dl_cb(done, total):
            progress = done / total if total > 0 else 0
            update_episode_status(episode_id, "downloading", progress=progress)
        
        await download_episode(ep.audio_url, audio_path, progress_callback=_dl_cb)
        
        update_episode_status(episode_id, "downloaded", progress=1.0)
        set_episode_path(episode_id, "local_path", audio_path)
        _emit("downloaded", 1.0, "✅ 下载完成")
        
        # ── 转写 ─────────────────────────────────────
        _emit("transcribing", 0.0, "正在转写音频...")
        update_episode_status(episode_id, "transcribing", progress=0.0)
        
        # faster-whisper 是同步阻塞的，在 RPi 上可能跑很久
        result: Optional[dict] = None
        error: Optional[Exception] = None
        
        def _run_transcribe():
            nonlocal result, error
            try:
                result = transcribe(audio_path)
            except Exception as e:
                error = e
        
        t = threading.Thread(target=_run_transcribe, daemon=True)
        t.start()
        # 轮询等待（faster-whisper 不提供逐段回调）
        while t.is_alive():
            t.join(timeout=1.0)
            # 可以在 future 中增加进度估计
        
        if error:
            raise RuntimeError(f"转写失败: {error}")
        
        # 保存转写结果
        with open(transcript_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        
        set_episode_path(episode_id, "transcript_path", transcript_path)
        update_episode_status(episode_id, "transcribed", progress=1.0)
        
        lang = result.get("language", "?")
        seg_count = len(result.get("segments", []))
        _emit("transcribed", 1.0, f"✅ 转写完成: {lang}, {seg_count} 段")
        
        # ── 翻译 ─────────────────────────────────────
        _emit("translating", 0.0, "正在调用 DeepSeek 翻译...")
        update_episode_status(episode_id, "translating", progress=0.0)
        
        translation = await translate_segments(
            result.get("segments", []),
            batch_size=20,
            episode_id=episode_id,
        )
        
        # 保存翻译结果
        with open(translation_path, "w", encoding="utf-8") as f:
            json.dump(translation, f, ensure_ascii=False, indent=2)
        
        set_episode_path(episode_id, "translation_path", translation_path)
        update_episode_status(episode_id, "translated", progress=1.0)
        
        summary = translation.get("summary_zh", "")
        key_points = translation.get("key_points", [])
        _emit("translated", 1.0, f"✅ 翻译完成 ➜ {summary}")
        
        # ── 配音 ─────────────────────────────────────
        if not skip_dub:
            _emit("dubbing", 0.0, "正在生成中文配音...")
            update_episode_status(episode_id, "dubbing", progress=0.0)
            
            dub_path_result = await dub_translation(
                translation.get("segments", []),
                dub_path,
                episode_id=episode_id,
            )
            
            set_episode_path(episode_id, "dub_path", dub_path_result)
            update_episode_status(episode_id, "dubbed", progress=1.0)
            _emit("dubbed", 1.0, "✅ 配音完成 (Yunyang 云扬)")
        else:
            update_episode_status(episode_id, "dubbed", progress=1.0)
            _emit("dubbed", 1.0, "⏭️ 跳过配音")
        
        return {
            "episode_id": episode_id,
            "status": "dubbed" if not skip_dub else "dubbed",
            "audio_path": audio_path,
            "transcript_path": transcript_path,
            "translation_path": translation_path,
            "dub_path": dub_path,
        }
        
    except Exception as e:
        update_episode_status(
            episode_id, "error",
            progress=0.0,
            error_message=str(e),
        )
        raise RuntimeError(f"流水线失败 (ep #{episode_id}): {e}")
```

### 修改 cli.py

`cli.py` 的 `download` 命令（L385-L637）替换为：

```python
@app.command()
def download(
    episode_id: int = typer.Argument(..., help="要下载和处理的剧集 ID"),
    skip_dub: bool = typer.Option(False, "--skip-dub", help="跳过中文配音步骤"),
) -> None:
    \"\"\"下载剧集音频，然后转写、翻译、配音。\"\"\"
    from .pipeline import run_pipeline, PipelineProgress
    
    ep = get_episode(episode_id)
    if not ep:
        console.print(f"[red]❌ 未找到剧集 ID: {episode_id}[/red]")
        raise typer.Exit(code=1)
    
    console.print(f"\n[bold]🚀 启动流水线:[/bold] [cyan]{ep.title}[/cyan]\n")
    
    try:
        result = asyncio.run(run_pipeline(
            episode_id,
            skip_dub=skip_dub,
        ))
        
        # 显示完成面板
        console.print()
        console.print(Panel(
            f"[bold green]✅ 全部完成! 剧集 #{episode_id}[/bold green]\n\n"
            f"[bold cyan]▶️ 播放原声:[/bold cyan]     [green]podmate play {episode_id}[/green]\n"
            f"[bold cyan]🎙️ 播放配音:[/bold cyan]     [green]podmate play {episode_id} --dub[/green]\n"
            f"[bold cyan]📄 查看详情:[/bold cyan]     [green]podmate show {episode_id}[/green]",
            title="PodMate 处理完成",
            border_style="green",
        ))
        
    except Exception as e:
        console.print(Panel(
            f"[red]❌ 处理失败: {e}[/red]",
            title=f"剧集 #{episode_id} 错误",
            border_style="red",
        ))
        raise typer.Exit(code=1)
```

### 清理 cli.py 中不再需要的 import

移除 cli.py 中旧的 Rich Progress 相关导入（DownloadColumn, TransferSpeedColumn, TimeRemainingColumn, SpinnerColumn 等），以及不再使用的 `download_episode`、`transcribe`、`translate_segments`、`dub_translation` 等模块的直接导入——这些现在由 pipeline.py 管理。

### 验证

```bash
cd ~/hermes-workspace/podmate && python3 -m podmate download --help
# 应该看到简洁的 help，不再有内部细节

# 测试简单场景：用 ep 15 (已有音频，停在 transcribing)
python3 -c "
from podmate.pipeline import run_pipeline
import asyncio
result = asyncio.run(run_pipeline(15, skip_dub=True))
print(result)
"
# 应该看到：下载（已有就跳过文件）→ 转写 → 翻译，不配音
```

### RPi 注意事项
- ep 15 音频 76MB/1h21min → base 模型转写约 40-60 分钟
- 翻译分批调用 DeepSeek API → 注意 API key 是否可用
- 如果转写太慢，考虑跑 ep 15 的 `--skip-transcribe` 快速验证翻译→配音

---

## Task 8: CLI 命令实现 + 展示美化

### 命令实现

把所有 stub 命令用 rich 美化填实：

- **`discover`**: rich Table，显示 #, 标题, 作者, 集数
- **`sub`**: 成功后打印绿色 ✓ Subscribed to {title}
- **`list`**: rich Table，显示 ID, 播客名, 标题, 日期, 时长, 状态
  - 状态显示颜色标签：✅ dubbed / 🎯 translated / ⏳ downloading / ❌ error
- **`show`**: rich Panel 显示详情 + 摘要（如果有的话）
- **`download`**: rich Progress 实时显示进度
- **`play`**: 提示用哪个播放器，开始播放
- **`status`**: rich 仪表盘：总订阅数、总集数、处理完成数、磁盘占用

### 示例输出

```
$ python3 -m podmate list

 PodMate — Recent Episodes
┏━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━┓
┃ ID ┃ Title                        ┃ Feed         ┃ Date     ┃Status┃
┡━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━┓
│  1 │ #456 – The Future of...      │ Lex Fridman  │ 2026-06...│ ✅   │
│  2 │ AGI Safety Debate            │ Latent Space │ 2026-06...│ ⏳   │
└────┴───────────────────────────────┴──────────────┴──────────┴──────┘
```

---

## Task 10: 系统配置层 (config.py)

### 设计目标

PodMate 需要一个统一的配置系统，替代目前散落在各模块的 `os.environ.get()` 调用。

### 配置文件

**位置：** `~/.config/podmate/config.toml`

**格式：** TOML（Python 内置支持，比 JSON 可读性好，支持注释）

```toml
# PodMate 配置
[deepgram]
api_key = "dg_xxx..."
api_url = "https://api.deepgram.com/v1/listen"
model = "nova-2"
diarize = true

[deepseek]
api_key = "sk-xxx..."
api_url = "https://api.deepseek.com/v1/chat/completions"
model = "deepseek-chat"
temperature = 0.3

[dubbing]
voice = "zh-CN-YunyangNeural"
rate = "+0%"
volume = "+0%"

[storage]
data_dir = "~/hermes-workspace/podmate/data"
keep_episodes = 5
```

### 新文件: `podmate/config.py`

```python
"""PodMate 配置管理 — 从 ~/.config/podmate/config.toml 加载。"""

from __future__ import annotations

import os
import tomllib  # Python 3.11+ built-in, no extra dep needed
from pathlib import Path
from typing import Any

CONFIG_DIR = Path.home() / ".config" / "podmate"
CONFIG_PATH = CONFIG_DIR / "config.toml"

# 默认配置
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
    "storage": {
        "data_dir": os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"),
        "keep_episodes": 5,
    },
}

_config: dict[str, Any] | None = None


def _merge(default: dict, override: dict) -> dict:
    """递归合并字典，保留默认值中 override 未覆盖的键。"""
    result = default.copy()
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _merge(result[k], v)
        else:
            result[k] = v
    return result


def load() -> dict[str, Any]:
    """加载配置。首次调用时读取文件，后续返回缓存。"""
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
    """获取配置项，例如 get('deepgram', 'api_key')。"""
    cfg = load()
    return cfg.get(section, {}).get(key, default)


def init() -> bool:
    """创建默认配置文件（如果不存在）。返回 True 如果创建了文件。"""
    if CONFIG_PATH.exists():
        return False

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _write(CONFIG_PATH, DEFAULT_CONFIG)
    return True


def set_key(section: str, key: str, value: str) -> None:
    """设置配置项并保存。用于 podmate config set 命令。"""
    cfg = load()
    if section not in cfg:
        cfg[section] = {}
    cfg[section][key] = value
    _config = cfg
    _write(CONFIG_PATH, cfg)


def _write(path: Path, cfg: dict) -> None:
    """将配置写为 TOML 文件。"""
    lines: list[str] = []
    for section, values in cfg.items():
        lines.append(f"\n[{section}]")
        for k, v in values.items():
            if isinstance(v, bool):
                lines.append(f'{k} = {"true" if v else "false"}')
            elif isinstance(v, (int, float)):
                lines.append(f"{k} = {v}")
            else:
                # 字符串，需要转义特殊字符
                escaped = str(v).replace("\\", "\\\\").replace('"', '\\"')
                lines.append(f'{k} = "{escaped}"')
    path.write_text("\n".join(lines).lstrip("\n") + "\n")


def mask(value: str, visible: int = 4) -> str:
    """脱敏显示 key，显示前 visible 位，其余用 * 代替。"""
    if not value:
        return "(未设置)"
    if len(value) <= visible + 4:
        return value[:visible] + "..." + value[-4:]
    return value[:visible] + "..." + value[-4:]


def show() -> dict[str, Any]:
    """返回脱敏后的配置，用于 podmate config show。"""
    cfg = load()
    masked = {}
    for section, values in cfg.items():
        masked[section] = {}
        for k, v in values.items():
            if "key" in k.lower() or "token" in k.lower() or "secret" in k.lower():
                masked[section][k] = mask(str(v))
            else:
                masked[section][k] = v
    return masked
```

### 修改各模块

**`transcriber.py`：**
- 模块级常量改为从 `config.get('deepgram', 'api_key')` 读取
- Deepgram API URL 也从 config 读
- 错误提示改为"请运行: podmate config set deepgram.api_key=***"

**`translator.py`：**
- `API_KEY` 改为从 `config.get('deepseek', 'api_key')` 读取
- `DEEPSEEK_API_URL`、temperature 也从 config 读
- 错误提示改为"请运行: podmate config set deepseek.api_key=***"

**`dubbing.py`：**
- `DUB_VOICE` 改为从 `config.get('dubbing', 'voice')` 读取
- 同样 rate/volume 从 config 读

### 新增 CLI 命令

在 `cli.py` 中新增 `config` 命令组：

```python
@app.group()
def config():
    \"\"\"管理 PodMate 配置。\"\"\"
    pass

@config.command("init")
def config_init():
    \"\"\"创建默认配置文件。\"\"\"
    from .config import init
    if init():
        console.print("[green]✅ 配置文件已创建: ~/.config/podmate/config.toml[/green]")
        console.print("[dim]请运行以下命令设置 API key:[/dim]")
        console.print("  [cyan]podmate config set deepgram.api_key 'your_key'[/cyan]")
        console.print("  [cyan]podmate config set deepseek.api_key 'your_key'[/cyan]")
    else:
        console.print("[yellow]配置文件已存在[/yellow]")

@config.command("show")
def config_show():
    \"\"\"显示当前配置（key 脱敏）。\"\"\"
    from .config import show as config_show_data
    cfg = config_show_data()
    table = Table(title="PodMate 配置", box=box.ROUNDED)
    table.add_column("模块", style="bold")
    table.add_column("键", style="cyan")
    table.add_column("值")
    for section, values in cfg.items():
        for k, v in values.items():
            table.add_row(section, k, str(v))
    console.print(table)

@config.command("set")
def config_set(
    key: str = typer.Argument(..., help="配置键，如 deepgram.api_key"),
    value: str = typer.Argument(..., help="配置值"),
):
    \"\"\"设置配置项。\"\"\"
    if "." not in key:
        console.print("[red]❌ 格式错误，请使用 section.key 格式，如 deepgram.api_key[/red]")
        raise typer.Exit(code=1)
    section, k = key.split(".", 1)
    from .config import set_key
    set_key(section, k, value)
    console.print(f"[green]✅ {section}.{k} 已设置[/green]")
```

### 验证

```bash
cd ~/hermes-workspace/podmate
python3 -m podmate config init
python3 -m podmate config set deepgram.api_key "4479800d184a6c135b26d144d4ac44a2c4d184e9"
python3 -m podmate config set deepseek.api_key "sk-xxx..."
python3 -m podmate config show
# 应该看到 key 脱敏显示

# 运行完整 pipeline（不再依赖环境变量）
python3 -m podmate download 15 --skip-dub
# 应该成功完成
```

### preset-feeds.json

```json
[
  {
    "title": "Lex Fridman Podcast",
    "url": "https://lexfridman.com/feed/podcast/",
    "author": "Lex Fridman",
    "description": "Conversations about AI, science, and the meaning of life"
  },
  {
    "title": "Latent Space",
    "url": "https://feeds.transistor.fm/latent-space-podcast",
    "author": "Alessio Fanelli & swyx",
    "description": "The AI Engineer podcast — deep dives into AI engineering"
  },
  {
    "title": "Acquired",
    "url": "https://feeds.megaphone.fm/acquired",
    "author": "Ben Gilbert & David Rosenthal",
    "description": "The history of great companies"
  },
  {
    "title": "Theo — t3.gg",
    "url": "https://t3dotgg.com/rss",
    "author": "Theo",
    "description": "Web development, TypeScript, and tech hot takes"
  },
  {
    "title": "Decoder with Nilay Patel",
    "url": "https://feeds.simplecast.com/nM6kymOQ",
    "author": "The Verge",
    "description": "Big ideas from the people making technology"
  }
]
```

### README.md

简洁的说明：安装、使用方法、依赖。

### init 命令

`podmate init` 或首次运行自动：
1. 创建 data/ 目录结构
2. 导入 preset-feeds.json
3. 显示欢迎 + 几个热门播客

---

## 执行顺序

1. Task 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9
2. 每个 task 单独跑一次 `claude -p` 任务
3. 每个 task 完成后验证
4. Task 8 依赖 Task 1-7 全部完成，最后一次性美化所有输出

## 注意事项

1. **RPi 4GB RAM 限制** — faster-whisper 用 `base` 模型，`compute_type="int8"`，不要用 large
2. **磁盘 1.9GB 空闲** — 每期 ~60MB MP3 + ~60MB 配音，不要下载过多
3. **DeepSeek API** — 翻译分批处理，每批 20 段，避免超长 context
4. **edge-tts** — 长文本分段生成后 ffmpeg 拼接
5. **播放器** — 优先检测 `mpv`，没有就 `ffplay`
6. **交互** — 所有输出用 rich 美化，不用 curses/textual
7. **中文界面** — 所有 UI 标签、提示用中文
