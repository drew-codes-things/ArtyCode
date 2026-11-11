import hashlib
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from arty_cli import brain, settings


def sse(*chunks):
    lines = []
    for chunk in chunks:
        lines += [f"data: {json.dumps({'choices': [{'delta': chunk}]})}", ""]
    return lines + ["data: [DONE]", ""]


def test_a_streamed_answer_is_shown_as_it_arrives_and_returned_whole():
    shown = []
    message = brain.read_reply(sse({"content": "Hel"}, {"content": "lo"}), shown.append)
    assert shown == ["Hel", "lo"]
    assert message == {"role": "assistant", "content": "Hello", "tool_calls": []}


def test_tool_calls_are_collected_in_order():
    call_a = {"index": 0, "id": "a", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
    call_b = {"index": 1, "id": "b", "type": "function", "function": {"name": "search", "arguments": "{}"}}
    message = brain.read_reply(sse({"tool_calls": [call_b]}, {"tool_calls": [call_a]}), lambda text: None)
    assert [call["id"] for call in message["tool_calls"]] == ["a", "b"]
    assert all("index" not in call for call in message["tool_calls"])


def test_garbage_in_the_stream_is_an_error():
    with pytest.raises(brain.BrainError, match="unreadable"):
        brain.read_reply(["data: {nope"], lambda text: None)


class FakeBrain(BaseHTTPRequestHandler):
    seen = []
    status = 200

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeBrain.seen.append({"path": self.path, "headers": self.headers, "body": body})
        if FakeBrain.status != 200:
            self.send_response(FakeBrain.status)
            self.end_headers()
            self.wfile.write(b'{"error": {"message": "bad key"}}')
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write("\n".join(sse({"content": "ok"})).encode() + b"\n")

    def log_message(self, *args):
        pass


@pytest.fixture
def remote():
    FakeBrain.seen, FakeBrain.status = [], 200
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeBrain)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    server.shutdown()


def unused_port_url():
    return "http://127.0.0.1:9/v1"


def test_falls_back_to_the_remote_address_with_access_headers(remote):
    config = settings.Settings(base_url=unused_port_url(), remote_url=remote, api_key="key",
                               access_client_id="id.access", access_client_secret="secret")
    client = brain.Brain(config)
    message = client.reply([{"role": "user", "content": "hi"}], tools=[{"type": "function", "function": {"name": "x"}}],
                           on_text=lambda text: None)
    assert message["content"] == "ok"
    request = FakeBrain.seen[0]
    assert request["path"] == "/v1/chat/completions"
    assert request["headers"]["Authorization"] == "Bearer key"
    assert request["headers"]["CF-Access-Client-Id"] == "id.access"
    assert request["headers"]["X-Arty-Client-Tools"] == "1"
    assert "computer" in request["headers"]["X-Arty-Skip-Groups"]
    assert request["body"]["stream"] is True
    assert request["body"]["model"] == settings.DEFAULT_MODEL
    assert client.address() == remote


def test_the_local_address_gets_no_access_headers(home_brain):
    client = brain.Brain(settings.Settings(base_url=home_brain, api_key="key", access_client_id="id",
                                           access_client_secret="s"))
    client.reply([{"role": "user", "content": "hi"}], tools=[], on_text=lambda text: None)
    assert "CF-Access-Client-Id" not in FakeBrain.seen[0]["headers"]


def test_a_refused_key_says_so(home_brain):
    FakeBrain.status = 401
    Identity.key = "wrong"
    client = brain.Brain(settings.Settings(base_url=home_brain, api_key="wrong"))
    with pytest.raises(brain.BrainError, match="API key"):
        client.reply([{"role": "user", "content": "hi"}], tools=[], on_text=lambda text: None)


def test_nothing_reachable_names_what_was_tried():
    client = brain.Brain(settings.Settings(base_url=unused_port_url(), api_key="k"))
    with pytest.raises(brain.BrainError, match="127.0.0.1:9"):
        client.reply([{"role": "user", "content": "hi"}], tools=[], on_text=lambda text: None)


