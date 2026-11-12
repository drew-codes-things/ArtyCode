import _thread
import contextlib
import json
import os
import queue
import re
import secrets
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from arty_cli import __version__, brain, permissions, prompt_line

SESSIONS_PATH = "/remote/sessions"
INBOX_WAIT_SECONDS = 25
WAIT_GRACE_SECONDS = 15
CALL_SECONDS = 15
RETRY_PAUSE_SECONDS = 3
POST_TRIES = 3
JOIN_SECONDS = 5
MAX_EVENTS_PER_POST = 50
MAX_TEXT_CHARS = 8000
LINE_POLL_SECONDS = 0.2
READ_CHUNK = 4096
REQUEST_ID_BYTES = 6
HERE = "the computer"
GONE_CODES = (404, 410)
PASSED_THROUGH = frozenset({"user", "permission", "permission_answered", "status", "error"})
IDLE, AT_PROMPT, IN_TURN = "idle", "at_prompt", "in_turn"
NOTHING_TO_STOP = "Nothing was running, so there was nothing to stop."
ERASE_KEY = "\b \b"
CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class RelayError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class Relay:
    def __init__(self, url: str, headers: dict, opener=urllib.request.urlopen):
        self.url = url.rstrip("/")
        self.headers = headers
        self.opener = opener

    def call(self, method: str, path: str, body: dict | None = None, timeout: float = CALL_SECONDS) -> dict:
        headers = {**self.headers, "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(f"{self.url}{path}", data=data, headers=headers, method=method)
        try:
            with self.opener(request, timeout=timeout) as response:
                answer = json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            reason = brain.error_message(error) or error.code
            raise RelayError(f"Arty's brain turned down remote control: {reason}", error.code) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, ValueError) as error:
            raise RelayError(f"Couldn't reach Arty's brain for remote control: {error}") from None
        if not isinstance(answer, dict):
            raise RelayError("Arty's brain sent something unexpected for remote control.")
        return answer

    def create(self, details: dict) -> str:
        session_id = self.call("POST", SESSIONS_PATH, details).get("id")
        if not isinstance(session_id, str) or not session_id:
            raise RelayError("Arty's brain didn't send a remote control session id.")
        return session_id

    def post(self, session_id: str, events: list[dict]) -> None:
        self.call("POST", f"{SESSIONS_PATH}/{session_id}/events", {"events": events})

    def inbox(self, session_id: str, wait: int = INBOX_WAIT_SECONDS) -> list[dict]:
        answer = self.call("GET", f"{SESSIONS_PATH}/{session_id}/inbox?wait={wait}", timeout=wait + WAIT_GRACE_SECONDS)
        items = answer.get("inbox")
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    def end(self, session_id: str) -> None:
        self.call("DELETE", f"{SESSIONS_PATH}/{session_id}")


def relay_for(client) -> Relay:
    route = client.pick_route()
    headers = {"Authorization": f"Bearer {client.settings.api_key}", "User-Agent": f"arty-cli/{__version__}",
               **route.headers}
    return Relay(route.url, headers)


def printable(text: str) -> str:
    return CONTROL_CHARACTERS.sub("", text)


def shown_input(given) -> str:
    return given if isinstance(given, str) else json.dumps(given, ensure_ascii=False)


def relay_event(event: dict) -> dict | None:
    kind = event.get("type")
    if kind == "text":
        return {"type": "assistant", "text": event["text"]} if event.get("text") else None
    if kind == "tool_use":
        return {"type": "tool_call", "tool": event.get("name", ""), "text": shown_input(event.get("input"))}
    if kind == "tool_result":
        return {"type": "tool_result", "tool": event.get("name", ""), "text": str(event.get("content", ""))}
    return dict(event) if kind in PASSED_THROUGH else None


def within_limit(event: dict) -> list[dict]:
    text = event.get("text")
    if not isinstance(text, str) or len(text) <= MAX_TEXT_CHARS:
        return [event]
    if event["type"] != "assistant":
        return [{**event, "text": text[:MAX_TEXT_CHARS]}]
    return [{**event, "text": text[start:start + MAX_TEXT_CHARS]} for start in range(0, len(text), MAX_TEXT_CHARS)]


def batched(events: list[dict]) -> list[list[dict]]:
    merged = []
    for event in events:
        if merged and event["type"] == "assistant" and merged[-1]["type"] == "assistant":
            merged[-1] = {**merged[-1], "text": merged[-1]["text"] + event["text"]}
        else:
            merged.append(event)
    pieces = [piece for event in merged for piece in within_limit(event)]
    return [pieces[start:start + MAX_EVENTS_PER_POST] for start in range(0, len(pieces), MAX_EVENTS_PER_POST)]


def interrupt_main_thread() -> None:
    if hasattr(signal, "pthread_kill"):
        signal.pthread_kill(threading.main_thread().ident, signal.SIGINT)
    else:
        _thread.interrupt_main()


