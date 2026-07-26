from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(slots=True)
class AppConfig:
    server_url: str
    download_dir: Path
    database_path: Path
    device_name: str = "emby-cache-tool"
    user_agent: str = "emby-cache-tool/0.1"
    username: Optional[str] = None
    password: Optional[str] = None
    api_key: Optional[str] = None
    access_token: Optional[str] = None
    user_id: Optional[str] = None
    timeout: int = 30
    chunk_size: int = 1024 * 1024
    segments: int = 4
    experimental_force_multipart: bool = False
    proxy_http: Optional[str] = None
    proxy_https: Optional[str] = None
    log_dir: Path = Path("logs")
    log_level: str = "INFO"
    stream_segments: int = 2


@dataclass(slots=True)
class MediaItem:
    item_id: str
    name: str
    type: str
    year: Optional[int] = None
    series_name: Optional[str] = None
    season_name: Optional[str] = None
    episode_number: Optional[int] = None
    parent_index_number: Optional[int] = None
    container: Optional[str] = None
    overview: Optional[str] = None


@dataclass(slots=True)
class DownloadRecord:
    item_id: str
    item_name: str
    item_type: str
    target_path: str
    status: str
    bytes_downloaded: int
    expected_size: Optional[int] = None
    download_mode: Optional[str] = None
    source_url: Optional[str] = None
