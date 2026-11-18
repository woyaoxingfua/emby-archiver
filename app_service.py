from __future__ import annotations

import os
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from config import load_config, save_default_download_dir
from downloader import DownloadCancelled, DownloadError, Downloader
from emby_client import EmbyClient, EmbyClientError
from library import CacheLibrary, redact_url
from logger import get_logger, setup_logging
from models import DownloadRecord, MediaItem
from notify import Notifier

logger = get_logger()


def _human_size(size: int) -> str:
    """将字节数格式化为人类可读字符串（纯函数，与 gui.py 一致）。"""
    if size >= 1024 * 1024 * 1024:
        return f"{size / 1024 / 1024 / 1024:.2f} GB"
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.2f} MB"
    if size >= 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size} B"


def _human_speed(speed: float) -> str:
    """将字节/秒速度格式化为人类可读字符串（纯函数，与 gui.py 一致）。"""
    if speed >= 1024 * 1024:
        return f"{speed / 1024 / 1024:.2f} MB/s"
    if speed >= 1024:
        return f"{speed / 1024:.1f} KB/s"
    return f"{speed:.0f} B/s"


def _human_eta(seconds: float) -> str:
    """将剩余秒数格式化为人类可读字符串（纯函数，与 gui.py 一致）。"""
    total = max(0, int(seconds))
    minutes, sec = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {sec}s"
    if minutes:
        return f"{minutes}m {sec}s"
    return f"{sec}s"


def _open_path(path: Path) -> None:
    # N17：跨平台打开文件/目录，替代 Windows-only 的 os.startfile
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        opener = "termux-open" if os.environ.get("TERMUX_VERSION") else "xdg-open"
        subprocess.Popen([opener, str(path)])


@dataclass
class _DownloadJob:
    job_id: str
    batch_id: str
    item: MediaItem
    force_restart: bool = False
    override_dir: Path | None = None
    status: str = "queued"
    result: str | None = None
    error: str | None = None
    created_at: float = 0.0
    started_at: float | None = None
    finished_at: float | None = None
    updated_at: float = 0.0
    downloader: Downloader | None = None
    worker: threading.Thread | None = None
    done: threading.Event = field(default_factory=threading.Event, repr=False)


# 终态判定与 tests/test_download_queue.py 的 TERMINAL 集合保持一致（skipped 体现在 result 字段）
_TERMINAL_JOB_STATUSES = frozenset({"completed", "failed", "cancelled"})


