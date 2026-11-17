"""优化批次 B：令牌桶限速。"""
from __future__ import annotations

import time

from downloader import Downloader, _TokenBucket
from emby_client import EmbyClient
from models import MediaItem


def test_token_bucket_throttles():
    b = _TokenBucket(50_000)  # 50 KB/s
    b.acquire(50_000)  # 耗尽初始 1s 突发
    t0 = time.monotonic()
    b.acquire(50_000)  # 需补充约 1s
    assert time.monotonic() - t0 >= 0.8


def test_token_bucket_unlimited_noop():
    b = _TokenBucket(0)
    t0 = time.monotonic()
    b.acquire(10_000_000)
    assert time.monotonic() - t0 < 0.1


def _download(cfg, library, mock_emby, noop_progress, iid, kbps):
    cfg.rate_limit_kbps = kbps
    mock_emby.add_movie(iid, "Movie", size=200_000)
    client = EmbyClient(cfg)
    client.authenticate()
    dl = Downloader(client, library, progress_callback=noop_progress, segments=1)
    item = MediaItem(item_id=iid, name="Movie", type="Movie", container="mp4")
    t0 = time.monotonic()
    dl.download_item(item)
    return time.monotonic() - t0


def test_rate_limited_download_slower(cfg, library, mock_emby, noop_progress):
    elapsed = _download(cfg, library, mock_emby, noop_progress, "rl", 100)  # ~100 KB/s
    assert library.get("rl").status == "completed"
    assert elapsed >= 0.5  # 200KB 在 100KB/s 下约 1s


def test_unlimited_download_fast(cfg, library, mock_emby, noop_progress):
    elapsed = _download(cfg, library, mock_emby, noop_progress, "ul", 0)
    assert library.get("ul").status == "completed"
    assert elapsed < 0.5