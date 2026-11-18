"""下载队列：整季并发、重复准入与任务级目录隔离。"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from app_service import AppService
from downloader import Downloader
from emby_client import EmbyClient
from models import MediaItem


TERMINAL = {"completed", "completed_with_errors", "failed", "cancelled"}


def _service(cfg, library) -> AppService:
    service = AppService()
    service.config = cfg
    service.client = EmbyClient(cfg)
    service.client.authenticate()
    service.library = library
    service.downloader = Downloader(service.client, library, segments=1)
    return service


def _wait_jobs(service, expected, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        jobs = service.list_jobs()
        if len(jobs) >= expected and all(job["status"] in TERMINAL for job in jobs[-expected:]):
            return jobs
        time.sleep(0.05)
    raise AssertionError(f"任务未及时结束：{service.list_jobs()}")


def _item(item_id):
    return MediaItem(item_id=item_id, name=f"Movie {item_id}", type="Movie", container="mp4", year=2020)


def test_season_items_run_in_worker_pool(cfg, library, mock_emby, monkeypatch):
    cfg.download_workers = 2
    for item_id in ("q1", "q2"):
        mock_emby.add_movie(item_id, f"Movie {item_id}", size=80_000)
    items = [_item("q1"), _item("q2")]
    service = _service(cfg, library)
    monkeypatch.setattr(service.client, "expand_download_items", lambda _item_id: items)

    barrier = threading.Barrier(2)
    lock = threading.Lock()
    active = 0
    max_active = 0
    original = Downloader.download_item

    def concurrent_download(self, item, force_restart=False):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        try:
            barrier.wait(timeout=8)
            return original(self, item, force_restart=force_restart)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(Downloader, "download_item", concurrent_download)
    service.start_download("season")
    jobs = _wait_jobs(service, 2)

    assert max_active == 2
    assert library.get("q1").status == "completed"
    assert library.get("q2").status == "completed"
    assert sum(job["status"] == "completed" for job in jobs) == 2


def test_parallel_jobs_isolate_override_directory(cfg, library, mock_emby, tmp_path):
    cfg.download_workers = 2
    for item_id in ("d1", "d2"):
        mock_emby.add_movie(item_id, f"Movie {item_id}", size=60_000)
    service = _service(cfg, library)
    override = tmp_path / "override"
    barrier = threading.Barrier(2)
    original = Downloader.download_item

    def synchronized_download(self, item, force_restart=False):
        barrier.wait(timeout=8)
        return original(self, item, force_restart=force_restart)

    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(Downloader, "download_item", synchronized_download)
    try:
        service.start_download("d1", override_dir=override)
        service.start_download("d2")
        _wait_jobs(service, 2)
    finally:
        monkeypatch.undo()

    assert library.get("d1").status == "completed"
    assert library.get("d2").status == "completed"
    assert Path(library.get("d1").target_path).is_relative_to(override)
    assert Path(library.get("d2").target_path).is_relative_to(cfg.download_dir)
