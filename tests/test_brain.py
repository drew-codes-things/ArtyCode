import hashlib
import hmac
import json
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from arty_cli import brain, settings
from tests.fake_servers import serving


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


def test_every_chat_asks_the_brain_for_agent_mode(home_brain):
    client = brain.Brain(settings.Settings(base_url=home_brain, api_key="key"))
    client.reply([{"role": "user", "content": "hi"}], tools=[{"type": "function", "function": {"name": "x"}}],
                 on_text=lambda text: None)
    headers = FakeBrain.seen[-1]["headers"]
    assert headers["X-Arty-Agent"] == "1"
    assert headers["X-Arty-Client-Tools"] == "1"


def test_every_chat_asks_the_brain_to_stream_the_models_reasoning(home_brain):
    client = brain.Brain(settings.Settings(base_url=home_brain, api_key="key"))
    client.reply([{"role": "user", "content": "hi"}], tools=[], on_text=lambda text: None)
    assert FakeBrain.seen[-1]["headers"]["X-Arty-Reasoning"] == "1"


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


DESKTOP_MISSING = (404, {"error": {"message": "unknown tool workspace__browser_open"}})


class SearchingBrain(BaseHTTPRequestHandler):
    seen = []
    answer = (200, {"result": "1. Python docs\n   https://docs.python.org\n   The docs"})
    desktop = DESKTOP_MISSING

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        SearchingBrain.seen.append({"path": self.path, "headers": self.headers, "body": body})
        status, answer = SearchingBrain.desktop if self.path.endswith("browser_open") else SearchingBrain.answer
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(answer).encode())

    def log_message(self, *args):
        pass


@pytest.fixture
def searching():
    SearchingBrain.seen = []
    SearchingBrain.desktop = DESKTOP_MISSING
    server = ThreadingHTTPServer(("127.0.0.1", 0), SearchingBrain)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield brain.Brain(settings.Settings(base_url=unused_port_url(), remote_url=f"http://127.0.0.1:{server.server_address[1]}/v1",
                                        api_key="key", access_client_id="id.access", access_client_secret="secret"))
    server.shutdown()


def test_a_web_search_opens_the_results_page_in_arty_s_desktop_browser(searching):
    SearchingBrain.desktop = (200, {"result": "Title: python json at DuckDuckGo\nPage text:\njson docs"})
    assert searching.search_web("python json", 4) == SearchingBrain.desktop[1]["result"]
    assert [request["path"] for request in SearchingBrain.seen] == ["/v1/tools/workspace__browser_open"]
    assert SearchingBrain.seen[0]["body"] == {
        "arguments": {"url": "https://html.duckduckgo.com/html/?q=python+json"}}
    assert SearchingBrain.seen[0]["headers"]["Authorization"] == "Bearer key"


def test_a_desktop_browser_in_trouble_hands_over_to_the_brains_search(searching):
    SearchingBrain.desktop = (200, {"result": "browser error: Chrome isn't running"})
    assert searching.search_web("python json", 4) == SearchingBrain.answer[1]["result"]
    assert SearchingBrain.seen[-1]["path"] == "/v1/tools/research__web_search"


def test_without_a_desktop_a_web_search_uses_the_brains_own_search_tool(searching):
    assert searching.search_web("python json", 4) == SearchingBrain.answer[1]["result"]
    request = SearchingBrain.seen[-1]
    assert request["path"] == "/v1/tools/research__web_search"
    assert request["body"] == {"arguments": {"query": "python json", "results": 4}}
    assert request["headers"]["Authorization"] == "Bearer key"
    assert request["headers"]["CF-Access-Client-Id"] == "id.access"


@pytest.mark.parametrize("answer, problem", [
    ((404, {"error": {"message": "unknown tool research__web_search"}}), "no web search"),
    ((500, {"error": {"message": "boom"}}), "boom"),
    ((200, {"nothing": "here"}), "didn't send search results"),
])
def test_a_search_the_brain_cannot_do_says_why(searching, monkeypatch, answer, problem):
    monkeypatch.setattr(SearchingBrain, "answer", answer)
    with pytest.raises(brain.BrainError, match=problem):
        searching.search_web("python json", 4)


def written_names_and_arguments(text):
    return [(call["function"]["name"], json.loads(call["function"]["arguments"]))
            for call in brain.written_tool_calls(text, OFFERED_NAMES)]


