import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GOOD_KEY = "sf_outside_0123456789"
LISTED = ["Qwen3-14B-Q4_K_M", "friend/Big-Model"]


class OutsideServer:
    def __init__(self, models=None, status=200, offline=()):
        self.models = list(LISTED if models is None else models)
        self.status = status
        self.offline = set(offline)
        self.requests: list[dict] = []


def handler_for(server: OutsideServer):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status: int, data: bytes, content_type: str = "application/json"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def refused(self) -> bool:
            if self.headers.get("Authorization") == f"Bearer {GOOD_KEY}" and server.status == 200:
                return False
            status = 401 if server.status == 200 else server.status
            self.send(status, json.dumps({"error": {"message": f"refused with status {status}"}}).encode())
            return True

        def do_GET(self):
            server.requests.append({"path": self.path, "headers": dict(self.headers), "body": None})
            if self.refused():
                return
            listed = [{"id": name, "available": name not in server.offline} for name in server.models]
            self.send(200, json.dumps({"object": "list", "data": listed}).encode())

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            server.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
            if self.refused():
                return
            if body.get("model") not in server.models:
                self.send(404, json.dumps({"error": {"message": f"model {body.get('model')} not found"}}).encode())
                return
            if body.get("model") in server.offline:
                self.send(503, json.dumps({"error": {"message": f"{body.get('model')} is unavailable",
                                                     "code": "model_unavailable"}}).encode())
                return
            events = [{"choices": [{"delta": {"reasoning_content": "hmm"}}]},
                      {"choices": [{"delta": {"content": "from outside"}}]},
                      *split_tool_call(body),
                      {"choices": [{"delta": {}, "finish_reason": "stop"}]}]
            text = "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"
            self.send(200, text.encode(), "text/event-stream")

    return Handler


def split_tool_call(body: dict) -> list[dict]:
    if not body.get("tools"):
        return []
    first = {"index": 0, "id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": "{\"path\""}}
    rest = {"index": 0, "function": {"arguments": ": \"a.txt\"}"}}
    return [{"choices": [{"delta": {"tool_calls": [first]}}]}, {"choices": [{"delta": {"tool_calls": [rest]}}]}]


@contextmanager
def outside_server(**options):
    state = OutsideServer(**options)
    http = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(state))
    threading.Thread(target=http.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{http.server_address[1]}/v1", state
    finally:
        http.shutdown()
        http.server_close()
