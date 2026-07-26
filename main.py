from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

warnings.filterwarnings(
    "ignore",
    message=r"urllib3 .* doesn't match a supported version!",
)

from config import ConfigError, load_config
from downloader import DownloadError, Downloader
from emby_client import EmbyClient, EmbyClientError
from library import CacheLibrary
from logger import setup_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Offline downloader for Emby shared accounts")
    parser.add_argument("--config", default="config.json", help="Path to config json")
    parser.add_argument("--log-dir", default=None, help="Directory for log files (overrides config)")
    parser.add_argument("--log-level", default=None, choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], help="Log level")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("login-test", help="Authenticate and print current user")
    subparsers.add_parser("libraries", help="List available library views")

    search = subparsers.add_parser("search", help="Search media by keyword")
    search.add_argument("keyword", help="Keyword to search")
    search.add_argument("--limit", type=int, default=20, help="Max results")

    download = subparsers.add_parser("download", help="Download one movie or a season/series")
    download.add_argument("--item-id", required=True, help="Emby item id")

    subparsers.add_parser("downloads", help="Show cached download records")
    subparsers.add_parser("resume", help="Resume unfinished downloads")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        config = load_config(Path(args.config))
        log_dir = Path(args.log_dir) if args.log_dir else config.log_dir
        log_level = args.log_level if args.log_level else config.log_level
        setup_logging(log_dir, log_level)
        client = EmbyClient(config)
        library = CacheLibrary(config.database_path)
        downloader = Downloader(client, library, segments=config.segments)

        if args.command == "login-test":
            user = client.authenticate()
            print(f"Connected as: {user.get('Name', 'unknown')} ({user.get('Id', 'n/a')})")
            return 0

        if args.command == "downloads":
            for record in library.list_all():
                size = record.expected_size or 0
                mode = record.download_mode or "-"
                print(
                    f"{record.status}\t{record.item_type}\t{record.item_name}\t{record.bytes_downloaded}/{size}\t{mode}\t{record.target_path}"
                )
            return 0

        if args.command == "libraries":
            client.authenticate()
            for view in client.list_views():
                print(f"{view.get('Name')}\t{view.get('CollectionType', '-') }\t{view.get('Id')}")
            return 0

        if args.command == "search":
            client.authenticate()
            results = client.search_items(args.keyword, limit=args.limit)
            for item in results:
                year = f" ({item.year})" if item.year else ""
                print(f"{item.item_id}\t{item.type}\t{item.name}{year}")
            return 0

        if args.command == "download":
            client.authenticate()
            items = client.expand_download_items(args.item_id)
            print(f"Downloading {len(items)} item(s)...")
            for item in items:
                path = downloader.download_item(item)
                print(f"Saved: {path}")
            return 0

        if args.command == "resume":
            client.authenticate()
            records = list(library.list_incomplete())
            if not records:
                print("No unfinished downloads.")
                return 0
            for record in records:
                item = client.get_item(record.item_id)
                path = downloader.download_item(item)
                print(f"Saved: {path}")
            return 0

        parser.error("Unknown command")
        return 2
    except (ConfigError, EmbyClientError, DownloadError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