class Waker:
    def __init__(self, interrupt=interrupt_main_thread):
        self.interrupt = interrupt
        self.lock = threading.RLock()
        self.state = IDLE
        self.sent_for = None
        self.previous_handler = None

    def install(self) -> None:
        self.previous_handler = signal.signal(signal.SIGINT, self.on_signal)

    def uninstall(self) -> None:
        if self.previous_handler is not None:
            signal.signal(signal.SIGINT, self.previous_handler)
            self.previous_handler = None

    @contextlib.contextmanager
    def during(self, state: str):
        with self.lock:
            before, self.state = self.state, state
        try:
            yield
        finally:
            with self.lock:
                self.state = before

    def wake_prompt(self) -> bool:
        return self.nudge(AT_PROMPT)

    def stop_turn(self) -> bool:
        return self.nudge(IN_TURN)

    def nudge(self, wanted: str) -> bool:
        with self.lock:
            if self.state != wanted or self.sent_for is not None:
                return False
            self.sent_for = wanted
        self.interrupt()
        return True

    def on_signal(self, _signum, _frame) -> None:
        with self.lock:
            sent_for, self.sent_for = self.sent_for, None
            if sent_for is not None and sent_for != self.state:
                return
        raise KeyboardInterrupt


class PosixLines:
    def __init__(self, descriptor: int, wait=None, read=os.read, flush=None):
        self.descriptor = descriptor
        self.wait = wait or default_wait
        self.read = read
        self.flush = flush or flush_typed

    def line(self, seconds: float) -> str | None:
        if not self.wait(self.descriptor, seconds):
            return None
        data = self.read(self.descriptor, READ_CHUNK)
        if not data:
            raise EOFError
        return data.decode("utf-8", errors="replace")

    def discard(self) -> None:
        self.flush(self.descriptor)


def default_wait(descriptor: int, seconds: float) -> bool:
    import select
    return bool(select.select([descriptor], [], [], seconds)[0])


def flush_typed(descriptor: int) -> None:
    import termios
    termios.tcflush(descriptor, termios.TCIFLUSH)


class ConsoleLines:
    def __init__(self, has_key, key, write, sleep=time.sleep, clock=time.monotonic):
        self.has_key = has_key
        self.key = key
        self.write = write
        self.sleep = sleep
        self.clock = clock
        self.typed = []

    def line(self, seconds: float) -> str | None:
        deadline = self.clock() + seconds
        while True:
            while self.has_key():
                finished = self.take(self.key())
                if finished is not None:
                    return finished
            if self.clock() >= deadline:
                return None
            self.sleep(prompt_line.KEY_POLL_SECONDS)

    def take(self, key: str) -> str | None:
        if key in prompt_line.ENTER_KEYS:
            self.write(prompt_line.NEW_LINE)
            finished, self.typed = "".join(self.typed), []
            return finished
        if key == prompt_line.CTRL_C:
            self.write(prompt_line.NEW_LINE)
            raise KeyboardInterrupt
        if key in prompt_line.EXTENDED_KEYS:
            self.key()
        elif key == prompt_line.BACKSPACE:
            if self.typed:
                self.typed.pop()
                self.write(ERASE_KEY)
        elif key.isprintable():
            self.typed.append(key)
            self.write(key)
        return None

    def discard(self) -> None:
        while self.has_key():
            self.key()
        self.typed = []


def answer_lines(is_interactive):
    if not is_interactive():
        return None
    if os.name == "nt":
        import msvcrt
        return ConsoleLines(msvcrt.kbhit, msvcrt.getwch, prompt_line.shown_at_once)
    return PosixLines(sys.stdin.fileno())


@dataclass(frozen=True)
class Answer:
    allow: bool
    by: str


@dataclass(frozen=True)
class Message:
    text: str
    by: str


