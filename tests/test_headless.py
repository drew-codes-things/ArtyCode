import base64
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from arty_cli import eyes, headless

needs_posix_programs = pytest.mark.skipif(sys.platform == "win32", reason="the fake browser is a POSIX script")
needs_browser = pytest.mark.skipif(headless.find_browser() is None, reason="no Chrome, Edge or Chromium installed")


def fake_program(folder: Path, name: str, body: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    program = folder / name
    program.write_text(f"#!{sys.executable}\n{body}")
    program.chmod(0o755)
    return program


@needs_posix_programs
def test_a_browser_on_the_path_is_found(tmp_path, monkeypatch):
    program = fake_program(tmp_path / "bin", "chromium", "")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    assert headless.find_browser({}) == str(program)


@needs_posix_programs
def test_a_vendors_chrome_on_the_path_comes_before_chromium(tmp_path, monkeypatch):
    fake_program(tmp_path / "bin", "chromium", "")
    chrome = fake_program(tmp_path / "bin", "acme-chrome-stable", "")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    assert headless.find_browser({}) == str(chrome)


@needs_posix_programs
def test_the_browser_setting_wins_over_the_path(tmp_path, monkeypatch):
    fake_program(tmp_path / "bin", "acme-chrome", "")
    chosen = fake_program(tmp_path / "other", "my-browser", "")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    assert headless.find_browser({headless.BROWSER_SETTING: str(chosen)}) == str(chosen)


def test_no_browser_is_none(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert headless.find_browser({}, platform="linux") is None
    assert headless.find_browser({headless.BROWSER_SETTING: str(tmp_path / "missing")}) is None


@pytest.mark.parametrize("place, relative", [
    ("ProgramFiles", "Acme/Chrome/Application/chrome.exe"),
    ("LOCALAPPDATA", "Acme/Chrome/Application/chrome.exe"),
    ("ProgramFiles(x86)", "Microsoft/Edge/Application/msedge.exe"),
    ("LOCALAPPDATA", "Chromium/Application/chrome.exe"),
])
def test_chrome_edge_and_chromium_are_found_where_windows_installs_them(tmp_path, monkeypatch, place, relative):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    installed = tmp_path / place / relative
    installed.parent.mkdir(parents=True)
    installed.write_bytes(b"")
    assert headless.find_browser({place: str(tmp_path / place)}, platform="win32") == str(installed)


def test_the_browser_starts_headless_with_a_throwaway_profile_and_no_sound(tmp_path):
    command = headless.launch_command("chrome", tmp_path / "profile")
    assert command[0] == "chrome" and command[-1] == headless.FIRST_PAGE
    assert f"--user-data-dir={tmp_path / 'profile'}" in command
    for flag in ("--headless=new", "--remote-debugging-port=0", "--mute-audio", "--no-first-run",
                 "--disable-extensions"):
        assert flag in command


@needs_posix_programs
def test_a_browser_that_stops_at_once_is_reported_and_its_profile_removed(tmp_path, monkeypatch):
    seen = tmp_path / "argv.json"
    program = fake_program(tmp_path / "bin", "chrome", "import json, sys\n"
                           f"json.dump(sys.argv, open({str(seen)!r}, 'w'))\n"
                           "sys.stderr.write('no display here')\nsys.exit(3)\n")
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temporary))
    with pytest.raises(headless.HeadlessError, match=r"exit code 3\).*no display here"):
        with headless.Browser(str(program)):
            pass
    arguments = json.loads(seen.read_text())
    profile = next(argument for argument in arguments if argument.startswith("--user-data-dir="))
    assert profile.startswith(f"--user-data-dir={temporary}")
    assert list(temporary.iterdir()) == []


@needs_posix_programs
def test_a_browser_that_never_gets_ready_is_stopped(tmp_path, monkeypatch):
    program = fake_program(tmp_path / "bin", "chrome", "import time\ntime.sleep(60)\n")
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temporary))
    monkeypatch.setattr(headless, "LAUNCH_SECONDS", 0.5)
    browser = headless.Browser(str(program))
    with pytest.raises(headless.HeadlessError, match="wasn't ready"):
        with browser:
            pass
    assert browser.process is None
    assert list(temporary.iterdir()) == []


def server_frame(payload: bytes, opcode: int = headless.TEXT, final: bool = True) -> bytes:
    size = len(payload)
    if size < 126:
        length = bytes([size])
    elif size < 1 << 16:
        length = bytes([126]) + struct.pack(">H", size)
    else:
        length = bytes([127]) + struct.pack(">Q", size)
    return bytes([(0x80 if final else 0) | opcode]) + length + payload


