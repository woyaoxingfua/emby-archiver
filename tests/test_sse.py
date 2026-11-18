"""SSE 状态推送、鉴权与 worker 配置入口。"""
from __future__ import annotations

import json

from starlette.testclient import TestClient

from app_service import AppService
from downloader import Downloader
from emby_client import EmbyClient
from webapp import HTML_PAGE, create_web_app


TOKEN = "test-token-ABC"
AUTH = {"X-Auth-Token": TOKEN}


def _app(cfg, library, mock_emby):
    service = AppService()
    service.config = cfg
    service.client = EmbyClient(cfg)
    service.client.authenticate()
    service.library = library
    service.downloader = Downloader(service.client, library, progress_callback=service._download_progress, segments=1)
    return service, TestClient(create_web_app(service, token=TOKEN), base_url="http://localhost")


def test_sse_requires_auth(cfg, library, mock_emby):
    _, client = _app(cfg, library, mock_emby)
    assert client.get("/api/events").status_code == 401


def test_sse_streams_snapshots_and_log_updates(cfg, library, mock_emby):
    service, client = _app(cfg, library, mock_emby)
    with client.stream("GET", "/api/events", headers=AUTH) as response:
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-cache"
        lines = response.iter_lines()
        assert next(lines) == "event: status"
        initial = json.loads(next(lines).removeprefix("data: "))
        assert initial["status"]["status"] == "idle"
        assert next(lines) == ""

        service._record_log("sse-update")
        assert next(lines) == "event: status"
        updated = json.loads(next(lines).removeprefix("data: "))
        assert "sse-update" in updated["logs"][-1]
        assert next(lines) == ""


def test_worker_count_api_and_event_source_page(cfg, library, mock_emby):
    service, client = _app(cfg, library, mock_emby)
    response = client.post("/api/control/workers", json={"workers": 3}, headers=AUTH)
    assert response.status_code == 200
    assert response.json()["workers"] == 3
    assert service.config.download_workers == 3
    assert 'new EventSource("/api/events")' in HTML_PAGE
    assert "refreshAll\", 3000" not in HTML_PAGE
    assert "refreshAll', 3000" not in HTML_PAGE
