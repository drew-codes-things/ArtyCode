import base64
import hashlib
import json
import os
import select
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from arty_cli import tools

BROWSER_SETTING = "ARTY_BROWSER"
VENDOR_CHROME_NAMES = ("*-chrome", "*-chrome-stable")
BROWSER_NAMES = ("chromium", "chromium-browser", "microsoft-edge", "microsoft-edge-stable", "chrome", "msedge")
WINDOWS_BROWSERS = (("ProgramFiles", "*/Chrome/Application/chrome.exe"),
                    ("ProgramFiles(x86)", "*/Chrome/Application/chrome.exe"),
                    ("LOCALAPPDATA", "*/Chrome/Application/chrome.exe"),
                    ("ProgramFiles(x86)", "Microsoft/Edge/Application/msedge.exe"),
                    ("ProgramFiles", "Microsoft/Edge/Application/msedge.exe"),
                    ("LOCALAPPDATA", "Chromium/Application/chrome.exe"))
MAC_APPLICATIONS = Path("/Applications")
MAC_BROWSERS = ("*Chrome.app/Contents/MacOS/*Chrome", "Chromium.app/Contents/MacOS/Chromium",
                "Microsoft Edge.app/Contents/MacOS/Microsoft Edge")
LAUNCH_FLAGS = ("--headless=new", "--remote-debugging-port=0", "--no-first-run", "--no-default-browser-check",
                "--disable-extensions", "--disable-sync", "--disable-background-networking",
                "--disable-component-update", "--disable-default-apps", "--mute-audio", "--hide-scrollbars",
                "--password-store=basic", "--use-mock-keychain")
ROOT_ONLY_FLAGS = ("--no-sandbox",)
FIRST_PAGE = "about:blank"
PROFILE_PREFIX = "arty-browser-"
PROFILE_FOLDER = "profile"
LOG_FILE = "browser.log"
PORT_FILE = "DevToolsActivePort"
LAUNCH_SECONDS = 30
POLL_SECONDS = 0.05
CONNECT_SECONDS = 10
CALL_SECONDS = 30
NAVIGATE_SECONDS = 30
LOAD_SECONDS = 20
SETTLE_SECONDS = 1.0
REMOVE_TRIES = 5
REMOVE_PAUSE_SECONDS = 0.2
LOG_TAIL_CHARS = 600
RECEIVE_BYTES = 65_536
WEBSOCKET_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
CONTINUATION, TEXT, CLOSE, PING, PONG = 0x0, 0x1, 0x8, 0x9, 0xA
FINAL_BIT, MASK_BIT = 0x80, 0x80
LENGTH_BITS, OPCODE_BITS = 0x7F, 0x0F
SHORT_SIZE, MEDIUM_SIZE = 126, 127
MASK_BYTES = 4
DOMAINS = ("Page", "Runtime", "Network", "Log")
ERROR_CONSOLE_TYPES = ("error", "assert")
WARNING_CONSOLE_TYPE = "warning"
ERROR_LEVEL = "error"
NETWORK_SOURCE = "network"
DOCUMENT = "Document"
FIRST_BAD_STATUS = 400
TITLE_SCRIPT = "document.title"
TEXT_SCRIPT = "document.body ? document.body.innerText : ''"


class HeadlessError(Exception):
    pass


def matching(folder: Path, pattern: str) -> list[Path]:
    return sorted(found for found in folder.glob(pattern) if found.is_file())


def vendor_chrome_on_path() -> str | None:
    folders = [folder for folder in os.environ.get("PATH", "").split(os.pathsep) if folder]
    for folder in folders:
        for pattern in VENDOR_CHROME_NAMES:
            runnable = [found for found in matching(Path(folder), pattern) if os.access(found, os.X_OK)]
            if runnable:
                return str(runnable[0])
    return None


def windows_browsers(environment) -> list[Path]:
    return [found for place, relative in WINDOWS_BROWSERS if environment.get(place)
            for found in matching(Path(environment[place]), relative)]


def installed_browsers(environment, platform: str) -> list[Path]:
    if platform == "win32":
        return windows_browsers(environment)
    if platform == "darwin":
        return [found for pattern in MAC_BROWSERS for found in matching(MAC_APPLICATIONS, pattern)]
    return []


def find_browser(environment=None, platform: str | None = None) -> str | None:
    environment = os.environ if environment is None else environment
    platform = sys.platform if platform is None else platform
    chosen = environment.get(BROWSER_SETTING, "").strip()
    if chosen:
        return shutil.which(chosen) or (chosen if Path(chosen).is_file() else None)
    found = vendor_chrome_on_path() or next(filter(None, (shutil.which(name) for name in BROWSER_NAMES)), None)
    if found:
        return found
    places = installed_browsers(environment, platform)
    return str(places[0]) if places else None


