"""迷你 Emby mock 服务器：用于下载器/服务层/Web 层的集成回归测试。

可控行为（按 item id 配置）：
- mode: normal(标准 Range/206) | no_range(永远 200) | bad_start(206 但 Content-Range 从 0 开始)
        | bad_total(Content-Range total 撒谎) | gzip(Content-Encoding: gzip)
        | chunked(无 Content-Length) | flaky(前 N 次请求直接断连) | slow(响应前延迟)
- download: ok | 403 | 401 | 500（/Items/{id}/Download 的行为，stream 始终可用）
- etag: 可变更，用于续传源同一性（If-Range）测试
- 恶意 payload：details/items 中可注入任意 Container / Id 字段

鉴权：Download/stream/search 要求 X-Emby-Token 头或 api_key query 等于 server.token。
"""
from __future__ import annotations

import gzip
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


def make_payload(size: int) -> bytes:
    block = bytes(range(256))
    return (block * (size // 256 + 1))[:size]


class MockEmby:
    def __init__(self) -> None:
        self.items: dict[str, dict] = {}
        self.details: dict[str, dict] = {}
        self.files: dict[str, bytes] = {}
        self.behavior: dict[str, dict] = {}
        self.subtitle_files: dict[tuple[str, int], bytes] = {}
        self.auth_password = "goodpass"
        self.token = "mock-token-SECRET-123"
        self.user_id = "u1"
        self.request_log: list[dict] = []
        self._fail_counters: dict[tuple, int] = {}
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # 静默
                pass

            def do_GET(self):
                outer._handle(self, "GET")

            def do_POST(self):
                outer._handle(self, "POST")

            def do_HEAD(self):
                outer._handle(self, "HEAD")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    # ---------- 生命周期 ----------
    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    # ---------- 数据装配 ----------
    def add_movie(self, item_id: str, name: str, size: int = 300_000,
                  container: str = "mp4", year: int = 2020, **behavior) -> None:
        self.items[item_id] = {
            "Id": item_id, "Name": name, "Type": "Movie",
            "ProductionYear": year, "Container": container,
        }
        self.details[item_id] = {
            "Id": item_id, "Name": name, "Type": "Movie",
            "ProductionYear": year, "Container": container,
            "MediaSources": [{"Id": "ms-" + item_id, "Container": container, "Size": size}],
        }
        self.files[item_id] = make_payload(size)
        self.behavior[item_id] = behavior

    def add_episode(self, item_id: str, series: str, season: int, episode: int,
                    size: int = 100_000, container: str = "mkv", **behavior) -> None:
        name = f"{series} S{season:02d}E{episode:02d}"
        self.items[item_id] = {
            "Id": item_id, "Name": name, "Type": "Episode",
            "SeriesName": series, "ParentIndexNumber": season,
            "IndexNumber": episode, "Container": container,
        }
        self.details[item_id] = {
            "Id": item_id, "Name": name, "Type": "Episode",
            "SeriesName": series, "ParentIndexNumber": season,
            "IndexNumber": episode, "Container": container,
            "MediaSources": [{"Id": "ms-" + item_id, "Container": container, "Size": size}],
        }
        self.files[item_id] = make_payload(size)
        self.behavior[item_id] = behavior

    def add_subtitle(self, item_id: str, stream_index: int, language: str = "chi",
                     content: str | None = None) -> None:
        """为已注册条目添加一条外挂字幕（MediaStreams[Type=Subtitle, IsExternal]）。"""
        detail = self.details.get(item_id)
        if detail is None:
            raise KeyError(f"item {item_id} not registered before add_subtitle")
        media_source = detail["MediaSources"][0]
        streams = media_source.setdefault("MediaStreams", [])
        streams.append({
            "Index": stream_index,
            "Type": "Subtitle",
            "IsExternal": True,
            "Codec": "subrip",
            "Language": language,
            "DisplayTitle": f"{language} (External)",
            "DeliveryUrl": f"/Videos/{item_id}/{media_source['Id']}/Subtitles/{stream_index}/Stream.srt",
        })
        self.subtitle_files[(item_id, stream_index)] = (
            content if content is not None else f"1\n00:00:01,000 --> 00:00:02,000\nsubtitle-{item_id}\n"
        ).encode("utf-8")

    # ---------- 请求处理 ----------
    def _authorized(self, h: BaseHTTPRequestHandler, qs: dict) -> bool:
        header_token = h.headers.get("X-Emby-Token", "")
        query_token = (qs.get("api_key") or [""])[0]
        return header_token == self.token or query_token == self.token

    def _json(self, h: BaseHTTPRequestHandler, obj, status: int = 200) -> None:
        body = json.dumps(obj).encode("utf-8")
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    def _handle(self, h: BaseHTTPRequestHandler, method: str) -> None:
        parsed = urlparse(h.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        with self._lock:
            self.request_log.append({
                "method": method, "path": path, "query": h.path[len(path):],
                "range": h.headers.get("Range"),
                "if_range": h.headers.get("If-Range"),
                "token_header": h.headers.get("X-Emby-Token"),
                "accept_encoding": h.headers.get("Accept-Encoding"),
            })

        if method == "POST" and path == "/Users/AuthenticateByName":
            length = int(h.headers.get("Content-Length", 0))
            body = json.loads(h.rfile.read(length) or b"{}")
            if body.get("Pw") == self.auth_password or body.get("Password") == self.auth_password:
                self._json(h, {"AccessToken": self.token,
                               "User": {"Id": self.user_id, "Name": body.get("Username", "mock")}})
            else:
                self._json(h, {"Message": "Invalid username or password"}, 401)
            return

        if not self._authorized(h, qs):
            self._json(h, {"Message": "Unauthorized"}, 401)
            return

        if path == "/Users/Me":
            self._json(h, {"Id": self.user_id, "Name": "mock"})
            return
        if path == f"/Users/{self.user_id}/Views":
            self._json(h, {"Items": [{"Id": "v1", "Name": "Movies", "CollectionType": "movies"}]})
            return
        if path == f"/Users/{self.user_id}/Items":
            term = (qs.get("SearchTerm") or [""])[0].lower()
            types = (qs.get("IncludeItemTypes") or [""])[0]
            found = [it for it in self.items.values()
                     if term in it["Name"].lower()
                     and (not types or it["Type"] in types.split(","))]
            self._json(h, {"Items": found, "TotalRecordCount": len(found)})
            return
        m = re.fullmatch(rf"/Users/{self.user_id}/Items/([^/]+)", path)
        if m:
            detail = self.details.get(m.group(1))
            if detail is None:
                self._json(h, {"Message": "not found"}, 404)
            else:
                self._json(h, detail)
            return
        m = re.fullmatch(r"/Items/([^/]+)/Download", path)
        if m:
            item_id = m.group(1)
            beh = self.behavior.get(item_id, {})
            dl = beh.get("download", "ok")
            if dl == "403":
                self._json(h, {"Message": "User is not allowed to DownloadContent"}, 403)
                return
            if dl == "401":
                self._json(h, {"Message": "Unauthorized"}, 401)
                return
            if dl == "500":
                self._json(h, {"Message": "boom"}, 500)
                return
            self._send_file(h, item_id)
            return
        m = re.fullmatch(r"/Videos/([^/]+)/stream", path)
        if m:
            self._send_file(h, m.group(1))
            return
        m = re.fullmatch(r"/Videos/([^/]+)/stream\.[^/]+", path)
        if m:
            self._send_file(h, m.group(1))
            return
        # 外挂字幕流：/Videos/{itemId}/{mediaSourceId}/Subtitles/{index}/Stream.srt
        m = re.fullmatch(r"/Videos/([^/]+)/[^/]+/Subtitles/(\d+)/Stream\.[^/]+", path)
        if m:
            data = self.subtitle_files.get((m.group(1), int(m.group(2))))
            if data is None:
                self._json(h, {"Message": "no such subtitle"}, 404)
                return
            h.send_response(200)
            h.send_header("Content-Type", "application/x-subrip")
            h.send_header("Content-Length", str(len(data)))
            h.end_headers()
            h.wfile.write(data)
            return
        self._json(h, {"Message": "not found"}, 404)

    # ---------- 文件服务 ----------
    def _send_file(self, h: BaseHTTPRequestHandler, item_id: str) -> None:
        data = self.files.get(item_id)
        if data is None:
            self._json(h, {"Message": "no such file"}, 404)
            return
        beh = self.behavior.get(item_id, {})
        mode = beh.get("mode", "normal")
        etag = beh.get("etag", '"mock-etag-1"')
        range_header = h.headers.get("Range")

        if mode == "slow":
            time.sleep(beh.get("delay", 0.0))

        if mode == "flaky" and range_header != "bytes=0-0":
            key = (item_id, range_header or "full")
            with self._lock:
                n = self._fail_counters.get(key, 0)
                self._fail_counters[key] = n + 1
            if n < beh.get("fail_times", 1):
                h.close_connection = True
                return  # 不响应直接断连 → 客户端 ConnectionError

        m = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header or "")
        has_range = bool(m and (m.group(1) or m.group(2)))

        if mode == "gzip":
            gz = gzip.compress(data)
            h.send_response(200)
            h.send_header("Content-Encoding", "gzip")
            h.send_header("Content-Length", str(len(gz)))
            h.send_header("ETag", etag)
            h.end_headers()
            h.wfile.write(gz)
            return

        if mode == "short_206":
            half = len(data) // 2
            h.send_response(206)
            h.send_header("Content-Range", f"bytes 0-{half - 1}/{len(data)}")
            h.send_header("Content-Length", str(half))
            h.send_header("ETag", etag)
            h.end_headers()
            h.wfile.write(data[:half])
            return

        if mode == "chunked":
            h.send_response(200)
            h.send_header("Transfer-Encoding", "chunked")
            h.send_header("ETag", etag)
            h.end_headers()
            for i in range(0, len(data), 8192):
                chunk = data[i:i + 8192]
                h.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
            h.wfile.write(b"0\r\n\r\n")
            return

        if mode == "no_range" or not has_range:
            # If-Range 不匹配 → 忽略 Range 返回 200（normal 模式下由 has_range 分支处理）
            h.send_response(200)
            h.send_header("Content-Length", str(len(data)))
            h.send_header("Accept-Ranges", "bytes" if mode != "no_range" else "none")
            h.send_header("ETag", etag)
            h.end_headers()
            h.wfile.write(data)
            return

        # If-Range：etag 不匹配时按 RFC 返回 200 全量
        if_range = h.headers.get("If-Range")
        if if_range and if_range != etag:
            h.send_response(200)
            h.send_header("Content-Length", str(len(data)))
            h.send_header("ETag", etag)
            h.end_headers()
            h.wfile.write(data)
            return

        start = int(m.group(1) or 0)
        end = int(m.group(2)) if m.group(2) else len(data) - 1
        end = min(end, len(data) - 1)
        if start >= len(data):
            h.send_response(416)
            h.send_header("Content-Range", f"bytes */{len(data)}")
            h.send_header("Content-Length", "0")
            h.end_headers()
            return
        body = data[start:end + 1]

        if mode == "bad_start":
            content_range = f"bytes 0-{len(data) - 1}/{len(data)}"
            body = data  # 服务端错误地从头发全量
        elif mode == "bad_total":
            content_range = f"bytes {start}-{end}/{len(data) // 2}"  # total 撒谎
        else:
            content_range = f"bytes {start}-{end}/{len(data)}"

        h.send_response(206)
        h.send_header("Content-Range", content_range)
        h.send_header("Content-Length", str(len(body)))
        h.send_header("Accept-Ranges", "bytes")
        h.send_header("ETag", etag)
        h.end_headers()
        h.wfile.write(body)