@pytest.mark.parametrize("size", [5, 300, 70_000])
def test_frames_sent_to_the_browser_are_masked_and_read_back_whole(size):
    payload = os.urandom(size)
    ours, theirs = socket.socketpair()
    with ours, theirs:
        ours.sendall(headless.frame(payload))
        final, opcode, received = headless.WebSocket(theirs).frame_parts()
    assert (final, opcode, received) == (True, headless.TEXT, payload)
    assert headless.frame(payload)[1] & headless.MASK_BIT


def test_a_message_in_pieces_is_joined_and_pings_are_answered():
    ours, theirs = socket.socketpair()
    with ours, theirs:
        theirs.sendall(server_frame(b'{"a": ', final=False) + server_frame(b"hi", headless.PING)
                       + server_frame(b'"b"}' + b" " * 200, headless.CONTINUATION))
        websocket = headless.WebSocket(ours)
        assert json.loads(websocket.message()) == {"a": "b"}
        assert headless.WebSocket(theirs).frame_parts() == (True, headless.PONG, b"hi")


def test_a_closed_connection_is_an_error():
    ours, theirs = socket.socketpair()
    with ours:
        theirs.sendall(server_frame(b"", headless.CLOSE))
        theirs.close()
        with pytest.raises(headless.HeadlessError, match="closed"):
            headless.WebSocket(ours).message()


def handshake_server(accept_of):
    listener = socket.create_server(("127.0.0.1", 0))

    def serve():
        connection, _ = listener.accept()
        with connection:
            request = b""
            while b"\r\n\r\n" not in request:
                request += connection.recv(4096)
            key = next(line.split(":", 1)[1].strip() for line in request.decode().split("\r\n")
                       if line.lower().startswith("sec-websocket-key"))
            connection.sendall(f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                               f"Sec-WebSocket-Accept: {accept_of(key)}\r\n\r\n".encode() + server_frame(b'{"id": 1}'))
            connection.recv(4096)
        listener.close()

    threading.Thread(target=serve, daemon=True).start()
    return f"ws://127.0.0.1:{listener.getsockname()[1]}/devtools/page/ABC"


def test_the_websocket_handshake_is_checked_before_messages_are_read():
    websocket = headless.WebSocket.opened(handshake_server(headless.accept_key), 5)
    try:
        assert json.loads(websocket.message()) == {"id": 1}
    finally:
        websocket.close()
    with pytest.raises(headless.HeadlessError, match="didn't match"):
        headless.WebSocket.opened(handshake_server(lambda key: "wrong"), 5)


class FakeWebSocket:
    def __init__(self, answers):
        self.answers = answers
        self.waiting = []
        self.sent = []

    def send_text(self, text):
        message = json.loads(text)
        self.sent.append(message)
        events, result = self.answers.get(message["method"], ([], {}))
        self.waiting += [{"method": name, "params": params} for name, params in events]
        self.waiting.append({"id": message["id"], **({"error": result["error"]} if "error" in result
                                                       else {"result": result})})

    def has_data(self, seconds):
        return bool(self.waiting)

    def message(self):
        return json.dumps(self.waiting.pop(0))


PAGE_EVENTS = [
    ("Network.requestWillBeSent", {"requestId": "1", "request": {"url": "http://localhost:8000/"}}),
    ("Network.responseReceived", {"requestId": "1", "type": "Document", "frameId": "F",
                                  "response": {"url": "http://localhost:8000/", "status": 200, "statusText": "OK"}}),
    ("Network.requestWillBeSent", {"requestId": "2", "request": {"url": "http://localhost:8000/logo.png"}}),
    ("Network.responseReceived", {"requestId": "2", "type": "Image", "frameId": "F",
                                  "response": {"url": "http://localhost:8000/logo.png", "status": 404}}),
    ("Network.requestWillBeSent", {"requestId": "3", "request": {"url": "http://localhost:9/api"}}),
    ("Network.loadingFailed", {"requestId": "3", "errorText": "net::ERR_CONNECTION_REFUSED"}),
    ("Network.loadingFailed", {"requestId": "4", "errorText": "net::ERR_ABORTED", "canceled": True}),
    ("Runtime.consoleAPICalled", {"type": "log", "args": [{"type": "string", "value": "hello"}]}),
    ("Runtime.consoleAPICalled", {"type": "warning", "args": [{"type": "string", "value": "careful"}]}),
    ("Runtime.consoleAPICalled", {"type": "error", "args": [{"type": "string", "value": "bad"},
                                                             {"type": "number", "value": 2}]}),
    ("Runtime.exceptionThrown", {"exceptionDetails": {
        "text": "Uncaught", "url": "http://localhost:8000/app.js", "lineNumber": 9,
        "exception": {"description": "ReferenceError: x is not defined\n    at app.js:10"}}}),
    ("Log.entryAdded", {"entry": {"level": "error", "source": "security", "text": "Refused to load the script"}}),
    ("Log.entryAdded", {"entry": {"level": "error", "source": "network", "text": "Failed to load resource"}}),
    ("Page.loadEventFired", {}),
]


