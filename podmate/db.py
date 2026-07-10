"""PodMate SQLite 数据层。"""

import os
import sqlite3
from typing import Any

from .config import load as load_config
from .models import Episode, Feed

# ── 数据库路径 ──────────────────────────────────────

DB_DIR = os.path.expanduser(load_config()["storage"]["data_dir"])
DB_PATH = os.path.join(DB_DIR, "feeds.db")

_conn: sqlite3.Connection | None = None


def get_connection() -> sqlite3.Connection:
    """获取数据库连接（单例）。"""
    global _conn
    if _conn is None:
        os.makedirs(DB_DIR, exist_ok=True)
        _conn = sqlite3.connect(DB_PATH)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
    return _conn


def init_db() -> None:
    """创建表（如果不存在）。"""
    conn = get_connection()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS feeds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            url TEXT UNIQUE NOT NULL,
            author TEXT,
            description TEXT,
            image_url TEXT,
            added_at TEXT DEFAULT (datetime('now')),
            last_fetched_at TEXT
        );

        CREATE TABLE IF NOT EXISTS episodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            feed_id INTEGER NOT NULL REFERENCES feeds(id),
            guid TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            pub_date TEXT,
            audio_url TEXT,
            duration_sec INTEGER,
            local_path TEXT,
            transcript_path TEXT,
            translation_path TEXT,
            dub_path TEXT,
            status TEXT DEFAULT 'none',
            progress REAL DEFAULT 0,
            error_message TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_episodes_feed_id ON episodes(feed_id);
        CREATE INDEX IF NOT EXISTS idx_episodes_guid ON episodes(guid);
        CREATE INDEX IF NOT EXISTS idx_episodes_status ON episodes(status);
    """)
    conn.commit()

    # 兼容旧数据库：尝试添加可能缺失的列
    _add_column_if_missing(conn, "episodes", "transcript_path", "TEXT")
    _add_column_if_missing(conn, "episodes", "translation_path", "TEXT")
    _add_column_if_missing(conn, "episodes", "dub_path", "TEXT")
    _add_column_if_missing(conn, "feeds", "episode_source", "TEXT DEFAULT 'rss'")
    _add_column_if_missing(conn, "feeds", "total_episodes", "INTEGER DEFAULT 0")
    _add_column_if_missing(conn, "feeds", "itunes_id", "INTEGER")
    _add_column_if_missing(conn, "episodes", "is_read", "INTEGER DEFAULT 0")
    _add_column_if_missing(conn, "episodes", "is_starred", "INTEGER DEFAULT 0")
    _add_column_if_missing(conn, "episodes", "exported_to_cbrain", "INTEGER DEFAULT 0")

    # 为 episodes.guid 添加 UNIQUE 约束（替换旧的非唯一索引）
    conn.execute("DROP INDEX IF EXISTS idx_episodes_guid")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_episodes_guid_unique ON episodes(guid)")
    conn.commit()


# ── Feeds ──────────────────────────────────────────────


def add_feed(
    url: str,
    title: str,
    author: str | None = None,
    description: str | None = None,
    image_url: str | None = None,
    episode_source: str = "rss",
    total_episodes: int = 0,
    itunes_id: int | None = None,
) -> Feed:
    """添加订阅源。如果 URL 已存在则忽略。"""
    conn = get_connection()
    conn.execute(
        """INSERT OR IGNORE INTO feeds
               (url, title, author, description, image_url,
                episode_source, total_episodes, itunes_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (url, title, author, description, image_url, episode_source, total_episodes, itunes_id),
    )
    conn.commit()
    return get_feed_by_url(url)


def get_feed_by_url(url: str) -> Feed | None:
    """按 URL 查询单个订阅源。"""
    conn = get_connection()
    row = conn.execute("SELECT * FROM feeds WHERE url = ?", (url,)).fetchone()
    return _row_to_feed(row) if row else None


def get_feed(feed_id: int) -> Feed | None:
    """按 ID 查询单个订阅源。"""
    conn = get_connection()
    row = conn.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()
    return _row_to_feed(row) if row else None


def get_feeds() -> list[Feed]:
    """列出所有订阅源。"""
    conn = get_connection()
    rows = conn.execute("SELECT * FROM feeds ORDER BY added_at DESC").fetchall()
    return [_row_to_feed(r) for r in rows]


