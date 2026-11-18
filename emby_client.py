from __future__ import annotations

import platform
import threading
from typing import Any
from urllib.parse import urlencode, urlparse

import requests

from logger import get_logger
from models import AppConfig, MediaItem


class EmbyClientError(RuntimeError):
    pass


logger = get_logger()


def _pick_media_source(sources: list) -> dict:
    """优化D：多版本媒体源择优——选 Size 最大者（默认最高质量），无 Size 时回退首个。"""
    if not sources:
        return {}

    def _size(s: dict) -> int:
        try:
            return int(s.get("Size") or 0)
        except (TypeError, ValueError):
            return 0

    return max(sources, key=_size)


class EmbyClient:
    VERSION = "0.1"

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.access_token = config.access_token
        self.api_key = config.api_key
        self.user_id = config.user_id
        self.device_id = platform.node() or config.device_name.replace(" ", "-")
        # P3：连接超时与读取超时分离，避免连接阶段长时间挂起
        self._timeout = (min(10, config.timeout), config.timeout)
        self._session_local = threading.local()
        self._sessions_lock = threading.Lock()
        self._sessions: set[requests.Session] = set()
        if config.proxy_http or config.proxy_https:
            logger.info("HTTP proxies enabled: http=%s https=%s", config.proxy_http or "-", config.proxy_https or "-")
        self._refresh_auth_headers()

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update({"User-Agent": self.config.user_agent, "Accept": "application/json"})
        if self.config.proxy_http:
            session.proxies["http"] = self.config.proxy_http
        if self.config.proxy_https:
            session.proxies["https"] = self.config.proxy_https
        self._apply_auth_headers(session)
        return session

    @property
    def session(self) -> requests.Session:
        session = getattr(self._session_local, "session", None)
        if session is None:
            session = self._build_session()
            self._session_local.session = session
            with self._sessions_lock:
                self._sessions.add(session)
        return session

    def close(self) -> None:
        with self._sessions_lock:
            sessions = list(self._sessions)
            self._sessions.clear()
        for session in sessions:
            session.close()
        self._session_local.session = None

    def authenticate(self) -> dict[str, Any]:
        if self.access_token or self.api_key:
            logger.info("Authenticating with pre-configured token/api_key")
            user = self.get_current_user(optional=True)
            if user:
                self.user_id = self.user_id or str(user.get("Id"))
                self._refresh_auth_headers()
                return user
            if self.api_key and self.user_id:
                return {"Id": self.user_id, "Name": "Configured API user"}

        if not (self.config.username and self.config.password):
            logger.error("Authentication config incomplete: no token/api_key and no username/password")
            raise EmbyClientError(
                "Authentication failed. Provide username/password, or a valid access_token/api_key."
            )

        logger.info("Authenticating with username/password for %s", self.config.username)
        last_error: str | None = None
        for payload in (
            {"Username": self.config.username, "Pw": self.config.password},
            {"Username": self.config.username, "pw": self.config.password},
            {"Username": self.config.username, "Password": self.config.password},
        ):
            response = self.session.post(
                self._url("/Users/AuthenticateByName"),
                json=payload,
                headers={"X-Emby-Authorization": self._authorization_header(include_token=False)},
                timeout=self._timeout,
            )
            if response.ok:
                data = response.json()
                self.access_token = data.get("AccessToken")
                user = data.get("User") or {}
                self.user_id = str(user.get("Id")) if user.get("Id") else None
                self._refresh_auth_headers()
                logger.info("Authentication succeeded: user=%s user_id=%s", user.get("Name", "unknown"), self.user_id)
                return user
            last_error = self._error_message(response)
            logger.warning("Authentication attempt failed: %s", last_error)

        raise EmbyClientError(f"Login failed: {last_error or 'unknown error'}")

    def get_current_user(self, optional: bool = False) -> dict[str, Any] | None:
        try:
            response = self._request("GET", "/Users/Me")
        except EmbyClientError:
            if optional:
                return None
            raise
        return response.json()

    def list_views(self) -> list[dict[str, Any]]:
        self._ensure_user_id()
        response = self._request("GET", f"/Users/{self.user_id}/Views")
        return response.json().get("Items", [])

    def search_items(self, keyword: str, limit: int = 20) -> list[MediaItem]:
        self._ensure_user_id()
        response = self._request(
            "GET",
            f"/Users/{self.user_id}/Items",
            params={
                "Recursive": "true",
                "SearchTerm": keyword,
                "IncludeItemTypes": "Movie,Series,Season,Episode,Video",
                "Limit": str(limit),
                "Fields": "BasicSyncInfo,MediaSources,Overview,Path",
            },
        )
        return [self._to_media_item(item) for item in response.json().get("Items", [])]

    def get_item(self, item_id: str) -> MediaItem:
        self._ensure_user_id()
        response = self._request(
            "GET",
            f"/Users/{self.user_id}/Items/{item_id}",
            params={"Fields": "MediaSources,Overview,Path"},
        )
        return self._to_media_item(response.json())

    def get_children(self, parent_id: str) -> list[MediaItem]:
        self._ensure_user_id()
        response = self._request(
            "GET",
            f"/Users/{self.user_id}/Items",
            params={
                "ParentId": parent_id,
                "Recursive": "false",
                "Fields": "MediaSources,Overview,Path",
                "SortBy": "ParentIndexNumber,IndexNumber,SortName",
            },
        )
        return [self._to_media_item(item) for item in response.json().get("Items", [])]

    def expand_download_items(self, item_id: str) -> list[MediaItem]:
        root = self.get_item(item_id)
        if root.type in {"Movie", "Episode", "Video"}:
            return [root]
        if root.type == "Season":
            return [item for item in self.get_children(root.item_id) if item.type in {"Episode", "Video"}]
        if root.type == "Series":
            episodes: list[MediaItem] = []
            for season in self.get_children(root.item_id):
                if season.type != "Season":
                    continue
                episodes.extend(
                    item for item in self.get_children(season.item_id) if item.type in {"Episode", "Video"}
                )
            return episodes
        raise EmbyClientError(f"Unsupported item type for download: {root.type}")

    def get_download_candidates(self, item: MediaItem) -> list[str]:
        detail = self._request(
            "GET",
            f"/Users/{self.user_id}/Items/{item.item_id}",
            params={"Fields": "MediaSources"},
        ).json()
        media_sources = detail.get("MediaSources") or []
        source = _pick_media_source(media_sources)
        media_source_id = source.get("Id")
        container = item.container or source.get("Container") or "mp4"

        candidates = [
            self.authorized_url(f"/Items/{item.item_id}/Download"),
            self.authorized_url(
                f"/Items/{item.item_id}/Download",
                {"MediaSourceId": str(media_source_id)} if media_source_id else None,
            ),
            self.authorized_url(
                f"/Videos/{item.item_id}/stream",
                {"Static": "true", "MediaSourceId": str(media_source_id)} if media_source_id else {"Static": "true"},
            ),
            self.authorized_url(
                f"/Videos/{item.item_id}/stream.{container}",
                {"Static": "true", "MediaSourceId": str(media_source_id)} if media_source_id else {"Static": "true"},
            ),
        ]
        return list(dict.fromkeys(candidates))

    def open_stream(self, url: str, resume_from: int = 0, if_range: str | None = None) -> requests.Response:
        headers = {"Accept-Encoding": "identity"}  # P3：禁止 gzip，保证 Content-Length/Range 语义正确
        if resume_from > 0:
            headers["Range"] = f"bytes={resume_from}-"
            if if_range:
                headers["If-Range"] = if_range  # N16：源同一性校验，ETag 不匹配时服务端返 200 全量
        response = self.session.get(
            url,
            headers=headers,
            stream=True,
            timeout=self._timeout,
            allow_redirects=True,
        )
        if response.status_code in {200, 206}:
            return response
        raise EmbyClientError(self._error_message(response))

    def describe_candidate(self, url: str) -> str:
        parsed = urlparse(url)
        path = parsed.path or url
        if path.endswith("/Download"):
            if "MediaSourceId=" in parsed.query:
                return "Items/Download + MediaSourceId"
            return "Items/Download"
        if "/stream." in path:
            suffix = path.rsplit("/", 1)[-1]
            return f"Videos/{suffix}"
        if path.endswith("/stream"):
            return "Videos/stream"
        return path.rsplit("/", 2)[-1] if "/" in path else path

    def authorized_url(self, path: str, extra_params: dict[str, str] | None = None) -> str:
        params = dict(extra_params or {})
        token = self.api_key or self.access_token
        if token:
            params.setdefault("api_key", token)
        query = urlencode(params)
        return f"{self._url(path)}?{query}" if query else self._url(path)

    def _apply_auth_headers(self, session: requests.Session) -> None:
        session.headers["X-Emby-Authorization"] = self._authorization_header(include_token=True)
        token = self.api_key or self.access_token
        if token:
            session.headers["X-Emby-Token"] = token
        else:
            session.headers.pop("X-Emby-Token", None)

    def _refresh_auth_headers(self) -> None:
        self.session  # 确保当前线程的 Session 已创建并登记
        with self._sessions_lock:
            sessions = list(self._sessions)
        for session in sessions:
            self._apply_auth_headers(session)

    def _authorization_header(self, include_token: bool) -> str:
        parts = [
            f'Client="{self.config.device_name}"',
            f'Device="{self.config.device_name}"',
            f'DeviceId="{self.device_id}"',
            f'Version="{self.VERSION}"',
        ]
        token = self.api_key or self.access_token
        if include_token and token:
            parts.append(f'Token="{token}"')
        return "Emby " + ", ".join(parts)

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        response = self.session.request(
            method,
            self._url(path),
            timeout=self._timeout,
            **kwargs,
        )
        if not response.ok:
            raise EmbyClientError(self._error_message(response))
        return response

    def _url(self, path: str) -> str:
        return f"{self.config.server_url}{path}"

    def _ensure_user_id(self) -> None:
        if not self.user_id:
            user = self.authenticate()
            user_id = user.get("Id") if isinstance(user, dict) else None
            self.user_id = self.user_id or (str(user_id) if user_id else None)
        if not self.user_id:
            raise EmbyClientError("Unable to determine Emby user id. Set user_id in config.json.")

    @staticmethod
    def _error_message(response: requests.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            payload = response.text.strip()
        return f"HTTP {response.status_code}: {payload}"

    @staticmethod
    def _to_media_item(item: dict[str, Any]) -> MediaItem:
        media_sources = item.get("MediaSources") or []
        source = _pick_media_source(media_sources)
        return MediaItem(
            item_id=str(item.get("Id")),
            name=str(item.get("Name") or "Unnamed"),
            type=str(item.get("Type") or "Unknown"),
            year=item.get("ProductionYear"),
            series_name=item.get("SeriesName"),
            season_name=item.get("SeasonName"),
            episode_number=item.get("IndexNumber"),
            parent_index_number=item.get("ParentIndexNumber"),
            container=item.get("Container") or source.get("Container"),
            overview=item.get("Overview"),
        )
