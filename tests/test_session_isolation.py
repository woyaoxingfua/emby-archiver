"""每个分段线程使用独立 requests.Session，并继承相同认证配置。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading

from emby_client import EmbyClient


def test_session_is_thread_local_and_preserves_auth(cfg):
    client = EmbyClient(cfg)
    main_session = client.session
    barrier = threading.Barrier(2)

    def worker_session(_):
        session = client.session
        barrier.wait(timeout=5)
        return session, session.headers.get("X-Emby-Token"), session.headers.get("User-Agent"), session is client.session

    with ThreadPoolExecutor(max_workers=2) as executor:
        sessions = list(executor.map(worker_session, range(2)))

    assert len({id(main_session), *(id(entry[0]) for entry in sessions)}) == 3
    expected_token = client.access_token or client.api_key
    for session, token, user_agent, reused in sessions:
        assert token == expected_token
        assert user_agent == cfg.user_agent
        assert reused

    client.close()
    assert client.session is not main_session
    client.close()
