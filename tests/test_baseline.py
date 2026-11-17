"""基线特征测试（当前 HEAD API）：锁定修复前 already-correct 的行为。

这些测试在修复前后都必须保持绿色。
"""
from __future__ import annotations

import json
from pathlib import Path

from config import save_default_download_dir
from downloader import Downloader, _parse_content_range
from emby_client import EmbyClient
from models import MediaItem


# ---------- 纯函数 ----------

def test_segment_plan_full_coverage():
    dl = Downloader.__new__(Downloader)
    for total in (1, 2, 7, 10, 100, 1000003, 10**12 + 7):
        for n in (1, 2, 3, 4, 8, 16, 32):
            plan = dl._multipart_plan_from_total(total, n)
            assert plan[0][1] == 0
            assert plan[-1][2] == total - 1
            for a, b in zip(plan, plan[1:]):
                assert a[2] + 1 == b[1], (total, n, plan)
            assert sum(e - s + 1 for _, s, e in plan) == total


def test_parse_content_range():
    assert _parse_content_range("bytes 0-0/12345") == (0, 0, 12345)
    assert _parse_content_range("bytes 500-999/1000") == (500, 999, 1000)
    assert _parse_content_range("items 0-10/20") is None
    assert _parse_content_range("bytes 9-1/20") is None
    assert _parse_content_range("bytes 0-10/5") is None
    assert _parse_content_range("") is None


def test_describe_candidate_strips_query(cfg, mock_emby):
    client = EmbyClient(cfg)
    url = client.authorized_url("/Items/1/Download", {"static": "true"})
    desc = client.describe_candidate(url)
    assert "api_key" not in desc
    assert "Download" in desc


def test_config_atomic_write_preserves_keys(tmp_path: Path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"server_url": "http://x", "username": "u",
                             "password": "p", "download_dir": "./old"}), encoding="utf-8")
    save_default_download_dir(p, tmp_path / "new")
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert raw["download_dir"] == str(tmp_path / "new")
    assert raw["server_url"] == "http://x"
    assert raw["password"] == "p"
    assert not list(tmp_path.glob(".config.json.*.tmp"))


# ---------- 集成（mock 服务器） ----------

def _client(cfg) -> EmbyClient:
    client = EmbyClient(cfg)
    client.authenticate()
    return client


def _movie(mock, item_id: str) -> MediaItem:
    d = mock.details[item_id]
    return MediaItem(item_id=d["Id"], name=d["Name"], type="Movie",
                     container=d["Container"], year=d.get("ProductionYear"))


def _dl(cfg, library, noop_progress) -> Downloader:
    return Downloader(_client(cfg), library, progress_callback=noop_progress, segments=cfg.segments)


def test_single_thread_download(cfg, library, mock_emby, noop_progress):
    cfg.segments = 1
    mock_emby.add_movie("m1", "Test Movie", size=300_000)
    dl = _dl(cfg, library, noop_progress)
    path = dl.download_item(_movie(mock_emby, "m1"))
    assert path.read_bytes() == mock_emby.files["m1"]
    rec = library.get("m1")
    assert rec.status == "completed"
    assert rec.bytes_downloaded == 300_000
    assert rec.expected_size == 300_000


def test_multipart_download_and_cleanup(cfg, library, mock_emby, noop_progress):
    cfg.segments = 4
    mock_emby.add_movie("m2", "Multipart Movie", size=1_000_003)
    dl = _dl(cfg, library, noop_progress)
    path = dl.download_item(_movie(mock_emby, "m2"))
    assert path.read_bytes() == mock_emby.files["m2"]
    assert library.get("m2").status == "completed"
    assert list(path.parent.glob("*.seg*")) == []
    assert not Path(str(path) + ".part").exists()


def test_resume_gets_200_restarts_cleanly(cfg, library, mock_emby, noop_progress):
    cfg.segments = 1
    mock_emby.add_movie("m3", "NoRange Movie", size=200_000, mode="no_range")
    dl = _dl(cfg, library, noop_progress)
    item = _movie(mock_emby, "m3")
    target = dl._build_target_path(item)
    target.parent.mkdir(parents=True, exist_ok=True)
    Path(str(target) + ".part").write_bytes(b"GARBAGE-PREFIX" * 100)
    path = dl.download_item(item)
    assert path.read_bytes() == mock_emby.files["m3"]


def test_download_403_falls_back_to_stream(cfg, library, mock_emby, noop_progress):
    cfg.segments = 1
    mock_emby.add_movie("m4", "Denied Movie", size=150_000, download="403")
    dl = _dl(cfg, library, noop_progress)
    path = dl.download_item(_movie(mock_emby, "m4"))
    assert path.read_bytes() == mock_emby.files["m4"]
    assert "/stream" in (library.get("m4").source_url or "")


def test_force_restart_redownloads(cfg, library, mock_emby, noop_progress):
    cfg.segments = 1
    mock_emby.add_movie("m5", "Restart Movie", size=120_000)
    dl = _dl(cfg, library, noop_progress)
    item = _movie(mock_emby, "m5")
    dl.download_item(item)
    path = dl.download_item(item, force_restart=True)
    assert path.read_bytes() == mock_emby.files["m5"]
    assert library.get("m5").status == "completed"


def test_library_crud_and_incomplete(cfg, library):
    item_a = MediaItem(item_id="a", name="A", type="Movie")
    item_b = MediaItem(item_id="b", name="B", type="Movie")
    item_c = MediaItem(item_id="c", name="C", type="Movie")
    library.upsert(item_a, Path("a.mp4"), "downloading", 0, None, None)
    library.upsert(item_b, Path("b.mp4"), "completed", 1, 1, None)
    library.upsert(item_c, Path("c.mp4"), "paused", 0, None, None)
    assert {r.item_id for r in library.list_incomplete()} == {"a", "c"}
    assert library.get("a").item_name == "A"
    library.delete("a")
    assert library.get("a") is None
