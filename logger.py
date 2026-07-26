from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional


_CONFIGURED_KEY: tuple[str, str, bool] | None = None


def setup_logging(
    log_dir: Path,
    level: str = "INFO",
    console: bool = True,
) -> logging.Logger:
    """Configure root logger for the application.

    Returns the same logger instance so CLI and GUI share handlers.
    """
    global _CONFIGURED_KEY

    log_dir.mkdir(parents=True, exist_ok=True)
    level_name = level.upper()
    config_key = (str(log_dir.resolve()), level_name, console)
    date_str = datetime.now().strftime("%Y%m%d")
    log_file = log_dir / f"emby-cache-tool_{date_str}.log"

    root = logging.getLogger("emby_cache_tool")
    root.setLevel(getattr(logging, level_name, logging.INFO))

    if _CONFIGURED_KEY == config_key and root.handlers:
        return root

    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    if console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        root.addHandler(console_handler)

    _CONFIGURED_KEY = config_key
    root.info("Logging initialized: log_dir=%s, level=%s", log_dir, level_name)
    return root


def get_logger(name: str = "emby_cache_tool") -> logging.Logger:
    return logging.getLogger(name)


def parse_log_level(value: Optional[str]) -> str:
    if not value:
        return "INFO"
    allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
    upper = str(value).strip().upper()
    return upper if upper in allowed else "INFO"
