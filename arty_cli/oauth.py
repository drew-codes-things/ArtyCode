import base64
import hashlib
import html
import http.client
import ipaddress
import json
import re
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from arty_cli import __version__

REQUEST_SECONDS = 20
CLIENT_NAME = "ArtyCode"
CALLBACK_PATH = "/callback"
LOOPBACK_HOST = "127.0.0.1"
SIGN_IN_SECONDS = 300
POLL_SECONDS = 0.05
SECONDS_PER_MINUTE = 60
PKCE_METHOD = "S256"
VERIFIER_BYTES = 48
STATE_BYTES = 32
PROTECTED_RESOURCE = "/.well-known/oauth-protected-resource"
AUTHORIZATION_SERVER = "/.well-known/oauth-authorization-server"
OPENID_CONFIGURATION = "/.well-known/openid-configuration"
PROTOCOL_VERSION = "2025-06-18"
UNAUTHORISED = 401
AUTH_PARAMETER = re.compile(r'([A-Za-z_]+)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^\s,]+))')
SECRET_METHODS = ("client_secret_basic", "client_secret_post")
LOOPBACK_NAMES = ("localhost",)
PRIVATE_NAME_ENDINGS = (".localhost", ".local", ".internal", ".home.arpa")
CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
MAX_ANSWER_BYTES = 1024 * 1024
CALLBACK_REQUEST_SECONDS = 10
TOO_MANY_REQUESTS = 429
FIRST_SERVER_ERROR = 500
LONGEST_LIFETIME_SECONDS = 10 * 365 * 24 * 60 * 60
ACCOUNT_CLAIMS = ("email", "preferred_username", "name")
MAX_ACCOUNT_CHARS = 120
JWT_PARTS = 3
SIGNED_IN_PAGE = ("<!doctype html><title>ArtyCode</title><body style=\"font-family:sans-serif\">"
                  "<h2>Signed in</h2><p>You can close this tab and go back to ArtyCode.</p>")
REFUSED_PAGE = ("<!doctype html><title>ArtyCode</title><body style=\"font-family:sans-serif\">"
                "<h2>Not signed in</h2><p>{reason}</p><p>Go back to ArtyCode to try again.</p>")


class OAuthError(Exception):
    pass


class Unavailable(OAuthError):
    pass


class WrongAnswer(OAuthError):
    pass


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


NO_REDIRECTS = urllib.request.build_opener(NoRedirects)


@dataclass(frozen=True)
class Discovery:
    server_url: str
    resource: str
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str
    scope: str
    revocation_endpoint: str = ""


@dataclass(frozen=True)
class Client:
    client_id: str
    client_secret: str
    auth_method: str


@dataclass(frozen=True)
class Tokens:
    access_token: str
    refresh_token: str
    expires_at: float
    scope: str
    account: str = ""


@dataclass(frozen=True)
class Pending:
    url: str
    state: str
    verifier: str
    redirect_uri: str


def literal_address(host: str):
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        pass
    try:
        return ipaddress.ip_address(socket.inet_aton(host))
    except (OSError, ValueError):
        return None


def is_loopback_host(host: str) -> bool:
    if host in LOOPBACK_NAMES:
        return True
    address = literal_address(host)
    return address is not None and address.is_loopback


def is_private_host(host: str) -> bool:
    address = literal_address(host)
    if address is not None:
        return not address.is_global or address.is_multicast
    name = host.lower().rstrip(".")
    return "." not in name or name.endswith(PRIVATE_NAME_ENDINGS)


def split_address(url: str) -> urllib.parse.SplitResult | None:
    try:
        parts = urllib.parse.urlsplit(url)
        return parts if parts.port is None or parts.port >= 0 else None
    except ValueError:
        return None


def host_of(url: str) -> str:
    parts = split_address(url)
    return (parts.hostname or "") if parts else ""


def is_safe_address(url) -> bool:
    if not isinstance(url, str) or CONTROL_CHARACTERS.search(url):
        return False
    parts = split_address(url)
    if parts is None or not parts.hostname or "@" in parts.netloc:
        return False
    return parts.scheme == "https" or (parts.scheme == "http" and is_loopback_host(parts.hostname))


