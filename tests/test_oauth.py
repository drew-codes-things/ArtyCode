import html
import http.server
import json
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

from arty_cli import oauth
from tests import fake_oauth_mcp


@pytest.fixture
def provider():
    found = fake_oauth_mcp.Provider()
    server, url = fake_oauth_mcp.serving(found)
    yield found, url
    server.shutdown()
    server.server_close()


def answers(table: dict):
    asked = []

    def fetch(url, data=None, headers=None, method=None):
        asked.append((method, url))
        status, body, extra = table.get(url, (404, {}, {}))
        return status, extra, json.dumps(body).encode()

    fetch.asked = asked
    return fetch


def test_discovery_follows_the_401_hint_to_both_metadata_documents(provider):
    found, url = provider
    discovery = oauth.discover(url)
    assert discovery == oauth.Discovery(url, url, found.base, f"{found.base}/authorize", f"{found.base}/token",
                                        f"{found.base}/register", "read")


def test_without_resource_metadata_the_server_s_own_origin_is_the_sign_in_server():
    metadata = {"issuer": "https://mcp.example", "authorization_endpoint": "https://mcp.example/v1/authorize",
                "token_endpoint": "https://mcp.example/v1/token", "registration_endpoint": "https://mcp.example/v1/register",
                "code_challenge_methods_supported": ["plain", "S256"]}
    fetch = answers({"https://mcp.example/v1/mcp": (401, {}, {}),
                     "https://mcp.example/.well-known/oauth-authorization-server": (200, metadata, {})})
    discovery = oauth.discover("https://mcp.example/v1/mcp", fetch)
    assert discovery.issuer == "https://mcp.example" and discovery.resource == "https://mcp.example/v1/mcp"
    assert ("GET", "https://mcp.example/.well-known/oauth-protected-resource/v1/mcp") in fetch.asked


def test_a_sign_in_server_with_a_path_is_looked_up_the_rfc_8414_way_first():
    found = oauth.well_known_urls("https://access.example/mcp", (oauth.AUTHORIZATION_SERVER, oauth.OPENID_CONFIGURATION), True)
    assert found == ["https://access.example/.well-known/oauth-authorization-server/mcp",
                     "https://access.example/.well-known/openid-configuration/mcp",
                     "https://access.example/mcp/.well-known/openid-configuration",
                     "https://access.example/.well-known/oauth-authorization-server",
                     "https://access.example/.well-known/openid-configuration"]


def test_resource_metadata_naming_another_address_is_refused(provider):
    found, url = provider
    found.resource_override = "https://elsewhere.example/mcp"
    with pytest.raises(oauth.OAuthError, match="isn't the address you gave"):
        oauth.discover(url)


def test_a_sign_in_server_without_self_registration_is_refused(provider):
    found, url = provider
    found.registration_open = False
    with pytest.raises(oauth.OAuthError, match="register themselves"):
        oauth.discover(url)


def test_a_sign_in_server_without_s256_is_refused(provider):
    found, url = provider
    found.offers_s256 = False
    with pytest.raises(oauth.OAuthError, match="PKCE"):
        oauth.discover(url)


def test_plain_http_off_this_computer_is_refused_before_any_request():
    fetch = answers({})
    with pytest.raises(oauth.OAuthError, match="safe https"):
        oauth.discover("http://mcp.example/mcp", fetch)
    assert fetch.asked == []


def test_registration_asks_for_a_public_client_with_the_loopback_redirect(provider):
    found, url = provider
    client = oauth.register(oauth.discover(url), "http://127.0.0.1:5555/callback")
    assert client.client_id in found.clients and client.auth_method == "none" and client.client_secret == ""
    sent = found.registrations[-1]
    assert sent["redirect_uris"] == ["http://127.0.0.1:5555/callback"]
    assert sent["token_endpoint_auth_method"] == "none" and "refresh_token" in sent["grant_types"]


def test_the_pkce_challenge_is_the_s256_of_the_verifier():
    assert oauth.code_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    verifier = oauth.code_verifier()
    assert 43 <= len(verifier) <= 128 and oauth.code_verifier() != verifier


