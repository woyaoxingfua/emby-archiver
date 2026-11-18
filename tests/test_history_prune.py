"""长运行历史清理：终态 job/batch/item 锁有界保留（job_history_limit）。

覆盖交接要求的「大量终态任务 + 活动任务 + 删除/取消竞态」组合：
- 基本有界性：批次完成后 _jobs/_batches/_item_locks 均被清理，磁盘记录不受影响；
- 活动任务保护：running 任务、其条目锁与其批次在超限清理下保留；
- 0=不清理的旧行为兼容；
- 已取消 queued 任务的 job id 残留在队列时，worker 取出不得 KeyError；
- 超限终态任务下 delete_record 的取消+等待 done+删除协议不破坏；
- 跨批次清理不吞掉后续批次的汇总日志。
"""
from __future__ import annotations

import threading
import time

import pytest

from app_service import AppService
from downloader import Downloader
from emby_client import EmbyClient
from models import MediaItem


def _service(cfg, library) -> AppService:
    service = AppService()
    service.config = cfg
    service.client = EmbyClient(cfg)
    service.client.authenticate()
    service.library = library
    service.downloader = Downloader(service.client, library, segments=1)
    return service


def _item(item_id):
    return MediaItem(item_id=item_id, name=f"Movie {item_id}", type="Movie", container="mp4", year=2020)