def is_safe_endpoint(url, server_url: str) -> bool:
    if not is_safe_address(url):
        return False
    return is_loopback_host(host_of(server_url)) or not is_private_host(host_of(url))


def checked_address(url, what: str, server_url: str = "") -> str:
    if not is_safe_address(url):
        raise OAuthError(f"The {what} isn't a safe https address ({url!r}), so ArtyCode won't use it.")
    if server_url and not is_safe_endpoint(url, server_url):
        raise OAuthError(f"The {what} ({url!r}) is on this computer or a private network, but {server_url} isn't, "
                         "so ArtyCode won't use it.")
    return url


def comparable(url: str) -> str:
    parts = split_address(url)
    if parts is None:
        return ""
    return urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), parts.query, ""))


def read_json(raw: bytes) -> dict:
    try:
        parsed = json.loads(raw or b"{}")
    except (ValueError, RecursionError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def text_field(found: dict, name: str, where: str) -> str:
    value = found.get(name) or ""
    if not isinstance(value, str):
        raise OAuthError(f"{where} gave a {name} that isn't text, so ArtyCode won't use it.")
    return value


def text_list(found: dict, name: str, where: str) -> list[str]:
    value = found.get(name) or []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise OAuthError(f"{where} gave a {name} that isn't a list of text, so ArtyCode won't use it.")
    return value


def capped(answer, url: str) -> bytes:
    raw = answer.read(MAX_ANSWER_BYTES + 1)
    if len(raw) > MAX_ANSWER_BYTES:
        raise OAuthError(f"{url} sent more than {MAX_ANSWER_BYTES // 1024} KB, so ArtyCode stopped reading it.")
    missing = getattr(answer, "length", None)
    if isinstance(missing, int) and missing > 0:
        raise http.client.IncompleteRead(raw, missing)
    return raw


def answer_to(request: urllib.request.Request) -> tuple[int, dict, bytes]:
    try:
        with NO_REDIRECTS.open(request, timeout=REQUEST_SECONDS) as response:
            return response.status, dict(response.headers.items()), capped(response, request.full_url)
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers.items()) if error.headers else {}, capped(error, request.full_url)


def fetched(url: str, data: bytes | None = None, headers: dict | None = None,
            method: str | None = None) -> tuple[int, dict, bytes]:
    try:
        return answer_to(urllib.request.Request(url, data=data, method=method,
                                                headers={"User-Agent": f"arty-cli/{__version__}", **(headers or {})}))
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, http.client.HTTPException) as error:
        raise Unavailable(f"Couldn't reach {url}: {error}") from None
    except ValueError as error:
        raise OAuthError(f"ArtyCode can't use the address {url}: {error}") from None


def header(headers: dict, name: str) -> str:
    return next((value for key, value in headers.items() if key.lower() == name.lower()), "")


def challenge_parameters(value: str) -> dict:
    return {name.lower(): quoted.replace('\\"', '"') if quoted else bare
            for name, quoted, bare in AUTH_PARAMETER.findall(value or "")}


def unauthorised_hints(server_url: str, fetch=fetched) -> dict:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
        "clientInfo": {"name": "arty-cli", "version": __version__}}}).encode()
    status, headers, _body = fetch(server_url, body, {"Content-Type": "application/json",
                                                      "Accept": "application/json, text/event-stream"}, "POST")
    return challenge_parameters(header(headers, "WWW-Authenticate")) if status == UNAUTHORISED else {}


def well_known_urls(url: str, suffixes: tuple[str, ...], also_inside_path: bool = False) -> list[str]:
    parts = urllib.parse.urlsplit(url)
    origin, path = f"{parts.scheme}://{parts.netloc}", parts.path.rstrip("/")
    found = [f"{origin}{suffix}{path}" for suffix in suffixes] if path else []
    if path and also_inside_path:
        found.append(f"{origin}{path}{OPENID_CONFIGURATION}")
    return found + [f"{origin}{suffix}" for suffix in suffixes]


