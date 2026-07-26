from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

warnings.filterwarnings(
    "ignore",
    message=r"urllib3 .* doesn't match a supported version!",
)

from app_service import AppService
from logger import get_logger
from webapp import create_web_app

logger = get_logger()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Emby Cache Tool launcher", add_help=False)
    parser.add_argument("-h", "--help", action="store_true", help="Show help message")
    parser.add_argument("--mode", choices=["auto", "gui", "web", "cli"], default="auto", help="Startup mode")
    parser.add_argument("--config", default="config.json", help="Path to config json")
    parser.add_argument("--host", default="127.0.0.1", help="Web mode bind host")
    parser.add_argument("--port", type=int, default=8765, help="Web mode bind port")
    parser.add_argument("--log-dir", default=None, help="Directory for log files (overrides config)")
    parser.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Log level",
    )
    return parser


def run_gui(config_path: str) -> None:
    import tkinter as tk

    from gui import EmbyCacheGui

    root = tk.Tk()
    app = EmbyCacheGui(root)
    app.config_var.set(config_path)
    try:
        app.load_config_only()
    except Exception:
        pass
    root.mainloop()


def run_web(service: AppService, host: str, port: int) -> None:
    import uvicorn

    logger.info("Starting web mode on http://%s:%s", host, port)
    uvicorn.run(create_web_app(service), host=host, port=port, log_level="info")


def main() -> None:
    parser = build_parser()
    args, extra = parser.parse_known_args()

    if args.help and args.mode != "cli":
        parser.print_help()
        return

    if args.mode == "cli":
        from main import main as cli_main

        forwarded = []
        if args.config:
            forwarded.extend(["--config", args.config])
        if args.log_dir:
            forwarded.extend(["--log-dir", args.log_dir])
        if args.log_level:
            forwarded.extend(["--log-level", args.log_level])
        if args.help:
            forwarded.append("--help")
        sys.argv = [sys.argv[0], *forwarded, *extra]
        cli_main()
        return

    if args.mode == "gui":
        run_gui(args.config)
        return

    if args.mode == "web":
        service = AppService()
        service.load(
            Path(args.config),
            log_dir=Path(args.log_dir) if args.log_dir else None,
            log_level=args.log_level,
        )
        run_web(service, args.host, args.port)
        return

    try:
        run_gui(args.config)
    except Exception as exc:
        logger.warning("GUI unavailable, falling back to web mode: %s", exc)
        print(f"GUI 不可用，已回退到 Web 模式：http://{args.host}:{args.port}")
        service = AppService()
        service.load(
            Path(args.config),
            log_dir=Path(args.log_dir) if args.log_dir else None,
            log_level=args.log_level,
        )
        run_web(service, args.host, args.port)


if __name__ == "__main__":
    main()
