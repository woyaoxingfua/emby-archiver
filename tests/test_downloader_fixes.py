"""下载器安全/正确性修复回归测试（N1/N18/N4/N2/N9/N11/N20 + 解析加固）。"""
from __future__ import annotations

from pathlib import Path

import pytest

from downloader import DownloadError, Downloader, _parse_content_range, _safe_ext, _sanitize
from emby_client import EmbyClient
from models import MediaItem


def _client(cfg):
    c = EmbyClient(cfg)
    c.authenticate()
    return c


def _dl(cfg, library, noop_progress):
    return Downloader(_client(cfg), library, progress_callback=noop_progress, segments=cfg.segments)


def _movie(mock, item_id):
    d = mock.details[item_id]
    return MediaItem(item_id=d["Id"], name=d["Name"], type="Movie",
                     container=d["Container"], year=d.get("ProductionYear"))


# ---------- N18 扩展名白名单 / 文件名消毒 ----------

def test_safe_ext_whitelist():
    assert _safe_ext("matroska,webm") == "mkv"
    assert _safe_ext("mov,mp4,m4a,3gp") == "mp4"
    assert _safe_ext("MKV") == "mkv"
    assert _safe_ext(None) == "mp4"
    assert _safe_ext("totally-unknown") == "bin"


def test_safe_ext_blocks_injection():
    assert _safe_ext("mp4/../../../../windows/temp/evil") == "bin"
    assert _safe_ext("..\\..\\evil.bat") == "bin"
    assert _safe_ext("mp4\x00.bat") == "bin"


def test_sanitize_hardening():
    assert _sanitize("CON") == "CON_"
    assert _sanitize("NUL") == "NUL_"
    assert _sanitize("COM1") == "COM1_"
    assert _sanitize("Name . ") == "Name"
    assert _sanitize('a/b\\c:d') == "a_b_c_d"
    assert _sanitize("") == "untitled"
    assert _sanitize("[Group] Show") == "[Group] Show"  # 方括号保留（合法字符）
    assert len(_sanitize("A" * 300)) <= 200


# ---------- N1 路径穿越 containment ----------

def test_container_traversal_neutralized(cfg, library, mock_emby, noop_progress):
    dl = _dl(cfg, library, noop_progress)
    evil = MediaItem(item_id="evil", name="Innocent", type="Movie",
                     container="mp4/../../../../windows/temp/evil")
    # 白名单把恶意 container 中和为 .bin，路径仍安全落在下载目录内
    p = dl._build_target_path(evil)
    assert p.resolve().is_relative_to(cfg.download_dir.resolve())
    assert p.suffix == ".bin"
    assert not Path(cfg.download_dir.parent / "windows").exists()


def test_containment_backstop(cfg, library, mock_emby, noop_progress, monkeypatch):
    # 兜底：即便扩展名消毒被绕过，containment 仍拒绝越界路径
    import downloader as dlmod
    monkeypatch.setattr(dlmod, "_safe_ext", lambda c: "mp4/../../../../evil")
    dl = _dl(cfg, library, noop_progress)
    evil = MediaItem(item_id="evil2", name="Innocent", type="Movie", container="mp4")
    with pytest.raises(DownloadError):
        dl._build_target_path(evil)


def test_normal_path_still_inside(cfg, library, mock_emby, noop_progress):
    mock_emby.add_movie("ok", "Normal Movie", size=1000)
    dl = _dl(cfg, library, noop_progress)
    p = dl._build_target_path(_movie(mock_emby, "ok"))
    assert p.resolve().is_relative_to(cfg.download_dir.resolve())


# ---------- 解析加固 ----------

def test_parse_content_range_sanity():
    assert _parse_content_range("bytes 9-1/20") is None
    assert _parse_content_range("bytes 0-10/5") is None
    assert _parse_content_range("bytes 0-0/12345") == (0, 0, 12345)


# ---------- N4 续传损坏链 ----------

