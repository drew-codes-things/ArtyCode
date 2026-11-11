import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SESSION = "session-123"
TOOLS = [
    {"name": "lookup", "description": "Find a note", "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}},
     "annotations": {"readOnlyHint": True}},
    {"name": "save", "description": "Save a note", "inputSchema": {"type": "object", "properties": {}}},
]


class Recorded:
    def __init__(self):
        self.requests = []
        self.client_answers = []
        self.deleted = []


def handler_for(recorded: Recorded):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            message = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            recorded.requests.append({"message": message, "headers": dict(self.headers)})
            if "method" not in message:
                recorded.client_answers.append(message)
                return self.empty(202)
            if self.headers.get("Authorization") != "Bearer secret-token":
                return self.empty(401)
            if "id" not in message:
                return self.empty(202)
            method = message["method"]
            if method == "initialize":
                return self.json_answer(message, {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                                                  "serverInfo": {"name": "fake-http", "version": "1"}}, SESSION)
            if self.headers.get("Mcp-Session-Id") != SESSION:
                return self.empty(400)
            if method == "tools/list":
                return self.streamed(message, {"tools": TOOLS})
            if method == "tools/call":
                name = message["params"]["name"]
                if name == "save":
                    return self.json_answer(message, {"content": [{"type": "text", "text": "disk full"}], "isError": True})
                text = f"found {message['params']['arguments'].get('q')}"
                return self.json_answer(message, {"content": [{"type": "text", "text": text}]})
            return self.json_answer(message, None)

        def do_DELETE(self):
            recorded.deleted.append(self.headers.get("Mcp-Session-Id"))
            self.empty(200)

        def empty(self, status):
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def json_answer(self, message, result, session=""):
            body = json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            if session:
                self.send_header("Mcp-Session-Id", session)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def streamed(self, message, result):
            ping = {"jsonrpc": "2.0", "id": "server-ping", "method": "ping"}
            note = {"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "info", "data": "hi"}}
            answer = {"jsonrpc": "2.0", "id": message["id"], "result": result}
            events = "".join(f"event: message\ndata: {json.dumps(item)}\n\n" for item in (ping, note, answer))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(events.encode())

    return Handler


def serving(recorded: Recorded):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(recorded))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/mcp"
