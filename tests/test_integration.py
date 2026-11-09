import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from mediamigrate.client import APIError, MediaViewerClient


@pytest.fixture
def api_server():
    state = {"requests": [], "redirect": False, "status": 200, "payload": {"version": 1, "candidates": []}}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"].append(("GET", self.path, self.headers.get("Authorization")))
            if state["redirect"]:
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1/other")
                self.end_headers()
                return
            self._reply()

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            state["requests"].append(("POST", self.path, self.headers.get("Authorization"), body))
            self._reply()

        def do_DELETE(self):
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            state["requests"].append(("DELETE", self.path, self.headers.get("Authorization"), body))
            self._reply()

        def _reply(self):
            payload = state["payload"]
            if self.path.endswith("/migrations/run/") and self.command == "POST":
                payload = {"acquired": True}
            body = json.dumps(payload).encode()
            self.send_response(state["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/mediaviewer/api/", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_client_auth_contract_and_no_retry(api_server):
    url, state = api_server
    client = MediaViewerClient(url, "operator", "pw", "api-token")
    assert client.candidates(4, "bucket", "prefix/") == []
    method, path, auth = state["requests"][0]
    assert (method, path, auth) == ("GET", "/mediaviewer/api/migrations/candidates/?limit=4&bucket=bucket&prefix=prefix%2F", "Token api-token")
    client.acquire_run("run-1")
    assert state["requests"][1][3] == {"run_id": "run-1", "writers_paused": True}
    assert len(state["requests"]) == 2


@pytest.mark.parametrize("status,payload", [(401, {"error": "secret body"}), (200, []), (200, {"version": 9, "candidates": []})])
def test_client_rejects_http_and_shape_failures(api_server, status, payload):
    url, state = api_server
    state["status"], state["payload"] = status, payload
    with pytest.raises(APIError):
        MediaViewerClient(url).candidates(2)


def test_client_refuses_redirect_without_following(api_server):
    url, state = api_server
    state["redirect"] = True
    with pytest.raises(APIError, match="redirect refused"):
        MediaViewerClient(url, token="secret").candidates(1)
    assert len(state["requests"]) == 1


def test_client_rejects_remote_http_and_over_limit(api_server):
    with pytest.raises(APIError, match="loopback"):
        MediaViewerClient("http://viewer/api/")
    url, _ = api_server
    with pytest.raises(APIError, match="between"):
        MediaViewerClient(url).candidates(1001)
