"""Web 层安全回归（N8 鉴权/Host/CSRF、N3 接口脱敏、N6 XSS、N5 线程化、畸形 JSON）。"""
from __future__ import annotations

import sqlite3

from starlette.testclient import TestClient

from app_service import AppService
from downloader import Downloader
from emby_client import EmbyClient
from models import MediaItem
from webapp import HTML_PAGE, create_web_app

TOKEN = "test-token-ABC"


def _app(cfg, library, mock_emby):
    svc = AppService()
    svc.config = cfg
    svc.client = EmbyClient(cfg)
    svc.client.authenticate()
    svc.library = library
    svc.downloader = Downloader(svc.client, library, progress_callback=svc._download_progress, segments=1)
    app = create_web_app(svc, token=TOKEN)
    return svc, TestClient(app, base_url="http://localhost")


def test_api_requires_auth(cfg, library, mock_emby):
    _, client = _app(cfg, library, mock_emby)
    assert client.get("/api/downloads").status_code == 401
    assert client.post("/api/control/pause").status_code == 401


def test_wrong_token_rejected(cfg, library, mock_emby):
    _, client = _app(cfg, library, mock_emby)
    assert client.get("/?token=wrong").status_code == 401


def test_token_login_sets_cookie_then_authorized(cfg, library, mock_emby):
    _, client = _app(cfg, library, mock_emby)
    r = client.get(f"/?token={TOKEN}", follow_redirects=False)
    assert r.status_code == 302
    assert "emby_web_token" in r.cookies
    assert client.get("/api/downloads").status_code == 200  # cookie 自动携带


def test_header_auth(cfg, library, mock_emby):
    _, client = _app(cfg, library, mock_emby)
    r = client.get("/api/downloads", headers={"X-Auth-Token": TOKEN})
    assert r.status_code == 200


def test_host_rebinding_blocked(cfg, library, mock_emby):
    _, client = _app(cfg, library, mock_emby)
    r = client.get("/api/downloads", headers={"X-Auth-Token": TOKEN, "Host": "evil.com"})
    assert r.status_code == 403


def test_cross_origin_post_blocked(cfg, library, mock_emby):
    _, client = _app(cfg, library, mock_emby)
    r = client.post("/api/control/pause", headers={"X-Auth-Token": TOKEN, "Origin": "http://evil.com"})
    assert r.status_code == 403


def test_downloads_api_no_token_leak(cfg, library, mock_emby):
    svc, client = _app(cfg, library, mock_emby)
    mock_emby.add_movie("m1", "Movie", size=50_000)
    item = MediaItem(item_id="m1", name="Movie", type="Movie", container="mp4")
    svc.downloader.download_item(item)
    r = client.get("/api/downloads", headers={"X-Auth-Token": TOKEN})
    assert r.status_code == 200
    assert "mock-token-SECRET" not in r.text
    assert "api_key=" not in r.text


def test_legacy_token_redacted_in_api(cfg, library, mock_emby):
    svc, client = _app(cfg, library, mock_emby)
    library.upsert(MediaItem(item_id="x", name="X", type="Movie"), __import__("pathlib").Path("x.mp4"),
                   "completed", 1, 1, "http://h/x")
    conn = sqlite3.connect(cfg.database_path)
    conn.execute("UPDATE downloads SET source_url=? WHERE item_id=?", ("http://h/x?api_key=LEGACY", "x"))
    conn.commit()
    conn.close()
    r = client.get("/api/downloads", headers={"X-Auth-Token": TOKEN})
    assert "LEGACY" not in r.text


def test_malformed_json_400(cfg, library, mock_emby):
    _, client = _app(cfg, library, mock_emby)
    r = client.post("/api/download", content="{bad json",
                    headers={"Content-Type": "application/json", "X-Auth-Token": TOKEN})
    assert r.status_code == 400


def test_log_file_traversal_still_blocked(cfg, library, mock_emby):
    _, client = _app(cfg, library, mock_emby)
    r = client.get("/api/log-file?name=../../config.json", headers={"X-Auth-Token": TOKEN})
    assert r.status_code == 400


def test_xss_inline_onclick_removed():
    assert "onclick=\"resumeRecord('${" not in HTML_PAGE
    assert "onclick=\"deleteRecord('${" not in HTML_PAGE
    assert "onclick=\"startDownload('${" not in HTML_PAGE
    assert 'data-action="resume"' in HTML_PAGE
    assert 'data-action="download"' in HTML_PAGE
    assert "addEventListener('click'" in HTML_PAGE


def test_blocking_io_offloaded():
    import inspect
    import webapp
    src = inspect.getsource(webapp)
    assert "anyio.to_thread.run_sync" in src