def test_the_sign_in_page_address_carries_pkce_state_resource_and_scope():
    discovery = oauth.Discovery("https://mcp.example/mcp", "https://mcp.example/mcp", "https://mcp.example",
                                "https://mcp.example/authorize?tenant=7", "https://mcp.example/token",
                                "https://mcp.example/register", "read write")
    pending = oauth.pending_sign_in(discovery, oauth.Client("abc", "", "none"), "http://127.0.0.1:9/callback")
    parts = urllib.parse.urlsplit(pending.url)
    query = dict(urllib.parse.parse_qsl(parts.query))
    assert parts.path == "/authorize" and query["tenant"] == "7"
    assert query["code_challenge"] == oauth.code_challenge(pending.verifier) and query["code_challenge_method"] == "S256"
    assert query["state"] == pending.state and len(pending.state) >= 40
    assert query["resource"] == "https://mcp.example/mcp" and query["scope"] == "read write"
    assert query["redirect_uri"] == "http://127.0.0.1:9/callback" and query["response_type"] == "code"


def callback_answer(address: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(address, timeout=5) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


def test_the_loopback_callback_listens_on_127_0_0_1_on_a_random_port():
    first, second = oauth.Callback(), oauth.Callback()
    try:
        assert first.redirect_uri.startswith("http://127.0.0.1:") and first.redirect_uri.endswith("/callback")
        assert first.port != second.port and first.server.server_address[0] == "127.0.0.1"
    finally:
        first.close()
        second.close()


def test_the_callback_takes_the_code_when_the_state_matches():
    callback = oauth.Callback()
    callback.listen("the-state")
    try:
        status, page = callback_answer(f"{callback.redirect_uri}?code=abc&state=the-state")
        assert status == 200 and "Signed in" in page
        assert callback.wait(1) == "abc"
    finally:
        callback.close()


def test_a_stray_answer_with_the_wrong_state_is_refused_and_the_real_one_still_finishes():
    callback = oauth.Callback()
    callback.listen("the-state")
    try:
        status, page = callback_answer(f"{callback.redirect_uri}?code=junk&state=forged")
        assert status == 400 and "match the sign-in" in page
        assert not callback.finished()
        status, page = callback_answer(f"{callback.redirect_uri}?code=real&state=the-state")
        assert status == 200 and "Signed in" in page
        assert callback.wait(1) == "real"
    finally:
        callback.close()


def test_an_answer_without_any_state_does_not_end_the_sign_in():
    callback = oauth.Callback()
    callback.listen("the-state")
    try:
        assert callback_answer(f"{callback.redirect_uri}?error=access_denied")[0] == 400
        with pytest.raises(oauth.OAuthError, match="wasn't finished"):
            callback.wait(0.3)
    finally:
        callback.close()


def test_an_idle_connection_neither_blocks_the_real_answer_nor_stops_close():
    callback = oauth.Callback()
    callback.listen("the-state")
    idle = socket.create_connection((oauth.LOOPBACK_HOST, callback.port))
    try:
        time.sleep(0.2)
        status, _page = callback_answer(f"{callback.redirect_uri}?code=real&state=the-state")
        assert status == 200 and callback.wait(1) == "real"
        closer = threading.Thread(target=callback.close, daemon=True)
        started = time.monotonic()
        closer.start()
        closer.join(3)
        assert not closer.is_alive() and time.monotonic() - started < 3
    finally:
        idle.close()


def test_the_callback_waits_only_so_long_for_a_silent_connection():
    assert 0 < oauth.CALLBACK_REQUEST_SECONDS <= 30
    callback = oauth.Callback()
    try:
        assert callback.server.daemon_threads and not callback.server.block_on_close
        assert callback.server.RequestHandlerClass.timeout == oauth.CALLBACK_REQUEST_SECONDS
    finally:
        callback.close()


def test_the_refused_page_never_repeats_markup_from_the_address():
    callback = oauth.Callback()
    callback.listen("s")
    markup = "<script>document.title='ran'</script>"
    query = urllib.parse.urlencode({"state": "s", "error": "x", "error_description": markup})
    try:
        status, page = callback_answer(f"{callback.redirect_uri}?{query}")
    finally:
        callback.close()
    assert status == 400 and markup not in page and html.escape(markup) in page


def test_a_turned_down_sign_in_says_so():
    callback = oauth.Callback()
    callback.listen("s")
    try:
        callback_answer(f"{callback.redirect_uri}?error=access_denied&error_description=No+thanks&state=s")
        with pytest.raises(oauth.OAuthError, match="turned down: No thanks"):
            callback.wait(1)
    finally:
        callback.close()


def test_a_pending_sign_in_times_out():
    callback = oauth.Callback()
    callback.listen("s")
    started = time.monotonic()
    try:
        with pytest.raises(oauth.OAuthError, match="wasn't finished"):
            callback.wait(0.2)
    finally:
        callback.close()
    assert time.monotonic() - started < 5


def test_a_pasted_address_is_checked_like_the_callback():
    assert oauth.code_from_address("http://127.0.0.1:5/callback?code=xyz&state=s", "s") == "xyz"
    with pytest.raises(oauth.OAuthError, match="doesn't match"):
        oauth.code_from_address("http://127.0.0.1:5/callback?code=xyz&state=t", "s")


def test_the_code_is_exchanged_with_the_verifier_and_resource(provider):
    found, url = provider
    discovery = oauth.discover(url)
    callback = oauth.Callback()
    client = oauth.register(discovery, callback.redirect_uri)
    pending = oauth.pending_sign_in(discovery, client, callback.redirect_uri)
    callback.listen(pending.state)
    try:
        urllib.request.urlopen(pending.url, timeout=5).read()
        code = callback.wait(2)
    finally:
        callback.close()
    tokens = oauth.exchange(discovery, client, pending, code, clock=lambda: 1000.0)
    assert tokens.access_token in found.access_tokens and tokens.refresh_token in found.refresh_tokens
    assert tokens.expires_at == 1000.0 + found.expires_in
    sent = found.token_requests[-1]
    assert sent["code_verifier"] == pending.verifier and sent["resource"] == url and "client_secret" not in sent


def test_a_wrong_verifier_is_turned_down_by_the_server(provider):
    found, url = provider
    discovery = oauth.discover(url)
    client = oauth.register(discovery, "http://127.0.0.1:7/callback")
    pending = oauth.pending_sign_in(discovery, client, "http://127.0.0.1:7/callback")
    status, location = no_redirect(pending.url)
    code = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(location).query))["code"]
    forged = oauth.Pending(pending.url, pending.state, oauth.code_verifier(), pending.redirect_uri)
    with pytest.raises(oauth.OAuthError, match="invalid_grant"):
        oauth.exchange(discovery, client, forged, code)
    assert status == 302


