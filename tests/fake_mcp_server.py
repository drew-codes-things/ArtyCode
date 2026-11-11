import json
import os
import sys
import time

TOOLS = [
    {"name": "echo", "description": "Say it back", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
     "annotations": {"readOnlyHint": True}},
    {"name": "fail", "description": "Always fails", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "slow", "description": "Takes a while", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "crash", "description": "Ends the server", "inputSchema": {"type": "object", "properties": {}}},
]
LOG = os.environ.get("FAKE_MCP_LOG", "")


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def note(line):
    if LOG:
        with open(LOG, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def answer(request):
    method, params = request.get("method"), request.get("params") or {}
    if method == "initialize":
        return {"protocolVersion": params.get("protocolVersion"), "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake", "version": "1"}}
    if method == "tools/list":
        if params.get("cursor") == "page2":
            return {"tools": TOOLS[2:]}
        return {"tools": TOOLS[:2], "nextCursor": "page2"}
    if method == "tools/call":
        name, arguments = params.get("name"), params.get("arguments") or {}
        if name == "echo":
            send({"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "info", "data": "hi"}})
            send({"jsonrpc": "2.0", "id": "server-1", "method": "ping"})
            return {"content": [{"type": "text", "text": f"echo: {arguments.get('text', '')}"},
                                {"type": "image", "data": "AAAA", "mimeType": "image/png"}]}
        if name == "fail":
            return {"content": [{"type": "text", "text": "it broke"}], "isError": True}
        if name == "slow":
            time.sleep(float(os.environ.get("FAKE_MCP_SLOW", "5")))
            return {"content": [{"type": "text", "text": "late"}]}
        if name == "crash":
            sys.exit(3)
    return None


for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        note(f"notification {request.get('method')}")
        continue
    if "method" not in request:
        note(f"client answered {request.get('id')} with {json.dumps(request.get('result'))}")
        continue
    note(f"request {request.get('method')}")
    result = answer(request)
    if result is None:
        send({"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32601, "message": "no such method"}})
    else:
        send({"jsonrpc": "2.0", "id": request["id"], "result": result})