def test_page_events_become_status_errors_and_failed_requests():
    visit = headless.Visit("http://localhost:8000/", 1280, 800)
    headless.note_events(visit, [{"method": name, "params": params} for name, params in PAGE_EVENTS], "F")
    assert (visit.status, visit.status_text) == (200, "OK")
    assert visit.errors == ["console.error: bad 2",
                            "Uncaught ReferenceError: x is not defined (http://localhost:8000/app.js:10)",
                            "Refused to load the script"]
    assert visit.warnings == 1
    assert visit.failed_requests == ["404 http://localhost:8000/logo.png",
                                     "net::ERR_CONNECTION_REFUSED http://localhost:9/api"]


def test_a_visit_sizes_the_page_loads_it_and_takes_a_picture():
    picture = b"\x89PNG fake"
    fake = FakeWebSocket({
        "Page.navigate": (PAGE_EVENTS, {"frameId": "F"}),
        "Runtime.evaluate": ([], {"result": {"type": "string", "value": "Shop"}}),
        "Page.captureScreenshot": ([], {"data": base64.b64encode(picture).decode()}),
    })
    visit = headless.visit(headless.DevToolsPage(fake), "http://localhost:8000/", 390, 844, mobile=True,
                           settle_seconds=0)
    assert fake.sent[0]["params"] == {"width": 390, "height": 844, "deviceScaleFactor": 1, "mobile": True}
    assert fake.sent[1] == {"id": 2, "method": "Page.navigate", "params": {"url": "http://localhost:8000/"}}
    assert visit.picture == picture and visit.title == "Shop" and visit.status == 200 and visit.loaded


def test_a_navigation_that_fails_is_reported_without_waiting_for_the_load():
    fake = FakeWebSocket({"Page.navigate": ([], {"frameId": "F", "errorText": "net::ERR_CONNECTION_REFUSED"}),
                          "Page.captureScreenshot": ([], {"data": ""})})
    visit = headless.visit(headless.DevToolsPage(fake), "http://localhost:1/", 1280, 800, settle_seconds=0)
    assert visit.failure == "net::ERR_CONNECTION_REFUSED"


def test_a_refused_browser_call_is_an_error():
    fake = FakeWebSocket({"Page.enable": ([], {"error": {"message": "no such domain"}})})
    with pytest.raises(headless.HeadlessError, match="refused Page.enable: no such domain"):
        headless.DevToolsPage(fake).call("Page.enable")


def test_a_browser_that_doesnt_answer_in_time_is_an_error():
    class Silent(FakeWebSocket):
        def send_text(self, text):
            self.sent.append(json.loads(text))

    with pytest.raises(headless.HeadlessError, match="didn't answer Page.enable"):
        headless.DevToolsPage(Silent({})).call("Page.enable", seconds=0.1)


BROKEN_PAGE = """<!doctype html><html><head><title>Eyes test</title></head>
<body style="background:#123456"><h1 style="color:white">Hello eyes</h1><img src="missing.png">
<script>console.error("made to fail"); notDefinedAnywhere();</script></body></html>"""


def chrome_processes_using(folder: str) -> list[str]:
    try:
        listed = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
    return [line for line in listed.splitlines() if folder in line]


@needs_browser
def test_a_real_headless_browser_opens_a_local_page_and_cleans_up(tmp_path, monkeypatch):
    page = tmp_path / "page.html"
    page.write_text(BROKEN_PAGE)
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temporary))
    browser = headless.Browser(headless.find_browser())
    with browser as opened:
        visit = headless.visit(opened, page.as_uri(), 640, 480)
    assert visit.title == "Eyes test" and "Hello eyes" in visit.text
    assert any("made to fail" in error for error in visit.errors)
    assert any("notDefinedAnywhere" in error for error in visit.errors)
    assert any("missing.png" in request for request in visit.failed_requests)
    assert eyes.picture_facts(visit.picture) == (eyes.PNG, 640, 480)
    assert browser.leftover == ""
    assert list(temporary.iterdir()) == []
    assert chrome_processes_using(str(temporary)) == []
