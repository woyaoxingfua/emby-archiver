"""统一通知钩子：下载事件触发用户自定义脚本（Sonarr/aria2/SABnzbd 的 env-var 协议）。

设计要点（对齐竞品调研结论）：
- 协议核心独立于 AppService/GUI/CLI——三端都能调用；
- 环境变量传参，不拼命令行（避免标题里的空格/引号/注入问题）；
- 脚本 stdout 记 INFO 日志、非零退出码记 WARNING，但绝不影响下载任务本身成败
  （SABnzbd 语义：外部脚本失败不是下载失败）；
- 短超时（默认 10s）+ 线程池单并发派发，通知慢/挂死不拖垮下载路径；
- 事件在独立线程触发，调用方（worker 线程）不等待脚本执行完成。

事件类型：
- completed：下载成功（含 skipped，变量 SKIPPED=1 区分）
- failed：下载失败（含 ERROR）
- cancelled：用户取消
"""
from __future__ import annotations

import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from logger import get_logger

logger = get_logger()

#: 事件环境变量前缀（参照 sonarr_eventtype / aria2 on-download-* 协议）
EVENT_TYPES = ("completed", "failed", "cancelled")


class Notifier:
    """下载事件 -> 自定义脚本派发器。线程安全；stop 后丢弃后续事件。"""

    def __init__(self, scripts: list[str], timeout: float = 10.0) -> None:
        self._scripts = [s for s in scripts if str(s).strip()]
        self._timeout = max(1.0, float(timeout))
        self._executor: ThreadPoolExecutor | None = None
        self._lock = threading.Lock()
        self._stopped = False

    @property
    def enabled(self) -> bool:
        return bool(self._scripts)

    def emit(
        self,
        event: str,
        item_id: str = "",
        item_name: str = "",
        path: str = "",
        error: str | None = None,
        batch_id: str = "",
        skipped: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """派发事件；无脚本或已停止时为空操作。绝不抛出异常。"""
        if not self._scripts or event not in EVENT_TYPES:
            return
        try:
            with self._lock:
                if self._stopped:
                    return
                if self._executor is None:
                    self._executor = ThreadPoolExecutor(
                        max_workers=1, thread_name_prefix="notify"
                    )
                executor = self._executor
            env = self._build_env(
                event, item_id, item_name, path, error, batch_id, skipped, extra
            )
            executor.submit(self._run_scripts, env, event, item_name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("通知派发失败（忽略）：%s", exc)

    def stop(self) -> None:
        """停机：不再接受新事件，等待已提交脚本跑完。"""
        with self._lock:
            self._stopped = True
            executor = self._executor
        if executor is not None:
            executor.shutdown(wait=True)

    @staticmethod
    def _build_env(
        event: str,
        item_id: str,
        item_name: str,
        path: str,
        error: str | None,
        batch_id: str,
        skipped: bool,
        extra: dict[str, Any] | None,
    ) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k.startswith("EMBYTOOL_") is False}
        env.update({
            "EMBYTOOL_EVENTTYPE": event,
            "EMBYTOOL_ITEM_ID": item_id,
            "EMBYTOOL_ITEM_NAME": item_name,
            "EMBYTOOL_PATH": path,
            "EMBYTOOL_BATCH_ID": batch_id,
            "EMBYTOOL_SKIPPED": "1" if skipped else "0",
        })
        if error:
            env["EMBYTOOL_ERROR"] = str(error)
        if extra:
            for key, value in extra.items():
                env[f"EMBYTOOL_{str(key).upper()}"] = str(value)
        return env

    def _run_scripts(self, env: dict[str, str], event: str, item_name: str) -> None:
        for script in self._scripts:
            try:
                completed = subprocess.run(
                    [script],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=self._timeout,
                )
                if completed.stdout and completed.stdout.strip():
                    logger.info("[notify] %s: %s", Path(script).name, completed.stdout.strip()[:500])
                if completed.returncode != 0:
                    logger.warning(
                        "[notify] 脚本 %s 返回码 %s（事件 %s，条目 %s，不影响下载任务）",
                        script, completed.returncode, event, item_name,
                    )
                    if completed.stderr and completed.stderr.strip():
                        logger.warning("[notify] stderr: %s", completed.stderr.strip()[:500])
            except FileNotFoundError:
                logger.warning("[notify] 通知脚本不存在：%s", script)
            except subprocess.TimeoutExpired:
                logger.warning("[notify] 通知脚本超时（%.0fs）：%s", self._timeout, script)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[notify] 通知脚本执行失败（忽略）：%s -> %s", script, exc)
