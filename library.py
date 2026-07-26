from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

from models import DownloadRecord, MediaItem


class CacheLibrary:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

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
            conn.commit()

    def get(self, item_id: str) -> DownloadRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT item_id, item_name, item_type, target_path, status, bytes_downloaded, expected_size, download_mode, source_url FROM downloads WHERE item_id = ?",
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
    ) -> None:
        with self._connect() as conn:
            current_mode = conn.execute(
                "SELECT download_mode FROM downloads WHERE item_id = ?",
                (item.item_id,),
            ).fetchone()
            resolved_mode = download_mode if download_mode is not None else (current_mode[0] if current_mode else None)
            conn.execute(
                """
                INSERT INTO downloads (
                    item_id, item_name, item_type, target_path, status, bytes_downloaded, expected_size, download_mode, source_url, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(item_id) DO UPDATE SET
                    item_name = excluded.item_name,
                    item_type = excluded.item_type,
                    target_path = excluded.target_path,
                    status = excluded.status,
                    bytes_downloaded = excluded.bytes_downloaded,
                    expected_size = excluded.expected_size,
                    download_mode = excluded.download_mode,
                    source_url = excluded.source_url,
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
                ),
            )
            conn.commit()

    def list_all(self) -> Iterable[DownloadRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT item_id, item_name, item_type, target_path, status, bytes_downloaded, expected_size, download_mode, source_url FROM downloads ORDER BY rowid DESC"
            ).fetchall()
        return [DownloadRecord(*row) for row in rows]

    def delete(self, item_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM downloads WHERE item_id = ?", (item_id,))
            conn.commit()

    def list_incomplete(self) -> Iterable[DownloadRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT item_id, item_name, item_type, target_path, status, bytes_downloaded, expected_size, download_mode, source_url FROM downloads WHERE status NOT IN ('completed', 'cancelled') ORDER BY rowid DESC"
            ).fetchall()
        return [DownloadRecord(*row) for row in rows]
