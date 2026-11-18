"""统一通知钩子：下载事件 -> 自定义脚本（EMBYTOOL_* env-var 协议）。

契约：
- completed/failed/cancelled 事件触发脚本，环境变量正确传递；
- 脚本 stdout 记日志、非零退出码不影响下载任务成败；
- 脚本缺失/超时只告警；
- 未配置脚本时零开销空操作；
- stop() 后不再派发。
脚本用 python 包装（跨平台，不依赖 bash）。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from notify import Notifier


def _write_script(tmp_path: Path, name: str, body: str) -> str:
    """生成一个 Python 记录器脚本，返回可执行路径（Windows 用 venv python 包装）。"""
    script = tmp_path / name
    script.write_text(body, encoding="utf-8")
    return str(sys.executable)


def _recorder_body(out_file: str) -> str:
    # 注意：写入字符串里不能出现"反斜杠+n"序列被再次解释的问题，
    # 换行统一用 chr(10) 生成，保证产出的 rec.py 是合法单行源码。
    nl = "chr(10)"
    return (
        "import os, sys, json\n"
        f"open({out_file!r}, 'a', encoding='utf-8').write("
        f"json.dumps({{k: v for k, v in os.environ.items() if k.startswith('EMBYTOOL_')}}) + {nl})\n"
        "sys.exit(0)\n"
    )


def _wait_file(path: Path, min_lines: int = 1, timeout: float = 15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and len(path.read_text(encoding="utf-8").splitlines()) >= min_lines:
            return path.read_text(encoding="utf-8")
        time.sleep(0.1)
    raise AssertionError(f"通知脚本输出未出现：{path}")


def _launch_args(tmp_path: Path, out_name: str) -> tuple[str, str]:
    """跨平台包装：Windows 用 .bat 转发到当前解释器，Unix 直接给 .py 加执行位。

    .bat 用短 8.3 风格安全写法：逐行 @echo off + 全路径加引号 + %* 传参。
    """
    out_file = str(tmp_path / out_name)
    py = tmp_path / "rec.py"
    py.write_text(_recorder_body(out_file), encoding="utf-8")
    if sys.platform == "win32":
        bat = tmp_path / "rec.bat"
        bat.write_text(
            "@echo off\r\n"
            f'"{sys.executable}" "{py}" %*\r\n'
            "exit /b %ERRORLEVEL%\r\n",
            encoding="utf-8",
        )
        return str(bat), out_file
    py.chmod(0o755)
    return str(py), out_file


def test_notifier_completed_event_env_vars(tmp_path):
    script, out_file = _launch_args(tmp_path, "completed.jsonl")
    notifier = Notifier([script])
    notifier.emit(
        "completed",
        item_id="m1",
        item_name="Movie m1",
        path="/dl/movie.mp4",
        batch_id="b1",
        skipped=False,
    )
    notifier.stop()  # 等待脚本执行完成
    text = _wait_file(Path(out_file))
    data = json.loads(text.splitlines()[0])
    assert data["EMBYTOOL_EVENTTYPE"] == "completed"
    assert data["EMBYTOOL_ITEM_ID"] == "m1"
    assert data["EMBYTOOL_ITEM_NAME"] == "Movie m1"
    assert data["EMBYTOOL_PATH"] == "/dl/movie.mp4"
    assert data["EMBYTOOL_BATCH_ID"] == "b1"
    assert data["EMBYTOOL_SKIPPED"] == "0"
    assert "EMBYTOOL_ERROR" not in data


def test_notifier_failed_event_carries_error(tmp_path):
    script, out_file = _launch_args(tmp_path, "failed.jsonl")
    notifier = Notifier([script])
    notifier.emit("failed", item_id="x1", item_name="Movie x1", error="HTTP 500", batch_id="b2")
    notifier.stop()
    text = _wait_file(Path(out_file))
    data = json.loads(text.splitlines()[0])
    assert data["EMBYTOOL_EVENTTYPE"] == "failed"
    assert data["EMBYTOOL_ERROR"] == "HTTP 500"


def test_notifier_missing_script_does_not_raise(tmp_path):
    notifier = Notifier([str(tmp_path / "nope.bat"), str(tmp_path / "nope.py")])
    notifier.emit("completed", item_id="a", item_name="A")  # 不抛异常
    notifier.stop()


def test_notifier_no_scripts_is_noop():
    notifier = Notifier([])
    assert notifier.enabled is False
    notifier.emit("completed", item_id="a")  # 空操作不抛
    notifier.stop()


def test_notifier_stop_discards_new_events(tmp_path):
    script, out_file = _launch_args(tmp_path, "stopped.jsonl")
    notifier = Notifier([script])
    notifier.stop()
    notifier.emit("completed", item_id="late", item_name="Late")
    time.sleep(0.5)
    assert not Path(out_file).exists()


def test_service_run_job_emits_events(cfg, library, mock_emby, tmp_path):
    """端到端：队列下载 completed 触发 notify_scripts。"""
    from app_service import AppService
    from downloader import Downloader
    from emby_client import EmbyClient
    from models import MediaItem

    script, out_file = _launch_args(tmp_path, "svc.jsonl")
    cfg.notify_scripts = (script,)
    cfg.download_workers = 1
    mock_emby.add_movie("ev1", "Movie ev1", size=50_000)
    service = AppService()
    service.config = cfg
    service.client = EmbyClient(cfg)
    service.client.authenticate()
    service.library = library
    service.downloader = Downloader(service.client, library, segments=1)
    service.notifier = Notifier(list(cfg.notify_scripts))
    service.client.expand_download_items = lambda _iid: [
        MediaItem(item_id="ev1", name="Movie ev1", type="Movie", container="mp4", year=2020)
    ]

    service.start_download("ev1")
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if any(job["status"] == "completed" for job in service.list_jobs()):
            break
        time.sleep(0.05)
    service.notifier.stop()
    text = _wait_file(Path(out_file))
    data = json.loads(text.splitlines()[0])
    assert data["EMBYTOOL_EVENTTYPE"] == "completed"
    assert data["EMBYTOOL_ITEM_ID"] == "ev1"
