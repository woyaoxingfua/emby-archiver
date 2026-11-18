"""预分配分段引擎性能基准（AGENT_HANDOFF.md §8 P1）。

测量四类指标并打印数字（不做时间硬断言，避免 flaky）：
- 吞吐（MB/s）与墙钟；
- 磁盘持久化成本：os.fsync 总数、数据 fsync（总量 - 元数据写）、每 MB fsync 密度；
- 元数据更新频率：_write_multipart_metadata 次数；
- 恢复精度：中断后 meta received 与已落盘字节的一致性。

flush_interval_seconds 参数化 {0.0, 0.5}：0=旧行为（每 chunk 刷盘），
0.5=节流——同一环境前后对照。
"""
from __future__ import annotations

import json
import threading
import time

import pytest

import downloader as downloader_module
from downloader import DownloadCancelled, Downloader
from emby_client import EmbyClient
from models import MediaItem


BENCH_SIZE = 8_000_000  # 8MB：远大于 OS 页缓存吞掉的量级，fsync 成本可见
BENCH_SEGMENTS = 4


def _client(cfg):
    client = EmbyClient(cfg)
    client.authenticate()
    return client


def _movie(item_id, name):
    return MediaItem(item_id=item_id, name=name, type="Movie", container="mp4", year=2020)


def _paths(downloader, item):
    target = downloader._build_target_path(item)
    part = target.with_name(target.name + ".part")
    metadata = part.with_name(part.name + ".meta.json")
    return target, part, metadata


class _CostMeter:
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


@pytest.mark.parametrize("flush_interval", [0.0, 0.5])
def test_bench_multipart_throughput_and_costs(
    cfg, library, mock_emby, monkeypatch, noop_progress, flush_interval, request
):
    cfg.flush_interval_seconds = flush_interval
    mock_emby.add_movie("bench", "Bench Movie", size=BENCH_SIZE)
    item = _movie("bench", "Bench Movie")
    downloader = Downloader(_client(cfg), library, segments=BENCH_SEGMENTS)
    meter = _CostMeter(monkeypatch)

    start = time.perf_counter()
    path = downloader.download_item(item)
    elapsed = time.perf_counter() - start

    assert path.read_bytes() == mock_emby.files["bench"]
    target, part, metadata = _paths(downloader, item)
    assert not part.exists() and not metadata.exists()

    throughput = BENCH_SIZE / 1024 / 1024 / elapsed
    per_mb_fsync = meter.total_fsyncs / (BENCH_SIZE / 1024 / 1024)
    print(
        f"[bench:{request.node.name}] flush_interval={flush_interval}s "
        f"size={BENCH_SIZE} segments={BENCH_SEGMENTS} chunk={cfg.chunk_size} | "
        f"elapsed={elapsed:.3f}s throughput={throughput:.1f}MB/s | "
        f"fsyncs total={meter.total_fsyncs} data={meter.data_fsyncs} meta_writes={meter.meta_writes} | "
        f"fsync_per_MB={per_mb_fsync:.2f}"
    )
    # 机械性下限断言：保护计量本身有效，不设时间上限
    assert meter.total_fsyncs > 0
    assert meter.meta_writes >= BENCH_SEGMENTS


def test_bench_single_stream_baseline(cfg, library, mock_emby, monkeypatch, noop_progress):
    """单线程对照路径：零数据 fsync（纯 OS 缓冲），量级与分段路径形成对照。"""
    cfg.segments = 1
    mock_emby.add_movie("single", "Single Movie", size=1_000_000)
    item = _movie("single", "Single Movie")
    downloader = Downloader(_client(cfg), library, segments=1)
    meter = _CostMeter(monkeypatch)

    start = time.perf_counter()
    path = downloader.download_item(item)
    elapsed = time.perf_counter() - start

    assert path.read_bytes() == mock_emby.files["single"]
    throughput = 1_000_000 / 1024 / 1024 / elapsed
    print(
        f"[bench:single-stream] size=1MB chunk={cfg.chunk_size} | "
        f"elapsed={elapsed:.3f}s throughput={throughput:.1f}MB/s | "
        f"fsyncs total={meter.total_fsyncs} data={meter.data_fsyncs} meta_writes={meter.meta_writes}"
    )
    # 单线程路径无 meta、无 per-chunk fsync；剩余 fsync 只应来自库或其他模块（接近 0）
    assert meter.meta_writes == 0
    assert meter.data_fsyncs <= 2  # 允许极少量的非引擎路径 fsync


@pytest.mark.parametrize("flush_interval", [0.0, 0.5])
def test_bench_resume_precision_after_interruption(
    cfg, library, mock_emby, monkeypatch, flush_interval, request
):
    """恢复精度：部分进度取消后，meta received 必须与已落盘字节一致，续传内容正确。"""
    cfg.flush_interval_seconds = flush_interval
    size = 400_000
    mock_emby.add_movie("rp", "RP Movie", size=size, etag='"rp-one"')
    item = _movie("rp", "RP Movie")
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
    # 每段 received 区间内的字节与源一致（取消前已精确落盘）
    source = mock_emby.files["rp"]
    raw = part.read_bytes()
    for segment in saved["segments"]:
        assert raw[segment["start"]:segment["start"] + segment["received"]] == \
            source[segment["start"]:segment["start"] + segment["received"]]
    print(
        f"[bench:{request.node.name}] flush_interval={flush_interval}s | "
        f"received={received}/{size} precision=exact"
    )

    downloader2 = Downloader(_client(cfg), library, segments=4)
    path = downloader2.download_item(item)
    assert path.read_bytes() == source
    assert not part.exists() and not metadata.exists()
