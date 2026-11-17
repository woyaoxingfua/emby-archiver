from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from models import DownloadRecord, MediaItem


# N3：source_url 中可能携带 api_key/access_token 等长期凭据，存储/序列化前一律剥离
_SENSITIVE_QUERY_KEYS = {"api_key", "apikey", "access_token", "token", "x-emby-token"}


def redact_url(url: str | None) -> str | None:
    if not url:
        return url
    try:
        parts = urlsplit(url)
        if not parts.query:
            return url
        kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                if k.lower() not in _SENSITIVE_QUERY_KEYS]
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment))
    except ValueError:
        return url


class CacheLibrary:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._local = threading.local()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        # N10：每线程复用单连接 + WAL + busy_timeout，避免每操作新建连接与写锁竞争
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=5.0)
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS downloads (
                    item_id TEXT PRIMARY KEY,
                    item_name TEXT NOT NULL,
                    item_type TEXT NOT NULL,
                    target_path TEXT NOT NULL,
                    status TEXT NOT NULL,
                    bytes_downloaded INTEGER NOT NULL DEFAULT 0,
                    expected_size INTEGER,
                    download_mode TEXT,
                    source_url TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(downloads)").fetchall()
            }
            if "download_mode" not in columns:
                conn.execute("ALTER TABLE downloads ADD COLUMN download_mode TEXT")
            if "etag" not in columns:
                conn.execute("ALTER TABLE downloads ADD COLUMN etag TEXT")
            # N10：WAL 模式（持久化到 DB 文件），读写并发更友好
            conn.execute("PRAGMA journal_mode=WAL")
            # N3：清洗历史记录中残留的 URL 凭据（幂等）
            rows = conn.execute(
                "SELECT item_id, source_url FROM downloads WHERE source_url IS NOT NULL"
            ).fetchall()
            for item_id, surl in rows:
                clean = redact_url(surl)
                if clean != surl:
                    conn.execute(
                        "UPDATE downloads SET source_url=? WHERE item_id=?", (clean, item_id)
                    )
            conn.commit()

    def reconcile_interrupted(self) -> int:
        # N19：启动对账——把进程崩溃残留的 downloading 僵尸记录标记为 interrupted
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE downloads SET status='interrupted', updated_at=CURRENT_TIMESTAMP "
                "WHERE status='downloading'"
            )
            conn.commit()
            return cur.rowcount

    def get(self, item_id: str) -> DownloadRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT item_id, item_name, item_type, target_path, status, bytes_downloaded, expected_size, download_mode, source_url, etag FROM downloads WHERE item_id = ?",
                (item_id,),
            ).fetchone()
        if not row:
            return None
        return DownloadRecord(*row)

    def upsert(
        self,
        item: MediaItem,
        target_path: Path,
        status: str,
        bytes_downloaded: int,
        expected_size: int | None,
        source_url: str | None,
        download_mode: str | None = None,
        etag: str | None = None,
    ) -> None:
        source_url = redact_url(source_url)
        with self._connect() as conn:
            current = conn.execute(
                "SELECT download_mode, etag FROM downloads WHERE item_id = ?",
                (item.item_id,),
            ).fetchone()
            resolved_mode = download_mode if download_mode is not None else (current[0] if current else None)
            resolved_etag = etag if etag is not None else (current[1] if current else None)
            conn.execute(
                """
                INSERT INTO downloads (
                    item_id, item_name, item_type, target_path, status, bytes_downloaded, expected_size, download_mode, source_url, etag, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(item_id) DO UPDATE SET
                    item_name = excluded.item_name,
                    item_type = excluded.item_type,
                    target_path = excluded.target_path,
                    status = excluded.status,
                    bytes_downloaded = excluded.bytes_downloaded,
                    expected_size = excluded.expected_size,
                    download_mode = excluded.download_mode,
                    source_url = excluded.source_url,
                    etag = excluded.etag,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    item.item_id,
                    item.name,
                    item.type,
                    str(target_path),
                    status,
                    bytes_downloaded,
                    expected_size,
                    resolved_mode,
                    source_url,
                    resolved_etag,
                ),
            )
            conn.commit()

    def list_all(self) -> Iterable[DownloadRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT item_id, item_name, item_type, target_path, status, bytes_downloaded, expected_size, download_mode, source_url, etag FROM downloads ORDER BY rowid DESC"
            ).fetchall()
        return [DownloadRecord(*row) for row in rows]

    def delete(self, item_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM downloads WHERE item_id = ?", (item_id,))
            conn.commit()

    def list_incomplete(self) -> Iterable[DownloadRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT item_id, item_name, item_type, target_path, status, bytes_downloaded, expected_size, download_mode, source_url, etag FROM downloads WHERE status NOT IN ('completed', 'cancelled') ORDER BY rowid DESC"
            ).fetchall()
        return [DownloadRecord(*row) for row in rows]
