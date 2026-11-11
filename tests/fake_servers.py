import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Recorder:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []


def handler_for(recorder: Recorder, content_type: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            recorder.requests.append({"path": self.path, "headers": dict(self.headers),
                                      "body": json.loads(self.rfile.read(length) or b"{}")})
            self.answer()

        def do_GET(self):
            recorder.requests.append({"path": self.path, "headers": dict(self.headers), "body": None})
            self.answer()

        def answer(self):
            status, payload, *extra = recorder.responses.pop(0)
            data = payload.encode()
            self.send_response(status)
            for name, value in (extra[0] if extra else {}).items():
                self.send_header(name, value)
            self.send_header("Content-Type", content_type if status == 200 else "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


@contextmanager
def serving(responses, content_type: str):
    recorder = Recorder(responses)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(recorder, content_type))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", recorder
    finally:
        server.shutdown()
        server.server_close()

