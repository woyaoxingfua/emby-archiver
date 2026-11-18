"""P0 数据正确性回归：分段换源与目标文件覆盖。"""
from __future__ import annotations

from pathlib import Path

import pytest

from downloader import DownloadError, Downloader
from emby_client import EmbyClient
from models import MediaItem


def _dl(cfg, library, noop_progress, segments=2) -> Downloader:
    client = EmbyClient(cfg)
    client.authenticate()
    return Downloader(client, library, progress_callback=noop_progress, segments=segments)


def _movie(mock, item_id: str) -> MediaItem:
    detail = mock.details[item_id]
    return MediaItem(
        item_id=detail["Id"],
        name=detail["Name"],
        type="Movie",
        container=detail["Container"],
        year=detail.get("ProductionYear"),
    )


def _seed_multipart_resume(dl: Downloader, library, item: MediaItem, data: bytes, etag: str) -> Path:
    target = dl._build_target_path(item)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.with_name(target.name + ".part")
    total = len(data)
    plan = dl._multipart_plan_from_total(total, 2)
    downloaded = 0
    for idx, start, end in plan:
        seg_path = temp_path.with_suffix(f".part.seg{idx}")
        if idx == 0:
            chunk = data[start:end + 1]
        else:
            chunk = data[start:start + (end - start + 1) // 2]
        seg_path.write_bytes(chunk)
        downloaded += len(chunk)
    source_url = dl.client.authorized_url(f"/Items/{item.item_id}/Download")
    library.upsert(
        item,
        target,
        "paused",
        downloaded,
        total,
        source_url,
        download_mode="分段并发下载（2 段）",
        etag=etag,
    )
    return target


def test_multipart_resume_restarts_when_etag_changes(cfg, library, mock_emby, noop_progress):
    mock_emby.add_movie("multi", "Multipart Source", size=1000, etag='"old-source"')
    item = _movie(mock_emby, "multi")
    old_data = mock_emby.files["multi"]
    dl = _dl(cfg, library, noop_progress)
    target = _seed_multipart_resume(dl, library, item, old_data, '"old-source"')

    new_data = b"\xab" * len(old_data)
    mock_emby.files["multi"] = new_data
    mock_emby.behavior["multi"] = {"mode": "normal", "etag": '"new-source"'}

    path = dl.download_item(item)

    assert path == target
    assert path.read_bytes() == new_data
    assert library.get("multi").etag == '"new-source"'
    assert not list(target.parent.glob("*.seg*"))


def test_multipart_resume_same_etag_sends_if_range(cfg, library, mock_emby, noop_progress):
    mock_emby.add_movie("same", "Multipart Resume", size=1000, etag='"same-source"')
    item = _movie(mock_emby, "same")
    data = mock_emby.files["same"]
    dl = _dl(cfg, library, noop_progress)
    target = _seed_multipart_resume(dl, library, item, data, '"same-source"')
    mock_emby.request_log.clear()

    path = dl.download_item(item)

    assert path.read_bytes() == data
    resumed = [r for r in mock_emby.request_log if r["range"] == "bytes=750-999"]
    assert resumed and all(r["if_range"] == '"same-source"' for r in resumed)


def test_same_name_movie_does_not_overwrite_other_item(cfg, library, mock_emby, noop_progress):
    mock_emby.add_movie("movie-a", "Same Title", size=50_000)
    mock_emby.add_movie("movie-b", "Same Title", size=60_000)
    dl = _dl(cfg, library, noop_progress, segments=1)
    item_a = _movie(mock_emby, "movie-a")
    item_b = _movie(mock_emby, "movie-b")
    target = dl.download_item(item_a)
    original = target.read_bytes()

    with pytest.raises(DownloadError, match="拒绝覆盖"):
        dl.download_item(item_b)

    assert target.read_bytes() == original
    assert library.get("movie-a").status == "completed"


def test_untracked_target_file_is_not_overwritten(cfg, library, mock_emby, noop_progress):
    mock_emby.add_movie("movie", "User File", size=50_000)
    dl = _dl(cfg, library, noop_progress, segments=1)
    item = _movie(mock_emby, "movie")
    target = dl._build_target_path(item)
    target.parent.mkdir(parents=True, exist_ok=True)
    original = b"existing user-owned file"
    target.write_bytes(original)

    with pytest.raises(DownloadError, match="拒绝覆盖"):
        dl.download_item(item)

    assert target.read_bytes() == original


def test_legacy_segments_are_checked_before_resume(cfg, library, mock_emby, noop_progress):
    mock_emby.add_movie("legacy", "Legacy Segments", size=4000, etag='"current-source"')
    item = _movie(mock_emby, "legacy")
    old_data = mock_emby.files["legacy"]
    dl = _dl(cfg, library, noop_progress, segments=4)
    target = dl._build_target_path(item)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.with_name(target.name + ".part")
    temp_path.with_suffix(".part.seg0").write_bytes(old_data[:1000])

    new_data = b"\xcd" * len(old_data)
    mock_emby.files["legacy"] = new_data

    path = dl.download_item(item)

    assert path.read_bytes() == new_data


def test_target_created_during_download_is_not_overwritten(cfg, library, mock_emby):
    mock_emby.add_movie("late", "Late Target", size=50_000)
    item = _movie(mock_emby, "late")
    client = EmbyClient(cfg)
    client.authenticate()
    target = cfg.download_dir / "Late Target (2020)" / "Late Target (2020).mp4"
    original = b"created by another process"
    created = False

    def create_target_on_progress(status, _item, _downloaded, _expected, _path, _detail):
        nonlocal created
        if status == "progress" and not created:
            target.write_bytes(original)
            created = True

    dl = Downloader(client, library, progress_callback=create_target_on_progress, segments=1)
    target.parent.mkdir(parents=True, exist_ok=True)

    with pytest.raises(DownloadError, match="拒绝覆盖"):
        dl.download_item(item)

    assert created
    assert target.read_bytes() == original
