"""优化批次 D：MediaSources 多源择优。"""
from __future__ import annotations

from emby_client import EmbyClient, _pick_media_source
from models import MediaItem


def test_pick_media_source_unit():
    assert _pick_media_source([]) == {}
    assert _pick_media_source([{"Id": "a", "Size": 1}, {"Id": "b", "Size": 5}])["Id"] == "b"
    assert _pick_media_source([{"Id": "a"}, {"Id": "b"}])["Id"] == "a"  # 无 Size 回退首个
    assert _pick_media_source([{"Id": "a", "Size": "bad"}, {"Id": "b", "Size": 3}])["Id"] == "b"


def test_download_candidates_use_largest_source(cfg, library, mock_emby):
    mock_emby.add_movie("ms", "Multi", size=100_000)
    mock_emby.details["ms"]["MediaSources"] = [
        {"Id": "small", "Container": "mp4", "Size": 100_000},
        {"Id": "big", "Container": "mkv", "Size": 900_000},
    ]
    client = EmbyClient(cfg)
    client.authenticate()
    item = MediaItem(item_id="ms", name="Multi", type="Movie", container="mp4")
    candidates = client.get_download_candidates(item)
    assert any("MediaSourceId=big" in u for u in candidates)
    assert not any("MediaSourceId=small" in u for u in candidates)