class AppService:
    def __init__(self) -> None:
        self.config_path = Path("config.json")
        self.config = None
        self.client: EmbyClient | None = None
        self.library: CacheLibrary | None = None
        self.downloader: Downloader | None = None
        self._logs: deque[str] = deque(maxlen=500)
        self._worker: threading.Thread | None = None
        self._worker_threads: set[threading.Thread] = set()
        self._job_queue: queue.Queue[str] = queue.Queue()
        self._jobs: dict[str, _DownloadJob] = {}
        self._batches: dict[str, dict[str, int]] = {}
        self._item_locks: dict[str, threading.Lock] = {}
        self.notifier = Notifier([])  # load() 时按 config.notify_scripts 重建
        self._state_lock = threading.Lock()
        self._status_condition = threading.Condition()
        self._status_version = 0
        self._last_progress_publish = 0.0
        # 速度/ETA 计算所需的采样历史：item_id -> [(timestamp, downloaded), ...]
        self._speed_history: dict[str, list[tuple[float, int]]] = {}
        self._download_state: dict[str, Any] = {
            "active": False,
            "item_id": None,
            "item_name": None,
            "status": "idle",
            "mode": "未开始",
            "downloaded": 0,
            "expected": None,
            "path": None,
            "detail": None,
            "updated_at": time.time(),
            # 富展示字段（与 GUI 对齐，初始为无数据占位值）
            "speed_text": "--",
            "eta_text": "--",
            "percent": None,
            "bytes_text": "0 B / --",
        }

    def load(
        self,
        config_path: Path,
        log_dir: Path | None = None,
        log_level: str | None = None,
    ) -> None:
        self.config_path = config_path
        config = load_config(config_path)
        setup_logging(log_dir or config.log_dir, log_level or config.log_level)
        self.config = config
        self.client = EmbyClient(config)
        self.library = CacheLibrary(config.database_path)
        self.notifier = Notifier(list(config.notify_scripts))
        if config.notify_scripts:
            self._record_log(f"已启用通知脚本：{', '.join(config.notify_scripts)}")
        interrupted = self.library.reconcile_interrupted()
        if interrupted:
            self._record_log(f"启动对账：{interrupted} 条残留 downloading 记录已标记为 interrupted")
        self.downloader = Downloader(
            self.client,
            self.library,
            progress_callback=self._download_progress,
            segments=config.segments,
        )
        self._record_log(f"服务已加载配置：{config.server_url}")

    def ensure_ready(self) -> None:
        if not self.client or not self.library or not self.downloader or not self.config:
            raise RuntimeError("Service is not loaded. Call load() first.")

    def login_test(self) -> dict[str, Any]:
        self.ensure_ready()
        assert self.client is not None
        user = self.client.authenticate()
        self._record_log(f"登录成功：{user.get('Name', 'unknown')} ({user.get('Id', 'n/a')})")
        return user

    def search(self, keyword: str, limit: int = 50) -> list[dict[str, Any]]:
        self.ensure_ready()
        assert self.client is not None
        self.client.authenticate()
        return [self._media_item_to_dict(item) for item in self.client.search_items(keyword, limit=limit)]

    def list_downloads(self) -> list[dict[str, Any]]:
        self.ensure_ready()
        assert self.library is not None
        return [self._record_to_dict(record) for record in self.library.list_all()]

    def current_status(self) -> dict[str, Any]:
        with self._state_lock:
            state = dict(self._download_state)
            running = any(job.status == "running" for job in self._jobs.values())
            queued = sum(job.status == "queued" for job in self._jobs.values())
            worker_alive = any(worker.is_alive() for worker in self._worker_threads)
        state["active"] = running
        state["queued"] = queued
        state["worker_alive"] = worker_alive
        state["jobs"] = self.list_jobs()
        return state

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._state_lock:
            jobs = sorted(self._jobs.values(), key=lambda job: job.created_at)
            return [
                {
                    "job_id": job.job_id,
                    "batch_id": job.batch_id,
                    "item_id": job.item.item_id,
                    "item_name": job.item.name,
                    "status": job.status,
                    "result": job.result,
                    "error": job.error,
                    "created_at": job.created_at,
                    "started_at": job.started_at,
                    "finished_at": job.finished_at,
                    "updated_at": job.updated_at,
                    "override_dir": str(job.override_dir) if job.override_dir else None,
                }
                for job in jobs
            ]

    def _notify_status(self, force: bool = True) -> None:
        now = time.monotonic()
        with self._status_condition:
            if not force and now - self._last_progress_publish < 0.25:
                return
            self._last_progress_publish = now
            self._status_version += 1
            self._status_condition.notify_all()

    def status_snapshot(self) -> dict[str, Any]:
        return {
            "status": self.current_status(),
            "records": self.list_downloads(),
            "logs": self.logs(500),
            "config": self.config_summary(),
        }

    def wait_for_status(self, last_version: int, timeout: float = 15.0) -> tuple[int, dict[str, Any]]:
        with self._status_condition:
            if self._status_version == last_version:
                self._status_condition.wait(timeout=timeout)
            version = self._status_version
        return version, self.status_snapshot()

    def logs(self, limit: int = 200) -> list[str]:
        return list(self._logs)[-limit:]

    def list_log_files(self) -> list[dict[str, Any]]:
        """扫描 logs/ 目录，返回日志文件列表（按 mtime 降序）。

        Returns:
            [{"name": "xxx.log", "size": 12345, "mtime": "2026-07-25T23:50:00"}, ...]
        """
        self.ensure_ready()
        assert self.config is not None
        log_dir = self.config.log_dir
        files: list[dict[str, Any]] = []
        if not log_dir.exists():
            return files
        for entry in log_dir.iterdir():
            if entry.is_file() and entry.suffix == ".log":
                stat = entry.stat()
                mtime = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(stat.st_mtime))
                files.append({
                    "name": entry.name,
                    "size": stat.st_size,
                    "mtime": mtime,
                })
        files.sort(key=lambda f: f["mtime"], reverse=True)
        return files

    def read_log_file(
        self,
        name: str,
        offset: int = 0,
        limit: int = 200,
        level: str | None = None,
    ) -> dict[str, Any]:
        r"""读取日志文件内容，支持分页与级别过滤。

        Args:
            name: 文件名（必须以 .log 结尾，只含 [a-zA-Z0-9_\-]）。
            offset: 从末尾算的偏移行数（0 表示最后 limit 行）。
            limit: 返回行数上限。
            level: 级别过滤（INFO/WARNING/ERROR/DEBUG 或 None）。

        Returns:
            {"lines": [...], "total": 总行数, "offset": ..., "limit": ..., "filtered": 过滤后行数}

        Raises:
            ValueError: 文件名不合法。
            FileNotFoundError: 文件不存在。
        """
        # 安全检查：防止路径穿越
        if not re.match(r"^[a-zA-Z0-9_\-]+\.log$", name):
            raise ValueError(f"Invalid log file name: {name}")

        self.ensure_ready()
        assert self.config is not None
        log_path = self.config.log_dir / name
        if not log_path.exists():
            raise FileNotFoundError(f"Log file not found: {name}")

        # 读取所有行
        content = log_path.read_text(encoding="utf-8", errors="replace")
        all_lines = content.splitlines()
        total = len(all_lines)

        # 级别过滤
        if level:
            level_tag = f"[{level.upper()}]"
            filtered = [line for line in all_lines if level_tag in line]
        else:
            filtered = all_lines[:]

        filtered_count = len(filtered)

        # 分页：从末尾算 offset
        if offset < 0:
            offset = 0
        if limit < 1:
            limit = 1
        start = max(0, filtered_count - offset - limit)
        end = filtered_count - offset
        if end <= 0:
            lines: list[str] = []
        else:
            lines = filtered[start:end]

        return {
            "lines": lines,
            "total": total,
            "offset": offset,
            "limit": limit,
            "filtered": filtered_count,
        }

    def set_segments(self, n: int) -> int:
        """设置并发分段数，同步更新 config / client / downloader。

        Args:
            n: 并发数（1–32）。

        Returns:
            实际设置的有效值。
        """
        self.ensure_ready()
        assert self.config is not None and self.client is not None and self.downloader is not None
        n = max(1, min(32, int(n)))
        self.config.segments = n
        self.client.config.segments = n
        self.downloader.segments = n
        with self._state_lock:
            active = [job.downloader for job in self._jobs.values() if job.downloader is not None]
        for downloader in active:
            downloader.segments = n
        self._record_log(f"并发分段数已设置为 {n}")
        return n

    def set_download_workers(self, n: int) -> int:
        self.ensure_ready()
        assert self.config is not None
        n = max(1, min(8, int(n)))
        with self._state_lock:
            self.config.download_workers = n
            self._reconcile_workers_locked()
        self._record_log(f"下载 worker 并发已设置为 {n}")
        return n

    def start_download(
        self,
        item_id: str,
        force_restart: bool = False,
        override_dir: Path | None = None,
    ) -> dict[str, Any]:
        self.ensure_ready()
        assert self.client is not None
        # Web 入口本来就在工作线程中执行；先展开可避免父任务占住 worker 等待子任务。
        self.client.authenticate()
        items = self.client.expand_download_items(item_id)
        if not items:
            raise RuntimeError("未找到可下载的媒体文件")
        return self._enqueue_items(items, force_restart, override_dir)

    def resume_record(self, item_id: str) -> dict[str, Any]:
        self.ensure_ready()
        assert self.client is not None
        self.client.authenticate()
        item = self.client.get_item(item_id)
        return self._enqueue_items([item], force_restart=False, override_dir=None)

    def _enqueue_items(
        self,
        items: list[MediaItem],
        force_restart: bool,
        override_dir: Path | None,
    ) -> dict[str, Any]:
        assert self.config is not None and self.library is not None
        if override_dir is not None:
            override_dir.mkdir(parents=True, exist_ok=True)
        # DupeCheck：判重键用天然稳定的 Emby item_id。历史中已 completed 的条目
        # 不再入队（force_restart 语义优先，强制重下照常入队）。
        skipped_completed: list[str] = []
        pending_items = items
        if not force_restart:
            skipped_completed = [
                item.item_id for item in items
                if (rec := self.library.get(item.item_id)) is not None and rec.status == "completed"
            ]
            if skipped_completed:
                done_ids = set(skipped_completed)
                pending_items = [item for item in items if item.item_id not in done_ids]
                self._record_log(
                    f"已缓存完成、本次跳过 {len(skipped_completed)} 项：{', '.join(skipped_completed)}"
                )
                if not pending_items:
                    return {
                        "batch_id": None,
                        "total": len(items),
                        "queued": 0,
                        "skipped_completed": skipped_completed,
                    }
        batch_id = uuid.uuid4().hex[:12]
        now = time.time()
        jobs = [
            _DownloadJob(
                job_id=uuid.uuid4().hex[:12],
                batch_id=batch_id,
                item=item,
                force_restart=force_restart,
                override_dir=override_dir,
                created_at=now + index * 1e-6,
                updated_at=now,
            )
            for index, item in enumerate(pending_items)
        ]
        with self._state_lock:
            busy_ids = {
                job.item.item_id
                for job in self._jobs.values()
                if job.status in {"queued", "running"}
            }
            duplicates = [job.item.item_id for job in jobs if job.item.item_id in busy_ids]
            if duplicates:
                raise RuntimeError(f"资源已在下载队列中：{', '.join(duplicates)}")
            self._batches[batch_id] = {
                "total": len(jobs), "finished": 0, "ok": 0, "failed": 0, "skipped": 0, "cancelled": 0
            }
            for job in jobs:
                self._jobs[job.job_id] = job
                self._job_queue.put(job.job_id)
            self._prune_history_locked()
            self._reconcile_workers_locked()
        self._record_log(f"已加入下载队列：{len(jobs)} 个任务，worker 并发 {self.config.download_workers}")
        if force_restart:
            self._record_log("本批次已启用强制重下，旧断点将在各任务启动时清理")
        return {
            "batch_id": batch_id,
            "total": len(items),
            "queued": len(jobs),
            "skipped_completed": skipped_completed,
        }

    def _reconcile_workers_locked(self) -> None:
        assert self.config is not None
        self._worker_threads = {worker for worker in self._worker_threads if worker.is_alive()}
        desired = max(1, min(8, self.config.download_workers))
        while len(self._worker_threads) < desired and not self._job_queue.empty():
            worker = threading.Thread(target=self._queue_worker, daemon=True)
            self._worker_threads.add(worker)
            if self._worker is None or not self._worker.is_alive():
                self._worker = worker
            worker.start()

    def _queue_worker(self) -> None:
        current = threading.current_thread()
        while True:
            with self._state_lock:
                assert self.config is not None
                if len(self._worker_threads) > self.config.download_workers:
                    self._worker_threads.discard(current)
                    self._notify_status()
                    return
            try:
                job_id = self._job_queue.get_nowait()
            except queue.Empty:
                with self._state_lock:
                    self._worker_threads.discard(current)
                self._notify_status()
                return
            try:
                with self._state_lock:
                    job = self._jobs.get(job_id)
                    if job is None or job.status == "cancelled":
                        continue
                    job.status = "running"
                    job.started_at = time.time()
                    job.updated_at = job.started_at
                    job.worker = current
                self._notify_status()
                self._run_job(job)
            finally:
                self._job_queue.task_done()

    def _run_job(self, job: _DownloadJob) -> None:
        assert self.config is not None and self.library is not None
        job_config = replace(self.config)
        if job.override_dir is not None:
            job_config.download_dir = job.override_dir
        client = EmbyClient(job_config)
        downloader = Downloader(
            client,
            self.library,
            progress_callback=lambda status, item, downloaded, expected, path, detail: self._download_progress_for_job(
                job.job_id, status, item, downloaded, expected, path, detail
            ),
            segments=job_config.segments,
        )
        with self._state_lock:
            job.downloader = downloader
        try:
            client.authenticate()
            with self._state_lock:
                item_lock = self._item_locks.setdefault(job.item.item_id, threading.Lock())
            with item_lock:
                path = downloader.download_item(job.item, force_restart=job.force_restart)
            result = downloader.last_result_status or "completed"
            with self._state_lock:
                job.status = "completed"
                job.result = result
            if result == "skipped":
                self._record_log(f"文件已存在，跳过：{job.item.name}")
            else:
                self._record_log(f"已保存：{path}")
            self.notifier.emit(
                "completed",
                item_id=job.item.item_id,
                item_name=job.item.name,
                path=str(path) if path else "",
                batch_id=job.batch_id,
                skipped=result == "skipped",
            )
        except DownloadCancelled:
            with self._state_lock:
                job.status = "cancelled"
                job.result = "cancelled"
            self._record_log(f"已取消：{job.item.name}")
            self.notifier.emit(
                "cancelled",
                item_id=job.item.item_id,
                item_name=job.item.name,
                batch_id=job.batch_id,
            )
        except (DownloadError, EmbyClientError, OSError) as exc:
            disk_gated = "磁盘空间不足" in str(exc)
            with self._state_lock:
                job.status = "failed"
                job.result = "failed"
                job.error = str(exc)
                cancelled_for_disk: list[_DownloadJob] = []
                if disk_gated:
                    # 磁盘护栏：后续 queued 任务会以同样的方式失败，全部显式
                    # 取消并保留断点，避免队列逐个空转烧请求。
                    for other in self._jobs.values():
                        if other.status == "queued":
                            other.status = "cancelled"
                            other.result = "cancelled"
                            other.finished_at = time.time()
                            other.error = "因磁盘空间不足自动取消，清理空间后可重新下载"
                            cancelled_for_disk.append(other)
            self._record_log(f"下载失败：{job.item.name}（{exc}）")
            self.notifier.emit(
                "failed",
                item_id=job.item.item_id,
                item_name=job.item.name,
                error=str(exc),
                batch_id=job.batch_id,
            )
            if disk_gated:
                self._record_log(
                    f"磁盘空间不足：已自动取消 {len(cancelled_for_disk)} 个排队任务；"
                    "清理磁盘空间后可重新发起下载（已完成进度保留在断点中）"
                )
                for other in cancelled_for_disk:
                    self._finish_job(other)
                    other.done.set()
                if cancelled_for_disk:
                    with self._state_lock:
                        self._prune_history_locked()
        except Exception as exc:
            with self._state_lock:
                job.status = "failed"
                job.result = "failed"
                job.error = str(exc)
            logger.exception("后台下载任务异常：%s", job.item.name)
            self._record_log(f"后台任务异常：{job.item.name}（{exc}）")
        finally:
            client.close()
            with self._state_lock:
                job.downloader = None
                job.finished_at = time.time()
                job.updated_at = job.finished_at
            self._finish_job(job)
            job.done.set()
            # done 置位后补一次清理：本 job 自此刻起才满足可安全移除的条件
            with self._state_lock:
                self._prune_history_locked()
            self._notify_status()

    def _download_progress_for_job(self, job_id: str, *args: Any) -> None:
        with self._state_lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job.updated_at = time.time()
        self._download_progress(*args)

    def _finish_job(self, job: _DownloadJob) -> None:
        summary = None
        with self._state_lock:
            batch = self._batches.get(job.batch_id)
            if batch is None or job.result is None:
                return
            batch["finished"] += 1
            result_key = "ok" if job.result == "completed" else job.result
            if result_key in {"ok", "failed", "skipped", "cancelled"}:
                batch[result_key] += 1
            if batch["finished"] == batch["total"]:
                summary = dict(batch)
            self._prune_history_locked()
        if summary is not None:
            self._record_log(
                f"批量下载完成：成功 {summary['ok']} / 失败 {summary['failed']} / "
                f"跳过 {summary['skipped']} / 取消 {summary['cancelled']}，共 {summary['total']} 项"
            )

    def _prune_history_locked(self) -> None:
        """在 _state_lock 内清理终态历史，保证长运行内存有界。

        三条不变量：
        1. 仅移除终态（completed/failed/cancelled）且 done 事件已置位的 job；
           done.set() 在 _finish_job 之后，故计数与批次汇总日志必然已完成，
           也不会破坏 delete_record/cancel_current 锁外等待 done 的协议。
        2. 批次在「全部任务已计入（finished==total）」或「_jobs 中不再有任何
           引用该批次的 job」时移除（后者覆盖 result=None 悬空计数情形；
           _finish_job 是 _batches 的唯一读取者，对已移除批次安全 no-op）。
        3. 仅当该 item_id 已无 queued/running job 时移除 _item_locks 条目；
           with item_lock 块在状态变终态前必然已退出，删除终态条目对应的锁对象安全。
        job_history_limit=0 表示不清理（保留旧行为）。
        """
        assert self.config is not None
        limit = self.config.job_history_limit
        if limit <= 0:
            return
        terminal_jobs = [
            job for job in self._jobs.values()
            if job.status in _TERMINAL_JOB_STATUSES and job.done.is_set()
        ]
        if len(terminal_jobs) > limit:
            terminal_jobs.sort(key=lambda job: job.created_at)
            for job in terminal_jobs[: len(terminal_jobs) - limit]:
                self._jobs.pop(job.job_id, None)
        # 已全部计入或已无 job 引用的批次随之移除，避免 _batches 无界增长
        referenced_batches = {job.batch_id for job in self._jobs.values()}
        for batch_id, batch in list(self._batches.items()):
            if batch["finished"] == batch["total"] or batch_id not in referenced_batches:
                del self._batches[batch_id]
        # 已无活动 job 的条目锁随之移除；新建 job 时 setdefault 会按需重建
        active_item_ids = {
            job.item.item_id for job in self._jobs.values()
            if job.status in {"queued", "running"}
        }
        for item_id in list(self._item_locks):
            if item_id not in active_item_ids:
                del self._item_locks[item_id]

    def pause_current(self) -> None:
        self.ensure_ready()
        with self._state_lock:
            targets = [
                job.downloader for job in self._jobs.values()
                if job.status == "running" and job.downloader is not None
            ]
        for downloader in targets:
            downloader.pause()
        self._record_log(f"已请求暂停 {len(targets)} 个运行中的下载")

    def resume_current(self) -> None:
        self.ensure_ready()
        with self._state_lock:
            targets = [
                job.downloader for job in self._jobs.values()
                if job.status == "running" and job.downloader is not None
            ]
        for downloader in targets:
            downloader.resume()
        self._record_log(f"已请求继续 {len(targets)} 个运行中的下载")

    def cancel_current(self, cleanup_temp: bool = False) -> None:
        self.ensure_ready()
        cancelled_queued: list[_DownloadJob] = []
        with self._state_lock:
            targets = []
            for job in self._jobs.values():
                if job.status == "queued":
                    job.status = "cancelled"
                    job.result = "cancelled"
                    job.finished_at = time.time()
                    cancelled_queued.append(job)
                elif job.status == "running" and job.downloader is not None:
                    targets.append(job.downloader)
        for job in cancelled_queued:
            self._finish_job(job)
            job.done.set()
        if cancelled_queued:
            with self._state_lock:
                self._prune_history_locked()
        for downloader in targets:
            downloader.cancel(cleanup_temp=cleanup_temp)
        self._record_log(
            f"已请求取消 {len(targets)} 个运行中下载，并取消 {len(cancelled_queued)} 个排队任务"
            f"（cleanup_temp={cleanup_temp}）"
        )

    def delete_record(self, item_id: str) -> dict[str, Any]:
        self.ensure_ready()
        assert self.library is not None and self.downloader is not None
        record = self.library.get(item_id)
        if not record:
            raise RuntimeError("Record not found")
        # N15：只等待目标任务完成，不因 worker 继续处理其他条目而误删或误报。
        with self._state_lock:
            matching_jobs = [
                job for job in self._jobs.values()
                if job.item.item_id == item_id and job.status in {"queued", "running"}
            ]
            queued_jobs = []
            running_jobs = []
            for job in matching_jobs:
                if job.status == "queued":
                    job.status = "cancelled"
                    job.result = "cancelled"
                    job.finished_at = time.time()
                    queued_jobs.append(job)
                elif job.status == "running":
                    running_jobs.append(job)
            active_id = self._download_state.get("item_id")
            worker = self._worker
        for job in queued_jobs:
            self._finish_job(job)
            job.done.set()
        if queued_jobs:
            with self._state_lock:
                self._prune_history_locked()
        for job in running_jobs:
            if job.downloader is not None:
                job.downloader.cancel(cleanup_temp=True)
        for job in running_jobs:
            if not job.done.wait(timeout=15):
                raise RuntimeError("下载任务尚未停止，已保留文件和记录，请稍后重试")
        if not matching_jobs and active_id == item_id and worker and worker.is_alive():
            self.downloader.cancel(cleanup_temp=True)
            worker.join(timeout=15)
            if worker.is_alive():
                raise RuntimeError("下载任务尚未停止，已保留文件和记录，请稍后重试")
        path = Path(record.target_path)
        removed_file = False
        if path.exists():
            path.unlink()
            removed_file = True
        temp_path = path.with_name(path.name + ".part")
        temp_path.unlink(missing_ok=True)
        meta_path = temp_path.with_name(temp_path.name + ".meta.json")
        meta_path.unlink(missing_ok=True)
        meta_path.with_name(meta_path.name + ".tmp").unlink(missing_ok=True)
        # N9：iterdir+startswith 替代 glob，兼容 [ ] 等元字符标题
        seg_prefix = temp_path.name + ".seg"
        if temp_path.parent.exists():
            for seg_path in temp_path.parent.iterdir():
                if seg_path.is_file() and seg_path.name.startswith(seg_prefix):
                    seg_path.unlink(missing_ok=True)
        self.library.delete(item_id)
        removed_empty_dirs = self._cleanup_empty_dirs(path, removed_file)
        if removed_file:
            self._record_log(f"已删除文件和记录：{path}")
        else:
            self._record_log(f"文件已不存在，仅移除记录：{path}")
        return {
            "removed_file": removed_file,
            "path": str(path),
            "removed_empty_dirs": removed_empty_dirs,
        }

    def _cleanup_empty_dirs(self, path: Path, removed_file: bool) -> int:
        """删除文件后清理其所在链上的空目录，止步于下载根目录。

        影片按 `影片名 (年份)/影片名 (年份).mp4`、剧集按 `剧名/季名/集.mkv` 建目录；
        只删空目录且绝不越过下载根（config.download_dir 或 job 覆盖目录），
        用户自建的有内容目录不受影响。返回删除的目录数。
        """
        assert self.config is not None
        roots = {self.config.download_dir.resolve()}
        removed = 0
        current = path.parent
        while current != current.parent:
            try:
                if current.resolve() in roots or not current.exists():
                    break
                entries = list(current.iterdir())
            except OSError:
                break
            if entries:
                break
            try:
                current.rmdir()
            except OSError:
                break
            removed += 1
            current = current.parent
        return removed

    def open_record(self, item_id: str) -> str:
        self.ensure_ready()
        assert self.library is not None
        record = self.library.get(item_id)
        if not record:
            raise RuntimeError("Record not found")
        path = Path(record.target_path)
        if not path.exists():
            raise RuntimeError(f"File not found: {path}")
        _open_path(path)
        self._record_log(f"已打开文件：{path}")
        return str(path)

    def open_record_dir(self, item_id: str) -> str:
        self.ensure_ready()
        assert self.library is not None
        record = self.library.get(item_id)
        if not record:
            raise RuntimeError("Record not found")
        folder = Path(record.target_path).parent
        if not folder.exists():
            raise RuntimeError(f"Directory not found: {folder}")
        _open_path(folder)
        self._record_log(f"已打开目录：{folder}")
        return str(folder)

    def set_default_download_dir(self, path: Path, persist: bool = False) -> str:
        """切换默认下载目录（内存生效）。

        Args:
            path: 新的默认下载目录。
            persist: 是否同时写回 config.json 持久化保存（默认 False，保证向后兼容）。
        """
        self.ensure_ready()
        assert self.client is not None and self.downloader is not None and self.config is not None
        path.mkdir(parents=True, exist_ok=True)
        self.config.download_dir = path
        self.client.config.download_dir = path
        self.downloader.client.config.download_dir = path
        if persist:
            # 仅更新 config.json 的 download_dir 字段，原子写回，不影响其他字段与凭证
            save_default_download_dir(self.config_path, path)
            self._record_log(f"默认下载目录已切换并写入配置：{path}")
        else:
            self._record_log(f"默认下载目录已切换（仅本次会话）：{path}")
        return str(path)

    def config_summary(self) -> dict[str, Any]:
        self.ensure_ready()
        assert self.config is not None
        with self._state_lock:
            worker_alive = any(worker.is_alive() for worker in self._worker_threads)
        return {
            "server_url": self.config.server_url,
            "download_dir": str(self.config.download_dir),
            "segments": self.config.segments,
            "download_workers": self.config.download_workers,
            "stream_segments": self.config.stream_segments,
            "config_path": str(self.config_path),
            "proxy_http": self.config.proxy_http,
            "proxy_https": self.config.proxy_https,
            "experimental_force_multipart": self.config.experimental_force_multipart,
            "worker_alive": worker_alive,
        }

    def _download_progress(
        self,
        status: str,
        item: MediaItem,
        downloaded: int,
        expected: int | None,
        path: Path,
        detail: str | None = None,
    ) -> None:
        with self._state_lock:
            self._download_state.update(
                {
                    "active": status not in {"completed", "failed", "cancelled", "skipped"},
                    "item_id": item.item_id,
                    "item_name": item.name,
                    "status": status,
                    "mode": detail or self._download_state.get("mode") or "未开始",
                    "downloaded": downloaded,
                    "expected": expected,
                    "path": str(path),
                    "detail": detail,
                    "updated_at": time.time(),
                }
            )
            if status == "starting":
                self._download_state["mode"] = "准备中"
            elif status == "progress" and not detail:
                self._download_state["mode"] = str(self._download_state.get("mode") or "下载中")
            elif status == "completed":
                self._download_state["active"] = False
                self._download_state["mode"] = detail or "下载完成"
            elif status in {"failed", "cancelled", "skipped", "partial", "paused"}:
                self._download_state["active"] = False if status != "paused" else True
            # 计算并写回富展示字段（speed/eta/percent/bytes）
            self._compute_display_metrics(status, item, downloaded, expected)
        text = self._progress_message(status, item, downloaded, expected, path, detail)
        if text:
            self._record_log(text)

    def _calc_speed_and_eta(
        self, item_id: str, downloaded: int, expected: int | None
    ) -> tuple[str, str]:
        """基于最近采样历史计算速度与 ETA 文本。算法与 gui.py 一致。

        Returns:
            (speed_text, eta_text)，无足够数据时均为空字符串。
        """
        now = time.time()
        history = self._speed_history.setdefault(item_id, [])
        history.append((now, downloaded))
        # 仅保留最近 5 秒的采样点，控制历史长度
        cutoff = now - 5.0
        while history and history[0][0] < cutoff:
            history.pop(0)
        if len(history) < 2:
            return "", ""
        first_time, first_bytes = history[0]
        last_time, last_bytes = history[-1]
        delta_bytes = last_bytes - first_bytes
        delta_time = last_time - first_time
        if delta_time <= 0 or delta_bytes <= 0:
            return "", ""
        speed = delta_bytes / delta_time
        speed_text = _human_speed(speed)
        if expected and downloaded < expected:
            remain = expected - downloaded
            eta_seconds = remain / speed if speed > 0 else 0
            return speed_text, _human_eta(eta_seconds)
        return speed_text, ""

    def _compute_display_metrics(
        self, status: str, item: MediaItem, downloaded: int, expected: int | None
    ) -> None:
        """根据当前 status 计算并写回 _download_state 的富展示字段。

        必须在 self._state_lock 内调用。映射语义与 gui.py 的 _download_progress 对齐，
        保持 GUI 不动。新增字段：speed_text / eta_text / percent / bytes_text。
        """
        state = self._download_state
        # 字节进度文本（始终更新）
        if expected:
            state["bytes_text"] = f"{_human_size(downloaded)} / {_human_size(expected)}"
            state["percent"] = downloaded / expected * 100
        else:
            state["bytes_text"] = f"{_human_size(downloaded)} / --"
            state["percent"] = None

        if status == "starting":
            self._speed_history[item.item_id] = []
            state["speed_text"] = "--"
            state["eta_text"] = "--"
        elif status == "progress":
            speed_text, eta_text = self._calc_speed_and_eta(item.item_id, downloaded, expected)
            state["speed_text"] = speed_text or "--"
            state["eta_text"] = eta_text or "--"
        elif status == "completed":
            self._speed_history.pop(item.item_id, None)
            state["speed_text"] = "--"
            state["eta_text"] = "完成"
            if expected:
                state["percent"] = 100.0
        elif status == "skipped":
            self._speed_history.pop(item.item_id, None)
            state["speed_text"] = "--"
            state["eta_text"] = "已跳过"
        elif status == "paused":
            self._speed_history.pop(item.item_id, None)
            state["speed_text"] = "--"
            state["eta_text"] = "已暂停"
        elif status == "cancelled":
            self._speed_history.pop(item.item_id, None)
            state["speed_text"] = "--"
            state["eta_text"] = "已取消"
        elif status == "partial":
            self._speed_history.pop(item.item_id, None)
            state["speed_text"] = "--"
            state["eta_text"] = "--"
        elif status == "failed":
            self._speed_history.pop(item.item_id, None)
            state["speed_text"] = "--"
            state["eta_text"] = "--"
        elif status == "mode":
            # 仅更新模式文案，保留已有速度/ETA 占位
            state.setdefault("speed_text", "--")
            state.setdefault("eta_text", "--")
        else:
            # fallback / candidate_failed / diagnostic / 其它：保持默认占位
            state.setdefault("speed_text", "--")
            state.setdefault("eta_text", "--")
        self._notify_status(force=status != "progress")

    def _record_log(self, message: str) -> None:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"{timestamp} {message}"
        self._logs.append(line)
        logger.info("[service] %s", message)
        self._notify_status()

    def _mark_failed(self, detail: str) -> None:
        with self._state_lock:
            item_id = self._download_state.get("item_id")
            if item_id:
                self._speed_history.pop(item_id, None)
            self._download_state.update(
                {
                    "active": False,
                    "status": "failed",
                    "mode": "失败",
                    "speed_text": "--",
                    "eta_text": "--",
                    "detail": detail,
                    "updated_at": time.time(),
                }
            )

    @staticmethod
    def _progress_message(
        status: str,
        item: MediaItem,
        downloaded: int,
        expected: int | None,
        path: Path,
        detail: str | None,
    ) -> str | None:
        if status == "starting":
            return f"开始下载：{item.name} -> {path}"
        if status == "mode":
            return f"下载模式：{item.name} -> {detail or '未知模式'}"
        if status == "paused":
            return f"已暂停：{item.name}"
        if status == "cancelled":
            return f"已取消：{item.name}"
        if status == "fallback":
            return f"模式回退：{item.name} -> {detail or '已回退到单线程'}"
        if status == "candidate_failed":
            return f"候选地址失败，尝试下一个：{item.name}；原因：{detail or '未知'}"
        if status == "diagnostic":
            return f"诊断：{detail or item.name}"
        if status == "completed":
            return f"下载完成：{item.name} -> {path}"
        if status == "partial":
            return f"下载中断：{item.name}"
        if status == "failed":
            return f"下载失败：{item.name}"
        if status == "skipped":
            return f"已存在，跳过下载：{item.name}"
        return None

    @staticmethod
    def _media_item_to_dict(item: MediaItem) -> dict[str, Any]:
        return asdict(item)

    @staticmethod
    def _record_to_dict(record: DownloadRecord) -> dict[str, Any]:
        data = asdict(record)
        data["source_url"] = redact_url(data.get("source_url"))
        return data
