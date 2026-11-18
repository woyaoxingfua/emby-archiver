"""磁盘空间护栏：磁盘不足失败后自动取消剩余排队任务，不逐个空转。

契约（NZBGet DiskSpace 模式的队列级实现）：
- 某任务因「磁盘空间不足」失败时，仍为 queued 的任务全部置 cancelled，
  并写全局日志告知清理空间后可重新下载；
- 批次汇总计数正确（取消数计入 cancelled）；
- 其他错误（网络/权限）不触发该护栏；
- 被取消任务的断点（.part 等）不被清理，可续传。
"""
from __future__ import annotations

import threading
import time

import pytest

from app_service import AppService
from downloader import DownloadError, Downloader
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


def _wait_condition(cond, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with cond.lock if hasattr(cond, "lock") else threading.Lock():
            pass
        if cond():
            return
        time.sleep(0.05)
    raise AssertionError("条件未在超时内满足")


def test_disk_full_cancels_remaining_queued(cfg, library, mock_emby, monkeypatch):
    """第一个任务磁盘不足失败 → 其余 queued 全部自动取消。"""
    cfg.download_workers = 1
    for item_id in ("d1", "d2", "d3"):
        mock_emby.add_movie(item_id, f"Movie {item_id}", size=50_000)
    service = _service(cfg, library)
    service.client.expand_download_items = lambda _iid: [_item(i) for i in ("d1", "d2", "d3")]

    # d1 开始时抛磁盘不足（_stream_single 前的路径上抛即可）
    def fake_download(self, item, force_restart=False):
        raise DownloadError("磁盘空间不足：需要约 100 字节，剩余 10 字节（目录 X）")

    monkeypatch.setattr(Downloader, "download_item", fake_download)
    service.start_download("d1")

    def all_settled():
        jobs = service.list_jobs()
        statuses = {job["item_id"]: job["status"] for job in jobs}
        return (
            statuses.get("d1") == "failed"
            and statuses.get("d2") == "cancelled"
            and statuses.get("d3") == "cancelled"
        )

    _wait_condition(all_settled)

    logs = "\n".join(service.logs(200))
    assert "磁盘空间不足：已自动取消 2 个排队任务" in logs
    # 被自动取消的任务带说明性 error
    jobs = {job["item_id"]: job for job in service.list_jobs()}
    assert "因磁盘空间不足自动取消" in (jobs["d2"]["error"] or "")


def test_other_errors_do_not_trigger_guard(cfg, library, mock_emby, monkeypatch):
    """普通网络错误只失败本任务，不影响 queued（现有语义回归）。"""
    cfg.download_workers = 1
    for item_id in ("n1", "n2"):
        mock_emby.add_movie(item_id, f"Movie {item_id}", size=50_000)
    service = _service(cfg, library)
    service.client.expand_download_items = lambda _iid: [_item(i) for i in ("n1", "n2")]

    def fake_download(self, item, force_restart=False):
        if item.item_id == "n1":
            raise DownloadError("HTTP 500: boom")
        return original(self, item, force_restart=force_restart)

    original = Downloader.download_item
    monkeypatch.setattr(Downloader, "download_item", fake_download)
    service.start_download("n1")

    def settled():
        jobs = {job["item_id"]: job["status"] for job in service.list_jobs()}
        return jobs.get("n1") == "failed" and jobs.get("n2") in {"completed", "failed", "cancelled"}

    _wait_condition(settled)
    # 普通错误不触发护栏：n2 不被自动取消，走真实下载直到终态
    def n2_settled():
        jobs = {job["item_id"]: job["status"] for job in service.list_jobs()}
        return jobs.get("n2") == "completed"

    _wait_condition(n2_settled)
    jobs = {job["item_id"]: job["status"] for job in service.list_jobs()}
    assert jobs["n2"] == "completed"  # n2 走真实下载正常完成


def test_disk_cancelled_counts_in_batch_summary(cfg, library, mock_emby, monkeypatch):
    """护栏取消计入批次 cancelled 计数，汇总日志正确。"""
    cfg.download_workers = 1
    for item_id in ("s1", "s2"):
        mock_emby.add_movie(item_id, f"Movie {item_id}", size=50_000)
    service = _service(cfg, library)
    service.client.expand_download_items = lambda _iid: [_item(i) for i in ("s1", "s2")]

    def fake_download(self, item, force_restart=False):
        raise DownloadError("磁盘空间不足：需要约 100 字节，剩余 1 字节（目录 X）")

    monkeypatch.setattr(Downloader, "download_item", fake_download)
    service.start_download("s1")

    def done():
        return len(service.logs(200)) > 0 and any(
            "批量下载完成：成功 0 / 失败 1 / 跳过 0 / 取消 1" in line for line in service.logs(200)
        )

    _wait_condition(done)
    assert any("批量下载完成：成功 0 / 失败 1 / 跳过 0 / 取消 1" in line for line in service.logs(200))
