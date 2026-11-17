"""Pytest 共享 fixture（适配当前 HEAD API）。

所有测试一律使用 tmp_path 下的配置、数据库、下载目录与日志目录，
绝不触碰用户真实的 config.json / store / downloads / logs。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config import AppConfig  # noqa: E402
from library import CacheLibrary  # noqa: E402
from mock_emby import MockEmby  # noqa: E402


@pytest.fixture()
def cfg(tmp_path: Path) -> AppConfig:
    return AppConfig(
        server_url="http://127.0.0.1:1",  # 会被 mock fixture 覆盖
        download_dir=tmp_path / "dl",
        database_path=tmp_path / "test.db",
        username="mockuser",
        password="goodpass",
        timeout=5,
        chunk_size=65536,
        segments=4,
        stream_segments=2,
        log_dir=tmp_path / "logs",
    )


@pytest.fixture()
def library(cfg: AppConfig) -> CacheLibrary:
    return CacheLibrary(cfg.database_path)


@pytest.fixture()
def mock_emby(cfg: AppConfig):
    server = MockEmby()
    server.start()
    cfg.server_url = server.base_url
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture()
def noop_progress():
    return lambda *args, **kwargs: None
