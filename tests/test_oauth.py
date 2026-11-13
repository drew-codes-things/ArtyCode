import json
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


def test_a_state_mismatch_is_refused():
    callback = oauth.Callback()
    callback.listen("the-state")
    try:
        status, page = callback_answer(f"{callback.redirect_uri}?code=abc&state=forged")
        assert status == 400 and "doesn't match" in page
        with pytest.raises(oauth.OAuthError, match="doesn't match"):
            callback.wait(1)
    finally:
        callback.close()


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
