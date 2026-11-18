"""flush_interval_seconds 节流：刷盘批量化的正确性与崩溃窗口。

核心不变量：meta 的 received 只在对应字节 flush+fsync 后更新——元数据永不
超前已刷盘数据；暂停/取消/段结束/请求失败一律强制精确落盘。
"""
from __future__ import annotations

import json
import threading

import pytest

import downloader as downloader_module
from downloader import DownloadCancelled, Downloader
from emby_client import EmbyClient
from models import MediaItem


def _client(cfg):
    client = EmbyClient(cfg)
    client.authenticate()
    return client


def _movie(item_id="throttle", name="Throttle Movie"):
    return MediaItem(item_id=item_id, name=name, type="Movie", container="mp4", year=2020)


def _paths(downloader, item):
    target = downloader._build_target_path(item)
    part = target.with_name(target.name + ".part")
    metadata = part.with_name(part.name + ".meta.json")
    return target, part, metadata


class _CostMeter:
    """monkeypatch 计量：总 os.fsync 次数与元数据原子写次数。

    os.fsync 收到的是整数 fd（拿不到文件名），无法按句柄区分数据/元数据；
    但 _write_multipart_metadata 每次内部恰好一次 .tmp fsync + os.replace，
    故 元数据 fsync 数 == meta_writes，数据 fsync 数 = total_fsyncs - meta_writes。
    """

    def __init__(self, monkeypatch):
        self.total_fsyncs = 0
        self.meta_writes = 0
        real_fsync = downloader_module.os.fsync
        real_write_meta = Downloader._write_multipart_metadata

        meter = self

        def counting_fsync(fd):
            meter.total_fsyncs += 1
            return real_fsync(fd)

        def counting_write_meta(meta_path, state):
            meter.meta_writes += 1
            return real_write_meta(meta_path, state)

        monkeypatch.setattr(downloader_module.os, "fsync", counting_fsync)
        monkeypatch.setattr(Downloader, "_write_multipart_metadata", staticmethod(counting_write_meta))

    @property
    def data_fsyncs(self) -> int:
        return self.total_fsyncs - self.meta_writes


def _download_ok(cfg, library, mock_emby, item_id, size, flush_interval, monkeypatch, expect=None):
    cfg.flush_interval_seconds = flush_interval
    mock_emby.add_movie(item_id, f"Movie {item_id}", size=size)
    item = _movie(item_id, f"Movie {item_id}")
    downloader = Downloader(_client(cfg), library, segments=4)
    meter = _CostMeter(monkeypatch)
    path = downloader.download_item(item)
    assert path.read_bytes() == mock_emby.files[item_id]
    target, part, metadata = _paths(downloader, item)
    assert not part.exists() and not metadata.exists()
    return meter, (size + cfg.chunk_size - 1) // cfg.chunk_size


def test_throttle_zero_keeps_per_chunk_fsync(cfg, library, mock_emby, monkeypatch, noop_progress):
    size = 400_000
    meter, chunks = _download_ok(cfg, library, mock_emby, "zero", size, 0.0, monkeypatch)
    # 旧行为：每 chunk 数据 fsync + 每 chunk 元数据写（每次元数据写内含一次 .tmp fsync）
    assert meter.data_fsyncs >= chunks
    assert meter.meta_writes >= chunks


def test_throttle_batches_fsync_and_meta_writes(cfg, library, mock_emby, monkeypatch, noop_progress):
    size = 400_000
    # 大间隔：400KB/4段 在本地 mock 下瞬间完成，节流后落盘次数远小于 chunk 数
    meter, chunks = _download_ok(cfg, library, mock_emby, "batched", size, 60.0, monkeypatch)
    assert chunks >= 6  # 计量前提：chunk 数足够多，节流才有批量空间
    assert meter.data_fsyncs < chunks, f"节流未生效：data fsyncs={meter.data_fsyncs} chunks={chunks}"
    assert meter.meta_writes < chunks, f"元数据写未合并：writes={meter.meta_writes} chunks={chunks}"
    # 每段收尾精确落盘：4 段至少 4 次数据 fsync 与元数据写
    assert meter.data_fsyncs >= 4
    assert meter.meta_writes >= 4


def test_throttle_pause_and_cancel_flush_precisely(cfg, library, mock_emby, monkeypatch):
    size = 400_000
    cfg.flush_interval_seconds = 60.0
    mock_emby.add_movie("pc", "PC Movie", size=size)
    item = _movie("pc", "PC Movie")
    downloader = Downloader(_client(cfg), library, segments=4)
    target, part, metadata = _paths(downloader, item)
    cancelled = threading.Event()

    def cancel_on_progress(status, _item, downloaded, _expected, _path, _detail):
        if status == "progress" and downloaded >= 100_000 and not cancelled.is_set():
            cancelled.set()
            downloader.cancel(cleanup_temp=False)

    downloader.progress_callback = cancel_on_progress
    with pytest.raises(DownloadCancelled):
        downloader.download_item(item)
    assert cancelled.is_set()

    saved = json.loads(metadata.read_text(encoding="utf-8"))
    received = sum(segment["received"] for segment in saved["segments"])
    assert 0 < received < size
    # 取消前强制落盘：meta received 与 .part 上的字节逐段一致。
    # 预分配文件恒为 total 大小，无法直接从文件长度分辨，改校验内容区间：
    # 每段 received 区间内的字节必须等于源数据（写的是真实字节，未写区为 truncate 零）。
    source = mock_emby.files["pc"]
    for segment in saved["segments"]:
        seg_bytes = part.read_bytes()[segment["start"]:segment["start"] + segment["received"]]
        assert seg_bytes == source[segment["start"]:segment["start"] + segment["received"]]


def test_throttle_crash_window_resume_from_metadata(cfg, library, mock_emby, monkeypatch):
    """节流下中断后重下：恢复起点 = meta received（不超前数据），最终内容正确。"""
    size = 400_000
    cfg.flush_interval_seconds = 60.0
    mock_emby.add_movie("cw", "CW Movie", size=size, etag='"cw-one"')
    item = _movie("cw", "CW Movie")
    downloader = Downloader(_client(cfg), library, segments=4)
    target, part, metadata = _paths(downloader, item)
    cancelled = threading.Event()

    def cancel_on_progress(status, _item, downloaded, _expected, _path, _detail):
        if status == "progress" and downloaded >= 100_000 and not cancelled.is_set():
            cancelled.set()
            downloader.cancel(cleanup_temp=False)

    downloader.progress_callback = cancel_on_progress
    with pytest.raises(DownloadCancelled):
        downloader.download_item(item)

    saved = json.loads(metadata.read_text(encoding="utf-8"))
    received = sum(segment["received"] for segment in saved["segments"])
    assert 0 < received < size  # 节流存在，但取消强制落盘保证有可恢复进度

    downloader2 = Downloader(_client(cfg), library, segments=4)
    path = downloader2.download_item(item)
    assert path.read_bytes() == mock_emby.files["cw"]
    assert not part.exists() and not metadata.exists()


def test_throttle_segment_completion_boundary(cfg, library, mock_emby, monkeypatch):
    """大间隔节流下正常完成：每段收尾强制落盘，段校验与防覆盖路径不受影响。"""
    size = 300_000
    meter, chunks = _download_ok(cfg, library, mock_emby, "bound", size, 60.0, monkeypatch)
    assert chunks >= 5
    # 4 段各自收尾一次 + 预分配初始 fsync：数据 fsync 至少 5 次
    assert meter.data_fsyncs >= 5
    assert meter.meta_writes >= 4