def _wait_terminal(service, job_ids, timeout=20):
    """等待给定 job id 全部终态且 done 已置位（含从 _jobs 被清理的情形）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with service._state_lock:
            jobs = [service._jobs.get(jid) for jid in job_ids]
            if all(job is None or (job.status in {"completed", "failed", "cancelled"} and job.done.is_set()) for job in jobs):
                return
        time.sleep(0.05)
    with service._state_lock:
        state = {jid: getattr(service._jobs.get(jid), "status", "<pruned>") for jid in job_ids}
    raise AssertionError(f"任务未及时结束：{state}")


def _wait_record(library, item_id, status, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = library.get(item_id)
        if record is not None and record.status == status:
            return record
        time.sleep(0.05)
    raise AssertionError(f"记录未达到 {status}：{library.get(item_id)}")


def _enqueue(service, mock_emby, item_ids, **kwargs):
    """注册 mock 影片并把这一批 item 作为一个批次入队，返回本批 job id。"""
    for item_id in item_ids:
        mock_emby.add_movie(item_id, f"Movie {item_id}", size=40_000)
    service.client.expand_download_items = lambda _item_id: [_item(i) for i in item_ids]
    service.start_download(item_ids[0], **kwargs)
    with service._state_lock:
        return [
            job.job_id for job in service._jobs.values()
            if job.item.item_id in set(item_ids)
        ]


def test_prune_bounded_after_batch_completes(cfg, library, mock_emby):
    cfg.job_history_limit = 3
    cfg.download_workers = 2
    service = _service(cfg, library)
    ids1 = _enqueue(service, mock_emby, [f"a{i}" for i in range(4)])
    ids2 = _enqueue(service, mock_emby, [f"b{i}" for i in range(4)])
    _wait_terminal(service, ids1 + ids2)

    with service._state_lock:
        # 终态条目收敛到上限附近；总数 = 终态保留 + 活动条目（此刻应为 0）
        terminal = [job for job in service._jobs.values() if job.status in {"completed", "failed", "cancelled"}]
        assert len(terminal) <= cfg.job_history_limit
        # 两个批次都已无 job 引用，计数与条目锁随之清空
        assert service._batches == {}
        assert service._item_locks == {}
    # 磁盘下载记录不受内存清理影响
    for item_id in [f"a{i}" for i in range(4)] + [f"b{i}" for i in range(4)]:
        assert library.get(item_id).status == "completed"


def test_prune_keeps_running_job_and_its_lock(cfg, library, mock_emby, monkeypatch):
    cfg.job_history_limit = 2
    cfg.download_workers = 2
    service = _service(cfg, library)
    release = threading.Event()
    started = threading.Event()
    original = Downloader.download_item

    def blocking_download(self, item, force_restart=False):
        if item.item_id == "hold":
            started.set()
            release.wait(timeout=15)
        return original(self, item, force_restart=force_restart)

    monkeypatch.setattr(Downloader, "download_item", blocking_download)
    mock_emby.add_movie("hold", "Movie hold", size=40_000)
    service.client.expand_download_items = lambda _item_id: [_item("hold")]
    service.start_download("hold")
    assert started.wait(timeout=10)

    # running 任务挂起期间，再入队一批会立即完成的终态任务，触发超限清理
    ids = _enqueue(service, mock_emby, [f"p{i}" for i in range(4)])
    _wait_terminal(service, ids)

    with service._state_lock:
        hold_job = next(job for job in service._jobs.values() if job.item.item_id == "hold")
        assert hold_job.status == "running"
        assert "hold" in service._item_locks  # 活动条目锁保留
        assert hold_job.batch_id in service._batches  # 活动批次保留
        hold_job_id = hold_job.job_id
    release.set()
    _wait_terminal(service, [hold_job_id])
    assert library.get("hold").status == "completed"


def test_prune_zero_disables_cleanup(cfg, library, mock_emby):
    cfg.job_history_limit = 0
    service = _service(cfg, library)
    ids = _enqueue(service, mock_emby, [f"z{i}" for i in range(4)])
    _wait_terminal(service, ids)
    with service._state_lock:
        assert len(service._jobs) == 4
        assert len(service._batches) == 1
        assert len(service._item_locks) == 4  # 旧行为：锁保留


def test_cancelled_queued_job_ids_drain_without_worker_error(cfg, library, mock_emby, monkeypatch):
    cfg.job_history_limit = 1
    cfg.download_workers = 1
    service = _service(cfg, library)
    gate = threading.Event()
    original = Downloader.download_item

    def gated_download(self, item, force_restart=False):
        if item.item_id == "gate":
            gate.wait(timeout=15)
        return original(self, item, force_restart=force_restart)

    monkeypatch.setattr(Downloader, "download_item", gated_download)
    mock_emby.add_movie("gate", "Movie gate", size=40_000)
    service.client.expand_download_items = lambda _item_id: [_item("gate")]
    service.start_download("gate")

    # 占住唯一 worker 后，入队一批 queued 任务再全部取消
    ids = _enqueue(service, mock_emby, [f"x{i}" for i in range(3)])
    service.cancel_current()  # x 任务变 cancelled 且 done 置位；gate 是 running 不受影响

    # 手动触发一次清理，把已取消的 x 任务清出 _jobs（保留 1 条终态是设计行为），
    # 但它们的 job id 仍残留在 _job_queue 中等待 worker 取出
    with service._state_lock:
        service._prune_history_locked()
        assert not any(
            job.item.item_id in {"x0", "x1", "x2"} and job.status == "queued"
            for job in service._jobs.values()
        )

    # 放行 gate，让 worker 继续消费队列里已清理的 x 任务 id——不得 KeyError
    gate.set()
    _enqueue(service, mock_emby, ["fresh"])
    _wait_record(library, "fresh", "completed")


def test_delete_active_record_survives_prune(cfg, library, mock_emby, monkeypatch):
    cfg.job_history_limit = 2
    cfg.download_workers = 2
    service = _service(cfg, library)
    release = threading.Event()
    started = threading.Event()
    original = Downloader.download_item

    def blocking_download(self, item, force_restart=False):
        if item.item_id == "act":
            started.set()
            release.wait(timeout=15)
        return original(self, item, force_restart=force_restart)

    monkeypatch.setattr(Downloader, "download_item", blocking_download)
    mock_emby.add_movie("act", "Movie act", size=40_000)
    service.client.expand_download_items = lambda _item_id: [_item("act")]
    service.start_download("act")
    assert started.wait(timeout=10)
    # delete_record 以磁盘记录存在为前提；下载被阻塞时先写入一条 downloading 记录
    library.upsert(
        _item("act"),
        cfg.download_dir / "Movie act.mp4",
        "downloading",
        0,
        40_000,
        source_url=None,
    )

    # 制造大量终态历史，使清理持续发生
    ids = _enqueue(service, mock_emby, [f"h{i}" for i in range(5)])
    _wait_terminal(service, ids)

    # 另起线程调用 delete_record（running 未停止时它会等待），随后放行下载
    result = {}

    def _delete():
        try:
            result["value"] = service.delete_record("act")
        except Exception as exc:  # noqa: BLE001
            result["error"] = exc

    thread = threading.Thread(target=_delete)
    thread.start()
    time.sleep(0.3)  # 让 delete_record 走到等待 done 的位置
    release.set()
    thread.join(timeout=20)
    assert not thread.is_alive()
    assert "error" not in result, result.get("error")

    assert library.get("act") is None
    with service._state_lock:
        assert all(job.item.item_id != "act" or job.status != "running" for job in service._jobs.values())


def test_batch_summary_survives_cross_batch_prune(cfg, library, mock_emby):
    cfg.job_history_limit = 2
    cfg.download_workers = 1
    service = _service(cfg, library)

    # 批次 1：4 项全部完成，终态条目将被清到只剩 2 条
    ids1 = _enqueue(service, mock_emby, [f"s{i}" for i in range(4)])
    _wait_terminal(service, ids1)

    # 批次 2：同一批次内 1 成功 + 1 失败（bad 不注册详情 → 所有候选 404 失败）
    mock_emby.add_movie("ok", "Movie ok", size=40_000)
    service.client.expand_download_items = lambda _item_id: [_item("ok"), _item("bad")]
    service.start_download("ok")
    ids2 = [
        job.job_id for job in list(service._jobs.values()) if job.item.item_id in {"ok", "bad"}
    ]
    _wait_terminal(service, ids2)
    _wait_record(library, "ok", "completed")

    logs_text = "\n".join(service.logs(500))
    # 批次 2 的汇总日志在批次 1 被清理后依然正确发出
    assert "批量下载完成：成功 1 / 失败 1 / 跳过 0 / 取消 0，共 2 项" in logs_text
