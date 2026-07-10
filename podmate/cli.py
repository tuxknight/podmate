"""PodMate CLI — 终端里的播客伴侣。"""

import asyncio
import json
import os
import re
import shutil
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path

import typer
from rich import box
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import IntPrompt
from rich.table import Table

from . import __version__
from .config import load as load_config
from .db import (
    add_episode,
    add_feed,
    count_stats,
    delete_episode,
    delete_feed,
    get_connection,
    get_episode,
    get_episodes,
    get_feed,
    get_feeds,
    get_unexported_episodes,
    init_db,
    mark_episode_exported,
    mark_episode_read,
    mark_episode_starred,
    set_episode_path,
    update_episode_status,
)
from .models import Episode
from .downloader import download_episode
from .feed import PodcastIndexClient, _strip_html, parse_feed, resolve_feed, search_itunes
from .transcriber import _format_time, format_transcript, transcribe_via_deepgram

DATA_SUBDIRS = ["episodes", "transcripts", "translations", "dubs"]


def _get_data_dir() -> str:
    """Return configured data directory path."""
    return load_config()["storage"]["data_dir"]


def ensure_data_dirs() -> None:
    """确保数据目录存在。"""
    for sub in DATA_SUBDIRS:
        os.makedirs(os.path.join(_get_data_dir(), sub), exist_ok=True)


def _safe_filename(guid: str) -> str:
    """将 guid 中的不安全字符替换为 _，避免 Markdown URL 解析问题。"""
    return guid.replace(":", "_")

def _get_data_path(guid: str, subdir: str) -> str:
    """返回 data/{subdir}/{safe_guid}.json 或 data/{subdir}/{safe_guid}.mp3 的完整路径。"""
    ext = ".mp3" if subdir in ("episodes", "dubs") else ".json"
    return os.path.join(_get_data_dir(), subdir, f"{_safe_filename(guid)}{ext}")


def _get_cbrain_dir() -> Path:
    """Return cbrain podcasts directory from config, with fallback."""
    cbrain_dir = load_config().get("storage", {}).get("cbrain_dir", "")
    if cbrain_dir:
        return Path(os.path.expanduser(cbrain_dir))
    return Path.home() / "cbrain" / "docs" / "fuyuans-kb" / "podcasts"


def _status_label(status: str) -> str:
    """返回中文状态标签（含 emoji）。"""
    labels = {
        "none": "⏳ 待处理",
        "downloading": "⬇️ 下载中",
        "downloaded": "✅ 已下载",
        "transcribing": "📝 转写中",
        "transcribed": "📝 已转写",
        "translating": "🌐 翻译中",
        "translated": "🌐 已翻译",
        "dubbing": "🎙️ 配音中",
        "dubbed": "🎙️ 已配音",
        "error": "❌ 错误",
    }
    return labels.get(status, status)


def _status_emoji(status: str) -> str:
    """返回状态 emoji 简写。"""
    emojis = {
        "none": "⏳",
        "downloading": "⬇️",
        "downloaded": "🟢",
        "transcribing": "📝",
        "transcribed": "📝",
        "translating": "🌐",
        "translated": "🌐",
        "dubbing": "🎙️",
        "dubbed": "🎙️",
        "error": "❌",
    }
    return emojis.get(status, status)