def search_feeds(keyword: str) -> list[Feed]:
    """按标题搜索订阅源。"""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM feeds WHERE title LIKE ? ORDER BY added_at DESC",
        (f"%{keyword}%",),
    ).fetchall()
    return [_row_to_feed(r) for r in rows]


def delete_feed(feed_id: int) -> bool:
    """删除订阅源及其所有剧集。返回 True 如果删除成功。"""
    conn = get_connection()
    # 先删除关联剧集
    conn.execute("DELETE FROM episodes WHERE feed_id = ?", (feed_id,))
    cur = conn.execute("DELETE FROM feeds WHERE id = ?", (feed_id,))
    conn.commit()
    return cur.rowcount > 0


# ── Episodes ───────────────────────────────────────────


def add_episode(
    feed_id: int,
    guid: str,
    title: str,
    description: str | None = None,
    pub_date: str | None = None,
    audio_url: str | None = None,
    duration_sec: int | None = None,
) -> Episode:
    """添加剧集。如果 guid 已存在则忽略。"""
    conn = get_connection()
    conn.execute(
        """INSERT OR IGNORE INTO episodes
               (feed_id, guid, title, description, pub_date, audio_url, duration_sec)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (feed_id, guid, title, description, pub_date, audio_url, duration_sec),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM episodes WHERE feed_id = ? AND guid = ?",
        (feed_id, guid),
    ).fetchone()
    return _row_to_episode(row)


def get_episodes(
    feed_id: int | None = None, status: str | None = None, limit: int = 20, offset: int = 0
) -> list[Episode]:
    """列出剧集，可选按订阅源/状态筛选。"""
    conn = get_connection()
    conditions: list[str] = []
    params: list[Any] = []

    if feed_id is not None:
        conditions.append("e.feed_id = ?")
        params.append(feed_id)
    if status is not None:
        conditions.append("e.status = ?")
        params.append(status)

    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    sql = f"""SELECT e.*, f.title AS feed_title
              FROM episodes e
              LEFT JOIN feeds f ON e.feed_id = f.id
              {where}
              ORDER BY e.created_at DESC
              LIMIT ? OFFSET ?"""
    params.extend([limit, offset])
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_episode(r) for r in rows]


def get_episode(episode_id: int) -> Episode | None:
    """按 ID 查询单集。"""
    conn = get_connection()
    row = conn.execute(
        """SELECT e.*, f.title AS feed_title
           FROM episodes e
           LEFT JOIN feeds f ON e.feed_id = f.id
           WHERE e.id = ?""",
        (episode_id,),
    ).fetchone()
    return _row_to_episode(row) if row else None


def update_episode_status(
    episode_id: int, status: str, progress: float | None = None, error_message: str | None = None
) -> None:
    """更新剧集状态。"""
    conn = get_connection()
    sets = ["status = ?"]
    params: list[Any] = [status]
    if progress is not None:
        sets.append("progress = ?")
        params.append(progress)
    if error_message is not None:
        sets.append("error_message = ?")
        params.append(error_message)
    params.append(episode_id)
    conn.execute(f"UPDATE episodes SET {', '.join(sets)} WHERE id = ?", params)
    conn.commit()


def set_episode_path(episode_id: int, field: str, path: str) -> None:
    """设置剧集文件路径字段。

    field 必须是: local_path, transcript_path, translation_path, dub_path
    """
    allowed = {"local_path", "transcript_path", "translation_path", "dub_path"}
    if field not in allowed:
        raise ValueError(f"未知路径字段: {field}，允许: {allowed}")
    conn = get_connection()
    conn.execute(f"UPDATE episodes SET {field} = ? WHERE id = ?", (path, episode_id))
    conn.commit()


def search_episodes(keyword: str) -> list[Episode]:
    """按标题搜索剧集。"""
    conn = get_connection()
    rows = conn.execute(
        """SELECT e.*, f.title AS feed_title
           FROM episodes e
           LEFT JOIN feeds f ON e.feed_id = f.id
           WHERE e.title LIKE ?
           ORDER BY e.created_at DESC""",
        (f"%{keyword}%",),
    ).fetchall()
    return [_row_to_episode(r) for r in rows]


def delete_episode(episode_id: int) -> bool:
    """删除剧集记录。返回 True 如果删除成功。

    注意：不会自动删除磁盘上的文件，调用方应自行清理。
    """
    conn = get_connection()
    cur = conn.execute("DELETE FROM episodes WHERE id = ?", (episode_id,))
    conn.commit()
    return cur.rowcount > 0


def mark_episode_read(episode_id: int, read: bool = True) -> None:
    """标记剧集为已读或未读。"""
    conn = get_connection()
    conn.execute(
        "UPDATE episodes SET is_read = ? WHERE id = ?",
        (1 if read else 0, episode_id),
    )
    conn.commit()


def mark_episode_starred(episode_id: int, starred: bool = True) -> None:
    """添加或取消星标。"""
    conn = get_connection()
    conn.execute(
        "UPDATE episodes SET is_starred = ? WHERE id = ?",
        (1 if starred else 0, episode_id),
    )
    conn.commit()


def get_unexported_episodes(since: str | None = None) -> list[Episode]:
    """Return episodes with transcripts that haven't been exported to cbrain."""
    conn = get_connection()
    base_sql = (
        "SELECT e.*, f.title AS feed_title FROM episodes e "
        "LEFT JOIN feeds f ON e.feed_id = f.id "
        "WHERE e.transcript_path IS NOT NULL "
        "AND (e.exported_to_cbrain IS NULL OR e.exported_to_cbrain = 0) "
        "AND e.status IN ('transcribed', 'translated', 'error') "
    )
    params: list[Any] = []
    if since:
        base_sql += "AND e.created_at >= ? "
        params.append(since)
    base_sql += "ORDER BY e.created_at DESC"
    rows = conn.execute(base_sql, params).fetchall()
    return [_row_to_episode(r) for r in rows]


def mark_episode_exported(episode_id: int) -> None:
    """Mark an episode as exported to cbrain."""
    conn = get_connection()
    conn.execute(
        "UPDATE episodes SET exported_to_cbrain = 1 WHERE id = ?",
        (episode_id,),
    )
    conn.commit()


# ── Stats ──────────────────────────────────────────────


def count_stats() -> dict[str, Any]:
    """返回统计数据。"""
    conn = get_connection()
    total_feeds = conn.execute("SELECT COUNT(*) FROM feeds").fetchone()[0]
    total_episodes = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]

    status_counts = conn.execute(
        """SELECT status, COUNT(*) as cnt FROM episodes GROUP BY status"""
    ).fetchall()
    by_status = {row["status"]: row["cnt"] for row in status_counts}

    return {
        "total_feeds": total_feeds,
        "total_episodes": total_episodes,
        "by_status": by_status,
    }