def runs_as_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def launch_command(program: str, profile: Path) -> list[str]:
    extra = ROOT_ONLY_FLAGS if runs_as_root() else ()
    return [program, *LAUNCH_FLAGS, *extra, f"--user-data-dir={profile}", FIRST_PAGE]


def detached() -> dict:
    return {} if sys.platform == "win32" else {"start_new_session": True}


def log_tail(log: Path) -> str:
    try:
        text = log.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    return text[-LOG_TAIL_CHARS:]


def devtools_port(profile: Path, process: subprocess.Popen, log: Path) -> int:
    deadline = time.monotonic() + LAUNCH_SECONDS
    port_file = profile / PORT_FILE
    while time.monotonic() < deadline:
        code = process.poll()
        if code is not None:
            raise HeadlessError(f"the browser stopped before it was ready (exit code {code}). {log_tail(log)}".strip())
        try:
            first_line = port_file.read_text(encoding="utf-8").splitlines()[0]
        except (OSError, IndexError):
            first_line = ""
        if first_line.strip().isdigit():
            return int(first_line)
        time.sleep(POLL_SECONDS)
    raise HeadlessError(f"the browser wasn't ready after {LAUNCH_SECONDS} s. {log_tail(log)}".strip())


def without_proxies():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def page_socket_address(port: int) -> str:
    opener = without_proxies()
    base = f"http://127.0.0.1:{port}/json"
    try:
        with opener.open(f"{base}/list", timeout=CONNECT_SECONDS) as response:
            targets = json.loads(response.read() or b"[]")
        pages = [target for target in targets if isinstance(target, dict) and target.get("type") == "page"]
        if not pages:
            request = urllib.request.Request(f"{base}/new?{FIRST_PAGE}", method="PUT")
            with opener.open(request, timeout=CONNECT_SECONDS) as response:
                pages = [json.loads(response.read() or b"{}")]
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise HeadlessError(f"couldn't reach the browser's page: {error}") from None
    address = str(pages[0].get("webSocketDebuggerUrl") or "")
    if not address:
        raise HeadlessError("the browser didn't offer a page to control")
    return address


