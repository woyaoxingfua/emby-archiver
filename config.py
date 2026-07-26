from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from models import AppConfig
from logger import parse_log_level


DEFAULT_CONFIG_PATH = Path("config.json")


class ConfigError(RuntimeError):
    pass


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> AppConfig:
    if not path.exists():
        raise ConfigError(
            f"Config file not found: {path}. Copy config.example.json to config.json and fill it first."
        )

    raw = json.loads(path.read_text(encoding="utf-8"))
    server_url = str(raw.get("server_url", "")).strip().rstrip("/")
    download_dir = Path(raw.get("download_dir", "downloads"))
    database_path = Path(raw.get("database_path", "store/cache.db"))

    if not server_url:
        raise ConfigError("server_url is required")

    shared_proxy = _clean_optional(raw.get("proxy"))
    proxy_http = _clean_optional(raw.get("proxy_http")) or shared_proxy
    proxy_https = _clean_optional(raw.get("proxy_https")) or shared_proxy

    cfg = AppConfig(
        server_url=server_url,
        username=_clean_optional(raw.get("username")),
        password=_clean_optional(raw.get("password")),
        api_key=_clean_optional(raw.get("api_key")),
        access_token=_clean_optional(raw.get("access_token")),
        user_id=_clean_optional(raw.get("user_id")),
        download_dir=download_dir,
        database_path=database_path,
        device_name=str(raw.get("device_name", "emby-cache-tool")).strip() or "emby-cache-tool",
        user_agent=str(raw.get("user_agent", "emby-cache-tool/0.1")).strip() or "emby-cache-tool/0.1",
        timeout=int(raw.get("timeout", 30)),
        chunk_size=int(raw.get("chunk_size", 1024 * 1024)),
        segments=int(raw.get("segments", 4)),
        experimental_force_multipart=_as_bool(raw.get("experimental_force_multipart", False)),
        proxy_http=proxy_http,
        proxy_https=proxy_https,
        log_dir=Path(raw.get("log_dir", "logs")),
        log_level=parse_log_level(raw.get("log_level")),
        stream_segments=max(1, int(raw.get("stream_segments", 2))),
    )

    cfg.download_dir.mkdir(parents=True, exist_ok=True)
    cfg.database_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def save_default_download_dir(config_path: Path, download_dir: str | Path) -> None:
    """仅更新 config.json 中的 download_dir 字段，原子写回，保留其他所有字段原样。

    安全约束：
    - 不通过 AppConfig dataclass 重建整个 JSON，避免丢失未建模字段或改变凭证内容/顺序。
    - 只写入 download_dir 这一个键，绝不触碰 username / password / api_key 等敏感字段。
    - 先写入临时文件再 os.replace，保证写回过程原子、不会留下半截文件。
    - 不打印、不返回任何凭证内容。

    Args:
        config_path: 配置文件路径（默认与 load_config 一致，为相对路径 config.json）。
        download_dir: 新的默认下载目录。接受字符串或 Path；为相对路径时直接存原始字符串，
            不做额外规范化，以保持与用户期望一致。

    Raises:
        ConfigError: 当 config_path 不存在时（与 load_config 行为保持一致）。
    """
    if not config_path.exists():
        raise ConfigError(
            f"Config file not found: {config_path}. Copy config.example.json to config.json and fill it first."
        )

    # 读取现有配置原文，保留所有字段（含凭证）原样不动
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["download_dir"] = str(download_dir)

    # 原子写回：先写临时文件，再 os.replace 替换，避免半截文件或并发写入损坏
    with NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(config_path.parent) or ".",
        prefix=f".{config_path.name}.",
        suffix=".tmp",
        delete=False,
    ) as tmp_file:
        json.dump(raw, tmp_file, indent=2, ensure_ascii=False)
        tmp_path = Path(tmp_file.name)

    os.replace(tmp_path, config_path)


def _clean_optional(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    text = str(value).strip().lower()
    return text in {"1", "true", "yes", "on"}
