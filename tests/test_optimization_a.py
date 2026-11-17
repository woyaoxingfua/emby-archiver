"""优化批次 A：命名碰撞兜底、候选失败标记 failed、N16 ETag/If-Range 源同一性、重试配置化。"""
from __future__ import annotations

import pytest

from downloader import Downloader, DownloadError
from emby_client import EmbyClient
from models import MediaItem


def _dl(cfg, library, noop_progress, segments=1):
    client = EmbyClient(cfg)
    client.authenticate()
    return Downloader(client, library, progress_callback=noop_progress, segments=segments)


def test_episode_naming_collision_no_index(cfg, library, noop_progress):
    dl = Downloader(EmbyClient(cfg), library, progress_callback=noop_progress, segments=1)
    a = MediaItem(item_id="ep-a", name="Pilot", type="Episode", series_name="Show", parent_index_number=1, episode_number=None, container="mkv")
    b = MediaItem(item_id="ep-b", name="Pilot", type="Episode", series_name="Show", parent_index_number=1, episode_number=None, container="mkv")
    pa = dl._build_target_path(a)
    pb = dl._build_target_path(b)
    assert pa != pb  # 缺 IndexNumber 不再互相覆盖
    assert "ep-a" in pa.name and "ep-b" in pb.name
    # 正常集编号不受影响
    c = MediaItem(item_id="ep-c", name="Pilot", type="Episode", series_name="Show", parent_index_number=1, episode_number=2, container="mkv")
    assert "S01E02" in dl._build_target_path(c).name


def test_candidate_fetch_failure_marks_failed(cfg, library, mock_emby, noop_progress):
    mock_emby.add_movie("gone", "Gone", size=50_000)
    del mock_emby.details["gone"]  # 详情 404 → get_download_candidates 抛错
    del mock_emby.files["gone"]
    dl = _dl(cfg, library, noop_progress)
    item = MediaItem(item_id="gone", name="Gone", type="Movie", container="mp4")
    with pytest.raises(DownloadError):
        dl.download_item(item)
    rec = library.get("gone")
    assert rec is not None and rec.status == "failed"  # 不再停留 downloading


def test_etag_change_forces_restart(cfg, library, mock_emby, noop_progress):
    mock_emby.add_movie("m1", "Movie", size=100_000, mode="short_206")
    dl = _dl(cfg, library, noop_progress, segments=1)
    item = MediaItem(item_id="m1", name="Movie", type="Movie", container="mp4")
    with pytest.raises(DownloadError):
        dl.download_item(item)  # 只下一半 → partial，记录 etag=mock-etag-1
    rec = library.get("m1")
    assert rec.status == "failed"  # 全候选失败后 download_item 标记 failed（partial 为中间态）
    assert rec.etag == '"mock-etag-1"'
    part = cfg.download_dir / "Movie" / "Movie.mp4.part"
    assert part.exists() and part.stat().st_size == 50_000

    # 服务端换源：同尺寸但 ETag 变化
    mock_emby.behavior["m1"] = {"mode": "normal", "etag": '"new-etag-2"'}
    dl.download_item(item)  # 续传带 If-Range=旧etag → 服务端返 200 → 从头重下
    rec = library.get("m1")
    assert rec.status == "completed"
    assert rec.etag == '"new-etag-2"'
    final = cfg.download_dir / "Movie" / "Movie.mp4"
    assert final.exists() and final.stat().st_size == 100_000
    assert any(r.get("if_range") == '"mock-etag-1"' for r in mock_emby.request_log)


def test_retry_attempts_config(cfg, library, mock_emby, noop_progress, monkeypatch):
    dl = _dl(cfg, library, noop_progress, segments=2)
    # 限定单候选，避免 flaky 按 range 计数跨候选重试成功，干扰对重试次数的判定
    monkeypatch.setattr(dl.client, "get_download_candidates",
                        lambda item: [dl.client.authorized_url(f"/Items/{item.item_id}/Download")])
    item = MediaItem(item_id="fk", name="Flaky", type="Movie", container="mp4")

    mock_emby.add_movie("fk", "Flaky", size=80_000, mode="flaky", fail_times=1)
    cfg.retry_attempts = 1  # 只试一次 → 段首断即失败
    with pytest.raises(DownloadError):
        dl.download_item(item)
    assert library.get("fk").status != "completed"

    mock_emby.add_movie("fk2", "Flaky2", size=80_000, mode="flaky", fail_times=1)
    cfg.retry_attempts = 3  # 允许重试 → 第二次成功
    item2 = MediaItem(item_id="fk2", name="Flaky2", type="Movie", container="mp4")
    dl.download_item(item2)
    assert library.get("fk2").status == "completed"