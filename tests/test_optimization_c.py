"""优化批次 C：暂停即断连 / 恢复重开 Range（单线程中途暂停 + 分段暂停）。"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from downloader import Downloader
from emby_client import EmbyClient
from models import MediaItem


def _client(cfg):
    c = EmbyClient(cfg)
    c.authenticate()
    return c


def test_single_pause_midstream_resume(cfg, library, mock_emby):
    mock_emby.add_movie("pm", "PauseMid", size=300_000)
    dl = Downloader(_client(cfg), library, segments=1)
    item = MediaItem(item_id="pm", name="PauseMid", type="Movie", container="mp4")
    triggered = threading.Event()

    def cb(status, it, downloaded, expected, path, detail):
        if status == "progress" and downloaded >= 65536 and not triggered.is_set():
            dl.pause()
            triggered.set()

    dl.progress_callback = cb
    result = {}

    def run():
        result["path"] = dl.download_item(item)

    t = threading.Thread(target=run)
    t.start()
    assert triggered.wait(5), "暂停未触发"
    for _ in range(100):
        if library.get("pm").status == "paused":
            break
        time.sleep(0.05)
    rec = library.get("pm")
    assert rec.status == "paused"
    assert rec.bytes_downloaded >= 65536  # 中途暂停，保留断点
    dl.resume()
    t.join(timeout=10)
    assert not t.is_alive()
    assert library.get("pm").status == "completed"
    assert Path(result["path"]).stat().st_size == 300_000


def test_segment_pause_resume(cfg, library, mock_emby):
    mock_emby.add_movie("ps", "PauseSeg", size=400_000)
    dl = Downloader(_client(cfg), library, segments=4)
    item = MediaItem(item_id="ps", name="PauseSeg", type="Movie", container="mp4")
    triggered = threading.Event()

    def cb(status, it, downloaded, expected, path, detail):
        if status == "progress" and downloaded >= 65536 and not triggered.is_set():
            dl.pause()
            triggered.set()

    dl.progress_callback = cb
    result = {}

    def run():
        result["path"] = dl.download_item(item)

    t = threading.Thread(target=run)
    t.start()
    assert triggered.wait(5), "暂停未触发"
    time.sleep(0.3)  # 让各分段进入暂停等待（连接已释放）
    dl.resume()
    t.join(timeout=10)
    assert not t.is_alive()
    assert library.get("ps").status == "completed"
    assert Path(result["path"]).stat().st_size == 400_000