def test_a_written_call_after_a_sentence_is_recognised():
    written = 'I will read the file first.\n\n{"name": "read_file", "arguments": {"path":"calc/ops.py"}}'
    assert written_names_and_arguments(written) == [("read_file", {"path": "calc/ops.py"})]


def test_written_calls_one_per_line_are_all_recognised_in_order():
    written = ('{"name": "read_file", "arguments": {"path":"shop/prices.py"}}\n'
               '{"name": "run_command", "arguments": {"command":"pytest -q"}}\n')
    assert written_names_and_arguments(written) == [("read_file", {"path": "shop/prices.py"}),
                                                    ("run_command", {"command": "pytest -q"})]
    fenced = f"```json\n{written}```"
    assert written_names_and_arguments(fenced) == written_names_and_arguments(written)


@pytest.mark.parametrize("text", [
    'Here is the payload you asked about:\n{"debug": true}',
    'First:\n{"name": "read_file", "arguments": {"path": "a"}}\nthen this example:\n{"debug": true}',
    'Use it like this: {"name": "read_file", "arguments": {"path": "a"}} in your config.',
])
def test_json_that_is_not_only_calls_on_their_own_lines_stays_text(text):
    assert brain.written_tool_calls(text, OFFERED_NAMES) == []


HALF_AN_ANSWER = f"data: {json.dumps({'choices': [{'delta': {'content': 'half an ans'}}]})}\n\n".encode()


