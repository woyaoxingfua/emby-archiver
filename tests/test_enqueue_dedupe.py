"""入队判重（DupeCheck）：历史已 completed 的条目不再入队，入队响应显式报告。

判重键 = Emby item_id（天然稳定）。语义优先级：force_restart > 判重 > 队内重复拒绝。
"""
from __future__ import annotations

import time

import pytest

from app_service import AppService
from downloader import Downloader
from emby_client import EmbyClient
from models import MediaItem


def _item(item_id):
    return MediaItem(item_id=item_id, name=f"Movie {item_id}", type="Movie", container="mp4", year=2020)


def _service(cfg, library) -> AppService:
    service = AppService()
    service.config = cfg
    service.client = EmbyClient(cfg)
    service.client.authenticate()
    service.library = library
    service.downloader = Downloader(service.client, library, segments=1)
    return service


def _wait_terminal(service, item_ids, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with service._state_lock:
            jobs = [job for job in service._jobs.values() if job.item.item_id in set(item_ids)]
            if jobs and all(
                job.status in {"completed", "failed", "cancelled"} and job.done.is_set() for job in jobs
            ):
                return jobs
        time.sleep(0.05)
    raise AssertionError(f"任务未及时结束：{service.list_jobs()}")


def _download_one(cfg, library, mock_emby, item_id, noop_progress):
    """预置：完整下载一个条目，使其历史 status == completed。"""
    mock_emby.add_movie(item_id, f"Movie {item_id}", size=50_000)
    client = EmbyClient(cfg)
    client.authenticate()
    downloader = Downloader(client, library, progress_callback=noop_progress, segments=1)
    downloader.download_item(_item(item_id))
    assert library.get(item_id).status == "completed"


def test_enqueue_skips_completed_items(cfg, library, mock_emby, noop_progress):
    _download_one(cfg, library, mock_emby, "old", noop_progress)
    mock_emby.add_movie("new", "Movie new", size=50_000)
    service = _service(cfg, library)
    service.client.expand_download_items = lambda _iid: [_item("old"), _item("new")]

    result = service.start_download("season")

    assert result["total"] == 2
    assert result["queued"] == 1
    assert result["skipped_completed"] == ["old"]
    assert result["batch_id"] is not None
    _wait_terminal(service, {"new"})
    # old 没有新 job；new 正常完成
    with service._state_lock:
        item_ids = {job.item.item_id for job in service._jobs.values()}
    assert "old" not in item_ids
    assert library.get("new").status == "completed"


def test_enqueue_all_completed_no_empty_batch(cfg, library, mock_emby, noop_progress):
    _download_one(cfg, library, mock_emby, "done", noop_progress)
    service = _service(cfg, library)
    service.client.expand_download_items = lambda _iid: [_item("done")]
    batches_before = len(service._batches)

    result = service.start_download("done")

    assert result["queued"] == 0
    assert result["batch_id"] is None
    assert result["skipped_completed"] == ["done"]
    # 不创建空批次（无 total=0 的悬空计数）
    assert len(service._batches) == batches_before
    assert any("已缓存完成、本次跳过" in line for line in service.logs(100))


def test_force_restart_overrides_dedupe(cfg, library, mock_emby, noop_progress):
    _download_one(cfg, library, mock_emby, "again", noop_progress)
    service = _service(cfg, library)
    service.client.expand_download_items = lambda _iid: [_item("again")]

    result = service.start_download("again", force_restart=True)

    assert result["queued"] == 1
    assert result["skipped_completed"] == []  # 强制重下语义优先，不做判重
    _wait_terminal(service, {"again"})


def test_queue_busy_rejection_unchanged(cfg, library, mock_emby, monkeypatch):
    """队内 queued/running 重复入队拒绝保持现状（另一层防护，与判重正交）。"""
    import threading as _threading

    cfg.download_workers = 1
    mock_emby.add_movie("busy", "Movie busy", size=50_000)
    service = _service(cfg, library)
    gate = _threading.Event()
    original = Downloader.download_item

    def gated(self, item, force_restart=False):
        if item.item_id == "busy":
            gate.wait(timeout=15)
        return original(self, item, force_restart=force_restart)

    monkeypatch.setattr(Downloader, "download_item", gated)
    service.client.expand_download_items = lambda _iid: [_item("busy")]
    service.start_download("busy")

    with pytest.raises(RuntimeError, match="已在下载队列中"):
        service.start_download("busy")
    gate.set()
    _wait_terminal(service, {"busy"})


def test_web_response_reports_skipped(cfg, library, mock_emby, noop_progress):
    from webapp import create_web_app
    from starlette.testclient import TestClient

    _download_one(cfg, library, mock_emby, "dup", noop_progress)
    service = _service(cfg, library)
    service.client.expand_download_items = lambda _iid: [_item("dup")]
    app = create_web_app(service, allowed_hosts={"testserver"}, token="probe-token")
    client = TestClient(app)

    resp = client.post(
        "/api/download",
        json={"item_id": "dup"},
        headers={"x-auth-token": "probe-token"},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["queued"] == 0
    assert data["skipped_completed"] == ["dup"]
    assert "batch_id" in data