def test_resume_bad_start_restarts(cfg, library, mock_emby, noop_progress):
    cfg.segments = 1
    mock_emby.add_movie("bs", "BadStart", size=200_000, mode="bad_start")
    dl = _dl(cfg, library, noop_progress)
    item = _movie(mock_emby, "bs")
    target = dl._build_target_path(item)
    target.parent.mkdir(parents=True, exist_ok=True)
    Path(str(target) + ".part").write_bytes(b"GARBAGE" * 200)
    path = dl.download_item(item)
    assert path.read_bytes() == mock_emby.files["bs"]
    assert library.get("bs").status == "completed"


def test_bad_total_range_rejected_restarts_cleanly(cfg, library, mock_emby, noop_progress):
    # 撒谎的 Content-Range（end>=total）被判非法 → 触发从头重下 → 文件正确，不被固化损坏
    cfg.segments = 1
    mock_emby.add_movie("bt", "BadTotal", size=200_000, mode="bad_total")
    dl = _dl(cfg, library, noop_progress)
    item = _movie(mock_emby, "bt")
    target = dl._build_target_path(item)
    target.parent.mkdir(parents=True, exist_ok=True)
    Path(str(target) + ".part").write_bytes(b"GARBAGE" * 200)
    path = dl.download_item(item)
    assert path.read_bytes() == mock_emby.files["bt"]
    assert library.get("bt").status == "completed"


def test_short_response_marked_partial(cfg, library, mock_emby, noop_progress):
    # N4：final != expected（此处 final < expected）必须判 partial，绝不 completed
    cfg.segments = 1
    mock_emby.add_movie("sh", "ShortMovie", size=200_000, mode="short_206")
    dl = _dl(cfg, library, noop_progress)
    with pytest.raises(DownloadError):
        dl.download_item(_movie(mock_emby, "sh"))
    assert library.get("sh").status != "completed"


def test_gzip_stream_download_correct(cfg, library, mock_emby, noop_progress):
    cfg.segments = 1
    mock_emby.add_movie("gz", "GzipMovie", size=150_000, mode="gzip")
    dl = _dl(cfg, library, noop_progress)
    path = dl.download_item(_movie(mock_emby, "gz"))
    assert path.read_bytes() == mock_emby.files["gz"]
    assert library.get("gz").status == "completed"


# ---------- N11 段级重试 ----------

def test_multipart_flaky_retries(cfg, library, mock_emby, noop_progress):
    cfg.segments = 4
    mock_emby.add_movie("fl", "FlakyMovie", size=400_000, mode="flaky", fail_times=1)
    dl = _dl(cfg, library, noop_progress)
    path = dl.download_item(_movie(mock_emby, "fl"))
    assert path.read_bytes() == mock_emby.files["fl"]
    assert library.get("fl").status == "completed"


# ---------- N9 方括号标题 seg 清理 ----------

def test_bracket_title_seg_cleanup(cfg, library, mock_emby, noop_progress):
    cfg.segments = 4
    mock_emby.add_movie("br", "[Group] Show", size=300_000, container="mkv")
    dl = _dl(cfg, library, noop_progress)
    path = dl.download_item(_movie(mock_emby, "br"))
    assert path.read_bytes() == mock_emby.files["br"]
    leftovers = [p for p in path.parent.iterdir() if ".seg" in p.name]
    assert leftovers == []


# ---------- N2 分段失败进度保全 + 续传 ----------

def test_multipart_failure_preserves_and_resumes(cfg, library, mock_emby, noop_progress):
    cfg.segments = 4
    mock_emby.add_movie("pv", "PreserveMovie", size=400_000, mode="flaky", fail_times=999)
    dl = _dl(cfg, library, noop_progress)
    item = _movie(mock_emby, "pv")
    target = dl._build_target_path(item)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(str(target) + ".part")
    seg0 = temp.with_suffix(".part.seg0")
    seg0.write_bytes(mock_emby.files["pv"][:1000])  # 预置部分进度

    with pytest.raises(DownloadError):
        dl.download_item(item)
    # 关键断言：失败后 seg0 断点必须仍在、字节未被清零
    assert seg0.exists()
    assert seg0.stat().st_size >= 1000

    # 网络恢复后续传，应利用已有断点完成且字节正确
    mock_emby.behavior["pv"]["mode"] = "normal"
    dl2 = _dl(cfg, library, noop_progress)
    path = dl2.download_item(item)
    assert path.read_bytes() == mock_emby.files["pv"]
    assert library.get("pv").status == "completed"
