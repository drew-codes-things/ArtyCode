import base64
import hashlib
import json
import secrets
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOOLS = [{"name": "whoami", "description": "Who is signed in", "inputSchema": {"type": "object", "properties": {}},
          "annotations": {"readOnlyHint": True}}]
SESSION = "signed-in-session"
USER = "drew-test-user"


class Provider:
    def __init__(self):
        self.base = ""
        self.expires_in = 3600
        self.refresh_works = True
        self.registration_open = True
        self.offers_s256 = True
        self.resource_override = ""
        self.clients = {}
        self.codes = {}
        self.access_tokens = set()
        self.refresh_tokens = set()
        self.authorize_requests = []
        self.token_requests = []
        self.mcp_requests = []
        self.registrations = []
        self.lock = threading.Lock()

    def revoke_access(self) -> None:
        with self.lock:
            self.access_tokens.clear()

    def new_tokens(self, client_id: str) -> dict:
        access, refresh = f"access-{secrets.token_hex(8)}", f"refresh-{secrets.token_hex(8)}"
        self.access_tokens.add(access)
        self.refresh_tokens.add(refresh)
        answer = {"access_token": access, "token_type": "Bearer", "refresh_token": refresh, "scope": "read"}
        if self.expires_in is not None:
            answer["expires_in"] = self.expires_in
        return answer


def challenge_of(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def handler_for(provider: Provider):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return None

        def do_GET(self):
            parts = urllib.parse.urlsplit(self.path)
            if parts.path == "/.well-known/oauth-protected-resource/mcp":
                return self.json(200, {"resource": provider.resource_override or f"{provider.base}/mcp",
                                       "authorization_servers": [provider.base]})
            if parts.path == "/.well-known/oauth-authorization-server":
                metadata = {"issuer": provider.base, "authorization_endpoint": f"{provider.base}/authorize",
                            "token_endpoint": f"{provider.base}/token",
                            "code_challenge_methods_supported": ["S256"] if provider.offers_s256 else ["plain"]}
                if provider.registration_open:
                    metadata["registration_endpoint"] = f"{provider.base}/register"
                return self.json(200, metadata)
            if parts.path == "/authorize":
                return self.authorize(dict(urllib.parse.parse_qsl(parts.query)))
            return self.json(404, {"error": "not_found"})

        def authorize(self, query: dict):
            provider.authorize_requests.append(query)
            client = provider.clients.get(query.get("client_id"))
            if client is None or query.get("redirect_uri") not in client["redirect_uris"]:
                return self.json(400, {"error": "invalid_request"})
            if query.get("code_challenge_method") != "S256" or not query.get("code_challenge"):
                return self.json(400, {"error": "invalid_request", "error_description": "PKCE needed"})
            code = f"code-{secrets.token_hex(8)}"
            provider.codes[code] = query
            target = f"{query['redirect_uri']}?{urllib.parse.urlencode({'code': code, 'state': query['state']})}"
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            path = urllib.parse.urlsplit(self.path).path
            if path == "/register":
                return self.register(json.loads(raw))
            if path == "/token":
                return self.token(dict(urllib.parse.parse_qsl(raw.decode())))
            if path == "/mcp":
                return self.mcp(json.loads(raw))
            return self.json(404, {"error": "not_found"})

        def register(self, body: dict):
            provider.registrations.append(body)
            if not provider.registration_open:
                return self.json(404, {"error": "not_found"})
            client_id = f"client-{secrets.token_hex(4)}"
            provider.clients[client_id] = {"redirect_uris": body.get("redirect_uris") or []}
            return self.json(201, {"client_id": client_id, "token_endpoint_auth_method": "none",
                                   "redirect_uris": body.get("redirect_uris")})

        def token(self, form: dict):
            provider.token_requests.append(form)
            with provider.lock:
                if form.get("grant_type") == "authorization_code":
                    asked = provider.codes.pop(form.get("code"), None)
                    if (asked is None or asked["client_id"] != form.get("client_id")
                            or asked["redirect_uri"] != form.get("redirect_uri")
                            or challenge_of(form.get("code_verifier", "")) != asked["code_challenge"]
                            or form.get("resource") != f"{provider.base}/mcp"):
                        return self.json(400, {"error": "invalid_grant"})
                    return self.json(200, provider.new_tokens(form["client_id"]))
                if form.get("grant_type") == "refresh_token":
                    if not provider.refresh_works or form.get("refresh_token") not in provider.refresh_tokens:
                        return self.json(400, {"error": "invalid_grant", "error_description": "refresh token revoked"})
                    provider.refresh_tokens.discard(form["refresh_token"])
                    return self.json(200, provider.new_tokens(form.get("client_id", "")))
            return self.json(400, {"error": "unsupported_grant_type"})

        def mcp(self, message: dict):
            given = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
            provider.mcp_requests.append({"message": message, "authorization": given})
            if given not in provider.access_tokens:
                metadata = f"{provider.base}/.well-known/oauth-protected-resource/mcp"
                self.send_response(401)
                self.send_header("WWW-Authenticate", f'Bearer resource_metadata="{metadata}", scope="read"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            if "id" not in message:
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            method = message["method"]
            if method == "initialize":
                result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                          "serverInfo": {"name": "fake-signed-in", "version": "1"}}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                result = {"content": [{"type": "text", "text": f"signed in as {USER}"}]}
            else:
                result = {}
            return self.json(200, {"jsonrpc": "2.0", "id": message["id"], "result": result},
                             {"Mcp-Session-Id": SESSION})

        def json(self, status: int, body: dict, extra: dict | None = None):
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            for name, value in (extra or {}).items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return None

    return Handler


def serving(provider: Provider):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(provider))
    provider.base = f"http://127.0.0.1:{server.server_address[1]}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"{provider.base}/mcp"


def approving_browser(opened: list):
    def open_browser(url: str) -> bool:
        opened.append(url)
        threading.Thread(target=lambda: urllib.request.urlopen(url, timeout=10).read(), daemon=True).start()
        return True

    return open_browser
