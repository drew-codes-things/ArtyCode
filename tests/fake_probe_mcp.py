import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SESSION = "probe-session"
TOOLS = [{"name": "lookup", "description": "Find a note", "inputSchema": {"type": "object", "properties": {}},
          "annotations": {"readOnlyHint": True}}]


class Server:
    def __init__(self, answer):
        self.answer = answer
        self.requests = []
        self.address = ""

    def methods(self) -> list[str]:
        return [f"{command} {body.get('method') if body else path}" for command, path, _headers, body in self.requests]


def send(handler, status: int, body: bytes = b"", headers: dict | None = None) -> None:
    handler.send_response(status)
    for name, value in (headers or {}).items():
        handler.send_header(name, value)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def rpc(handler, message: dict, result=None, error=None, headers: dict | None = None) -> None:
    found = {"jsonrpc": "2.0", "id": message["id"]}
    found.update({"error": error} if error else {"result": result})
    send(handler, 200, json.dumps(found).encode(), {"Content-Type": "application/json", **(headers or {})})


def healthy(handler, message) -> None:
    if message is None:
        return send(handler, 404)
    if "id" not in message:
        return send(handler, 202)
    method = message["method"]
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "probe", "version": "1"}}
        return rpc(handler, message, result, headers={"Mcp-Session-Id": SESSION})
    if method == "tools/list":
        return rpc(handler, message, {"tools": TOOLS})
    return rpc(handler, message, {"content": [{"type": "text", "text": "found it"}]})


def serving(answer=healthy):
    holder = Server(answer)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return None

        def handle_any(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            message = json.loads(raw) if raw else None
            holder.requests.append((self.command, self.path, dict(self.headers), message))
            holder.answer(self, message)

        do_GET = do_POST = do_DELETE = handle_any

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    holder.address = f"http://127.0.0.1:{server.server_address[1]}"
    return server, holder