def no_redirect(address: str) -> tuple[int, str]:
    class Stop(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    try:
        urllib.request.build_opener(Stop).open(address, timeout=5)
    except urllib.error.HTTPError as error:
        return error.code, error.headers["Location"]
    return 200, ""


def test_a_refresh_keeps_the_old_refresh_token_when_none_comes_back():
    fetch = answers({"https://id.example/token": (200, {"access_token": "new", "token_type": "Bearer"}, {})})
    tokens = oauth.refreshed("https://id.example/token", "https://mcp.example/mcp", oauth.Client("c", "", "none"),
                             "old-refresh", fetch)
    assert tokens == oauth.Tokens("new", "old-refresh", 0.0, "")


def test_a_refused_refresh_is_an_error_without_the_token_in_it():
    fetch = answers({"https://id.example/token": (400, {"error": "invalid_grant"}, {})})
    with pytest.raises(oauth.OAuthError) as raised:
        oauth.refreshed("https://id.example/token", "r", oauth.Client("c", "", "none"), "secret-refresh", fetch)
    assert "invalid_grant" in str(raised.value) and "secret-refresh" not in str(raised.value)


def test_a_client_secret_goes_where_registration_said():
    sent = []

    def fetch(url, data=None, headers=None, method=None):
        sent.append((dict(urllib.parse.parse_qsl(data.decode())), headers))
        return 200, {}, b'{"access_token": "a"}'

    oauth.refreshed("https://id.example/token", "r", oauth.Client("c", "s", "client_secret_post"), "x", fetch)
    oauth.refreshed("https://id.example/token", "r", oauth.Client("c", "s", "client_secret_basic"), "x", fetch)
    assert sent[0][0]["client_secret"] == "s" and "Authorization" not in sent[0][1]
    assert "client_secret" not in sent[1][0] and sent[1][1]["Authorization"].startswith("Basic ")


def test_www_authenticate_parameters_are_read():
    found = oauth.challenge_parameters('Bearer realm="OAuth", resource_metadata="https://a/b", scope="read write", x=y')
    assert found == {"realm": "OAuth", "resource_metadata": "https://a/b", "scope": "read write", "x": "y"}


def canned(table: dict):
    asked = []

    def fetch(url, data=None, headers=None, method=None):
        asked.append((method, url))
        if url not in table:
            return 404, {}, b"{}"
        status, body = table[url]
        return status, {}, body if isinstance(body, bytes) else json.dumps(body).encode()

    fetch.asked = asked
    return fetch


REMOTE = "https://mcp.remote.example/mcp"
REMOTE_ISSUER = "https://mcp.remote.example"


def remote_metadata(**changes) -> dict:
    return {"issuer": REMOTE_ISSUER, "code_challenge_methods_supported": ["S256"],
            "authorization_endpoint": f"{REMOTE_ISSUER}/authorize", "token_endpoint": f"{REMOTE_ISSUER}/token",
            "registration_endpoint": f"{REMOTE_ISSUER}/register", **changes}


def remote_world(resource: dict | None = None, **metadata_changes):
    return canned({
        REMOTE: (401, b""),
        f"{REMOTE_ISSUER}/.well-known/oauth-protected-resource/mcp": (
            200, resource if resource is not None else {"resource": REMOTE, "authorization_servers": [REMOTE_ISSUER]}),
        f"{REMOTE_ISSUER}/.well-known/oauth-authorization-server": (200, remote_metadata(**metadata_changes)),
    })


@pytest.mark.parametrize("endpoint", [
    "http://127.0.0.1:8080/api/v1/containers/create",
    "https://127.0.0.1/register",
    "https://127.1/register",
    "https://0.0.0.0/register",
    "https://10.1.2.3/register",
    "https://192.168.1.1/register",
    "https://169.254.169.254/latest/meta-data/",
    "https://[::1]/register",
    "https://[fd00:ec2::254]/register",
    "https://[fe80::1]/register",
    "https://100.100.100.200/register",
    "https://metadata.google.internal/register",
    "https://router/register",
    "https://printer.local/register",
    "https://localhost/register",
])
def test_a_remote_server_cannot_point_its_sign_in_at_this_computer_or_a_private_network(endpoint):
    fetch = remote_world(registration_endpoint=endpoint)
    with pytest.raises(oauth.OAuthError, match="registration address"):
        oauth.discover(REMOTE, fetch)
    assert all(method == "GET" or url == REMOTE for method, url in fetch.asked)


def test_a_remote_server_cannot_send_discovery_to_this_computer():
    def fetch(url, data=None, headers=None, method=None):
        return 401, {"WWW-Authenticate": 'Bearer resource_metadata="https://127.0.0.1:9/prm"'}, b""

    with pytest.raises(oauth.OAuthError, match="resource metadata address"):
        oauth.discover(REMOTE, fetch)
    with pytest.raises(oauth.OAuthError, match="sign-in server address"):
        oauth.discover(REMOTE, remote_world({"resource": REMOTE, "authorization_servers": ["https://10.0.0.1"]}))


def test_a_server_on_this_computer_may_sign_in_on_this_computer(provider):
    found, url = provider
    assert oauth.discover(url).registration_endpoint == f"{found.base}/register"


def test_a_public_sign_in_server_is_still_accepted():
    discovery = oauth.discover(REMOTE, remote_world(token_endpoint="https://id.other.example/token"))
    assert discovery.token_endpoint == "https://id.other.example/token"


class Redirecting(http.server.BaseHTTPRequestHandler):
    hits = []

    def log_message(self, *args):
        return None

    def do_GET(self):
        Redirecting.hits.append(self.path)
        if self.path == "/target":
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")
            return
        self.send_response(302)
        self.send_header("Location", "/target")
        self.send_header("Content-Length", "0")
        self.end_headers()


def serving(handler):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def test_oauth_requests_never_follow_a_redirect():
    Redirecting.hits.clear()
    server, base = serving(Redirecting)
    try:
        status, headers, _body = oauth.fetched(f"{base}/prm", None, {"Accept": "application/json"}, "GET")
    finally:
        server.shutdown()
        server.server_close()
    assert status == 302 and oauth.header(headers, "Location") == "/target"
    assert Redirecting.hits == ["/prm"]


class Endless(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        return None

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        chunk = b" " * 65536
        try:
            for _ in range(64):
                self.wfile.write(chunk)
        except OSError:
            return


def test_an_answer_bigger_than_a_megabyte_is_not_read_whole():
    server, base = serving(Endless)
    try:
        with pytest.raises(oauth.OAuthError, match="more than 1024 KB"):
            oauth.fetched(f"{base}/.well-known/oauth-protected-resource")
    finally:
        server.shutdown()
        server.server_close()


def cut_short_server():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)

    def serve():
        while True:
            try:
                connection, _address = listener.accept()
            except OSError:
                return
            connection.recv(65536)
            connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 500\r\n\r\n{\"acc")
            connection.close()

    threading.Thread(target=serve, daemon=True).start()
    return listener, f"http://127.0.0.1:{listener.getsockname()[1]}/token"


def test_an_answer_cut_short_is_a_reach_problem_not_a_crash():
    listener, address = cut_short_server()
    try:
        with pytest.raises(oauth.Unavailable, match="Couldn't reach"):
            oauth.fetched(address, b"x", {}, "POST")
    finally:
        listener.close()


def test_an_address_urllib_cannot_use_is_an_oauth_error():
    with pytest.raises(oauth.OAuthError):
        oauth.fetched("http://[::1/x")


@pytest.mark.parametrize("resource, metadata_changes", [
    ({"resource": REMOTE, "authorization_servers": [REMOTE_ISSUER], "scopes_supported": [1, 2]}, {}),
    ({"resource": REMOTE, "authorization_servers": [REMOTE_ISSUER], "scopes_supported": "read"}, {}),
    ({"resource": REMOTE, "authorization_servers": [REMOTE_ISSUER]}, {"code_challenge_methods_supported": 7}),
    ({"resource": REMOTE, "authorization_servers": [["x"]]}, {}),
    ({"resource": REMOTE, "authorization_servers": "https://mcp.remote.example"}, {}),
    ({"resource": 5, "authorization_servers": [REMOTE_ISSUER]}, {}),
    ({"resource": REMOTE, "authorization_servers": [REMOTE_ISSUER]}, {"issuer": ["x"]}),
])
def test_metadata_of_the_wrong_shape_is_refused_plainly(resource, metadata_changes):
    with pytest.raises(oauth.OAuthError):
        oauth.discover(REMOTE, remote_world(resource, **metadata_changes))


def test_a_resource_metadata_hint_with_a_bad_port_is_refused_plainly():
    def fetch(url, data=None, headers=None, method=None):
        return 401, {"WWW-Authenticate": 'Bearer resource_metadata="https://mcp.remote.example:port/x"'}, b""

    with pytest.raises(oauth.OAuthError, match="resource metadata address"):
        oauth.discover(REMOTE, fetch)


@pytest.mark.parametrize("address", [
    "https://mcp.example/authorize\x1b]0;owned\x07",
    "https://mcp.example/authorize\x1b[2J",
    "https://mcp.example/a\nb",
    "https://mcp.example/a\x9bb",
    "https://user@mcp.example/authorize",
    "https://trusted.example@evil.example/authorize",
])
def test_addresses_with_control_characters_or_a_user_part_are_refused(address):
    assert not oauth.is_safe_address(address)
    with pytest.raises(oauth.OAuthError):
        oauth.checked_address(address, "sign-in page address")


def test_a_busy_or_broken_sign_in_server_is_unavailable_not_a_refusal():
    for status in (429, 500, 503):
        fetch = answers({"https://id.example/token": (status, {"error": "temporarily_unavailable"}, {})})
        with pytest.raises(oauth.Unavailable):
            oauth.refreshed("https://id.example/token", "r", oauth.Client("c", "", "none"), "x", fetch)
    fetch = answers({"https://id.example/token": (400, {"error": "invalid_grant"}, {})})
    with pytest.raises(oauth.OAuthError) as raised:
        oauth.refreshed("https://id.example/token", "r", oauth.Client("c", "", "none"), "x", fetch)
    assert not isinstance(raised.value, oauth.Unavailable)


def test_a_network_failure_is_unavailable():
    with pytest.raises(oauth.Unavailable):
        oauth.fetched("http://127.0.0.1:9/token", b"", {}, "POST")


def id_token(claims: dict) -> str:
    def part(data: dict) -> str:
        return oauth.base64_url(json.dumps(data).encode())

    return f"{part({'alg': 'none'})}.{part(claims)}.sig"


def test_the_account_comes_from_the_id_token_or_the_token_answer_when_the_service_says():
    with_id = {"access_token": "a", "id_token": id_token({"email": "me@example.com", "sub": "1"})}
    assert oauth.tokens_from(200, with_id, "", lambda: 0.0).account == "me@example.com"
    assert oauth.tokens_from(200, {"access_token": "a", "preferred_username": "drew"}, "", lambda: 0.0).account == "drew"
    assert oauth.tokens_from(200, {"access_token": "a", "id_token": "not.a.jwt!"}, "", lambda: 0.0).account == ""
    assert oauth.tokens_from(200, {"access_token": "a"}, "", lambda: 0.0).account == ""


def test_a_huge_lifetime_does_not_crash():
    tokens = oauth.tokens_from(200, {"access_token": "a", "expires_in": 10 ** 400}, "", lambda: 0.0)
    assert tokens.expires_at == oauth.LONGEST_LIFETIME_SECONDS


def test_revoking_sends_the_token_with_its_hint_to_the_advertised_address():
    sent = []

    def fetch(url, data=None, headers=None, method=None):
        sent.append((url, dict(urllib.parse.parse_qsl(data.decode()))))
        return 200, {}, b""

    oauth.revoked("https://id.example/revoke", "https://mcp.example/mcp", oauth.Client("c", "", "none"), "r1",
                  "refresh_token", fetch)
    assert sent == [("https://id.example/revoke", {"token": "r1", "token_type_hint": "refresh_token", "client_id": "c"})]
    with pytest.raises(oauth.OAuthError):
        oauth.revoked("https://10.0.0.1/revoke", "https://mcp.example/mcp", oauth.Client("c", "", "none"), "r1",
                      "refresh_token", fetch)


@pytest.mark.parametrize("address", ["https://[", "http://[::1/x", "https://a:99999/x", "https://a:port/x"])
def test_an_address_that_cannot_be_read_is_unsafe_rather_than_a_crash(address):
    assert not oauth.is_safe_address(address) and oauth.host_of(address) == "" and oauth.comparable(address) == ""
    with pytest.raises(oauth.WrongAnswer):
        oauth.code_from_address(address, "s")


def test_a_resource_that_cannot_be_read_is_refused_plainly():
    with pytest.raises(oauth.OAuthError, match="isn't the address you gave"):
        oauth.discover(REMOTE, remote_world({"resource": "https://[", "authorization_servers": [REMOTE_ISSUER]}))