class CutOffBrain(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(HALF_AN_ANSWER)

    def log_message(self, *args):
        pass


class ChunkedCutOffBrain(CutOffBrain):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self.wfile.write(f"{len(HALF_AN_ANSWER):x}\r\n".encode() + HALF_AN_ANSWER + b"\r\n")
        self.close_connection = True


@pytest.mark.parametrize("handler", [CutOffBrain, ChunkedCutOffBrain])
def test_a_reply_cut_off_before_its_end_is_an_error_not_an_answer(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    client = brain.Brain(settings.Settings(remote_url=f"http://127.0.0.1:{server.server_address[1]}/v1", api_key="key"))
    try:
        with pytest.raises(brain.BrainError, match="stopped before its end"):
            client.reply([{"role": "user", "content": "hi"}], tools=[], on_text=lambda text: None)
    finally:
        server.shutdown()
    assert client.address() == ""


def test_a_reply_that_ends_with_a_finish_reason_is_complete():
    finished = [f"data: {json.dumps({'choices': [{'delta': {'content': 'ok'}, 'finish_reason': 'stop'}]})}"]
    assert brain.read_reply(finished, lambda text: None)["content"] == "ok"


def test_a_secure_connection_failing_mid_reply_is_a_brain_error(remote, monkeypatch):
    def failing_lines(response):
        yield HALF_AN_ANSWER.decode().strip()
        raise ssl.SSLError("record layer failure")

    monkeypatch.setattr(brain, "decoded_lines", failing_lines)
    client = brain.Brain(settings.Settings(remote_url=remote, api_key="key"))
    with pytest.raises(brain.BrainError, match="record layer failure"):
        client.reply([{"role": "user", "content": "hi"}], tools=[], on_text=lambda text: None)


def brain_at(url, model=settings.DEFAULT_MODEL):
    found = brain.Brain(settings.Settings(remote_url=f"{url}/v1", api_key="k", model=model))
    found.route = brain.Route(f"{url}/v1", {})
    return found


def error_body(message):
    return json.dumps({"error": {"message": message}})


def asked(client):
    return client.reply([{"role": "user", "content": "hi"}], tools=[], on_text=lambda text: None)


def test_a_refused_key_is_a_setup_problem_that_names_the_login_command():
    with serving([(401, error_body("bad key"))], "text/event-stream") as (url, _recorder):
        with pytest.raises(brain.SetupProblem, match="arty-cli login"):
            asked(brain_at(url))


@pytest.mark.parametrize("status, message", [
    (400, "arty/qwen9:1b isn't downloaded on the AI server; pick one from /v1/models?all=1"),
    (404, "model 'arty/qwen9:1b' not found"),
])
def test_a_model_the_brain_does_not_have_says_how_to_pick_another(status, message):
    with serving([(status, error_body(message))], "text/event-stream") as (url, _recorder):
        with pytest.raises(brain.SetupProblem) as raised:
            asked(brain_at(url, "arty/qwen9:1b"))
    said = str(raised.value)
    assert "arty/qwen9:1b" in said and "/model" in said and "--model" in said
    assert "/v1/models" not in said


def test_any_other_brain_error_is_not_a_setup_problem():
    with serving([(500, error_body("boom"))] * brain.FAULT_TRIES, "text/event-stream") as (url, _recorder):
        with pytest.raises(brain.BrainError) as raised:
            asked(brain_at(url))
    assert not isinstance(raised.value, brain.SetupProblem)
    assert "boom" in str(raised.value)


def test_a_dropped_connection_is_described_without_python_internals():
    client = brain_at("http://127.0.0.1:9")
    with pytest.raises(brain.BrainError) as raised:
        asked(client)
    said = str(raised.value)
    assert said.startswith("Lost the connection to Arty's brain at http://127.0.0.1:9/v1")
    assert "<urlopen error" not in said and "[Errno" not in said


def finished(content):
    return f"data: {json.dumps({'choices': [{'delta': {'content': content}, 'finish_reason': 'stop'}]})}\n\ndata: [DONE]\n\n"


class FlakyBrain(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    plan = []
    served = 0

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        step = FlakyBrain.plan[min(FlakyBrain.served, len(FlakyBrain.plan) - 1)]
        FlakyBrain.served += 1
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        step(self.wfile)
        self.close_connection = True

    def log_message(self, *args):
        pass


def cut_off(out):
    out.write(HALF_AN_ANSWER)
    out.flush()


def answered(out):
    out.write(finished("all done").encode())
    out.flush()


def keeps_working_then_answers(out):
    for _beat in range(5):
        out.write(b": working\n\n")
        out.flush()
        __import__("time").sleep(0.2)
    answered(out)


def goes_silent(out):
    out.write(b": working\n\n")
    out.flush()
    __import__("time").sleep(1.5)


@pytest.fixture
def flaky():
    FlakyBrain.plan, FlakyBrain.served = [], 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), FlakyBrain)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield brain.Brain(settings.Settings(remote_url=f"http://127.0.0.1:{server.server_address[1]}/v1", api_key="k"))
    server.shutdown()
    server.server_close()


def test_a_reply_dropped_part_way_is_asked_for_again_and_the_job_carries_on(flaky):
    FlakyBrain.plan = [cut_off, cut_off, answered]
    assert asked(flaky)["content"] == "all done"
    assert FlakyBrain.served == 3


def keeps_working_well_past_the_limit_then_answers(out):
    for _beat in range(12):
        out.write(b": working\n\n")
        out.flush()
        __import__("time").sleep(0.1)
    answered(out)


def test_the_idle_limit_is_generous():
    assert brain.STREAM_IDLE_SECONDS == 120


def test_a_slow_brain_that_keeps_saying_it_is_working_is_waited_for(flaky, monkeypatch):
    monkeypatch.setattr(brain, "STREAM_IDLE_SECONDS", 0.5)
    FlakyBrain.plan = [keeps_working_then_answers]
    assert asked(flaky)["content"] == "all done"
    assert FlakyBrain.served == 1


def test_keep_alives_for_much_longer_than_the_idle_limit_never_cut_a_long_task_off(flaky, monkeypatch):
    monkeypatch.setattr(brain, "STREAM_IDLE_SECONDS", 0.3)
    FlakyBrain.plan = [keeps_working_well_past_the_limit_then_answers]
    assert asked(flaky)["content"] == "all done"
    assert FlakyBrain.served == 1


def test_a_stream_gone_silent_past_the_limit_ends_the_request_saying_the_brain_stopped_answering(flaky, monkeypatch):
    monkeypatch.setattr(brain, "STREAM_IDLE_SECONDS", 0.5)
    FlakyBrain.plan = [goes_silent, answered]
    with pytest.raises(brain.BrainError, match="stopped answering") as raised:
        asked(flaky)
    assert not isinstance(raised.value, brain.Dropped)
    assert "0.5 s" in str(raised.value)
    assert FlakyBrain.served == 1


class SilentBrain(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        __import__("time").sleep(1.5)

    def log_message(self, *args):
        pass


class ChunkedThenSilentBrain(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        beat = b": working\n\n"
        self.wfile.write(f"{len(beat):x}\r\n".encode() + beat + b"\r\n")
        self.wfile.flush()
        __import__("time").sleep(1.5)
        self.close_connection = True

    def log_message(self, *args):
        pass


def test_a_chunked_stream_gone_silent_ends_the_request_the_same_way(monkeypatch):
    monkeypatch.setattr(brain, "STREAM_IDLE_SECONDS", 0.5)
    server = ThreadingHTTPServer(("127.0.0.1", 0), ChunkedThenSilentBrain)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with pytest.raises(brain.BrainError, match="stopped answering"):
            asked(brain_at(f"http://127.0.0.1:{server.server_address[1]}"))
    finally:
        server.shutdown()
        server.server_close()


def test_a_brain_that_never_starts_its_reply_ends_the_request_too(monkeypatch):
    monkeypatch.setattr(brain, "STREAM_IDLE_SECONDS", 0.5)
    server = ThreadingHTTPServer(("127.0.0.1", 0), SilentBrain)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = brain_at(f"http://127.0.0.1:{server.server_address[1]}")
        with pytest.raises(brain.BrainError, match="stopped answering"):
            asked(client)
    finally:
        server.shutdown()
        server.server_close()


def test_a_brain_that_vanishes_mid_job_is_waited_for_until_the_patience_runs_out(retries_wait_in_pretend_time):
    client = brain_at("http://127.0.0.1:9")
    with pytest.raises(brain.Dropped):
        asked(client)
    assert sum(retries_wait_in_pretend_time.slept) <= brain.retry.PATIENCE_SECONDS
    assert sum(retries_wait_in_pretend_time.slept) > brain.retry.PATIENCE_SECONDS - brain.retry.MAX_WAIT_SECONDS * 2


def test_a_brain_never_reached_fails_at_once_without_waiting(retries_wait_in_pretend_time):
    client = brain.Brain(settings.Settings(remote_url="http://127.0.0.1:9/v1", api_key="k"))
    with pytest.raises(brain.BrainError, match="Can't reach"):
        asked(client)
    assert retries_wait_in_pretend_time.slept == []


def test_the_brain_fault_note_is_tried_again_not_taken_as_the_answer():
    fault = finished(f"\n\n{brain.BRAIN_FAULT_NOTE}")
    with serving([(200, fault), (200, finished("fixed"))], "text/event-stream") as (url, recorder):
        assert asked(brain_at(url))["content"] == "fixed"
    assert len(recorder.requests) == 2


def test_a_brain_fault_that_keeps_happening_is_reported_after_a_few_tries():
    fault = finished(brain.BRAIN_FAULT_NOTE)
    with serving([(200, fault)] * brain.FAULT_TRIES, "text/event-stream") as (url, recorder):
        with pytest.raises(brain.BrainFault):
            asked(brain_at(url))
    assert len(recorder.requests) == brain.FAULT_TRIES


@pytest.mark.parametrize("status", [502, 504])
def test_a_gateway_that_gives_up_is_tried_again(status):
    with serving([(status, error_body("gateway")), (200, finished("ok"))], "text/event-stream") as (url, recorder):
        assert asked(brain_at(url))["content"] == "ok"


def test_the_tokens_the_brain_counted_come_back_with_the_reply():
    usage = {"prompt_tokens": 900, "completion_tokens": 120, "total_tokens": 1020}
    lines = [f"data: {json.dumps({'choices': [{'delta': {'content': 'hi'}, 'finish_reason': 'stop'}], 'usage': usage})}"]
    assert brain.read_reply(lines, lambda text: None)["usage"] == usage


def test_a_context_size_can_be_set_for_the_brain():
    assert brain.Brain(settings.Settings(context_tokens="32768")).context_tokens() == 32768
    assert brain.Brain(settings.Settings()).context_tokens() is None


@pytest.mark.parametrize("field", ["reasoning", "reasoning_content", "thinking"])
def test_reasoning_the_brain_streams_apart_from_the_answer_is_passed_on_but_not_shown(field):
    thought, shown = [], []
    message = brain.read_reply(sse({field: "Let me think. "}, {field: "Two plus two."}, {"content": "4"}),
                               shown.append, thought.append)
    assert thought == ["Let me think. ", "Two plus two."] and shown == ["4"]
    assert message["content"] == "4"


def test_reasoning_is_ignored_when_nothing_listens_for_it():
    assert brain.read_reply(sse({"reasoning": "hm"}, {"content": "4"}), lambda text: None)["content"] == "4"
