import json
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

SESSION_ID = "0123456789abcdef"
PREFIX = "/v1/remote/sessions"


class Relay:
    def __init__(self):
        self.created = []
        self.title = "repo"
        self.renamed = []
        self.events = []
        self.ended = []
        self.headers = []
        self.inbox = queue.Queue()
        self.posted = threading.Event()
        self.inbox_status = 200
        self.events_status = 200
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(self))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *problem):
        self.server.shutdown()
        self.server.server_close()

    def give(self, *items) -> None:
        for item in items:
            self.inbox.put(item)

    def events_of(self, kind: str) -> list[dict]:
        return [event for event in self.events if event["type"] == kind]


def handler_for(relay: Relay):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(length) or b"{}")

        def answer(self, status: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            relay.headers.append(dict(self.headers))
            body = self.body()
            path = urlparse(self.path).path
            if path == PREFIX:
                relay.created.append(body)
                return self.answer(201, {"id": SESSION_ID, "title": relay.title})
            if path == f"{PREFIX}/{SESSION_ID}/title":
                relay.renamed.append(body.get("title"))
                relay.title = f"{body.get('title')} 2"
                return self.answer(200, {"title": relay.title})
            if path == f"{PREFIX}/{SESSION_ID}/events":
                if relay.events_status != 200:
                    return self.answer(relay.events_status, {"error": {"message": "that session has ended"}})
                relay.events.extend(body["events"])
                relay.posted.set()
                return self.answer(200, {"next": len(relay.events)})
            return self.answer(404, {"error": {"message": "not found"}})

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path != f"{PREFIX}/{SESSION_ID}/inbox":
                return self.answer(404, {"error": {"message": "not found"}})
            if relay.inbox_status != 200:
                return self.answer(relay.inbox_status, {"error": {"message": "that session has ended or never existed"}})
            wait = min(float(parse_qs(parsed.query).get("wait", ["0"])[0]), 0.3)
            items = []
            try:
                items.append(relay.inbox.get(timeout=wait))
            except queue.Empty:
                pass
            while not relay.inbox.empty():
                items.append(relay.inbox.get_nowait())
            return self.answer(200, {"inbox": items})

        def do_DELETE(self):
            relay.ended.append(urlparse(self.path).path)
            return self.answer(200, {"ok": True})

    return Handler