def masked(payload: bytes, mask: bytes) -> bytes:
    if not payload:
        return b""
    repeated = (mask * (len(payload) // MASK_BYTES + 1))[:len(payload)]
    return (int.from_bytes(payload, "big") ^ int.from_bytes(repeated, "big")).to_bytes(len(payload), "big")


def frame(payload: bytes, opcode: int = TEXT, mask: bytes | None = None) -> bytes:
    mask = os.urandom(MASK_BYTES) if mask is None else mask
    size = len(payload)
    if size < SHORT_SIZE:
        length = bytes([MASK_BIT | size])
    elif size < 1 << 16:
        length = bytes([MASK_BIT | SHORT_SIZE]) + struct.pack(">H", size)
    else:
        length = bytes([MASK_BIT | MEDIUM_SIZE]) + struct.pack(">Q", size)
    return bytes([FINAL_BIT | opcode]) + length + mask + masked(payload, mask)


def accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1(key.encode() + WEBSOCKET_GUID).digest()).decode()


def handshake(address: str, key: str) -> bytes:
    parts = urlparse(address)
    return (f"GET {parts.path or '/'} HTTP/1.1\r\nHost: {parts.hostname}:{parts.port}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode()


class WebSocket:
    def __init__(self, connection: socket.socket):
        self.connection = connection
        self.waiting = bytearray()

    @classmethod
    def opened(cls, address: str, seconds: float) -> "WebSocket":
        parts = urlparse(address)
        connection = socket.create_connection((parts.hostname, parts.port), timeout=seconds)
        found = cls(connection)
        key = base64.b64encode(os.urandom(16)).decode()
        try:
            connection.sendall(handshake(address, key))
            found.check_handshake(found.read_until(b"\r\n\r\n").decode("latin-1"), key)
        except (OSError, HeadlessError):
            connection.close()
            raise
        return found

    def check_handshake(self, reply: str, key: str) -> None:
        status, *headers = reply.split("\r\n")
        if " 101 " not in f"{status} ":
            raise HeadlessError(f"the browser refused the connection: {status}")
        accepted = {name.strip().lower(): value.strip() for name, _, value in
                    (line.partition(":") for line in headers)}.get("sec-websocket-accept")
        if accepted != accept_key(key):
            raise HeadlessError("the browser's connection reply didn't match")

    def received(self) -> None:
        chunk = self.connection.recv(RECEIVE_BYTES)
        if not chunk:
            raise HeadlessError("the browser closed the connection")
        self.waiting += chunk

    def read_until(self, marker: bytes) -> bytes:
        while marker not in self.waiting:
            self.received()
        end = self.waiting.index(marker) + len(marker)
        found = bytes(self.waiting[:end])
        del self.waiting[:end]
        return found

    def read_exactly(self, count: int) -> bytes:
        while len(self.waiting) < count:
            self.received()
        found = bytes(self.waiting[:count])
        del self.waiting[:count]
        return found

    def has_data(self, seconds: float) -> bool:
        if self.waiting:
            return True
        return bool(select.select([self.connection], [], [], max(seconds, 0))[0])

    def send_text(self, text: str) -> None:
        self.connection.sendall(frame(text.encode()))

    def frame_parts(self) -> tuple[bool, int, bytes]:
        first, second = self.read_exactly(2)
        size = second & LENGTH_BITS
        if size == SHORT_SIZE:
            size = struct.unpack(">H", self.read_exactly(2))[0]
        elif size == MEDIUM_SIZE:
            size = struct.unpack(">Q", self.read_exactly(8))[0]
        mask = self.read_exactly(MASK_BYTES) if second & MASK_BIT else b""
        payload = self.read_exactly(size)
        return bool(first & FINAL_BIT), first & OPCODE_BITS, masked(payload, mask) if mask else payload

    def message(self) -> str:
        parts = []
        while True:
            final, opcode, payload = self.frame_parts()
            if opcode == PING:
                self.connection.sendall(frame(payload, PONG))
                continue
            if opcode == PONG:
                continue
            if opcode == CLOSE:
                raise HeadlessError("the browser closed the connection")
            parts.append(payload)
            if final:
                return b"".join(parts).decode("utf-8", errors="replace")

    def close(self) -> None:
        self.connection.close()


class DevToolsPage:
    def __init__(self, websocket):
        self.websocket = websocket
        self.events = []
        self.last_id = 0

    def next_message(self, deadline: float) -> dict | None:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not self.websocket.has_data(remaining):
            return None
        try:
            message = json.loads(self.websocket.message())
        except ValueError as error:
            raise HeadlessError(f"the browser sent something unreadable: {error}") from None
        if "method" in message:
            self.events.append(message)
        return message

    def call(self, method: str, params: dict | None = None, seconds: float = CALL_SECONDS) -> dict:
        self.last_id += 1
        asked = self.last_id
        self.websocket.send_text(json.dumps({"id": asked, "method": method, "params": params or {}}))
        deadline = time.monotonic() + seconds
        while True:
            message = self.next_message(deadline)
            if message is None:
                raise HeadlessError(f"the browser didn't answer {method} within {seconds} s")
            if message.get("id") != asked:
                continue
            if "error" in message:
                raise HeadlessError(f"the browser refused {method}: {(message['error'] or {}).get('message')}")
            return message.get("result") or {}

    def wait_for(self, method: str, since: int, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while not any(event.get("method") == method for event in self.events[since:]):
            if self.next_message(deadline) is None:
                return False
        return True

    def settle(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while self.next_message(deadline) is not None:
            pass

    def evaluated(self, script: str) -> str:
        result = self.call("Runtime.evaluate", {"expression": script, "returnByValue": True}).get("result") or {}
        return str(result.get("value") or "")


@dataclass
class Visit:
    url: str
    width: int
    height: int
    picture: bytes = b""
    status: int | None = None
    status_text: str = ""
    failure: str = ""
    loaded: bool = True
    title: str = ""
    text: str = ""
    errors: list[str] = field(default_factory=list)
    warnings: int = 0
    failed_requests: list[str] = field(default_factory=list)


def first_line(text: str) -> str:
    return next((line.strip() for line in str(text).splitlines() if line.strip()), "")


def console_text(params: dict) -> str:
    shown = []
    for argument in params.get("args") or []:
        value = argument.get("value", argument.get("description", argument.get("type", "")))
        shown.append(value if isinstance(value, str) else json.dumps(value))
    return " ".join(shown)


def exception_text(params: dict) -> str:
    details = params.get("exceptionDetails") or {}
    described = first_line((details.get("exception") or {}).get("description") or "")
    text = described or details.get("text") or "an error"
    text = text if text.startswith("Uncaught") else f"Uncaught {text}"
    place = details.get("url") or ""
    return f"{text} ({place}:{details.get('lineNumber', 0) + 1})" if place else text


def noted_response(visit: Visit, params: dict, frame_id: str) -> None:
    response = params.get("response") or {}
    status = response.get("status")
    if params.get("type") == DOCUMENT and params.get("frameId") == frame_id:
        visit.status, visit.status_text = status, str(response.get("statusText") or "")
    elif isinstance(status, int) and status >= FIRST_BAD_STATUS:
        visit.failed_requests.append(f"{status} {response.get('url', '')}")


def noted_failure(visit: Visit, params: dict, requested: dict) -> None:
    if params.get("canceled"):
        return
    visit.failed_requests.append(f"{params.get('errorText', 'failed')} {requested.get(params.get('requestId'), '')}"
                                 .strip())


def noted_console(visit: Visit, params: dict) -> None:
    kind = params.get("type")
    if kind in ERROR_CONSOLE_TYPES:
        visit.errors.append(f"console.{kind}: {console_text(params)}")
    elif kind == WARNING_CONSOLE_TYPE:
        visit.warnings += 1


def noted_log(visit: Visit, params: dict) -> None:
    entry = params.get("entry") or {}
    if entry.get("level") == ERROR_LEVEL and entry.get("source") != NETWORK_SOURCE:
        visit.errors.append(str(entry.get("text") or ""))


def note_events(visit: Visit, events: list[dict], frame_id: str) -> None:
    requested = {}
    for event in events:
        method, params = event.get("method"), event.get("params") or {}
        if method == "Network.requestWillBeSent":
            requested[params.get("requestId")] = (params.get("request") or {}).get("url", "")
        elif method == "Network.responseReceived":
            noted_response(visit, params, frame_id)
        elif method == "Network.loadingFailed":
            noted_failure(visit, params, requested)
        elif method == "Runtime.consoleAPICalled":
            noted_console(visit, params)
        elif method == "Runtime.exceptionThrown":
            visit.errors.append(exception_text(params))
        elif method == "Log.entryAdded":
            noted_log(visit, params)


def visit(page: DevToolsPage, url: str, width: int, height: int, mobile: bool = False,
          settle_seconds: float = SETTLE_SECONDS) -> Visit:
    page.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": height, "deviceScaleFactor": 1,
                                                      "mobile": mobile})
    since = len(page.events)
    navigation = page.call("Page.navigate", {"url": url}, NAVIGATE_SECONDS)
    found = Visit(url, width, height, failure=str(navigation.get("errorText") or ""))
    if not found.failure:
        found.loaded = page.wait_for("Page.loadEventFired", since, LOAD_SECONDS)
    page.settle(settle_seconds)
    note_events(found, page.events[since:], str(navigation.get("frameId") or ""))
    found.title = page.evaluated(TITLE_SCRIPT)
    found.text = page.evaluated(TEXT_SCRIPT)
    shot = page.call("Page.captureScreenshot", {"format": "png"})
    found.picture = base64.b64decode(shot.get("data") or "")
    return found


def removed(folder: Path) -> str:
    problem = None
    for _attempt in range(REMOVE_TRIES):
        try:
            shutil.rmtree(folder)
            return ""
        except FileNotFoundError:
            return ""
        except OSError as error:
            problem = error
            time.sleep(REMOVE_PAUSE_SECONDS)
    return f"couldn't remove the throwaway browser profile at {folder}: {problem}"


class Browser:
    def __init__(self, program: str):
        self.program = program
        self.home = None
        self.process = None
        self.websocket = None
        self.leftover = ""

    def __enter__(self) -> DevToolsPage:
        self.home = Path(tempfile.mkdtemp(prefix=PROFILE_PREFIX))
        try:
            return self.started()
        except BaseException:
            self.close()
            raise

    def __exit__(self, *details) -> None:
        self.close()

    def started(self) -> DevToolsPage:
        profile, log = self.home / PROFILE_FOLDER, self.home / LOG_FILE
        with open(log, "wb") as log_file:
            try:
                self.process = subprocess.Popen(launch_command(self.program, profile), stdin=subprocess.DEVNULL,
                                                stdout=subprocess.DEVNULL, stderr=log_file, **detached())
            except OSError as error:
                raise HeadlessError(f"couldn't start the browser {self.program}: {error}") from None
        address = page_socket_address(devtools_port(profile, self.process, log))
        try:
            self.websocket = WebSocket.opened(address, CONNECT_SECONDS)
        except OSError as error:
            raise HeadlessError(f"couldn't connect to the browser: {error}") from None
        page = DevToolsPage(self.websocket)
        for domain in DOMAINS:
            page.call(f"{domain}.enable")
        return page

    def close(self) -> None:
        if self.websocket is not None:
            self.websocket.close()
            self.websocket = None
        if self.process is not None:
            tools.stop(self.process)
            self.process.wait()
            self.process = None
        if self.home is not None:
            self.leftover = removed(self.home)
            self.home = None
