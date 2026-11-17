"""CLI 批量容错（N7）与 GUI 纯逻辑/导入冒烟（N9/N14 配套）。"""
from __future__ import annotations

import json
import sys

import pytest

import main as cli
from emby_client import EmbyClient
from library import CacheLibrary
from models import MediaItem


def test_cli_batch_tolerates_single_failure(cfg, library, mock_emby, tmp_path, monkeypatch, capsys):
    for iid in ("e1", "e2", "e3"):
        mock_emby.add_movie(iid, f"E{iid}", size=40_000)
    del mock_emby.files["e2"]
    del mock_emby.details["e2"]  # 中间一项详情 404 → 候选全失败
    items = [
        MediaItem(item_id="e1", name="E1", type="Movie", container="mp4"),
        MediaItem(item_id="e2", name="E2", type="Movie", container="mp4"),
        MediaItem(item_id="e3", name="E3", type="Movie", container="mp4"),
    ]
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({
        "server_url": cfg.server_url,
        "username": cfg.username,
        "password": cfg.password,
        "download_dir": str(cfg.download_dir),
        "database_path": str(cfg.database_path),
        "log_dir": str(cfg.log_dir),
        "segments": 1,
    }), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["main", "--config", str(config_file), "download", "--item-id", "s1"])
    monkeypatch.setattr(EmbyClient, "expand_download_items", lambda self, item_id: items)

    rc = cli.main()
    out = capsys.readouterr().out
    assert rc == 1  # 有失败项
    assert "1 failed" in out
    lib = CacheLibrary(cfg.database_path)
    assert lib.get("e1").status == "completed"
    assert lib.get("e3").status == "completed"  # 失败之后仍继续
    assert lib.get("e2").status != "completed"


def test_gui_seg_files_brackets(tmp_path):
    pytest.importorskip("tkinter")
    from gui import _seg_files
    part = tmp_path / "Title [1080p].mp4.part"
    (tmp_path / "Title [1080p].mp4.part.seg0").write_bytes(b"a")
    (tmp_path / "Title [1080p].mp4.part.seg1").write_bytes(b"b")
    (tmp_path / "unrelated.seg0").write_bytes(b"c")
    files = _seg_files(part)
    assert {p.name for p in files} == {"Title [1080p].mp4.part.seg0", "Title [1080p].mp4.part.seg1"}


def test_gui_imports():
    pytest.importorskip("tkinter")
    import gui
    assert hasattr(gui, "EmbyCacheGui")
    assert hasattr(gui, "_seg_files")
    assert hasattr(gui, "_open_path")