def _format_duration(seconds: int) -> str:
    """将秒数格式化为 HH:MM:SS。"""
    h, remainder = divmod(seconds, 3600)
    m, s = divmod(remainder, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _search_transcripts(keyword: str) -> list[dict]:
    """Search all episode transcripts for keyword (case-insensitive).

    Returns list sorted by match_count descending, each with:
        {feed_title, episode_title, match_count, snippets: [{speaker, start, snippet}]}
    Max 3 snippets per episode.
    """
    keyword_lower = keyword.lower()
    episodes = get_episodes(limit=99999)
    results: list[dict] = []

    for ep in episodes:
        if not ep.transcript_path:
            continue
        if not os.path.isfile(ep.transcript_path):
            continue

        try:
            with open(ep.transcript_path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        segments = data.get("segments", [])
        snippets: list[dict] = []
        match_count = 0

        for seg in segments:
            text = seg.get("text", "")
            pos = text.lower().find(keyword_lower)
            if pos == -1:
                continue

            match_count += 1

            if len(snippets) < 3:
                start_idx = max(0, pos - 60)
                end_idx = min(len(text), pos + len(keyword) + 60)
                snippet = text[start_idx:end_idx]
                if start_idx > 0:
                    snippet = "..." + snippet
                if end_idx < len(text):
                    snippet = snippet + "..."

                snippets.append(
                    {
                        "speaker": seg.get("speaker", "?"),
                        "start": seg.get("start", 0),
                        "snippet": snippet,
                    }
                )

        if match_count > 0:
            results.append(
                {
                    "feed_title": ep.feed_title or "Unknown",
                    "episode_title": ep.title,
                    "match_count": match_count,
                    "snippets": snippets,
                }
            )

    results.sort(key=lambda r: r["match_count"], reverse=True)
    return results


def _show_search_table(keyword: str, results: list) -> None:
    """显示 iTunes 搜索结果表格。"""
    table = Table(
        title=f'📡 iTunes 搜索结果 — "{keyword}"',
        box=box.ROUNDED,
        header_style="bold cyan",
    )
    table.add_column("#", style="dim", width=4, justify="right")
    table.add_column("播客名称", style="bold", no_wrap=False)
    table.add_column("作者", style="green")
    table.add_column("集数", justify="right")

    for i, item in enumerate(results, start=1):
        table.add_row(
            str(i),
            item["trackName"][:60] + ("…" if len(item["trackName"]) > 60 else ""),
            item["artistName"][:40] if item["artistName"] else "-",
            str(item["trackCount"]) if item["trackCount"] else "-",
        )

    console.print(table)
    console.print()


# ── 控制台 ──────────────────────────────────────────

console = Console()

app = typer.Typer(
    name="podmate",
    help="Podcast 伴侣 — 下载、转写、翻译、配音",
    no_args_is_help=True,
    rich_markup_mode="rich",
)

feed_app = typer.Typer(name="feed", help="管理播客订阅")
episode_app = typer.Typer(name="episode", help="管理剧集与处理")
ep_app = typer.Typer(name="ep", help="管理剧集（episode 别名）", hidden=True)
export_app = typer.Typer(name="export", help="导出到 cbrain")

app.add_typer(feed_app)
app.add_typer(episode_app)
app.add_typer(ep_app)
app.add_typer(export_app)


@app.callback()
def main() -> None:
    """初始化数据目录和数据库。"""
    ensure_data_dirs()
    init_db()


# ═══════════════════════════════════════════════════
# 顶层命令
# ═══════════════════════════════════════════════════

# ── sub ──────────────────────────────────────────


@app.command()
def sub(
    url: str = typer.Argument(..., help="RSS 订阅地址或播客关键词"),
    pick: int | None = typer.Option(None, "--pick", "-p", help="直接选择搜索结果中的第 N 个"),
) -> None:
    """快捷订阅播客（等同于 feed add）。"""
    _cmd_feed_add(url, pick)


# ── play ──────────────────────────────────────────


@app.command()
def play(
    episode_id: int = typer.Argument(..., help="要播放的剧集 ID"),
    dub: bool = typer.Option(False, "--dub", "-d", help="播放中文配音而非原声"),
) -> None:
    """播放原声或中文配音。"""
    ep = get_episode(episode_id)
    if not ep:
        console.print(f"[red]❌ 未找到剧集 ID: {episode_id}[/red]")
        raise typer.Exit(code=1)

    if dub:
        from .dubbing import DUB_VOICE

        file_path = _get_data_path(ep.guid, "dubs")
        if not os.path.isfile(file_path):
            console.print(
                Panel(
                    f"[yellow]🎙️ 中文配音还不存在，请先运行:\n"
                    f"   [cyan]podmate episode process {episode_id}[/cyan][/yellow]\n\n"
                    f"[dim]当前配音设置: {DUB_VOICE}[/dim]",
                    title=f"剧集 #{episode_id}",
                    border_style="yellow",
                )
            )
            raise typer.Exit(code=1)
        mode_label = "🎙️ 中文配音"
    else:
        file_path = _get_data_path(ep.guid, "episodes")
        if not os.path.isfile(file_path):
            console.print(
                Panel(
                    f"[yellow]🔊 音频还不存在，请先运行:\n"
                    f"   [cyan]podmate episode process {episode_id}[/cyan][/yellow]",
                    title=f"剧集 #{episode_id}",
                    border_style="yellow",
                )
            )
            raise typer.Exit(code=1)
        mode_label = "🔊 原声"

    from .player import get_available_player

    player = get_available_player()
    if player is None:
        console.print("[red]❌ 未找到可用的播放器。[/red]")
        console.print("[yellow]💡 安装 mpv: [cyan]sudo apt install mpv[/cyan][/yellow]")
        raise typer.Exit(code=1)

    console.print(
        Panel(
            f"[bold cyan]{mode_label}: {ep.title}[/bold cyan]\n"
            f"[dim]播放器: {player}[/dim]\n"
            f"[dim]文件: {file_path}[/dim]\n\n"
            f"[green]▶️ 正在播放 ...[/green]\n"
            f"[yellow]按 Ctrl+C 停止播放[/yellow]",
            title=f"剧集 #{episode_id}",
        )
    )

    try:
        from .player import play_file

        play_file(file_path)
    except KeyboardInterrupt:
        console.print("\n[yellow]⏹️  播放结束[/yellow]")
    except Exception as e:
        console.print(f"[red]❌ 播放失败: {e}[/red]")
        raise typer.Exit(code=1)


# ── grep ──────────────────────────────────────────


@app.command()
def grep(
    keyword: str = typer.Argument(..., help="搜索关键词"),
) -> None:
    """在所有已转写剧集中搜索关键词。"""
    results = _search_transcripts(keyword)

    if not results:
        console.print(f'[yellow]🔍 未找到匹配结果: "{keyword}"[/yellow]')
        return

    total_matches = sum(r["match_count"] for r in results)
    episodes_searched = len(results)

    for r in results:
        console.print(f"\n[bold]📻 {r['feed_title']} → {r['episode_title']}[/bold]")
        console.print(f"  [dim]→ 找到 {r['match_count']} 处匹配[/dim]\n")

        for m in r["snippets"]:
            time_str = _format_time(m["start"])
            console.print(f"  [说话人 {m['speaker']}] [{time_str}] {m['snippet']}")

    console.print(f"\n[dim]共搜索 {episodes_searched} 个剧集，总计 {total_matches} 处匹配[/dim]")


# ── discover ──────────────────────────────────────


@app.command()
def discover(
    keyword: str = typer.Argument(..., help="搜索播客关键词"),
) -> None:
    """搜索并发现播客订阅源。"""
    with console.status(f'[bold green]🔍 正在搜索 "{keyword}" ...[/bold green]'):
        try:
            results = asyncio.run(search_itunes(keyword, limit=10))
        except Exception as e:
            console.print(
                Panel(
                    f"[red]❌ 搜索失败: {e}[/red]",
                    title="错误",
                    border_style="red",
                )
            )
            raise typer.Exit(code=1)

    if not results:
        console.print(f'[yellow]😕 未找到与 "{keyword}" 相关的播客[/yellow]')
        console.print("[dim]提示: 尝试使用英文关键词搜索[/dim]")
        return

    table = Table(
        title=f'📡 iTunes 搜索结果 — "{keyword}"',
        box=box.ROUNDED,
        header_style="bold cyan",
    )
    table.add_column("#", style="dim", width=4, justify="right")
    table.add_column("播客名称", style="bold", no_wrap=False)
    table.add_column("作者", style="green")
    table.add_column("集数", justify="right")

    for i, item in enumerate(results, start=1):
        table.add_row(
            str(i),
            item["trackName"][:60] + ("…" if len(item["trackName"]) > 60 else ""),
            item["artistName"][:40] if item["artistName"] else "-",
            str(item["trackCount"]) if item["trackCount"] else "-",
        )

    console.print(table)
    console.print()
    console.print(
        Panel(
            "[bold]订阅方式:[/bold] 使用 [cyan]podmate sub <播客名称>[/cyan] 关键词搜索订阅",
            border_style="green",
        )
    )


# ── poll ──────────────────────────────────────────


@app.command()
def poll(
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="仅检查更新，不入库",
    ),
) -> None:
    """轮询所有已订阅播客的 RSS，检查新剧集。"""
    feeds = get_feeds()
    if not feeds:
        console.print("[dim]📭 还没有订阅任何播客[/dim]")
        console.print("[dim]使用 [cyan]podmate sub <url>[/cyan] 订阅播客[/dim]")
        return

    total_found = 0
    added_count = 0
    feeds_checked = 0

    for feed in feeds:
        existing_eps = get_episodes(feed_id=feed.id, limit=99999)
        existing_guids = {ep.guid for ep in existing_eps}

        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(parse_feed, feed.url)
                feed_data = future.result(timeout=15)
        except FutureTimeoutError:
            console.print(f"[yellow]⚠️ {feed.title}: RSS 获取超时[/yellow]")
            continue
        except Exception as e:
            console.print(f"[yellow]⚠️ {feed.title}: RSS 获取失败 ({e})[/yellow]")
            continue

        feeds_checked += 1

        new_episodes = [
            ep for ep in feed_data.get("episodes", []) if ep.get("guid") not in existing_guids
        ]

        if new_episodes:
            total_found += len(new_episodes)
            console.print(
                f"🎙️ [bold]{feed.title}[/bold] → 发现 "
                f"[bold green]{len(new_episodes)}[/bold green] 集新剧集"
            )

            if not dry_run:
                for ep in new_episodes:
                    try:
                        add_episode(
                            feed_id=feed.id,
                            guid=ep.get("guid", ""),
                            title=ep.get("title", ""),
                            description=ep.get("description"),
                            pub_date=ep.get("pub_date"),
                            audio_url=ep.get("audio_url"),
                            duration_sec=ep.get("duration_sec"),
                        )
                        added_count += 1
                    except Exception:
                        pass

        if not dry_run:
            conn = get_connection()
            conn.execute(
                "UPDATE feeds SET last_fetched_at = datetime('now') WHERE id = ?",
                (feed.id,),
            )
            conn.commit()

    if feeds_checked == 0:
        return

    if dry_run:
        if total_found == 0:
            console.print(r"[dim]\[podmate] 暂无新剧集 (--dry-run)[/dim]")
        else:
            console.print(
                f"[dim]📊 检查 {feeds_checked} 个播客，发现 {total_found} 集新内容 "
                f"(--dry-run 模式，未入库)[/dim]"
            )
    else:
        if total_found == 0:
            console.print(r"[dim]\[podmate] 暂无新剧集[/dim]")
        else:
            console.print(
                f"[dim]📊 检查 {feeds_checked} 个播客，发现 {total_found} 集新内容，"
                f"已入库 {added_count} 集[/dim]"
            )


# ── clean ─────────────────────────────────────────


@app.command()
def clean(
    keep: int = typer.Option(5, "--keep", "-k", help="保留最近几集（按 ID 倒序）"),
    force: bool = typer.Option(False, "--force", help="直接清理，不确认"),
) -> None:
    """清理旧剧集以释放空间。"""
    episodes = get_episodes(limit=9999)
    if len(episodes) <= keep:
        console.print(f"[green]✅ 剧集数 ({len(episodes)}) 不超过保留数 ({keep})，无需清理[/green]")
        return

    to_keep_ids = set(ep.id for ep in sorted(episodes, key=lambda x: x.id, reverse=True)[:keep])
    to_delete = [ep for ep in episodes if ep.id not in to_keep_ids]

    total_bytes = 0
    for ep in to_delete:
        for subdir in ("episodes", "transcripts", "translations", "dubs"):
            path = _get_data_path(ep.guid, subdir)
            if os.path.isfile(path):
                total_bytes += os.path.getsize(path)

    if not force:
        size_mb = total_bytes / 1024 / 1024
        console.print(
            Panel(
                f"[yellow]即将清理 [bold]{len(to_delete)}[/bold] 集旧剧集[/yellow]\n"
                f"[yellow]释放空间: [bold]{size_mb:.1f} MB[/bold][/yellow]\n"
                f"[yellow]保留: [bold]{keep}[/bold] 集最新剧集[/yellow]\n\n"
                f"[dim]使用 [cyan]podmate clean --force[/cyan] 确认清理[/dim]",
                title="🧹 podmate clean",
                border_style="yellow",
            )
        )
        return

    deleted_count = 0
    freed_bytes = 0
    for ep in to_delete:
        for subdir in ("episodes", "transcripts", "translations", "dubs"):
            path = _get_data_path(ep.guid, subdir)
            if os.path.isfile(path):
                try:
                    freed_bytes += os.path.getsize(path)
                    os.remove(path)
                except OSError:
                    pass

        delete_episode(ep.id)
        deleted_count += 1

    freed_mb = freed_bytes / 1024 / 1024
    console.print(f"[green]✅ 清理完成: 删除 {deleted_count} 集，释放 {freed_mb:.1f} MB[/green]")


# ── status ────────────────────────────────────────


@app.command()
def status() -> None:
    """显示总体统计信息。"""
    ensure_data_dirs()
    init_db()
    stats = count_stats()

    total_feeds = stats["total_feeds"]
    total_episodes = stats["total_episodes"]
    by_status = stats["by_status"]

    info_lines = [
        f"[bold cyan]📡 已订阅播客:[/bold cyan]  [bold]{total_feeds}[/bold]",
        f"[bold cyan]📻 总剧集数:[/bold cyan]    [bold]{total_episodes}[/bold]",
        "",
        "[bold]剧集状态分布:[/bold]",
    ]

    status_labels = {
        "none": "⏳ 待处理",
        "downloading": "⬇️ 下载中",
        "downloaded": "🟢 已下载",
        "transcribing": "📝 转写中",
        "transcribed": "📝 已转写",
        "translating": "🌐 翻译中",
        "translated": "🌐 已翻译",
        "dubbing": "🎙️ 配音中",
        "dubbed": "🎙️ 已配音",
        "error": "❌ 错误",
    }

    if by_status:
        for s, count in sorted(by_status.items()):
            label = status_labels.get(s, s)
            info_lines.append(f"  • {label}: {count}")
    else:
        info_lines.append("  [dim]暂无剧集[/dim]")

    info_lines.append("")
    info_lines.append("[bold]数据目录:[/bold]")
    for sub in DATA_SUBDIRS:
        subdir = os.path.join(_get_data_dir(), sub)
        file_count = len(os.listdir(subdir)) if os.path.isdir(subdir) else 0
        info_lines.append(f"  • {sub}/: {file_count} 个文件")

    console.print(
        Panel(
            "\n".join(info_lines),
            title=f"📊 PodMate 状态 [dim]v{__version__}[/dim]",
            box=box.ROUNDED,
            border_style="cyan",
        )
    )


# ── read ──────────────────────────────────────────


@app.command()
def read(
    episode_id: int = typer.Argument(..., help="要阅读的剧集 ID"),
) -> None:
    """在终端分页阅读转写文字稿。"""
    ep = get_episode(episode_id)
    if not ep:
        console.print(f"[red]❌ 未找到剧集 ID: {episode_id}[/red]")
        raise typer.Exit(code=1)

    if not ep.transcript_path:
        console.print("[yellow]📝 该剧集尚未转写[/yellow]")
        console.print(
            f"[dim]提示: 先运行 [cyan]podmate episode process {episode_id}[/cyan] 下载并转写[/dim]"
        )
        raise typer.Exit(code=1)

    md_path = Path(ep.transcript_path).with_suffix(".md")

    if md_path.is_file():
        md_content = md_path.read_text()
        with console.pager(styles=True):
            console.print(Markdown(md_content))
    elif Path(ep.transcript_path).is_file():
        console.print("[yellow]📝 文字稿尚未生成 Markdown 版本[/yellow]")
        console.print(
            f"[dim]提示: 重新运行 [cyan]podmate episode process {episode_id}[/cyan]"
            f" 以生成 Markdown 文字稿[/dim]"
        )
        raise typer.Exit(code=1)
    else:
        console.print("[yellow]📝 该剧集尚未转写[/yellow]")
        console.print(
            f"[dim]提示: 先运行 [cyan]podmate episode process {episode_id}[/cyan] 下载并转写[/dim]"
        )
        raise typer.Exit(code=1)


# ── config ────────────────────────────────────────


@app.command()
def config(
    action: str = typer.Argument("show", help="操作: init / show / set"),
    key: str = typer.Argument(None, help="配置键，如 deepgram.api_key（set 时必填）"),
    value: str = typer.Argument(None, help="配置值（set 时必填）"),
) -> None:
    """管理 PodMate 配置。"""
    from .config import init, set_key
    from .config import show as config_show

    if action == "init":
        if init():
            console.print("[green]✅ 配置文件已创建: ~/.config/podmate/config.toml[/green]")
            console.print("[dim]请运行以下命令设置 API key:[/dim]")
            console.print("  [cyan]podmate config set deepgram.api_key 'your_key'[/cyan]")
            console.print("  [cyan]podmate config set deepseek.api_key 'your_key'[/cyan]")
            console.print(
                "[dim]可选 - Podcast Index 获取完整剧集 (https://podcastindex.org):[/dim]"
            )
            console.print("  [cyan]podmate config set podcast_index.api_key 'your_key'[/cyan]")
            console.print(
                "  [cyan]podmate config set podcast_index.api_secret 'your_secret'[/cyan]"
            )
        else:
            console.print("[yellow]配置文件已存在[/yellow]")

    elif action == "show":
        cfg = config_show()
        table = Table(title="PodMate 配置", box=box.ROUNDED)
        table.add_column("模块", style="bold")
        table.add_column("键", style="cyan")
        table.add_column("值")
        for section, values in cfg.items():
            for k, v in values.items():
                table.add_row(section, k, str(v))
        console.print(table)

    elif action == "set":
        if not key or not value:
            console.print("[red]❌ 用法: podmate config set <section.key> <value>[/red]")
            console.print("[dim]示例: podmate config set deepgram.api_key 'your_key'[/dim]")
            raise typer.Exit(code=1)
        if "." not in key:
            console.print("[red]❌ 格式错误，请使用 section.key 格式，如 deepgram.api_key[/red]")
            raise typer.Exit(code=1)
        section, k = key.split(".", 1)
        set_key(section, k, value)
        console.print(f"[green]✅ {section}.{k} 已设置[/green]")

    else:
        console.print(f"[red]❌ 未知操作: {action} (可选: init / show / set)[/red]")
        raise typer.Exit(code=1)


# ═══════════════════════════════════════════════════
# feed 命令组 (内部 helper)
# ═══════════════════════════════════════════════════


def _cmd_feed_add(
    url: str,
    pick: int | None = None,
) -> None:
    """Subscribe to a podcast by RSS URL or keyword search."""
    feed_url: str | None = None
    itunes_id: int | None = None

    if url.startswith("http://") or url.startswith("https://"):
        feed_url = url
    else:
        with console.status(f'[bold green]🔍 正在搜索 "{url}" ...[/bold green]'):
            try:
                results = asyncio.run(search_itunes(url, limit=10))
            except Exception as e:
                console.print(
                    Panel(
                        f"[red]❌ 搜索失败: {e}[/red]",
                        title="错误",
                        border_style="red",
                    )
                )
                raise typer.Exit(code=1)

        if not results:
            console.print(f'[yellow]😕 未找到与 "{url}" 相关的播客[/yellow]')
            console.print("[dim]提示: 尝试使用英文关键词搜索[/dim]")
            raise typer.Exit(code=1)

        _show_search_table(url, results)

        if pick is not None:
            if pick < 1 or pick > len(results):
                console.print(f"[red]❌ 编号超出范围: {pick}，有效范围 1-{len(results)}[/red]")
                raise typer.Exit(code=1)
            idx = pick
        else:
            idx = IntPrompt.ask("请输入编号订阅 (0 取消)")
            if idx == 0:
                console.print("[yellow]已取消[/yellow]")
                raise typer.Exit(code=0)
            if idx < 1 or idx > len(results):
                console.print(f"[red]❌ 编号超出范围: {idx}，有效范围 1-{len(results)}[/red]")
                raise typer.Exit(code=1)

        selected = results[idx - 1]
        feed_url = selected["feedUrl"]
        itunes_id = selected.get("collectionId") or None
        console.print(f"📋 已选择: [bold]{selected['trackName']}[/bold]")

    if not feed_url:
        console.print("[red]❌ 无法获取 RSS 地址[/red]")
        raise typer.Exit(code=1)

    podcast_index: PodcastIndexClient | None = None
    pi_api_key = load_config().get("podcast_index", {}).get("api_key", "")
    pi_api_secret = load_config().get("podcast_index", {}).get("api_secret", "")
    if pi_api_key and pi_api_secret:
        podcast_index = PodcastIndexClient(pi_api_key, pi_api_secret)

    with console.status(f"[bold green]📡 正在解析 {feed_url} ...[/bold green]"):
        try:
            feed_data = asyncio.run(
                resolve_feed(
                    feed_url,
                    itunes_id=itunes_id,
                    podcast_index=podcast_index,
                )
            )
        except Exception as e:
            console.print(
                Panel(
                    f"[red]❌ 解析订阅源失败: {e}[/red]\n\n"
                    f"[dim]请检查 URL 是否正确: {feed_url}[/dim]",
                    title="错误",
                    border_style="red",
                )
            )
            raise typer.Exit(code=1)

    feed_title = feed_data.get("title", "")
    if not feed_title:
        console.print("[red]❌ 无法获取订阅源标题，请检查 URL[/red]")
        raise typer.Exit(code=1)

    episode_source = feed_data.get("episode_source", "rss")
    total_episodes = feed_data.get("total_episodes", 0)

    try:
        feed = add_feed(
            url=feed_url,
            title=feed_title,
            author=feed_data.get("author") or None,
            description=feed_data.get("description") or None,
            image_url=feed_data.get("image_url") or None,
            episode_source=episode_source,
            total_episodes=total_episodes,
            itunes_id=itunes_id,
        )
    except Exception as e:
        console.print(
            Panel(
                f"[red]❌ 存储订阅源失败: {e}[/red]",
                title="错误",
                border_style="red",
            )
        )
        raise typer.Exit(code=1)

    feed_id = feed.id

    episodes = feed_data.get("episodes", [])
    added_count = 0
    with console.status(f"[bold green]📥 正在获取 {len(episodes)} 集信息 ...[/bold green]"):
        for ep in episodes:
            try:
                add_episode(
                    feed_id=feed_id,
                    guid=ep.get("guid", ""),
                    title=ep.get("title", ""),
                    description=ep.get("description"),
                    pub_date=ep.get("pub_date"),
                    audio_url=ep.get("audio_url"),
                    duration_sec=ep.get("duration_sec"),
                )
                added_count += 1
            except Exception:
                pass

    source_labels = {
        "rss": "RSS",
        "podcast-index": "Podcast Index",
        "merged": "RSS + Podcast Index",
    }
    source_label = source_labels.get(episode_source, episode_source)
    ep_list = "\n".join(
        f"  [dim]{i + 1}.[/dim] {ep.get('title', '')[:50]}" for i, ep in enumerate(episodes[:5])
    )
    console.print(
        Panel(
            f"[bold green]✅ 订阅成功![/bold green]\n\n"
            f"[bold cyan]📡 播客名称:[/bold cyan] [bold]{feed_title}[/bold]\n"
            f"[bold cyan]✍️ 作者:[/bold cyan]      {feed_data.get('author', '-')}\n"
            f"[bold cyan]🔗 RSS:[/bold cyan]        [dim]{feed_url}[/dim]\n"
            f"[bold cyan]📻 剧集数:[/bold cyan]    {total_episodes} 集（来源: {source_label}）"
            + (f"\n[bold cyan]🆔 订阅 ID:[/bold cyan]   {feed_id}" if feed_id else "")
            + f"\n\n[bold]已记录 {added_count} 集:[/bold]\n{ep_list or '  [dim](无剧集)[/dim]'}",
            title="podmate feed add",
            border_style="green",
        )
    )


def _cmd_feed_list() -> None:
    """显示已订阅播客列表。"""
    feeds = get_feeds()
    if not feeds:
        console.print("[dim]📭 还没有订阅任何播客[/dim]")
        return
    table = Table(
        title="📡 已订阅播客",
        box=box.ROUNDED,
        header_style="bold cyan",
    )
    table.add_column("ID", style="dim", width=4)
    table.add_column("播客名称", style="bold")
    table.add_column("作者", style="green")
    table.add_column("剧集数", justify="right")
    table.add_column("订阅时间")
    for f in feeds:
        eps = get_episodes(feed_id=f.id, limit=9999)
        table.add_row(
            str(f.id),
            f.title,
            f.author or "-",
            str(len(eps)),
            f.added_at or "-",
        )
    console.print(table)


def _cmd_feed_show(feed_id: int) -> None:
    """查看播客详情与统计。"""
    feed = get_feed(feed_id)
    if not feed:
        console.print(f"[red]❌ 未找到订阅源 ID: {feed_id}[/red]")
        raise typer.Exit(code=1)

    episodes = get_episodes(feed_id=feed_id, limit=9999)

    by_status: dict[str, int] = {}
    for ep in episodes:
        by_status[ep.status] = by_status.get(ep.status, 0) + 1

    lines = [
        f"[bold cyan]📡 播客名称:[/bold cyan] [bold]{feed.title}[/bold]",
        f"[bold cyan]✍️ 作者:[/bold cyan]      {feed.author or '-'}",
        f"[bold cyan]📝 描述:[/bold cyan]      {feed.description or '-'}",
        f"[bold cyan]🔗 RSS:[/bold cyan]        [dim]{feed.url}[/dim]",
        f"[bold cyan]🖼️ 图片:[/bold cyan]      {feed.image_url or '-'}",
        f"[bold cyan]📅 订阅时间:[/bold cyan]  {feed.added_at or '-'}",
        "",
        f"[bold]📊 剧集统计 (共 {len(episodes)} 集):[/bold]",
    ]

    status_labels = {
        "none": "⏳ 待处理",
        "downloading": "⬇️ 下载中",
        "downloaded": "🟢 已下载",
        "transcribing": "📝 转写中",
        "transcribed": "📝 已转写",
        "translating": "🌐 翻译中",
        "translated": "🌐 已翻译",
        "dubbing": "🎙️ 配音中",
        "dubbed": "🎙️ 已配音",
        "error": "❌ 错误",
    }

    if by_status:
        for s, count in sorted(by_status.items()):
            label = status_labels.get(s, s)
            lines.append(f"  • {label}: {count}")
    else:
        lines.append("  [dim]暂无剧集[/dim]")

    recent = get_episodes(feed_id=feed_id, limit=5)
    if recent:
        lines.append("")
        lines.append("[bold]📻 最近剧集:[/bold]")
        for i, ep in enumerate(recent, start=1):
            marks = ""
            if not ep.is_read:
                marks += " 📖"
            if ep.is_starred:
                marks += " ⭐"
            lines.append(
                f"  [dim]{i}.[/dim] {ep.title[:50]}"
                + ("…" if len(ep.title) > 50 else "")
                + f"  [dim]({_status_emoji(ep.status)})[/dim]{marks}"
            )

    console.print(
        Panel(
            "\n".join(lines),
            title=f"📡 播客详情 #{feed_id}",
            border_style="cyan",
        )
    )


def _cmd_feed_remove(feed_id: int, force: bool = False) -> None:
    """取消订阅一个播客。"""
    feed = get_feed(feed_id)
    if not feed:
        console.print(f"[red]❌ 未找到订阅源 ID: {feed_id}[/red]")
        raise typer.Exit(code=1)

    eps = get_episodes(feed_id=feed_id, limit=9999)
    console.print(
        Panel(
            f"[yellow]即将取消订阅: [bold]{feed.title}[/bold][/yellow]\n"
            f"[yellow]作者: {feed.author or '-'}[/yellow]\n"
            f"[yellow]影响 {len(eps)} 集记录[/yellow]\n\n"
            + (
                "[dim]使用 --force 同时删除本地文件[/dim]"
                if not force
                else "[red]将删除所有本地文件[/red]"
            ),
            title="📡 podmate feed remove",
            border_style="yellow",
        )
    )

    if force:
        for ep in eps:
            for subdir in ("episodes", "transcripts", "translations", "dubs"):
                path = _get_data_path(ep.guid, subdir)
                if os.path.isfile(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass
            delete_episode(ep.id)

    delete_feed(feed_id)
    console.print(f"[green]✅ 已取消订阅: {feed.title}[/green]")


def _cmd_feed_refresh(feed_id: int) -> None:
    """刷新已订阅播客的剧集列表（需配置 Podcast Index API）。"""
    feed = get_feed(feed_id)
    if not feed:
        console.print(f"[red]❌ 未找到订阅源 ID: {feed_id}[/red]")
        raise typer.Exit(code=1)

    pi_api_key = load_config().get("podcast_index", {}).get("api_key", "")
    pi_api_secret = load_config().get("podcast_index", {}).get("api_secret", "")
    if not pi_api_key or not pi_api_secret:
        console.print(
            Panel(
                "[yellow]⚠️ 未配置 Podcast Index API 密钥[/yellow]\n\n"
                "[dim]请先配置 PI API 密钥以获取完整剧集列表:[/dim]\n"
                "  [cyan]podmate config set podcast_index.api_key 'your_key'[/cyan]\n"
                "  [cyan]podmate config set podcast_index.api_secret 'your_secret'[/cyan]\n\n"
                "[dim]注册地址: https://podcastindex.org[/dim]",
                title="缺少 API 密钥",
                border_style="yellow",
            )
        )
        raise typer.Exit(code=1)

    podcast_index = PodcastIndexClient(pi_api_key, pi_api_secret)

    before_eps = get_episodes(feed_id=feed_id, limit=99999)
    before_count = len(before_eps)

    with console.status(f"[bold green]📡 正在刷新 {feed.title} ...[/bold green]"):
        try:
            feed_data = asyncio.run(
                resolve_feed(
                    feed.url,
                    itunes_id=feed.itunes_id,
                    podcast_index=podcast_index,
                )
            )
        except Exception as e:
            console.print(
                Panel(
                    f"[red]❌ 刷新失败: {e}[/red]",
                    title="错误",
                    border_style="red",
                )
            )
            raise typer.Exit(code=1)

    episodes = feed_data.get("episodes", [])
    for ep in episodes:
        try:
            add_episode(
                feed_id=feed_id,
                guid=ep.get("guid", ""),
                title=ep.get("title", ""),
                description=ep.get("description"),
                pub_date=ep.get("pub_date"),
                audio_url=ep.get("audio_url"),
                duration_sec=ep.get("duration_sec"),
            )
        except Exception:
            pass

    after_eps = get_episodes(feed_id=feed_id, limit=99999)
    after_count = len(after_eps)
    new_count = after_count - before_count

    episode_source = feed_data.get("episode_source", "rss")
    total_episodes = feed_data.get("total_episodes", after_count)
    conn = get_connection()
    conn.execute(
        "UPDATE feeds SET last_fetched_at = datetime('now'),"
        " episode_source = ?, total_episodes = ? WHERE id = ?",
        (episode_source, total_episodes, feed_id),
    )
    conn.commit()

    source_labels = {
        "rss": "RSS",
        "podcast-index": "Podcast Index",
        "merged": "RSS + Podcast Index",
    }
    source_label = source_labels.get(episode_source, episode_source)

    console.print(
        Panel(
            f"[bold green]✅ 刷新完成![/bold green]\n\n"
            f"[bold cyan]📡 播客:[/bold cyan] [bold]{feed.title}[/bold]\n"
            f"[bold cyan]📻 新增剧集:[/bold cyan] {new_count} 集\n"
            f"[bold cyan]📻 总剧集数:[/bold cyan] {after_count} 集\n"
            f"[bold cyan]📡 数据来源:[/bold cyan] {source_label}",
            title=f"podmate feed refresh #{feed_id}",
            border_style="green",
        )
    )


# Register feed commands
feed_app.command(name="list")(_cmd_feed_list)
feed_app.command(name="show")(_cmd_feed_show)
feed_app.command(name="refresh")(_cmd_feed_refresh)


@feed_app.command(name="add")
def feed_add(
    url: str = typer.Argument(..., help="RSS 订阅地址或播客关键词"),
    pick: int | None = typer.Option(None, "--pick", "-p", help="直接选择搜索结果中的第 N 个"),
) -> None:
    """订阅一个播客。支持 RSS URL 或关键词搜索。"""
    _cmd_feed_add(url, pick)


@feed_app.command(name="remove")
def feed_remove(
    feed_id: int = typer.Argument(..., help="要取消订阅的订阅源 ID"),
    force: bool = typer.Option(False, "--force", help="同时删除所有本地文件"),
) -> None:
    """取消订阅一个播客。"""
    _cmd_feed_remove(feed_id, force)


# ═══════════════════════════════════════════════════
# episode 命令组 (内部 helper)
# ═══════════════════════════════════════════════════


def _cmd_episode_list(
    feed_id: int | None = typer.Option(None, "--feed", "-f", help="按订阅源 ID 筛选剧集"),
    limit: int = typer.Option(20, "--limit", "-n", help="最大显示数量"),
    unread: bool = typer.Option(False, "--unread", help="仅显示未读剧集"),
    starred: bool = typer.Option(False, "--starred", help="仅显示星标剧集"),
) -> None:
    """列出剧集。"""
    episodes = get_episodes(feed_id=feed_id, limit=99999)

    if unread:
        episodes = [ep for ep in episodes if not ep.is_read]
    if starred:
        episodes = [ep for ep in episodes if ep.is_starred]

    episodes = sorted(episodes, key=lambda x: x.id, reverse=True)[:limit]

    if not episodes:
        console.print("[dim]📭 没有符合条件的剧集[/dim]")
        return

    feed_label = ""
    if feed_id:
        feed = get_feed(feed_id)
        feed_label = f" — {feed.title}" if feed else ""

    table = Table(
        title=f"📻 剧集列表{feed_label}",
        box=box.ROUNDED,
        header_style="bold cyan",
    )
    table.add_column("ID", style="dim", width=4)
    table.add_column("标题", style="bold")
    table.add_column("播客", style="green")
    table.add_column("日期")
    table.add_column("时长")
    table.add_column("状态")
    table.add_column("标记")

    for ep in episodes:
        marks = []
        if not ep.is_read:
            marks.append("📖")
        if ep.is_starred:
            marks.append("⭐")
        table.add_row(
            str(ep.id),
            ep.title[:50] + ("…" if len(ep.title) > 50 else ""),
            (ep.feed_title or "-")[:30],
            ep.pub_date or "-",
            _format_duration(ep.duration_sec) if ep.duration_sec else "-",
            _status_emoji(ep.status),
            " ".join(marks),
        )
    console.print(table)


def _episode_show_logic(episode_id_str: str, id_opt: int | None = None) -> None:
    """查看剧集详情的核心逻辑（纯 Python 函数，无 Typer 依赖）。"""
    if id_opt is not None:
        episode_id_int = id_opt
    elif episode_id_str:
        try:
            episode_id_int = int(episode_id_str)
        except ValueError:
            console.print(f"[red]❌ 剧集 ID 必须是数字: {episode_id_str}[/red]")
            raise typer.Exit(code=1)
    else:
        console.print("[red]❌ 请指定剧集 ID[/red]")
        raise typer.Exit(code=1)

    ep = get_episode(episode_id_int)
    if not ep:
        console.print(f"[red]❌ 未找到剧集 ID: {episode_id_int}[/red]")
        raise typer.Exit(code=1)

    lines = [
        f"[bold]{ep.title}[/bold]",
    ]

    if ep.description:
        clean_desc = re.sub(r"<[^>]+>", "", ep.description).strip()
        if clean_desc:
            if len(clean_desc) > 500:
                clean_desc = clean_desc[:500] + "…"
            lines.append("")
            lines.append("[bold]📝 描述:[/bold]")
            lines.append(clean_desc)

    lines += [
        "",
        f"[dim]播客:[/dim] {ep.feed_title or '-'}",
        f"[dim]发布日期:[/dim] {ep.pub_date or '-'}",
        f"[dim]时长:[/dim] {_format_duration(ep.duration_sec) if ep.duration_sec else '-'}",
        f"[dim]状态:[/dim] {_status_label(ep.status)}",
        f"[dim]进度:[/dim] {ep.progress * 100:.0f}%",
        f"[dim]阅读状态:[/dim] {'✅ 已读' if ep.is_read else '📖 未读'}",
        f"[dim]星标:[/dim] {'⭐ 是' if ep.is_starred else '否'}",
    ]

    paths = [
        ("原声音频", ep.local_path),
        ("转写文本 (JSON)", ep.transcript_path),
        (
            "转写文稿 (MD)",
            str(Path(ep.transcript_path).with_suffix(".md")) if ep.transcript_path else None,
        ),
        ("翻译文本", ep.translation_path),
        ("配音音频", ep.dub_path),
    ]
    existing = [(label, p) for label, p in paths if p and os.path.isfile(p)]
    if existing:
        lines.append("")
        lines.append("[bold]📁 本地文件:[/bold]")
        for label, p in existing:
            lines.append(f"  • {label}: [dim]{p}[/dim]")
    else:
        lines.append("")
        lines.append("[dim]📁 暂无本地文件[/dim]")

    if ep.error_message:
        lines.append("")
        lines.append(f"[red]⚠️ 错误: {ep.error_message}[/red]")

    console.print(
        Panel(
            "\n".join(lines),
            title=f"📄 剧集 #{episode_id_int}",
            border_style="cyan",
        )
    )


def _cmd_episode_show(
    episode_id: str = typer.Argument("", help="剧集 ID (正数). 负数请用 --id"),
    id_opt: int = typer.Option(None, "--id", "-i", help="剧集 ID（支持负数）"),
) -> None:
    """查看剧集详情。"""
    _episode_show_logic(episode_id, id_opt)


def _cmd_episode_mark(
    episode_id: str = typer.Argument("", help="剧集 ID (正数). 负数请用 --id"),
    id: int = typer.Option(None, "--id", "-i", help="剧集 ID（支持负数）"),
    read: bool = typer.Option(False, "--read", help="标记为已读"),
    unread: bool = typer.Option(False, "--unread", help="标记为未读"),
    star: bool = typer.Option(False, "--star", help="添加星标"),
    unstar: bool = typer.Option(False, "--unstar", help="取消星标"),
) -> None:
    """标记剧集已读/未读或添加/取消星标。"""
    if id is not None:
        episode_id_int = id
    elif episode_id:
        try:
            episode_id_int = int(episode_id)
        except ValueError:
            console.print(f"[red]❌ 剧集 ID 必须是数字: {episode_id}[/red]")
            raise typer.Exit(code=1)
    else:
        console.print("[red]❌ 请指定剧集 ID[/red]")
        raise typer.Exit(code=1)

    ep = get_episode(episode_id_int)
    if not ep:
        console.print(f"[red]❌ 未找到剧集 ID: {episode_id_int}[/red]")
        raise typer.Exit(code=1)

    messages: list[str] = []
    if read:
        mark_episode_read(episode_id_int, True)
        messages.append("已标记为已读")
    if unread:
        mark_episode_read(episode_id_int, False)
        messages.append("已标记为未读")
    if star:
        mark_episode_starred(episode_id_int, True)
        messages.append("已添加星标")
    if unstar:
        mark_episode_starred(episode_id_int, False)
        messages.append("已取消星标")

    if not messages:
        console.print("[yellow]请指定标记操作: --read / --unread / --star / --unstar[/yellow]")
        raise typer.Exit(code=1)

    title_short = ep.title[:40] + ("…" if len(ep.title) > 40 else "")
    console.print(f"✅ 剧集《{title_short}》{'，'.join(messages)}")


def _cmd_episode_download(
    episode_id: int = typer.Argument(..., help="要下载的剧集 ID"),
) -> None:
    """下载剧集音频。"""
    ep = get_episode(episode_id)
    if not ep:
        console.print(f"[red]❌ 未找到剧集 ID: {episode_id}[/red]")
        raise typer.Exit(code=1)

    if not ep.audio_url:
        console.print(f"[red]❌ 剧集 #{episode_id} 没有音频链接[/red]")
        raise typer.Exit(code=1)

    audio_path = _get_data_path(ep.guid, "episodes")
    os.makedirs(os.path.dirname(audio_path), exist_ok=True)

    if os.path.isfile(audio_path) and os.path.getsize(audio_path) > 1024:
        console.print(f"[green]✅ 音频已存在: {audio_path}[/green]")
        return

    console.print(f"[bold]⬇️ 正在下载:[/bold] [cyan]{ep.title}[/cyan]")
    update_episode_status(episode_id, "downloading", progress=0.0)

    def _dl_cb(done: int, total: int) -> None:
        progress = done / total if total > 0 else 0
        update_episode_status(episode_id, "downloading", progress=progress)

    try:
        asyncio.run(download_episode(ep.audio_url, audio_path, progress_callback=_dl_cb))
    except Exception as e:
        update_episode_status(episode_id, "error", progress=0.0, error_message=str(e))
        console.print(f"[red]❌ 下载失败: {e}[/red]")
        raise typer.Exit(code=1)

    update_episode_status(episode_id, "downloaded", progress=1.0)
    set_episode_path(episode_id, "local_path", audio_path)
    console.print(f"[green]✅ 下载完成: {audio_path}[/green]")


def _cmd_episode_transcribe(
    episode_id: int = typer.Argument(..., help="要转写的剧集 ID"),
) -> None:
    """转写剧集音频（自动下载如果音频不存在）。"""
    ep = get_episode(episode_id)
    if not ep:
        console.print(f"[red]❌ 未找到剧集 ID: {episode_id}[/red]")
        raise typer.Exit(code=1)

    if not ep.audio_url:
        console.print(f"[red]❌ 剧集 #{episode_id} 没有音频链接[/red]")
        raise typer.Exit(code=1)

    audio_path = _get_data_path(ep.guid, "episodes")

    if not os.path.isfile(audio_path) or os.path.getsize(audio_path) <= 1024:
        console.print("[dim]📥 音频不存在，先下载...[/dim]")
        os.makedirs(os.path.dirname(audio_path), exist_ok=True)
        update_episode_status(episode_id, "downloading", progress=0.0)
        try:
            asyncio.run(download_episode(ep.audio_url, audio_path))
        except Exception as e:
            update_episode_status(episode_id, "error", progress=0.0, error_message=str(e))
            console.print(f"[red]❌ 下载失败: {e}[/red]")
            raise typer.Exit(code=1)
        update_episode_status(episode_id, "downloaded", progress=1.0)
        set_episode_path(episode_id, "local_path", audio_path)

    console.print(f"[bold]📝 正在转写:[/bold] [cyan]{ep.title}[/cyan]")
    update_episode_status(episode_id, "transcribing", progress=0.0)

    try:
        result = asyncio.run(transcribe_via_deepgram(audio_path, episode_id=episode_id))
    except Exception as e:
        update_episode_status(episode_id, "error", progress=0.0, error_message=str(e))
        console.print(f"[red]❌ 转写失败: {e}[/red]")
        raise typer.Exit(code=1)

    transcript_path = _get_data_path(ep.guid, "transcripts")
    os.makedirs(os.path.dirname(transcript_path), exist_ok=True)

    with open(transcript_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    md_path = str(Path(transcript_path).with_suffix(".md"))
    md_content = format_transcript(result, title=ep.title)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_content)

    set_episode_path(episode_id, "transcript_path", transcript_path)
    update_episode_status(episode_id, "transcribed", progress=1.0)

    lang = result.get("language", "?")
    seg_count = len(result.get("segments", []))
    console.print(f"[green]✅ 转写完成 ({lang}, {seg_count} 段): {transcript_path}[/green]")


def _cmd_episode_process(
    episode_id: int = typer.Argument(..., help="要处理的剧集 ID"),
    skip_dub: bool = typer.Option(False, "--skip-dub", help="跳过中文配音步骤"),
) -> None:
    """完整流水线：下载 → 转写 → 翻译 → 配音。"""
    from .pipeline import run_pipeline

    ep = get_episode(episode_id)
    if not ep:
        console.print(f"[red]❌ 未找到剧集 ID: {episode_id}[/red]")
        raise typer.Exit(code=1)

    console.print(f"\n[bold]🚀 启动流水线:[/bold] [cyan]{ep.title}[/cyan]\n")

    try:
        result = asyncio.run(
            run_pipeline(
                episode_id,
                skip_dub=skip_dub,
            )
        )

        cbrain_line = "\n📚 已同步到 cbrain 知识库" if result.get("exported_to_cbrain") else ""

        console.print()
        console.print(
            Panel(
                f"[bold green]✅ 全部完成! 剧集 #{episode_id}[/bold green]\n\n"
                f"[bold cyan]▶️ 播放原声:[/bold cyan]     "
                f"[green]podmate play {episode_id}[/green]\n"
                f"[bold cyan]🎙️ 播放配音:[/bold cyan]     "
                f"[green]podmate play {episode_id} --dub[/green]\n"
                f"[bold cyan]📄 查看详情:[/bold cyan]     "
                f"[green]podmate episode show {episode_id}[/green]"
                f"{cbrain_line}",
                title="PodMate 处理完成",
                border_style="green",
            )
        )

    except Exception as e:
        console.print(
            Panel(
                f"[red]❌ 处理失败: {e}[/red]",
                title=f"剧集 #{episode_id} 错误",
                border_style="red",
            )
        )
        raise typer.Exit(code=1)


# Register episode commands on both episode_app and ep_app (alias)
episode_app.command(name="list")(_cmd_episode_list)
episode_app.command(name="show")(_cmd_episode_show)
episode_app.command(name="mark")(_cmd_episode_mark)
episode_app.command(name="download")(_cmd_episode_download)
episode_app.command(name="transcribe")(_cmd_episode_transcribe)
episode_app.command(name="process")(_cmd_episode_process)

ep_app.command(name="list")(_cmd_episode_list)
ep_app.command(name="show")(_cmd_episode_show)
ep_app.command(name="mark")(_cmd_episode_mark)
ep_app.command(name="download")(_cmd_episode_download)
ep_app.command(name="transcribe")(_cmd_episode_transcribe)
ep_app.command(name="process")(_cmd_episode_process)


# ═══════════════════════════════════════════════════
# export 命令组 (内部 helper)
# ═══════════════════════════════════════════════════


def _cmd_export_episode(
    episode_id: str | None = typer.Argument(None, help="剧集 ID"),
    id: int = typer.Option(None, "--id", "-i", help="剧集 ID（支持负数）"),
    output: str = typer.Option("", "--output", "-o", help="目标目录（默认 cbrain 目录）"),
    format: str = typer.Option("md", "--format", "-f", help="导出格式: md 或 json"),
) -> None:
    """导出单集转写稿到 cbrain 知识库。"""
    if id is not None:
        episode_id_int = id
    elif episode_id is not None and episode_id:
        try:
            episode_id_int = int(episode_id)
        except ValueError:
            console.print(f"[red]❌ 剧集 ID 必须是数字: {episode_id}[/red]")
            raise typer.Exit(code=1)
    else:
        console.print("[yellow]请指定剧集 ID[/yellow]")
        console.print("[dim]用法: podmate export episode <episode-id>[/dim]")
        raise typer.Exit(code=1)

    if format not in ("md", "json"):
        console.print(f"[red]❌ 不支持的格式: {format}，支持: md, json[/red]")
        raise typer.Exit(code=1)

    ep = get_episode(episode_id_int)
    if not ep:
        console.print(f"[red]❌ 未找到剧集 ID: {episode_id_int}[/red]")
        raise typer.Exit(code=1)

    if not ep.transcript_path and not ep.translation_path:
        console.print(f"[yellow]📝 剧集 #{episode_id_int} 尚未转写，无法导出[/yellow]")
        console.print(
            f"[dim]提示: 先运行 [cyan]podmate episode process {episode_id_int}[/cyan]"
            f" 下载并转写[/dim]"
        )
        raise typer.Exit(code=1)

    if output:
        dest_dir = Path(os.path.expanduser(output))
    else:
        dest_dir = _get_cbrain_dir()

    dest_dir.mkdir(parents=True, exist_ok=True)

    # 友好文件名: feed_title/guid-safe.md (避免 : 等 Markdown 特殊字符)
    feed_name = ep.feed_title or "podcast"
    slug = _safe_filename(ep.guid)

    if format == "json":
        src = Path(ep.transcript_path or ep.translation_path)
        if not src.is_file():
            console.print(f"[yellow]📝 剧集 #{episode_id_int} 的 JSON 转写稿不存在[/yellow]")
            raise typer.Exit(code=1)
        dest = dest_dir / f"{slug}.json"
        shutil.copy2(src, dest)
        console.print(f"[green]✅ 已导出到: {dest}[/green]")
    else:
        # 导出英文文字稿（如果有）
        en_exported = False
        if ep.transcript_path:
            md_path = Path(ep.transcript_path).with_suffix(".md")
            if md_path.is_file():
                dest = dest_dir / f"{slug}.md"
                _export_with_metadata(md_path, dest, ep)
                en_exported = True

        # 导出中文翻译（如果有）
        if ep.translation_path:
            zh_path = Path(ep.translation_path)
            if zh_path.is_file():
                dest = dest_dir / f"{slug}.zh.md"
                _export_with_metadata(zh_path, dest, ep)
                en_exported = True

        if not en_exported:
            console.print(f"[yellow]📝 剧集 #{episode_id_int} 的 Markdown 文字稿不存在[/yellow]")
            raise typer.Exit(code=1)

        console.print(f"[green]✅ 已导出到: {dest_dir / f'{slug}'}.md/.zh.md[/green]")


def _cmd_export_sync(
    dry_run: bool = typer.Option(False, "--dry-run", help="预览模式，不实际导出"),
    since: str = typer.Option("", "--since", help="只导出指定日期后的剧集 (YYYY-MM-DD)"),
) -> None:
    """批量同步转写稿到 cbrain 知识库。"""
    from .pipeline import _update_podcasts_index

    since_val = since if since else None
    episodes = get_unexported_episodes(since=since_val)

    cbrain_dir = _get_cbrain_dir()
    cbrain_dir.mkdir(parents=True, exist_ok=True)

    if not episodes:
        console.print(r"[dim]\[podmate] 所有转写稿已同步到 cbrain[/dim]")
        return

    if dry_run:
        console.print(f"[bold]🔍 预览模式 — 将导出 [cyan]{len(episodes)}[/cyan] 集:[/bold]\n")
        for ep in episodes:
            md_path = Path(ep.transcript_path).with_suffix(".md") if ep.transcript_path else None
            json_path = Path(ep.transcript_path) if ep.transcript_path else None
            md_status = "✅" if md_path and md_path.is_file() else "❌"
            json_status = "✅" if json_path and json_path.is_file() else "❌"
            console.print(
                f"  [dim]#{ep.id}[/dim] {ep.title[:50]}"
                + ("…" if len(ep.title) > 50 else "")
                + f"  md={md_status} json={json_status}"
            )
        console.print()
        console.print(f"[dim]📊 共 {len(episodes)} 集待导出 (--dry-run 模式，未实际导出)[/dim]")
        return

    exported = 0
    for ep in episodes:
        copied = False
        if ep.transcript_path:
            md_src = Path(ep.transcript_path).with_suffix(".md")
            if md_src.is_file():
                shutil.copy2(md_src, cbrain_dir / md_src.name)
                copied = True
            json_src = Path(ep.transcript_path)
            if json_src.is_file():
                shutil.copy2(json_src, cbrain_dir / json_src.name)
                copied = True
        if copied:
            mark_episode_exported(ep.id)
            exported += 1

    _update_podcasts_index(str(cbrain_dir))
    console.print(f"[dim]📊 已同步 [bold]{exported}[/bold] 集到 cbrain ({cbrain_dir})[/dim]")


def _export_with_metadata(src: Path, dest: Path, ep: Episode) -> None:
    """将文字稿复制到目标路径，并附加剧集元数据头部。"""
    content = src.read_text(encoding="utf-8")

    # 去掉可能已存在的元数据头部（以 --- 围起来的部分）
    lines = content.split("\n")
    if lines and lines[0].strip() == "---":
        end_idx = 1
        while end_idx < len(lines) and lines[end_idx].strip() != "---":
            end_idx += 1
        if end_idx < len(lines):
            content = "\n".join(lines[end_idx + 1:])

    # 构建元数据
    meta_lines = ["---"]
    meta_lines.append(f'title: "{ep.title}"')
    if ep.feed_title:
        meta_lines.append(f'source: "{ep.feed_title}"')
    if ep.pub_date:
        meta_lines.append(f'date: "{ep.pub_date[:10]}"')
    if ep.description:
        # 简短摘录作为 description
        desc_short = _strip_html(ep.description)[:300].replace('"', "'")
        meta_lines.append(f'description: "{desc_short}"')
    meta_lines.append("---")
    meta_lines.append("")

    dest.write_text("\n".join(meta_lines) + content.lstrip(), encoding="utf-8")


def _cmd_export_index() -> None:
    """重建 cbrain podcasts index.md。"""
    from .pipeline import _update_podcasts_index

    cbrain_podcasts = _get_cbrain_dir()
    cbrain_podcasts.mkdir(parents=True, exist_ok=True)
    _update_podcasts_index(str(cbrain_podcasts))
    console.print(f"[green]✅ 索引已重建: {cbrain_podcasts / 'index.md'}[/green]")


# Register export commands
export_app.command(name="episode")(_cmd_export_episode)
export_app.command(name="sync")(_cmd_export_sync)
export_app.command(name="index")(_cmd_export_index)


# ═══════════════════════════════════════════════════
# 废弃命令 (deprecation stubs)
# ═══════════════════════════════════════════════════

_DEPRECATION_MSG = "[yellow]⚠️ 'podmate {old}' 已废弃，请使用 'podmate {new}'[/yellow]"


@app.command(name="show", hidden=True)
def show_deprecated(
    episode_id: int = typer.Argument(..., help="剧集 ID"),
) -> None:
    """[已废弃] 查看剧集详情 — 请使用 episode show。"""
    console.print(_DEPRECATION_MSG.format(old="show", new="episode show"))
    _episode_show_logic(episode_id_str=str(episode_id))


@app.command(name="list", hidden=True)
def list_deprecated(
    feed_id: int | None = typer.Option(None, "--feed", "-f", help="按订阅源 ID 筛选剧集"),
    limit: int = typer.Option(20, "--limit", "-n", help="最大显示数量"),
) -> None:
    """[已废弃] 列出播客或剧集 — 请使用 feed list 或 episode list。"""
    if feed_id is None:
        console.print(_DEPRECATION_MSG.format(old="list", new="feed list"))
        _cmd_feed_list()
    else:
        console.print(_DEPRECATION_MSG.format(old="list --feed", new="episode list --feed"))
        _cmd_episode_list(feed_id=feed_id, limit=limit)


@app.command(name="describe", hidden=True)
def describe_deprecated(
    feed_id: int = typer.Argument(..., help="订阅源 ID"),
) -> None:
    """[已废弃] 查看播客详情 — 请使用 feed show。"""
    console.print(_DEPRECATION_MSG.format(old="describe", new="feed show"))
    _cmd_feed_show(feed_id)


@app.command(name="unsubscribe", hidden=True)
def unsubscribe_deprecated(
    feed_id: int = typer.Argument(..., help="要取消订阅的订阅源 ID"),
    force: bool = typer.Option(False, "--force", help="同时删除所有本地文件"),
) -> None:
    """[已废弃] 取消订阅 — 请使用 feed remove。"""
    console.print(_DEPRECATION_MSG.format(old="unsubscribe", new="feed remove"))
    _cmd_feed_remove(feed_id, force)


@app.command(name="refresh", hidden=True)
def refresh_deprecated(
    feed_id: int = typer.Argument(..., help="要刷新的订阅 ID"),
) -> None:
    """[已废弃] 刷新播客 — 请使用 feed refresh。"""
    console.print(_DEPRECATION_MSG.format(old="refresh", new="feed refresh"))
    _cmd_feed_refresh(feed_id)


@app.command(name="sync-cbrain", hidden=True)
def sync_cbrain_deprecated(
    dry_run: bool = typer.Option(False, "--dry-run", help="预览模式，不实际导出"),
    since: str = typer.Option("", "--since", help="只导出指定日期后的剧集 (YYYY-MM-DD)"),
) -> None:
    """[已废弃] 同步 cbrain — 请使用 export sync。"""
    console.print(_DEPRECATION_MSG.format(old="sync-cbrain", new="export sync"))
    _cmd_export_sync(dry_run=dry_run, since=since)


@app.command(name="search", hidden=True)
def search_deprecated(
    keyword: str = typer.Argument(..., help="搜索关键词"),
) -> None:
    """[已废弃] 搜索转写稿 — 请使用 grep。"""
    console.print(_DEPRECATION_MSG.format(old="search", new="grep"))
    results = _search_transcripts(keyword)

    if not results:
        console.print(f'[yellow]🔍 未找到匹配结果: "{keyword}"[/yellow]')
        return

    total_matches = sum(r["match_count"] for r in results)
    episodes_searched = len(results)

    for r in results:
        console.print(f"\n[bold]📻 {r['feed_title']} → {r['episode_title']}[/bold]")
        console.print(f"  [dim]→ 找到 {r['match_count']} 处匹配[/dim]\n")

        for m in r["snippets"]:
            time_str = _format_time(m["start"])
            console.print(f"  [说话人 {m['speaker']}] [{time_str}] {m['snippet']}")

    console.print(f"\n[dim]共搜索 {episodes_searched} 个剧集，总计 {total_matches} 处匹配[/dim]")


@app.command(name="mark", hidden=True)
def mark_deprecated(
    episode_id: str = typer.Argument("", help="剧集 ID (正数). 负数请用 --id"),
    id: int = typer.Option(None, "--id", "-i", help="剧集 ID（支持负数）"),
    read: bool = typer.Option(False, "--read", help="标记为已读"),
    unread: bool = typer.Option(False, "--unread", help="标记为未读"),
    star: bool = typer.Option(False, "--star", help="添加星标"),
    unstar: bool = typer.Option(False, "--unstar", help="取消星标"),
) -> None:
    """[已废弃] 标记剧集 — 请使用 episode mark。"""
    console.print(_DEPRECATION_MSG.format(old="mark", new="episode mark"))
    _cmd_episode_mark(
        episode_id=episode_id, id=id, read=read, unread=unread, star=star, unstar=unstar
    )


@app.command(name="download", hidden=True)
def download_deprecated(
    episode_id: int = typer.Argument(..., help="要下载和处理的剧集 ID"),
    skip_dub: bool = typer.Option(False, "--skip-dub", help="跳过中文配音步骤"),
) -> None:
    """[已废弃] 下载并处理剧集 — 请使用 episode process。"""
    console.print(_DEPRECATION_MSG.format(old="download", new="episode process"))
    _cmd_episode_process(episode_id=episode_id, skip_dub=skip_dub)
