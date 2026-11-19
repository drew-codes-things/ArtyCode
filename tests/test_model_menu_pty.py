import fcntl
import json
import os
import re
import select
import struct
import sys
import termios
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="pseudo terminals are POSIX only")

ROOT = Path(__file__).resolve().parents[1]
ROWS, COLUMNS = 30, 100
WAIT_SECONDS = 25
ESCAPE, ENTER = b"\x1b", b"\r"
BRAIN_MODELS = [{"id": "arty/qwen3:14b", "description": "14B, all tools"},
                {"id": "arty/mimo-v2.6:9b", "description": "9B, sees images"},
                {"id": "own/Coder", "description": "private coder"}]
ESCAPE_SEQUENCE = re.compile(r"\x1b(\[[0-9;?]*[A-Za-z]|[()][0-9A-Za-z]|[=>])")


class FakeBrain:
    def __init__(self, delay=0.0, status=200):
        self.delay, self.status, self.paths = delay, status, []
        self.gate = threading.Event()
        self.gate.set()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                outer.paths.append(self.path)
                outer.gate.wait(WAIT_SECONDS)
                time.sleep(outer.delay)
                body = json.dumps({"object": "list", "data": BRAIN_MODELS} if outer.status == 200
                                  else {"error": "the model server is down"}).encode()
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def hold(self):
        self.gate.clear()

    def release(self):
        self.gate.set()

    def stop(self):
        self.gate.set()
        self.server.shutdown()
        self.server.server_close()


class Terminal:
    def __init__(self, brain, tmp_path):
        import pty
        import subprocess
        self.shown = ""
        folder = tmp_path / "project"
        folder.mkdir()
        environment = {**os.environ, "ARTY_REMOTE_URL": brain.url, "ARTY_API_KEY": "stand-in-key",
                       "XDG_CONFIG_HOME": str(tmp_path / "config"), "APPDATA": str(tmp_path / "config"),
                       "TERM": "xterm-256color", "PYTHONPATH": str(ROOT)}
        for name in ("ARTY_URL", "ARTY_CLI_MODEL", "NO_COLOR"):
            environment.pop(name, None)
        self.descriptor, screen_end = pty.openpty()
        fcntl.ioctl(screen_end, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLUMNS, 0, 0))
        self.process = subprocess.Popen([sys.executable, "-m", "arty_cli"], cwd=folder, env=environment,
                                        stdin=screen_end, stdout=screen_end, stderr=screen_end,
                                        start_new_session=True)
        os.close(screen_end)

    def read_for(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            ready, _, _ = select.select([self.descriptor], [], [], 0.05)
            if not ready:
                continue
            try:
                data = os.read(self.descriptor, 65536)
            except OSError:
                return
            self.shown += ESCAPE_SEQUENCE.sub("", data.decode("utf-8", "replace"))

    def sees(self, text: str, seconds: float = WAIT_SECONDS) -> bool:
        squeezed = re.sub(r"\s+", "", text)
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if squeezed in re.sub(r"\s+", "", self.shown):
                return True
            self.read_for(0.1)
        return False

    def type(self, data: bytes) -> None:
        os.write(self.descriptor, data)

    def close(self) -> None:
        self.process.kill()
        self.process.wait()
        os.close(self.descriptor)


@pytest.fixture
def start(tmp_path):
    opened = []

    def starting(brain):
        terminal = Terminal(brain, tmp_path)
        opened.append((terminal, brain))
        assert terminal.sees("Ask Arty to fix, explain or build something")
        return terminal

    yield starting
    for terminal, brain in opened:
        terminal.close()
        brain.stop()


def test_a_whole_slash_model_lists_every_model_in_a_real_terminal_before_enter(start):
    terminal = start(FakeBrain())
    terminal.type(b"/model")
    assert terminal.sees("arty/mimo-v2.6:9b")
    assert terminal.sees("9B, sees images") and terminal.sees("private coder") and terminal.sees("current")


def test_enter_on_a_whole_slash_model_opens_the_picker_with_every_model(start):
    terminal = start(FakeBrain())
    terminal.type(b"/model")
    assert terminal.sees("9B, sees images")
    terminal.type(ENTER)
    assert terminal.sees("Switch to which?")
    assert terminal.sees("own/Coder") and terminal.sees("(current)")
    terminal.type(ESCAPE)
    terminal.type(b"/")
    assert terminal.sees("/help")


def test_a_slow_brain_shows_loading_models_and_never_blocks_typing(start):
    brain = FakeBrain(delay=3.0)
    terminal = start(brain)
    terminal.type(b"/model" + ENTER)
    assert terminal.sees("Loading models...")
    terminal.type(b"abc")
    assert terminal.sees("Switch to which?")
    assert terminal.sees("private coder")


def test_a_brain_that_cannot_list_models_is_named_in_one_line_and_the_prompt_stays_usable(start):
    terminal = start(FakeBrain(status=500))
    terminal.type(b"/model" + ENTER)
    assert terminal.sees("Loading models...")
    assert terminal.sees("the model server is down")
    terminal.type(b"/")
    assert terminal.sees("/help")


def test_a_list_that_arrives_after_slash_model_was_typed_opens_the_menu_by_itself(start):
    brain = FakeBrain()
    brain.hold()
    terminal = start(brain)
    terminal.type(b"/model")
    terminal.read_for(1.5)
    assert "mimo-v2.6:9b" not in terminal.shown
    brain.release()
    assert terminal.sees("mimo-v2.6:9b") and terminal.sees("private coder")


def test_a_list_that_arrives_long_after_slash_model_was_typed_still_opens_the_menu_by_itself(start):
    brain = FakeBrain()
    brain.hold()
    terminal = start(brain)
    terminal.type(b"/model")
    terminal.read_for(6.5)
    assert "mimo-v2.6:9b" not in terminal.shown
    brain.release()
    assert terminal.sees("mimo-v2.6:9b") and terminal.sees("private coder")
