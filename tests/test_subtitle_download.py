"""字幕随行下载：主文件完成后保存外挂文本字幕，失败不阻断主文件。

契约：
- MediaStreams 中 Type=Subtitle 且 IsExternal 的流被拉取，保存为 <视频名>.<语言>.srt；
- 字幕接口失败/缺失只发诊断事件与警告日志，主文件仍 completed；
- download_subtitles=false 关闭该行为；
- 内嵌字幕（IsExternal=false）不拉取。
"""
from __future__ import annotations

from downloader import Downloader
from emby_client import EmbyClient
from models import MediaItem


def _client(cfg):
    client = EmbyClient(cfg)
    client.authenticate()
    return client


def _movie(item_id="sub"):
    return MediaItem(item_id=item_id, name=f"Movie {item_id}", type="Movie", container="mp4", year=2020)


def test_subtitle_saved_beside_video(cfg, library, mock_emby, noop_progress):
    mock_emby.add_movie("sub", "Movie sub", size=50_000)
    mock_emby.add_subtitle("sub", 3, language="chi")
    downloader = Downloader(_client(cfg), library, progress_callback=noop_progress, segments=1)
    target = downloader._build_target_path(_movie())

    path = downloader.download_item(_movie())

    assert library.get("sub").status == "completed"
    srt = target.with_name(f"{target.stem}.chi.srt")
    assert srt.exists()
    assert b"subtitle-sub" in srt.read_bytes()
    assert path.exists()


def test_subtitle_failure_does_not_block_completion(cfg, library, mock_emby, noop_progress):
    """字幕接口 404（字幕被服务端删除等）时主文件仍成功。"""
    mock_emby.add_movie("gone", "Movie gone", size=50_000)
    mock_emby.add_subtitle("gone", 5, language="eng")
    mock_emby.subtitle_files.pop(("gone", 5))  # 清单在但内容 404
    downloader = Downloader(_client(cfg), library, progress_callback=noop_progress, segments=1)
    diagnostics = []

    def collect(status, item, downloaded, expected, path, detail):
        if status == "diagnostic":
            diagnostics.append(str(detail))

    downloader.progress_callback = collect
    path = downloader.download_item(_movie("gone"))

    assert library.get("gone").status == "completed"
    assert path.exists()
    assert any("字幕下载失败" in d for d in diagnostics)


def test_subtitle_disabled_by_config(cfg, library, mock_emby, noop_progress):
    cfg.download_subtitles = False
    mock_emby.add_movie("off", "Movie off", size=50_000)
    mock_emby.add_subtitle("off", 1, language="chi")
    downloader = Downloader(_client(cfg), library, progress_callback=noop_progress, segments=1)
    target = downloader._build_target_path(_movie("off"))

    downloader.download_item(_movie("off"))

    srt = target.with_name(f"{target.stem}.chi.srt")
    assert not srt.exists()


def test_embedded_subtitle_not_fetched(cfg, library, mock_emby, noop_progress):
    """内嵌字幕（IsExternal=false）不拉取——离线播放器读不到容器内字幕流。"""
    mock_emby.add_movie("emb", "Movie emb", size=50_000)
    detail = mock_emby.details["emb"]
    detail["MediaSources"][0]["MediaStreams"] = [{
        "Index": 2, "Type": "Subtitle", "IsExternal": False,
        "Codec": "subrip", "Language": "chi", "DisplayTitle": "chi (Embedded)",
    }]
    downloader = Downloader(_client(cfg), library, progress_callback=noop_progress, segments=1)
    target = downloader._build_target_path(_movie("emb"))

    path = downloader.download_item(_movie("emb"))

    assert library.get("emb").status == "completed"
    assert not list(target.parent.glob("*.srt"))


def test_no_subtitles_no_extra_requests_or_files(cfg, library, mock_emby, noop_progress):
    mock_emby.add_movie("plain", "Movie plain", size=50_000)
    downloader = Downloader(_client(cfg), library, progress_callback=noop_progress, segments=1)
    target = downloader._build_target_path(_movie("plain"))

    path = downloader.download_item(_movie("plain"))

    assert library.get("plain").status == "completed"
    assert not list(target.parent.glob("*.srt"))
