"""存储层 + 服务层修复回归（N3/N10/N19/N7/N15/N17）。"""
from __future__ import annotations

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import app_service
from app_service import AppService
from downloader import Downloader
from emby_client import EmbyClient
from library import CacheLibrary, redact_url
from models import MediaItem


def _client(cfg):
    c = EmbyClient(cfg)
    c.authenticate()
    return c


# ---------- N3 token 剥离 ----------

def test_redact_url():
    assert "SECRET" not in redact_url("http://h/Items/1/Download?api_key=SECRET&static=true")
    assert "static=true" in redact_url("http://h/Items/1/Download?api_key=SECRET&static=true")
    assert redact_url("http://h/x?access_token=TOK") == "http://h/x"
    assert redact_url("http://h/x") == "http://h/x"
    assert redact_url(None) is None
    assert redact_url("") == ""


def test_upsert_redacts_source_url(cfg, library):
    item = MediaItem(item_id="a", name="A", type="Movie")
    library.upsert(item, Path("a.mp4"), "completed", 1, 1,
                   "http://h/Items/a/Download?api_key=SECRET&MediaSourceId=ms")
    rec = library.get("a")
    assert "SECRET" not in (rec.source_url or "")
    assert "MediaSourceId=ms" in rec.source_url


def test_legacy_rows_redacted_on_init(cfg):
    lib = CacheLibrary(cfg.database_path)
    item = MediaItem(item_id="a", name="A", type="Movie")
    lib.upsert(item, Path("a.mp4"), "completed", 1, 1, "http://h/x")
    conn = sqlite3.connect(cfg.database_path)
    conn.execute("UPDATE downloads SET source_url=? WHERE item_id=?",
                 ("http://h/Items/a/Download?api_key=LEGACY_SECRET", "a"))
    conn.commit()
    conn.close()
    lib2 = CacheLibrary(cfg.database_path)  # 重新初始化触发清洗
    assert "LEGACY_SECRET" not in (lib2.get("a").source_url or "")


def test_record_to_dict_redacts(cfg, library):
    item = MediaItem(item_id="a", name="A", type="Movie")
    library.upsert(item, Path("a.mp4"), "completed", 1, 1, "http://h/x?api_key=S")
    # 即便 DB 被外部塞入 token，序列化层也兜底剥离
    conn = sqlite3.connect(cfg.database_path)
    conn.execute("UPDATE downloads SET source_url=? WHERE item_id=?", ("http://h/x?api_key=S2", "a"))
    conn.commit()
    conn.close()
    d = AppService._record_to_dict(library.get("a"))
    assert "S2" not in (d["source_url"] or "")


# ---------- N10 WAL + 并发 ----------

def test_wal_mode(cfg, library):
    conn = sqlite3.connect(cfg.database_path)
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    assert mode.lower() == "wal"


def test_concurrent_upserts(cfg, library):
    def worker(n):
        item = MediaItem(item_id=f"item{n}", name=f"N{n}", type="Movie")
        for i in range(40):
            library.upsert(item, Path(f"{n}.mp4"), "downloading", i, 40, None)

    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(worker, range(8)))
    assert len(list(library.list_all())) == 8
    assert library.get("item3").bytes_downloaded == 39


# ---------- N19 启动对账 ----------

def test_reconcile_interrupted(cfg, library):
    for iid, status in (("a", "downloading"), ("b", "paused"), ("c", "completed"), ("d", "downloading")):
        library.upsert(MediaItem(item_id=iid, name=iid, type="Movie"), Path(f"{iid}.mp4"), status, 0, None, None)
    n = library.reconcile_interrupted()
    assert n == 2
    assert library.get("a").status == "interrupted"
    assert library.get("d").status == "interrupted"
    assert library.get("b").status == "paused"
    assert library.get("c").status == "completed"


# ---------- N7 批量容错 ----------

def _service(cfg, library) -> AppService:
    svc = AppService()
    svc.config = cfg
    svc.client = _client(cfg)
    svc.library = library
    svc.downloader = Downloader(svc.client, library, progress_callback=svc._download_progress, segments=1)
    return svc


def test_batch_continues_after_item_failure(cfg, library, mock_emby, monkeypatch):
    mock_emby.add_movie("e1", "Ep1", size=50_000)
    mock_emby.add_movie("e2", "Ep2", size=50_000)
    mock_emby.add_movie("e3", "Ep3", size=50_000)
    items = [MediaItem(item_id=i, name=mock_emby.details[i]["Name"], type="Movie", container="mp4")
             for i in ("e1", "e2", "e3")]
    del mock_emby.files["e2"]
    del mock_emby.details["e2"]  # e2 详情 404 → 所有候选失败
    svc = _service(cfg, library)
    monkeypatch.setattr(svc.client, "expand_download_items", lambda iid: items)
    svc.start_download("series")
    svc._worker.join(timeout=30)
    assert library.get("e1").status == "completed"
    assert library.get("e3").status == "completed"
    assert library.get("e2").status != "completed"
    assert any("成功 2 / 失败 1" in line for line in svc.logs(500))


# ---------- N15 删除进行中记录 ----------

def test_delete_active_record_cancels(cfg, library, mock_emby):
    mock_emby.add_movie("act", "ActiveMovie", size=200_000, mode="slow", delay=1.5)
    svc = _service(cfg, library)
    svc.start_download("act")
    time.sleep(0.4)  # 等待 starting 事件写入 active item_id
    assert svc.current_status()["item_id"] == "act"
    svc.delete_record("act")
    assert library.get("act") is None
    time.sleep(0.3)
    assert library.get("act") is None  # 无复活


# ---------- N17 跨平台打开 ----------

def test_open_path_dispatch(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(app_service.subprocess, "Popen", lambda args, *a, **k: calls.append(list(args)))
    monkeypatch.setattr(app_service.os, "startfile", lambda p: calls.append(("startfile", str(p))), raising=False)
    p = tmp_path / "x.mp4"

    monkeypatch.setattr(app_service.sys, "platform", "linux")
    monkeypatch.delenv("TERMUX_VERSION", raising=False)
    app_service._open_path(p)
    assert calls[-1] == ["xdg-open", str(p)]

    monkeypatch.setenv("TERMUX_VERSION", "0.118")
    app_service._open_path(p)
    assert calls[-1] == ["termux-open", str(p)]

    monkeypatch.setattr(app_service.sys, "platform", "darwin")
    app_service._open_path(p)
    assert calls[-1] == ["open", str(p)]

    monkeypatch.setattr(app_service.sys, "platform", "win32")
    app_service._open_path(p)
    assert calls[-1] == ("startfile", str(p))