def auto_vacuum() -> None:
    """执行 VACUUM 回收空间。"""
    conn = get_connection()
    conn.execute("VACUUM")
    conn.commit()


# ── Internal helpers ──────────────────────────────────


def _row_to_feed(row: sqlite3.Row) -> Feed:
    keys = row.keys()
    return Feed(
        id=row["id"],
        title=row["title"],
        url=row["url"],
        author=row["author"],
        description=row["description"],
        image_url=row["image_url"],
        added_at=row["added_at"],
        last_fetched_at=row["last_fetched_at"],
        episode_source=row["episode_source"] if "episode_source" in keys else "rss",
        total_episodes=row["total_episodes"] if "total_episodes" in keys else 0,
        itunes_id=row["itunes_id"] if "itunes_id" in keys else None,
    )


def _row_to_episode(row: sqlite3.Row) -> Episode:
    keys = row.keys()
    feed_title = row["feed_title"] if "feed_title" in keys else None
    return Episode(
        id=row["id"],
        feed_id=row["feed_id"],
        guid=row["guid"],
        title=row["title"],
        description=row["description"],
        pub_date=row["pub_date"],
        audio_url=row["audio_url"],
        duration_sec=row["duration_sec"],
        local_path=row["local_path"],
        transcript_path=row["transcript_path"] if "transcript_path" in keys else None,
        translation_path=row["translation_path"] if "translation_path" in keys else None,
        dub_path=row["dub_path"] if "dub_path" in keys else None,
        status=row["status"],
        progress=row["progress"],
        error_message=row["error_message"],
        created_at=row["created_at"],
        feed_title=feed_title,
        is_read=bool(row["is_read"]) if "is_read" in keys else False,
        is_starred=bool(row["is_starred"]) if "is_starred" in keys else False,
        exported_to_cbrain=(
            bool(row["exported_to_cbrain"]) if "exported_to_cbrain" in keys else False
        ),
    )


def _add_column_if_missing(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    col_type: str,
) -> None:
    """安全地添加列（如果不存在）。"""
    cursor = conn.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in cursor.fetchall()}
    if column not in existing:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # 列已存在，忽略错误
