"""真实 uvicorn 全链路冒烟：端到端下载/删除、控制端点、N5 事件循环不阻塞证明。"""
from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

from app_service import AppService
from webapp import create_web_app

TOKEN = "smoke-token-XYZ"
AUTH = {"X-Auth-Token": TOKEN}


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture()
def live_server(cfg, mock_emby, tmp_path):
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({
        "server_url": cfg.server_url,
        "username": cfg.username,
        "password": cfg.password,
        "download_dir": str(cfg.download_dir),
        "database_path": str(cfg.database_path),
        "log_dir": str(cfg.log_dir),
        "segments": 2,
    }), encoding="utf-8")
    svc = AppService()
    svc.load(config_file)
    app = create_web_app(svc, token=TOKEN)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "uvicorn 未能启动"
    try:
        yield f"http://127.0.0.1:{port}", svc
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_smoke_download_delete_end_to_end(live_server, mock_emby):
    base, _ = live_server
    mock_emby.add_movie("sm1", "SmokeMovie", size=120_000)
    with httpx.Client(base_url=base, headers=AUTH, timeout=15) as c:
        assert c.get("/api/downloads").status_code == 200
        assert c.post("/api/download", json={"item_id": "sm1"}).status_code == 200
        rec = None
        for _ in range(150):
            recs = c.get("/api/downloads").json()["records"]
            rec = next((x for x in recs if x["item_id"] == "sm1"), None)
            if rec and rec["status"] == "completed":
                break
            time.sleep(0.1)
        assert rec and rec["status"] == "completed"
        assert "api_key=" not in json.dumps(rec)  # N3：接口不泄漏凭据
        target = Path(rec["target_path"])
        assert target.exists() and target.stat().st_size == 120_000
        assert c.delete("/api/downloads/sm1").status_code == 200
        assert not target.exists()
        recs = c.get("/api/downloads").json()["records"]
        assert all(x["item_id"] != "sm1" for x in recs)


def test_smoke_control_endpoints(live_server, mock_emby):
    base, _ = live_server
    mock_emby.add_movie("sm2", "ControlMovie", size=300_000, mode="slow", delay=1.0)
    with httpx.Client(base_url=base, headers=AUTH, timeout=15) as c:
        assert c.post("/api/download", json={"item_id": "sm2"}).status_code == 200
        time.sleep(0.3)
        assert c.post("/api/control/pause").json().get("ok") is True
        assert c.post("/api/control/resume").json().get("ok") is True
        assert c.post("/api/control/cancel", json={"cleanup_temp": True}).json().get("ok") is True


def test_smoke_n5_event_loop_not_blocked(live_server, monkeypatch):
    base, svc = live_server

    def slow_search(keyword, limit=50):
        time.sleep(1.0)
        return []

    monkeypatch.setattr(svc, "search", slow_search)
    results: dict = {}

    def do_search():
        with httpx.Client(base_url=base, headers=AUTH, timeout=15) as c:
            t0 = time.time()
            c.get("/api/search?keyword=x")
            results["search"] = time.time() - t0

    def do_status():
        time.sleep(0.2)  # 让慢搜索先发起
        with httpx.Client(base_url=base, headers=AUTH, timeout=15) as c:
            t0 = time.time()
            r = c.get("/api/status")
            results["status"] = time.time() - t0
            results["code"] = r.status_code

    ts = threading.Thread(target=do_search)
    tt = threading.Thread(target=do_status)
    ts.start()
    tt.start()
    ts.join()
    tt.join()
    assert results["search"] >= 0.9
    assert results["code"] == 200
    assert results["status"] < 0.6, f"事件循环被慢请求阻塞：status 耗时 {results['status']:.2f}s"