class Identity(BaseHTTPRequestHandler):
    key = "key"
    seen = []

    def do_GET(self):
        Identity.seen.append({"path": self.path, "headers": self.headers})
        nonce = self.path.partition("nonce=")[2]
        proof = hmac.new(Identity.key.encode(), f"arty-identity:{nonce}".encode(), hashlib.sha256).hexdigest()
        body = json.dumps({"proofs": ["0" * 64, proof]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        FakeBrain.do_POST(self)

    def log_message(self, *args):
        pass


@pytest.fixture
def home_brain():
    FakeBrain.seen, FakeBrain.status, Identity.seen, Identity.key = [], 200, [], "key"
    server = ThreadingHTTPServer(("127.0.0.1", 0), Identity)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    server.shutdown()


def test_the_home_address_is_used_once_it_proves_it_knows_the_key(home_brain):
    client = brain.Brain(settings.Settings(base_url=home_brain, api_key="key"))
    client.reply([{"role": "user", "content": "hi"}], tools=[], on_text=lambda text: None)
    check = Identity.seen[0]
    assert check["path"].startswith("/v1/identity?nonce=")
    assert len(check["path"].partition("nonce=")[2]) == 32
    assert "Authorization" not in check["headers"]
    assert FakeBrain.seen[0]["headers"]["Authorization"] == "Bearer key"


def test_something_else_at_the_home_address_never_gets_the_key(home_brain, remote):
    Identity.key = "someone else"
    client = brain.Brain(settings.Settings(base_url=home_brain, remote_url=remote, api_key="key"))
    client.reply([{"role": "user", "content": "hi"}], tools=[], on_text=lambda text: None)
    assert client.address() == remote
    assert len(FakeBrain.seen) == 1


def test_a_home_address_that_cannot_prove_itself_is_named_in_the_error(remote):
    client = brain.Brain(settings.Settings(base_url=remote, api_key="key"))
    with pytest.raises(brain.BrainError, match="couldn't prove"):
        client.reply([{"role": "user", "content": "hi"}], tools=[], on_text=lambda text: None)
    assert FakeBrain.seen == []


def test_a_device_key_is_proved_with_its_sha256_hex_as_the_hmac_key(home_brain):
    device_key = "arty_dev_" + "a1" * 20
    Identity.key = hashlib.sha256(device_key.encode()).hexdigest()
    client = brain.Brain(settings.Settings(base_url=home_brain, api_key=device_key))
    client.reply([{"role": "user", "content": "hi"}], tools=[], on_text=lambda text: None)
    assert FakeBrain.seen[0]["headers"]["Authorization"] == f"Bearer {device_key}"
    assert brain.identity_secret("shared-key") == "shared-key"


OFFERED = [{"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}
           for name in ("run_command", "read_file")]
OFFERED_NAMES = {"run_command", "read_file"}


def test_a_tool_call_written_as_a_fenced_json_block_becomes_a_real_call():
    written = 'Running them now.\n```json\n{"name": "run_command", "arguments": {"command": "pytest -q"}}\n```'
    calls = brain.written_tool_calls(written, OFFERED_NAMES)
    assert [(call["function"]["name"], json.loads(call["function"]["arguments"])) for call in calls] == \
        [("run_command", {"command": "pytest -q"})]
    assert calls[0]["type"] == "function" and calls[0]["id"]


def test_a_tool_call_in_tool_call_tags_or_bare_json_is_recognised():
    tagged = '<tool_call>\n{"name": "read_file", "arguments": "{\\"path\\": \\"a.py\\"}"}\n</tool_call>'
    bare = '{"name": "read_file", "parameters": {"path": "b.py"}}'
    assert json.loads(brain.written_tool_calls(tagged, OFFERED_NAMES)[0]["function"]["arguments"]) == {"path": "a.py"}
    assert json.loads(brain.written_tool_calls(bare, OFFERED_NAMES)[0]["function"]["arguments"]) == {"path": "b.py"}


def test_several_written_calls_keep_their_order_and_get_distinct_ids():
    written = '```json\n[{"name": "read_file", "arguments": {"path": "a"}}, {"name": "read_file", "arguments": {"path": "b"}}]\n```'
    calls = brain.written_tool_calls(written, OFFERED_NAMES)
    assert [json.loads(call["function"]["arguments"])["path"] for call in calls] == ["a", "b"]
    assert len({call["id"] for call in calls}) == 2


@pytest.mark.parametrize("text", [
    "The answer is 42.",
    '```json\n{"name": "delete_everything", "arguments": {}}\n```',
    'Here is a config example:\n```json\n{"name": "my-app", "version": "1.0"}\n```',
    '```json\n{"name": "read_file", "arguments": {"path": "a"}}\n```\nand an example:\n```json\n{"debug": true}\n```',
    "",
])
def test_ordinary_text_and_unknown_tools_are_never_turned_into_calls(text):
    assert brain.written_tool_calls(text, OFFERED_NAMES) == []


def test_a_reply_that_only_writes_its_call_is_run_as_a_call(monkeypatch):
    written = '```json\n{"name": "run_command", "arguments": {"command": "pytest -q"}}\n```'
    arty = brain.Brain(settings.Settings(base_url="http://brain.lan:8081/v1", api_key="k"))
    monkeypatch.setattr(arty, "pick_route", lambda: brain.Route("http://brain.lan:8081/v1", {}))
    monkeypatch.setattr(arty, "streamed_reply",
                        lambda route, body, on_text: {"role": "assistant", "content": written, "tool_calls": []})
    reply = arty.reply([{"role": "user", "content": "run the tests"}], OFFERED, lambda text: None)
    assert reply["content"] == ""
    assert [call["function"]["name"] for call in reply["tool_calls"]] == ["run_command"]


def test_a_reply_without_offered_tools_is_left_as_text(monkeypatch):
    written = '{"name": "run_command", "arguments": {"command": "ls"}}'
    arty = brain.Brain(settings.Settings(base_url="http://brain.lan:8081/v1", api_key="k"))
    monkeypatch.setattr(arty, "pick_route", lambda: brain.Route("http://brain.lan:8081/v1", {}))
    monkeypatch.setattr(arty, "streamed_reply",
                        lambda route, body, on_text: {"role": "assistant", "content": written, "tool_calls": []})
    assert arty.reply([{"role": "user", "content": "hi"}], [], lambda text: None)["content"] == written
