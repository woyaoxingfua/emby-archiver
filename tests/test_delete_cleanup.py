"""删除记录后的空目录清理：Web 与 GUI 路径语义一致。

黑盒 Web 验收发现：删除 `影片名 (年份)/影片名 (年份).mp4` 后影片目录残留。
契约：
- 删除文件后，其所在空目录链被清理，止步于下载根；
- 下载根本身绝不删除；
- 目录内仍有其他文件时保留；
- 只有目标文件被删才可能触发目录清理（文件本来就不存在时不动目录）。
"""
from __future__ import annotations

from pathlib import Path

from app_service import AppService
from downloader import Downloader
from emby_client import EmbyClient
from library import CacheLibrary
from mock_emby import MockEmby
from models import MediaItem


def _movie(item_id="clean"):
    return MediaItem(item_id=item_id, name=f"Movie {item_id}", type="Movie", container="mp4", year=2020)


def _service(cfg, library) -> AppService:
    service = AppService()
    service.config = cfg
    service.client = EmbyClient(cfg)
    service.client.authenticate()
    service.library = library
    service.downloader = Downloader(service.client, library, segments=1)
    return service


def test_delete_record_removes_empty_dirs_up_to_root(cfg, library, mock_emby, noop_progress):
    mock_emby.add_movie("clean", "Movie clean", size=50_000)
    service = _service(cfg, library)
    downloader = Downloader(service.client, library, progress_callback=noop_progress, segments=1)
    item = _movie()
    path = downloader.download_item(item)
    # 目标位于 <download_dir>/Movie clean (2020)/Movie clean (2020).mp4
    assert path.parent != cfg.download_dir
    assert path.exists()

    result = service.delete_record("clean")

    assert result["removed_file"] is True
    assert result["removed_empty_dirs"] >= 1
    assert not path.parent.exists()          # 影片目录已清
    assert cfg.download_dir.exists()         # 下载根保留


def test_delete_record_keeps_dir_with_other_content(cfg, library, mock_emby, noop_progress):
    mock_emby.add_movie("keep", "Movie keep", size=50_000)
    service = _service(cfg, library)
    downloader = Downloader(service.client, library, progress_callback=noop_progress, segments=1)
    path = downloader.download_item(_movie("keep"))
    sibling = path.parent / "其他文件.txt"
    sibling.write_text("用户文件", encoding="utf-8")

    result = service.delete_record("keep")

    assert result["removed_file"] is True
    assert result["removed_empty_dirs"] == 0
    assert path.parent.exists()              # 有内容的目录保留
    assert sibling.exists()


def test_delete_record_missing_file_still_cleans_empty_dir(cfg, library, mock_emby, noop_progress):
    """文件已被外部删除仅剩空目录时，删除记录应顺带清掉空壳目录。

    用户删除记录的意图是清掉这个条目；此时目录内没有任何用户文件，
    清理是安全且符合意图的（有内容时仍保留，见 keep 测试）。
    """
    mock_emby.add_movie("gone", "Movie gone", size=50_000)
    service = _service(cfg, library)
    downloader = Downloader(service.client, library, progress_callback=noop_progress, segments=1)
    path = downloader.download_item(_movie("gone"))
    path.unlink()  # 文件已被外部删除，仅剩空目录

    result = service.delete_record("gone")

    assert result["removed_file"] is False
    assert result["removed_empty_dirs"] >= 1
    assert not path.parent.exists()          # 空壳目录一并清理


def test_cleanup_never_crosses_download_root(cfg, library, mock_emby, noop_progress):
    """override_dir 覆盖目录本身就是第一层：删除后覆盖目录保留。"""
    import tempfile

    override = Path(tempfile.mkdtemp())
    cfg2 = cfg
    cfg2.download_dir = override  # 用 override 作为下载根
    mock_emby.add_movie("edge", "Movie edge", size=50_000)
    service = _service(cfg2, library)
    downloader = Downloader(service.client, library, progress_callback=noop_progress, segments=1)
    path = downloader.download_item(_movie("edge"))

    service.delete_record("edge")

    assert override.exists()                  # 下载根永不清
    assert not (override / path.parent.name).exists()
