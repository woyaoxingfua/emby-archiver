"""预分配分段引擎：单文件写入、持久进度与换源恢复。"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from downloader import DownloadCancelled, Downloader
from emby_client import EmbyClient
from models import MediaItem


def _client(cfg):
    client = EmbyClient(cfg)
    client.authenticate()
    return client


def _movie(item_id="block", name="Block Movie"):
    return MediaItem(item_id=item_id, name=name, type="Movie", container="mp4", year=2020)


def _paths(downloader, item):
    target = downloader._build_target_path(item)
    part = target.with_name(target.name + ".part")
    metadata = part.with_name(part.name + ".meta.json")
    return target, part, metadata


def test_multipart_preallocates_single_part_without_merge(cfg, library, mock_emby):
    size = 400_000
    mock_emby.add_movie("block", "Block Movie", size=size)
    item = _movie()
    downloader = Downloader(_client(cfg), library, segments=4)
    target, part, metadata = _paths(downloader, item)
    observed = {}
    lock = threading.Lock()

    def inspect_progress(status, _item, downloaded, _expected, _path, _detail):
        with lock:
            if status != "progress" or observed:
                return
            observed["part_size"] = part.stat().st_size if part.exists() else None
            observed["segments"] = list(part.parent.glob(part.name + ".seg*"))
            observed["metadata"] = json.loads(metadata.read_text(encoding="utf-8")) if metadata.exists() else None
            observed["downloaded"] = downloaded

    downloader.progress_callback = inspect_progress
    path = downloader.download_item(item)

    assert observed["part_size"] == size
    assert observed["segments"] == []
    assert observed["metadata"]["total"] == size
    assert path.read_bytes() == mock_emby.files["block"]
    assert not part.exists() and not metadata.exists()


def _cancel_with_partial_state(cfg, library, mock_emby, item_id="resume"):
    size = 400_000
    mock_emby.add_movie(item_id, "Resume Movie", size=size, etag='"source-one"')
    item = _movie(item_id, "Resume Movie")
    downloader = Downloader(_client(cfg), library, segments=4)
    target, part, metadata = _paths(downloader, item)
    cancelled = threading.Event()

    def cancel_on_progress(status, _item, downloaded, _expected, _path, _detail):
        if status == "progress" and downloaded >= 65_536 and not cancelled.is_set():
            cancelled.set()
            downloader.cancel(cleanup_temp=False)

    downloader.progress_callback = cancel_on_progress
    with pytest.raises(DownloadCancelled):
        downloader.download_item(item)
    assert cancelled.is_set()
    return item, target, part, metadata, size


def test_preallocated_download_resumes_from_atomic_metadata(cfg, library, mock_emby, noop_progress):
    item, target, part, metadata, size = _cancel_with_partial_state(cfg, library, mock_emby)

    assert part.stat().st_size == size
    saved = json.loads(metadata.read_text(encoding="utf-8"))
    received = sum(segment["received"] for segment in saved["segments"])
    assert 0 < received < size
    assert "mock-token" not in metadata.read_text(encoding="utf-8")
    assert "source_url" not in saved

    downloader = Downloader(_client(cfg), library, progress_callback=noop_progress, segments=4)
    path = downloader.download_item(item)

    assert path.read_bytes() == mock_emby.files["resume"]
    assert not part.exists() and not metadata.exists()


def test_preallocated_resume_restarts_after_source_change(cfg, library, mock_emby, noop_progress):
    item, target, part, metadata, size = _cancel_with_partial_state(cfg, library, mock_emby, "changed")
    new_data = b"\xef" * size
    mock_emby.files["changed"] = new_data
    mock_emby.behavior["changed"]["etag"] = '"source-two"'

    downloader = Downloader(_client(cfg), library, progress_callback=noop_progress, segments=4)
    path = downloader.download_item(item)

    assert path == target
    assert path.read_bytes() == new_data
    assert library.get("changed").etag == '"source-two"'
