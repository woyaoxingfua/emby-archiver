from __future__ import annotations

import os
import re
import threading
import time
from collections import deque
from dataclasses import asdict
from pathlib import Path
from typing import Any

from config import load_config, save_default_download_dir
from downloader import DownloadCancelled, Downloader
from emby_client import EmbyClient
from library import CacheLibrary
from logger import get_logger, setup_logging
from models import DownloadRecord, MediaItem

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


class AppService:
    def __init__(self) -> None:
        self.config_path = Path("config.json")
        self.config = None
        self.client: EmbyClient | None = None
        self.library: CacheLibrary | None = None
        self.downloader: Downloader | None = None
        self._logs: deque[str] = deque(maxlen=500)
        self._worker: threading.Thread | None = None
        self._state_lock = threading.Lock()
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
        state["worker_alive"] = bool(self._worker and self._worker.is_alive())
        return state

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
        self._record_log(f"并发分段数已设置为 {n}")
        return n

    def start_download(
        self,
        item_id: str,
        force_restart: bool = False,
        override_dir: Path | None = None,
    ) -> None:
        self.ensure_ready()
        self._ensure_idle()
        assert self.client is not None
        self._worker = threading.Thread(
            target=self._download_item_task,
            args=(item_id, force_restart, override_dir),
            daemon=True,
        )
        self._worker.start()

    def resume_record(self, item_id: str) -> None:
        self.ensure_ready()
        self._ensure_idle()
        self._worker = threading.Thread(target=self._resume_record_task, args=(item_id,), daemon=True)
        self._worker.start()

    def pause_current(self) -> None:
        self.ensure_ready()
        assert self.downloader is not None
        self.downloader.pause()
        self._record_log("已请求暂停当前下载")

    def resume_current(self) -> None:
        self.ensure_ready()
        assert self.downloader is not None
        self.downloader.resume()
        self._record_log("已请求继续当前下载")

    def cancel_current(self, cleanup_temp: bool = False) -> None:
        self.ensure_ready()
        assert self.downloader is not None
        self.downloader.cancel(cleanup_temp=cleanup_temp)
        self._record_log(f"已请求取消当前下载（cleanup_temp={cleanup_temp}）")

    def delete_record(self, item_id: str) -> dict[str, Any]:
        self.ensure_ready()
        assert self.library is not None
        record = self.library.get(item_id)
        if not record:
            raise RuntimeError("Record not found")
        path = Path(record.target_path)
        removed_file = False
        if path.exists():
            path.unlink()
            removed_file = True
        temp_path = path.with_name(path.name + ".part")
        temp_path.unlink(missing_ok=True)
        for seg_path in temp_path.parent.glob(f"{temp_path.name}.seg*"):
            seg_path.unlink(missing_ok=True)
        self.library.delete(item_id)
        if removed_file:
            self._record_log(f"已删除文件和记录：{path}")
        else:
            self._record_log(f"文件已不存在，仅移除记录：{path}")
        return {"removed_file": removed_file, "path": str(path)}

    def open_record(self, item_id: str) -> str:
        self.ensure_ready()
        assert self.library is not None
        record = self.library.get(item_id)
        if not record:
            raise RuntimeError("Record not found")
        path = Path(record.target_path)
        if not path.exists():
            raise RuntimeError(f"File not found: {path}")
        os.startfile(path)  # type: ignore[attr-defined]
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
        os.startfile(folder)  # type: ignore[attr-defined]
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
        return {
            "server_url": self.config.server_url,
            "download_dir": str(self.config.download_dir),
            "segments": self.config.segments,
            "stream_segments": self.config.stream_segments,
            "config_path": str(self.config_path),
            "proxy_http": self.config.proxy_http,
            "proxy_https": self.config.proxy_https,
            "experimental_force_multipart": self.config.experimental_force_multipart,
            "worker_alive": bool(self._worker and self._worker.is_alive()),
        }

    def _ensure_idle(self) -> None:
        if self._worker and self._worker.is_alive():
            raise RuntimeError("A download task is already running")

    def _download_item_task(self, item_id: str, force_restart: bool, override_dir: Path | None) -> None:
        assert self.client is not None and self.downloader is not None
        original_download_dir = self.client.config.download_dir
        try:
            if override_dir is not None:
                override_dir.mkdir(parents=True, exist_ok=True)
                self.client.config.download_dir = override_dir
                self.downloader.client.config.download_dir = override_dir
                self._record_log(f"本次下载目录：{override_dir}")
            self.client.authenticate()
            items = self.client.expand_download_items(item_id)
            if items:
                self._record_log(f"准备下载 {len(items)} 个条目：{items[0].name}")
            if force_restart:
                self._record_log("已启用忽略 .part 重新开始")
            for item in items:
                try:
                    path = self.downloader.download_item(item, force_restart=force_restart)
                    if self.downloader.last_result_status == "skipped":
                        self._record_log(f"已存在，跳过下载：{path}")
                    else:
                        self._record_log(f"已保存：{path}")
                except DownloadCancelled:
                    self._record_log(f"已取消：{item.name}")
                    break
        except Exception as exc:
            self._mark_failed(str(exc))
            self._record_log(f"下载任务失败：{exc}")
        finally:
            self.client.config.download_dir = original_download_dir
            self.downloader.client.config.download_dir = original_download_dir

    def _resume_record_task(self, item_id: str) -> None:
        assert self.client is not None and self.downloader is not None
        try:
            self.client.authenticate()
            item = self.client.get_item(item_id)
            self._record_log(f"从下载记录继续：{item.name}")
            try:
                path = self.downloader.download_item(item)
                if self.downloader.last_result_status == "skipped":
                    self._record_log(f"已存在，跳过下载：{path}")
                else:
                    self._record_log(f"已保存：{path}")
            except DownloadCancelled:
                self._record_log(f"已取消：{item.name}")
        except Exception as exc:
            self._mark_failed(str(exc))
            self._record_log(f"继续下载失败：{exc}")

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

    def _record_log(self, message: str) -> None:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"{timestamp} {message}"
        self._logs.append(line)
        logger.info("[service] %s", message)

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
        return asdict(record)
