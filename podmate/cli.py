"""PodMate CLI — 终端里的播客伴侣。"""

import asyncio
import os

import typer
from rich import box
from rich.console import Console
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
    init_db,
)
from .feed import PodcastIndexClient, resolve_feed, search_itunes

DATA_SUBDIRS = ["episodes", "transcripts", "translations", "dubs"]


def _get_data_dir() -> str:
    """Return configured data directory path."""
    return load_config()["storage"]["data_dir"]


def ensure_data_dirs() -> None:
    """确保数据目录存在。"""
    for sub in DATA_SUBDIRS:
        os.makedirs(os.path.join(_get_data_dir(), sub), exist_ok=True)


# ── 控制台 ──────────────────────────────────────────

console = Console()

app = typer.Typer(
    name="podmate",
    help="Podcast 伴侣 — 下载、转写、翻译、配音",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


@app.callback()
def main() -> None:
    """初始化数据目录和数据库。"""
    ensure_data_dirs()
    init_db()


# ── 命令：discover ──────────────────────────────────


@app.command()
def discover(
    keyword: str = typer.Argument(
        ..., help="搜索播客关键词"
    ),
) -> None:
    """搜索并发现播客订阅源。"""
    with console.status(f"[bold green]🔍 正在搜索 \"{keyword}\" ...[/bold green]"):
        try:
            results = asyncio.run(search_itunes(keyword, limit=10))
        except Exception as e:
            console.print(Panel(
                f"[red]❌ 搜索失败: {e}[/red]",
                title="错误",
                border_style="red",
            ))
            raise typer.Exit(code=1)

    if not results:
        console.print(f"[yellow]😕 未找到与 \"{keyword}\" 相关的播客[/yellow]")
        console.print("[dim]提示: 尝试使用英文关键词搜索[/dim]")
        return

    table = Table(
        title=f"📡 iTunes 搜索结果 — \"{keyword}\"",
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


# ── 命令：sub ────────────────────────────────────────


@app.command()
def sub(
    url: str = typer.Argument(
        ..., help="RSS 订阅地址或播客关键词"
    ),
    pick: int | None = typer.Option(
        None, "--pick", "-p",
        help="直接选择搜索结果中的第 N 个"
    ),
) -> None:
    """订阅一个播客。支持 RSS URL 或关键词搜索。"""
    feed_url: str | None = None
    itunes_id: int | None = None

    # URL 模式
    if url.startswith("http://") or url.startswith("https://"):
        feed_url = url
    else:
        # 搜索模式
        with console.status(f"[bold green]🔍 正在搜索 \"{url}\" ...[/bold green]"):
            try:
                results = asyncio.run(search_itunes(url, limit=10))
            except Exception as e:
                console.print(Panel(
                    f"[red]❌ 搜索失败: {e}[/red]",
                    title="错误",
                    border_style="red",
                ))
                raise typer.Exit(code=1)

        if not results:
            console.print(f"[yellow]😕 未找到与 \"{url}\" 相关的播客[/yellow]")
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

    # 构建 PodcastIndexClient（如果配置了 API key）
    podcast_index: PodcastIndexClient | None = None
    pi_api_key = load_config().get("podcast_index", {}).get("api_key", "")
    pi_api_secret = load_config().get("podcast_index", {}).get("api_secret", "")
    if pi_api_key and pi_api_secret:
        podcast_index = PodcastIndexClient(pi_api_key, pi_api_secret)

    # 解析订阅源（RSS + 可选 Podcast Index）
    with console.status(f"[bold green]📡 正在解析 {feed_url} ...[/bold green]"):
        try:
            feed_data = asyncio.run(resolve_feed(
                feed_url,
                itunes_id=itunes_id,
                podcast_index=podcast_index,
            ))
        except Exception as e:
            console.print(Panel(
                f"[red]❌ 解析订阅源失败: {e}[/red]\n\n"
                f"[dim]请检查 URL 是否正确: {feed_url}[/dim]",
                title="错误",
                border_style="red",
            ))
            raise typer.Exit(code=1)

    feed_title = feed_data.get("title", "")
    if not feed_title:
        console.print("[red]❌ 无法获取订阅源标题，请检查 URL[/red]")
        raise typer.Exit(code=1)

    episode_source = feed_data.get("episode_source", "rss")
    total_episodes = feed_data.get("total_episodes", 0)

    # 存入数据库
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
        console.print(Panel(
            f"[red]❌ 存储订阅源失败: {e}[/red]",
            title="错误",
            border_style="red",
        ))
        raise typer.Exit(code=1)

    feed_id = feed.id

    # 存入全部剧集元信息
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

    # 显示成功信息
    source_labels = {
        "rss": "RSS",
        "podcast-index": "Podcast Index",
        "merged": "RSS + Podcast Index",
    }
    source_label = source_labels.get(episode_source, episode_source)
    ep_list = "\n".join(
        f"  [dim]{i+1}.[/dim] {ep.get('title', '')[:50]}"
        for i, ep in enumerate(episodes[:5])
    )
    console.print(Panel(
        f"[bold green]✅ 订阅成功![/bold green]\n\n"
        f"[bold cyan]📡 播客名称:[/bold cyan] [bold]{feed_title}[/bold]\n"
        f"[bold cyan]✍️ 作者:[/bold cyan]      {feed_data.get('author', '-')}\n"
        f"[bold cyan]🔗 RSS:[/bold cyan]        [dim]{feed_url}[/dim]\n"
        f"[bold cyan]📻 剧集数:[/bold cyan]    {total_episodes} 集（来源: {source_label}）"
        + (f"\n[bold cyan]🆔 订阅 ID:[/bold cyan]   {feed_id}" if feed_id else "")
        + f"\n\n[bold]已记录 {added_count} 集:[/bold]\n{ep_list or '  [dim](无剧集)[/dim]'}",
        title="podmate sub",
        border_style="green",
    ))


# ── 命令：refresh ────────────────────────────────────


@app.command()
def refresh(
    feed_id: int = typer.Argument(
        ..., help="要刷新的订阅 ID"
    ),
) -> None:
    """刷新已订阅播客的剧集列表（需配置 Podcast Index API）。"""
    feed = get_feed(feed_id)
    if not feed:
        console.print(f"[red]❌ 未找到订阅源 ID: {feed_id}[/red]")
        raise typer.Exit(code=1)

    pi_api_key = load_config().get("podcast_index", {}).get("api_key", "")
    pi_api_secret = load_config().get("podcast_index", {}).get("api_secret", "")
    if not pi_api_key or not pi_api_secret:
        console.print(Panel(
            "[yellow]⚠️ 未配置 Podcast Index API 密钥[/yellow]\n\n"
            "[dim]请先配置 PI API 密钥以获取完整剧集列表:[/dim]\n"
            "  [cyan]podmate config set podcast_index.api_key 'your_key'[/cyan]\n"
            "  [cyan]podmate config set podcast_index.api_secret 'your_secret'[/cyan]\n\n"
            "[dim]注册地址: https://podcastindex.org[/dim]",
            title="缺少 API 密钥",
            border_style="yellow",
        ))
        raise typer.Exit(code=1)

    podcast_index = PodcastIndexClient(pi_api_key, pi_api_secret)

    before_eps = get_episodes(feed_id=feed_id, limit=99999)
    before_count = len(before_eps)

    with console.status(f"[bold green]📡 正在刷新 {feed.title} ...[/bold green]"):
        try:
            feed_data = asyncio.run(resolve_feed(
                feed.url,
                itunes_id=feed.itunes_id,
                podcast_index=podcast_index,
            ))
        except Exception as e:
            console.print(Panel(
                f"[red]❌ 刷新失败: {e}[/red]",
                title="错误",
                border_style="red",
            ))
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

    console.print(Panel(
        f"[bold green]✅ 刷新完成![/bold green]\n\n"
        f"[bold cyan]📡 播客:[/bold cyan] [bold]{feed.title}[/bold]\n"
        f"[bold cyan]📻 新增剧集:[/bold cyan] {new_count} 集\n"
        f"[bold cyan]📻 总剧集数:[/bold cyan] {after_count} 集\n"
        f"[bold cyan]📡 数据来源:[/bold cyan] {source_label}",
        title=f"podmate refresh #{feed_id}",
        border_style="green",
    ))


# ── 命令：unsubscribe ────────────────────────────────


@app.command()
def unsubscribe(
    feed_id: int = typer.Argument(
        ..., help="要取消订阅的订阅源 ID"
    ),
    force: bool = typer.Option(
        False, "--force",
        help="同时删除所有本地文件"
    ),
) -> None:
    """取消订阅一个播客。"""
    from .db import delete_episode, get_episodes, get_feed

    feed = get_feed(feed_id)
    if not feed:
        console.print(f"[red]❌ 未找到订阅源 ID: {feed_id}[/red]")
        raise typer.Exit(code=1)

    eps = get_episodes(feed_id=feed_id, limit=9999)
    console.print(Panel(
        f"[yellow]即将取消订阅: [bold]{feed.title}[/bold][/yellow]\n"
        f"[yellow]作者: {feed.author or '-'}[/yellow]\n"
        f"[yellow]影响 {len(eps)} 集记录[/yellow]\n\n"
        + ("[dim]使用 --force 同时删除本地文件[/dim]" if not force
           else "[red]将删除所有本地文件[/red]"),
        title="📡 podmate unsubscribe",
        border_style="yellow",
    ))

    if force:
        for ep in eps:
            for subdir in ("episodes", "transcripts", "translations", "dubs"):
                ext = ".mp3" if subdir in ("episodes", "dubs") else ".json"
                path = os.path.join(_get_data_dir(), subdir, f"{ep.guid}{ext}")
                if os.path.isfile(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass
            delete_episode(ep.id)

    delete_feed(feed_id)
    console.print(f"[green]✅ 已取消订阅: {feed.title}[/green]")


# ── 命令：list ───────────────────────────────────────


@app.command(name="list")
def list_episodes(
    feed_id: int | None = typer.Option(
        None, "--feed", "-f",
        help="按订阅源 ID 筛选剧集"
    ),
    limit: int = typer.Option(
        20, "--limit", "-n",
        help="最大显示数量"
    ),
) -> None:
    """列出已订阅播客或指定播客的剧集。"""
    if feed_id is None:
        # 默认：显示已订阅播客列表
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
    else:
        # --feed <id>：显示该播客的剧集列表
        feed = get_feed(feed_id)
        if not feed:
            console.print(f"[red]❌ 未找到订阅源 ID: {feed_id}[/red]")
            raise typer.Exit(code=1)

        episodes = get_episodes(feed_id=feed_id, limit=limit)
        if not episodes:
            console.print(f"[dim]📭 \"{feed.title}\" 还没有剧集[/dim]")
            return

        table = Table(
            title=f"📻 {feed.title} — 剧集列表",
            box=box.ROUNDED,
            header_style="bold cyan",
        )
        table.add_column("ID", style="dim", width=4)
        table.add_column("标题", style="bold")
        table.add_column("日期")
        table.add_column("时长")
        table.add_column("状态")

        for ep in episodes:
            table.add_row(
                str(ep.id),
                ep.title[:50] + ("…" if len(ep.title) > 50 else ""),
                ep.pub_date or "-",
                _format_duration(ep.duration_sec) if ep.duration_sec else "-",
                _status_emoji(ep.status),
            )
        console.print(table)


# ── 命令：describe ───────────────────────────────────


@app.command()
def describe(
    feed_id: int = typer.Argument(
        ..., help="订阅源 ID"
    ),
) -> None:
    """查看播客详情与统计。"""
    feed = get_feed(feed_id)
    if not feed:
        console.print(f"[red]❌ 未找到订阅源 ID: {feed_id}[/red]")
        raise typer.Exit(code=1)

    episodes = get_episodes(feed_id=feed_id, limit=9999)

    # 计算各状态统计
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

    # 最近 5 集
    recent = get_episodes(feed_id=feed_id, limit=5)
    if recent:
        lines.append("")
        lines.append("[bold]📻 最近剧集:[/bold]")
        for i, ep in enumerate(recent, start=1):
            lines.append(
                f"  [dim]{i}.[/dim] {ep.title[:50]}"
                + ("…" if len(ep.title) > 50 else "")
                + f"  [dim]({_status_emoji(ep.status)})[/dim]"
            )

    console.print(Panel(
        "\n".join(lines),
        title=f"📡 播客详情 #{feed_id}",
        border_style="cyan",
    ))


# ── 命令：episode ────────────────────────────────────


@app.command()
def episode(
    episode_id: int = typer.Argument(
        ..., help="剧集 ID"
    ),
) -> None:
    """查看剧集详情。"""
    ep = get_episode(episode_id)
    if not ep:
        console.print(f"[red]❌ 未找到剧集 ID: {episode_id}[/red]")
        raise typer.Exit(code=1)

    lines = [
        f"[bold]{ep.title}[/bold]",
        "",
        f"[dim]播客:[/dim] {ep.feed_title or '-'}",
        f"[dim]发布日期:[/dim] {ep.pub_date or '-'}",
        f"[dim]时长:[/dim] {_format_duration(ep.duration_sec) if ep.duration_sec else '-'}",
        f"[dim]状态:[/dim] {_status_label(ep.status)}",
        f"[dim]进度:[/dim] {ep.progress * 100:.0f}%",
    ]

    paths = [
        ("原声音频", ep.local_path),
        ("转写文本", ep.transcript_path),
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

    console.print(Panel(
        "\n".join(lines),
        title=f"📄 剧集 #{episode_id}",
        border_style="cyan",
    ))


# ── 命令：show ────────────────────────────────────────


@app.command()
def show(
    episode_id: int = typer.Argument(
        ..., help="剧集 ID"
    ),
) -> None:
    """查看剧集详情。"""
    ep = get_episode(episode_id)
    if not ep:
        console.print(f"[red]❌ 未找到剧集 ID: {episode_id}[/red]")
        raise typer.Exit(code=1)

    console.print(
        Panel(
            f"[bold]{ep.title}[/bold]\n\n"
            f"[dim]播客:[/dim] {ep.feed_title or '-'}\n"
            f"[dim]状态:[/dim] {_status_label(ep.status)}\n"
            f"[dim]GUID:[/dim] {ep.guid}\n"
            f"[dim]发布时间:[/dim] {ep.pub_date or '-'}\n"
            f"[dim]时长:[/dim] {_format_duration(ep.duration_sec) if ep.duration_sec else '-'}\n"
            f"[dim]本地文件:[/dim] {ep.local_path or '-'}\n"
            f"[dim]进度:[/dim] {ep.progress * 100:.0f}%\n"
            f"[dim]错误信息:[/dim] {ep.error_message or '无'}",
            title=f"📄 剧集 #{episode_id}",
        )
    )


# ── 命令：download ────────────────────────────────────


@app.command()
def download(
    episode_id: int = typer.Argument(
        ..., help="要下载和处理的剧集 ID"
    ),
    skip_dub: bool = typer.Option(
        False, "--skip-dub", help="跳过中文配音步骤"
    ),
) -> None:
    """下载剧集音频，然后转写、翻译、配音。"""
    from .pipeline import run_pipeline

    ep = get_episode(episode_id)
    if not ep:
        console.print(f"[red]❌ 未找到剧集 ID: {episode_id}[/red]")
        raise typer.Exit(code=1)

    console.print(f"\n[bold]🚀 启动流水线:[/bold] [cyan]{ep.title}[/cyan]\n")

    try:
        asyncio.run(run_pipeline(
            episode_id,
            skip_dub=skip_dub,
        ))

        console.print()
        console.print(Panel(
            f"[bold green]✅ 全部完成! 剧集 #{episode_id}[/bold green]\n\n"
            f"[bold cyan]▶️ 播放原声:[/bold cyan]     "
            f"[green]podmate play {episode_id}[/green]\n"
            f"[bold cyan]🎙️ 播放配音:[/bold cyan]     "
            f"[green]podmate play {episode_id} --dub[/green]\n"
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


# ── 命令：play ────────────────────────────────────────


@app.command()
def play(
    episode_id: int = typer.Argument(
        ..., help="要播放的剧集 ID"
    ),
    dub: bool = typer.Option(
        False, "--dub", "-d",
        help="播放中文配音而非原声"
    ),
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
            console.print(Panel(
                f"[yellow]🎙️ 中文配音还不存在，请先运行:\n"
                f"   [cyan]podmate download {episode_id}[/cyan][/yellow]\n\n"
                f"[dim]当前配音设置: {DUB_VOICE}[/dim]",
                title=f"剧集 #{episode_id}",
                border_style="yellow",
            ))
            raise typer.Exit(code=1)
        mode_label = "🎙️ 中文配音"
    else:
        file_path = _get_data_path(ep.guid, "episodes")
        if not os.path.isfile(file_path):
            console.print(Panel(
                f"[yellow]🔊 音频还不存在，请先运行:\n"
                f"   [cyan]podmate download {episode_id}[/cyan][/yellow]",
                title=f"剧集 #{episode_id}",
                border_style="yellow",
            ))
            raise typer.Exit(code=1)
        mode_label = "🔊 原声"

    from .player import get_available_player

    player = get_available_player()
    if player is None:
        console.print("[red]❌ 未找到可用的播放器。[/red]")
        console.print("[yellow]💡 安装 mpv: [cyan]sudo apt install mpv[/cyan][/yellow]")
        raise typer.Exit(code=1)

    console.print(Panel(
        f"[bold cyan]{mode_label}: {ep.title}[/bold cyan]\n"
        f"[dim]播放器: {player}[/dim]\n"
        f"[dim]文件: {file_path}[/dim]\n\n"
        f"[green]▶️ 正在播放 ...[/green]\n"
        f"[yellow]按 Ctrl+C 停止播放[/yellow]",
        title=f"剧集 #{episode_id}",
    ))

    try:
        from .player import play_file
        play_file(file_path)
    except KeyboardInterrupt:
        console.print("\n[yellow]⏹️  播放结束[/yellow]")
    except Exception as e:
        console.print(f"[red]❌ 播放失败: {e}[/red]")
        raise typer.Exit(code=1)


# ── 命令：clean ────────────────────────────────────────


@app.command()
def clean(
    keep: int = typer.Option(
        5, "--keep", "-k",
        help="保留最近几集（按 ID 倒序）"
    ),
    force: bool = typer.Option(
        False, "--force",
        help="直接清理，不确认"
    ),
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
        console.print(Panel(
            f"[yellow]即将清理 [bold]{len(to_delete)}[/bold] 集旧剧集[/yellow]\n"
            f"[yellow]释放空间: [bold]{size_mb:.1f} MB[/bold][/yellow]\n"
            f"[yellow]保留: [bold]{keep}[/bold] 集最新剧集[/yellow]\n\n"
            f"[dim]使用 [cyan]podmate clean --force[/cyan] 确认清理[/dim]",
            title="🧹 podmate clean",
            border_style="yellow",
        ))
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


# ── 命令：status ──────────────────────────────────────


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

    # 数据目录大小统计
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


# ── 命令：config ──────────────────────────────────────


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


# ── 辅助函数 ──────────────────────────────────────────


def _get_data_path(guid: str, subdir: str) -> str:
    """返回 data/{subdir}/{guid}.json 或 data/{subdir}/{guid}.mp3 的完整路径。"""
    ext = ".mp3" if subdir in ("episodes", "dubs") else ".json"
    return os.path.join(_get_data_dir(), subdir, f"{guid}{ext}")


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


def _show_search_table(keyword: str, results: list) -> None:
    """显示 iTunes 搜索结果表格。"""
    table = Table(
        title=f"📡 iTunes 搜索结果 — \"{keyword}\"",
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
