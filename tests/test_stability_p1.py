"""P1 稳定性回归：任务删除、磁盘预检与 GUI 生命周期。"""
from __future__ import annotations

import queue
from pathlib import Path
from types import SimpleNamespace

import pytest

from app_service import AppService
from downloader import DownloadError, Downloader
from emby_client import EmbyClient
from models import MediaItem


def _service(cfg, library) -> AppService:
    service = AppService()
    service.config = cfg
    service.client = EmbyClient(cfg)
    service.client.authenticate()
    service.library = library
    service.downloader = Downloader(service.client, library, segments=1)
    return service


def test_delete_refuses_when_active_worker_does_not_stop(cfg, library, mock_emby, monkeypatch):
    service = _service(cfg, library)
    item = MediaItem(item_id="stuck", name="Stuck", type="Movie", container="mp4")
    target = cfg.download_dir / "Stuck.mp4"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"keep me")
    library.upsert(item, target, "downloading", 0, 7, None)

    class StuckWorker:
        def is_alive(self):
            return True

        def join(self, timeout=None):
            return None

    service._worker = StuckWorker()
    service._download_state["item_id"] = "stuck"
    monkeypatch.setattr(service.downloader, "cancel", lambda cleanup_temp=False: None)

    with pytest.raises(RuntimeError, match="尚未停止"):
        service.delete_record("stuck")

    assert target.read_bytes() == b"keep me"
    assert library.get("stuck") is not None


def test_multipart_space_check_includes_merge_destination(tmp_path, monkeypatch):
    downloader = Downloader.__new__(Downloader)
    monkeypatch.setattr(
        "downloader.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=109),
    )

    with pytest.raises(DownloadError, match="磁盘空间不足"):
        downloader._check_disk_space(tmp_path, expected_size=100, resume_from=90, multipart=True)


def test_multipart_download_invokes_space_preflight(cfg, library, mock_emby, noop_progress, monkeypatch):
    mock_emby.add_movie("space", "Space Check", size=100_000)
    client = EmbyClient(cfg)
    client.authenticate()
    downloader = Downloader(client, library, progress_callback=noop_progress, segments=2)
    calls = []
    original = downloader._check_disk_space

    def record_check(folder, expected_size, resume_from, multipart):
        calls.append((expected_size, resume_from, multipart))
        return original(folder, expected_size, resume_from, multipart)

    monkeypatch.setattr(downloader, "_check_disk_space", record_check)
    item = MediaItem(item_id="space", name="Space Check", type="Movie", container="mp4", year=2020)
    downloader.download_item(item)

    assert calls and calls[0][0] == 100_000


class _Var:
    def __init__(self, value=None):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


def test_gui_load_reconciles_interrupted_records(cfg, library, monkeypatch):
    pytest.importorskip("tkinter")
    import gui

    item = MediaItem(item_id="old", name="Old", type="Movie")
    library.upsert(item, Path("old.mp4"), "downloading", 1, 2, None)
    window = gui.EmbyCacheGui.__new__(gui.EmbyCacheGui)
    window.config_var = _Var("unused.json")
    window.segments_var = _Var()
    window.download_dir_var = _Var()
    window.status_var = _Var()
    window.logs = []
    window.log = window.logs.append
    window.refresh_downloads = lambda: None
    monkeypatch.setattr(gui, "load_config", lambda _path: cfg)
    monkeypatch.setattr(gui, "setup_logging", lambda *_args: None)
    monkeypatch.setattr(gui, "CacheLibrary", lambda _path: library)
    monkeypatch.setattr(gui, "EmbyClient", lambda config: SimpleNamespace(config=config))
    monkeypatch.setattr(gui, "Downloader", lambda client, _library, **_kwargs: SimpleNamespace(client=client))

    window.load_config_only()

    assert library.get("old").status == "interrupted"


def test_gui_batch_continues_after_item_failure(cfg, library, mock_emby):
    pytest.importorskip("tkinter")
    import gui

    for item_id in ("g1", "g2", "g3"):
        mock_emby.add_movie(item_id, f"Movie {item_id}", size=40_000)
    items = [
        MediaItem(item_id=item_id, name=f"Movie {item_id}", type="Movie", container="mp4", year=2020)
        for item_id in ("g1", "g2", "g3")
    ]
    del mock_emby.files["g2"]
    del mock_emby.details["g2"]
    client = EmbyClient(cfg)
    client.authenticate()
    client.expand_download_items = lambda _item_id: items

    window = gui.EmbyCacheGui.__new__(gui.EmbyCacheGui)
    window.client = client
    window.library = library
    window.downloader = Downloader(client, library, progress_callback=lambda *_args: None, segments=1)
    window.event_queue = queue.Queue()
    window._current_download_item = None

    window._download_task(items[0])

    assert library.get("g1").status == "completed"
    assert library.get("g2").status != "completed"
    assert library.get("g3").status == "completed"
    messages = [payload for event, payload in list(window.event_queue.queue) if event == "log"]
    assert any("成功 2 / 失败 1" in str(message) for message in messages)