class Bridge:
    def __init__(self, relay: Relay, waker: Waker | None = None, lines=None, write=None):
        self.relay = relay
        self.waker = waker or Waker()
        self.lines = lines
        self.write_prompt = write or prompt_line.shown_at_once
        self.session_id = ""
        self.outgoing = queue.Queue()
        self.messages = queue.Queue()
        self.notices = queue.Queue()
        self.answers = {}
        self.open_requests = set()
        self.answered = threading.Condition()
        self.stopping = threading.Event()
        self.ended = False
        self.stopped_by = ""
        self.told_mode = ""
        self.threads = []

    def start(self, details: dict) -> str:
        self.session_id = self.relay.create(details)
        self.told_mode = details.get("mode", "")
        self.waker.install()
        self.threads = [threading.Thread(target=self.listen, daemon=True, name="arty-remote-inbox"),
                        threading.Thread(target=self.deliver, daemon=True, name="arty-remote-events")]
        for thread in self.threads:
            thread.start()
        return self.session_id

    def stop(self) -> None:
        if self.stopping.is_set():
            return
        self.outgoing.put(None)
        self.threads[1].join(JOIN_SECONDS)
        self.stopping.set()
        self.waker.uninstall()
        with self.answered:
            self.answered.notify_all()
        if not self.ended:
            try:
                self.relay.end(self.session_id)
            except RelayError as error:
                self.notices.put(f"Couldn't end the shared session cleanly: {error}")
        self.ended = True
        self.threads[0].join(JOIN_SECONDS)

    def write(self, event: dict) -> None:
        self.send(event)

    def send(self, event: dict) -> None:
        shared = relay_event(event)
        if shared is not None and not self.stopping.is_set():
            self.outgoing.put(shared)

    def tell_mode(self, mode: str) -> None:
        if mode != self.told_mode:
            self.told_mode = mode
            self.send({"type": "status", "mode": mode, "text": f"Mode: {mode}"})

    def deliver(self) -> None:
        while True:
            waiting = [self.outgoing.get()]
            while not self.outgoing.empty():
                waiting.append(self.outgoing.get_nowait())
            finishing = None in waiting
            for batch in batched([event for event in waiting if event is not None]):
                self.post(batch)
            if finishing:
                return

    def post(self, batch: list[dict]) -> None:
        for attempt in range(POST_TRIES):
            try:
                self.relay.post(self.session_id, batch)
                return
            except RelayError as error:
                if error.status in GONE_CODES:
                    self.lose(error)
                    return
                if attempt == POST_TRIES - 1:
                    self.notices.put(f"Some of this session didn't reach your other devices: {error}")
                    return
                time.sleep(RETRY_PAUSE_SECONDS)

    def listen(self) -> None:
        while not self.stopping.is_set():
            try:
                items = self.relay.inbox(self.session_id)
            except RelayError as error:
                if error.status in GONE_CODES:
                    self.lose(error)
                    return
                if self.stopping.wait(RETRY_PAUSE_SECONDS):
                    return
                continue
            for item in items:
                self.take(item)

    def lose(self, error: RelayError) -> None:
        if self.stopping.is_set() or self.ended:
            return
        self.ended = True
        self.notices.put(f"Remote control ended: {error}")
        with self.answered:
            self.answered.notify_all()
        self.waker.wake_prompt()

    def take(self, item: dict) -> None:
        kind, by = item.get("type"), printable(str(item.get("by") or "another device"))
        text = printable(item["text"]) if isinstance(item.get("text"), str) else ""
        if kind == "message" and text.strip():
            self.messages.put(Message(text, by))
            self.waker.wake_prompt()
        elif kind == "answer" and isinstance(item.get("request_id"), str) and isinstance(item.get("allow"), bool):
            with self.answered:
                if item["request_id"] in self.open_requests:
                    self.answers[item["request_id"]] = Answer(item["allow"], by)
                    self.answered.notify_all()
        elif kind == "interrupt":
            self.stopped_by = by
            if not self.waker.stop_turn():
                self.stopped_by = ""
                self.send({"type": "status", "text": NOTHING_TO_STOP})

    def next_message(self) -> Message | None:
        try:
            return self.messages.get_nowait()
        except queue.Empty:
            return None

    def take_notices(self) -> list[str]:
        found = []
        while not self.notices.empty():
            found.append(self.notices.get_nowait())
        return found

    def stop_reason(self) -> str:
        by, self.stopped_by = self.stopped_by, ""
        return f"Stopped by {by}." if by else ""

    def remote_answer(self, request_id: str, wait: float) -> Answer | None:
        with self.answered:
            if request_id not in self.answers and not self.ended:
                self.answered.wait(wait)
            return self.answers.pop(request_id, None)

    def answer(self, prompt: str, answers: dict, question: str, details: str = "") -> tuple[str, str]:
        request_id = secrets.token_hex(REQUEST_ID_BYTES)
        with self.answered:
            self.open_requests.add(request_id)
        self.send({"type": "permission", "request_id": request_id,
                   "text": f"{question}\n{details}" if details else question})
        try:
            choice, by = self.raced(request_id, prompt, answers)
        except BaseException:
            self.close_request(request_id)
            self.send({"type": "permission_answered", "request_id": request_id, "allow": False,
                       "by": self.stopped_by or HERE})
            raise
        self.close_request(request_id)
        self.send({"type": "permission_answered", "request_id": request_id, "allow": choice != permissions.NO,
                   "by": by or HERE})
        return choice, by

    def close_request(self, request_id: str) -> None:
        with self.answered:
            self.open_requests.discard(request_id)
            self.answers.pop(request_id, None)

    def raced(self, request_id: str, prompt: str, answers: dict) -> tuple[str, str]:
        self.write_prompt(prompt)
        while True:
            found = self.remote_answer(request_id, 0 if self.lines else LINE_POLL_SECONDS)
            if found:
                if self.lines:
                    self.lines.discard()
                    self.write_prompt(prompt_line.NEW_LINE)
                return (permissions.YES if found.allow else permissions.NO), found.by
            if self.ended and not self.lines:
                return permissions.NO, ""
            typed = self.lines.line(LINE_POLL_SECONDS) if self.lines else None
            if typed is None:
                continue
            choice = typed.strip().lower()
            if choice in answers:
                return answers[choice], ""
            self.write_prompt(prompt)


def connect(client, details: dict, lines) -> Bridge:
    bridge = Bridge(relay_for(client), lines=lines)
    bridge.start(details)
    return bridge