def first_json(urls: list[str], fetch) -> tuple[str, dict]:
    for url in urls:
        status, _headers, raw = fetch(url, None, {"Accept": "application/json"}, "GET")
        found = read_json(raw) if status == 200 else {}
        if found:
            return url, found
    return "", {}


def protected_resource(server_url: str, hints: dict, fetch) -> dict:
    pointed = hints.get("resource_metadata", "")
    urls = [checked_address(pointed, "resource metadata address", server_url)] if pointed else []
    _url, found = first_json(urls + well_known_urls(server_url, (PROTECTED_RESOURCE,)), fetch)
    if found and comparable(text_field(found, "resource", server_url)) != comparable(server_url):
        raise OAuthError(f"{server_url} says it is {found.get('resource')!r}, which isn't the address you gave, "
                         "so ArtyCode won't sign in to it.")
    return found


def origin_of(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def authorization_server(issuer: str, fetch) -> dict:
    _url, found = first_json(well_known_urls(issuer, (AUTHORIZATION_SERVER, OPENID_CONFIGURATION), True), fetch)
    if not found:
        raise OAuthError(f"The sign-in server {issuer} doesn't publish its details, so ArtyCode can't sign in there.")
    if comparable(text_field(found, "issuer", issuer)) != comparable(issuer):
        raise OAuthError(f"The sign-in server {issuer} names itself {found.get('issuer')!r}; not trusting it.")
    if PKCE_METHOD not in text_list(found, "code_challenge_methods_supported", issuer):
        raise OAuthError(f"The sign-in server {issuer} doesn't offer PKCE ({PKCE_METHOD}), which ArtyCode needs.")
    if not found.get("registration_endpoint"):
        raise OAuthError(f"The sign-in server {issuer} doesn't let apps register themselves, so it needs a client "
                         "set up by hand. Use a token in headers instead (see docs/mcp-servers.md).")
    return found


def optional_endpoint(metadata: dict, name: str, server_url: str) -> str:
    found = metadata.get(name)
    return found if is_safe_endpoint(found, server_url) else ""


def discover(server_url: str, fetch=fetched) -> Discovery:
    checked_address(server_url, "MCP server address")
    hints = unauthorised_hints(server_url, fetch)
    resource = protected_resource(server_url, hints, fetch)
    servers = text_list(resource, "authorization_servers", server_url) or [origin_of(server_url)]
    issuer = checked_address(servers[0], "sign-in server address", server_url)
    metadata = authorization_server(issuer, fetch)
    scope = hints.get("scope") or " ".join(text_list(resource, "scopes_supported", server_url))
    return Discovery(server_url, text_field(resource, "resource", server_url) or server_url, metadata["issuer"],
                     checked_address(metadata.get("authorization_endpoint"), "sign-in page address", server_url),
                     checked_address(metadata.get("token_endpoint"), "token address", server_url),
                     checked_address(metadata.get("registration_endpoint"), "registration address", server_url), scope,
                     optional_endpoint(metadata, "revocation_endpoint", server_url))


def register(discovery: Discovery, redirect_uri: str, fetch=fetched) -> Client:
    body = {"client_name": CLIENT_NAME, "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
            "token_endpoint_auth_method": "none"}
    status, _headers, raw = fetch(discovery.registration_endpoint, json.dumps(body).encode(),
                                  {"Content-Type": "application/json", "Accept": "application/json"}, "POST")
    found = read_json(raw)
    if status not in (200, 201) or not isinstance(found.get("client_id"), str) or not found["client_id"]:
        raise OAuthError(f"{discovery.issuer} didn't register ArtyCode ({status}): {problem_in(found)}")
    method = str(found.get("token_endpoint_auth_method") or "none")
    secret = str(found.get("client_secret") or "")
    return Client(found["client_id"], secret, method if secret and method in SECRET_METHODS else "none")


def problem_in(answer: dict) -> str:
    return str(answer.get("error_description") or answer.get("error") or "no details")


def base64_url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def code_verifier() -> str:
    return base64_url(secrets.token_bytes(VERIFIER_BYTES))


def code_challenge(verifier: str) -> str:
    return base64_url(hashlib.sha256(verifier.encode("ascii")).digest())


def pending_sign_in(discovery: Discovery, client: Client, redirect_uri: str) -> Pending:
    verifier, state = code_verifier(), base64_url(secrets.token_bytes(STATE_BYTES))
    wanted = {"response_type": "code", "client_id": client.client_id, "redirect_uri": redirect_uri,
              "code_challenge": code_challenge(verifier), "code_challenge_method": PKCE_METHOD, "state": state,
              "resource": discovery.resource}
    if discovery.scope:
        wanted["scope"] = discovery.scope
    parts = urllib.parse.urlsplit(discovery.authorization_endpoint)
    query = "&".join(filter(None, (parts.query, urllib.parse.urlencode(wanted))))
    return Pending(urllib.parse.urlunsplit(parts._replace(query=query)), state, verifier, redirect_uri)


def is_our_state(query: dict, state: str) -> bool:
    given = (query.get("state") or [""])[0]
    return secrets.compare_digest(given.encode(), state.encode())


def code_from(query: dict, state: str) -> str:
    if not is_our_state(query, state):
        raise WrongAnswer("The answer from the browser doesn't match the sign-in ArtyCode started, so it was refused.")
    if query.get("error"):
        reason = (query.get("error_description") or query["error"])[0]
        raise OAuthError(f"The sign-in was turned down: {reason}")
    code = (query.get("code") or [""])[0]
    if not code:
        raise WrongAnswer("The browser came back without a sign-in code.")
    return code


def code_from_address(address: str, state: str) -> str:
    parts = split_address(address.strip())
    if parts is None:
        raise WrongAnswer("That isn't an address ArtyCode can read.")
    return code_from(urllib.parse.parse_qs(parts.query), state)


def waited_text(seconds: float) -> str:
    if seconds < SECONDS_PER_MINUTE:
        return f"{seconds:g} seconds"
    return f"{seconds / SECONDS_PER_MINUTE:g} minutes"


def refused_page(reason: str) -> str:
    return REFUSED_PAGE.format(reason=html.escape(reason))


class Callback:
    def __init__(self, port: int = 0):
        self.server = ThreadingHTTPServer((LOOPBACK_HOST, port), self.handler())
        self.server.daemon_threads = True
        self.server.block_on_close = False
        self.port = self.server.server_address[1]
        self.redirect_uri = f"http://{LOOPBACK_HOST}:{self.port}{CALLBACK_PATH}"
        self.state = ""
        self.code = ""
        self.problem = ""
        self.done = threading.Event()
        self.answering = threading.Lock()
        self.thread = threading.Thread(target=self.server.serve_forever, args=(POLL_SECONDS,), daemon=True)

    def handler(self):
        callback = self

        class Handler(BaseHTTPRequestHandler):
            timeout = CALLBACK_REQUEST_SECONDS

            def log_message(self, *args):
                return None

            def do_GET(self):
                parts = urllib.parse.urlsplit(self.path)
                query = urllib.parse.parse_qs(parts.query)
                with callback.answering:
                    if parts.path != CALLBACK_PATH or callback.done.is_set():
                        self.answer(404, refused_page("This address isn't waiting for a sign-in."))
                        return
                    try:
                        callback.code = code_from(query, callback.state)
                    except WrongAnswer as error:
                        self.answer(400, refused_page(str(error)))
                        return
                    except OAuthError as error:
                        callback.problem = str(error)
                        self.answer(400, refused_page(str(error)))
                    else:
                        self.answer(200, SIGNED_IN_PAGE)
                    callback.done.set()

            def answer(self, status: int, page: str):
                body = page.encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

        return Handler

    def listen(self, state: str) -> None:
        self.state = state
        self.thread.start()

    def finished(self) -> bool:
        return self.done.is_set()

    def wait(self, seconds: float = SIGN_IN_SECONDS) -> str:
        if not self.done.wait(seconds):
            raise OAuthError(f"The sign-in wasn't finished within {waited_text(seconds)}, so it was stopped.")
        if self.problem:
            raise OAuthError(self.problem)
        return self.code

    def close(self) -> None:
        if self.thread.is_alive():
            self.server.shutdown()
        self.server.server_close()


def form_post(url: str, fields: dict, client: Client, fetch) -> tuple[int, dict]:
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
    fields = {**fields, "client_id": client.client_id}
    if client.auth_method == "client_secret_post":
        fields["client_secret"] = client.client_secret
    elif client.auth_method == "client_secret_basic":
        pair = f"{urllib.parse.quote(client.client_id)}:{urllib.parse.quote(client.client_secret)}".encode()
        headers["Authorization"] = "Basic " + base64.b64encode(pair).decode("ascii")
    status, _headers, raw = fetch(url, urllib.parse.urlencode(fields).encode(), headers, "POST")
    return status, read_json(raw)


def is_unavailable(status: int) -> bool:
    return status == TOO_MANY_REQUESTS or status >= FIRST_SERVER_ERROR


def id_token_claims(token) -> dict:
    if not isinstance(token, str) or token.count(".") != JWT_PARTS - 1:
        return {}
    payload = token.split(".")[1]
    try:
        return read_json(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except ValueError:
        return {}


def account_in(answer: dict) -> str:
    for source in (id_token_claims(answer.get("id_token")), answer):
        for claim in ACCOUNT_CLAIMS:
            value = source.get(claim)
            if isinstance(value, str) and value.strip():
                return value.strip()[:MAX_ACCOUNT_CHARS]
    return ""


def lifetime_end(answer: dict, clock) -> float:
    lifetime = answer.get("expires_in")
    if not isinstance(lifetime, (int, float)) or isinstance(lifetime, bool):
        return 0.0
    return clock() + float(min(lifetime, LONGEST_LIFETIME_SECONDS))


def tokens_from(status: int, answer: dict, kept_refresh: str, clock) -> Tokens:
    if is_unavailable(status):
        raise Unavailable(f"the sign-in server answered {status}: {problem_in(answer)}")
    if status != 200 or not isinstance(answer.get("access_token"), str) or not answer["access_token"]:
        raise OAuthError(f"the sign-in server answered {status}: {problem_in(answer)}")
    if str(answer.get("token_type", "bearer")).lower() != "bearer":
        raise OAuthError(f"the sign-in server gave a {answer.get('token_type')} token, not a bearer token")
    return Tokens(answer["access_token"], str(answer.get("refresh_token") or kept_refresh), lifetime_end(answer, clock),
                  str(answer.get("scope") or ""), account_in(answer))


def exchange(discovery: Discovery, client: Client, pending: Pending, code: str, fetch=fetched,
             clock=time.time) -> Tokens:
    status, answer = form_post(discovery.token_endpoint, {
        "grant_type": "authorization_code", "code": code, "redirect_uri": pending.redirect_uri,
        "code_verifier": pending.verifier, "resource": discovery.resource}, client, fetch)
    return tokens_from(status, answer, "", clock)


def refreshed(token_endpoint: str, resource: str, client: Client, refresh_token: str, fetch=fetched,
              clock=time.time) -> Tokens:
    if not refresh_token:
        raise OAuthError("there is no refresh token to renew it with")
    status, answer = form_post(token_endpoint, {"grant_type": "refresh_token", "refresh_token": refresh_token,
                                                "resource": resource}, client, fetch)
    return tokens_from(status, answer, refresh_token, clock)


def revoked(revocation_endpoint: str, server_url: str, client: Client, token: str, hint: str,
            fetch=fetched) -> None:
    checked_address(revocation_endpoint, "revocation address", server_url)
    status, answer = form_post(revocation_endpoint, {"token": token, "token_type_hint": hint}, client, fetch)
    if status != 200:
        raise OAuthError(f"the sign-in server answered {status}: {problem_in(answer)}")
