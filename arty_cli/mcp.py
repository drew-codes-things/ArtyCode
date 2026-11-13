import http.client
import json
import os
import queue
import re
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from arty_cli import __version__, sign_ins

PROTOCOL_VERSION = "2025-06-18"
SETTINGS_FILE = "settings.json"
PROJECT_FILE = Path(".arty") / "mcp.json"
SERVERS_KEY = "mcpServers"
PREFIX = "mcp__"
MAX_TOOL_NAME = 64
UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9_-]")
START_SECONDS = 30
CALL_SECONDS = 300
CLOSE_SECONDS = 3
STDERR_TAIL_CHARS = 2_000
MAX_PAGES = 50
METHOD_NOT_FOUND = -32601
ENDED = object()
EMPTY_SCHEMA = {"type": "object", "properties": {}}
WEB_SCHEMES = ("http", "https")
ENVIRONMENT_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
SESSION_HEADER = "Mcp-Session-Id"
VERSION_HEADER = "Mcp-Protocol-Version"
DATA_PREFIX = "data:"
EVENT_STREAM = "text/event-stream"
SIGN_IN_KEY = "signIn"
SIGN_IN_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
UNAUTHORISED = 401
CONNECTED, NEEDS_SIGN_IN, NOT_STARTED = "connected", "needs sign-in again", "didn't start"


class McpError(Exception):
    pass


class SignInNeeded(McpError):
    pass


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


SIGNED_IN_OPENER = urllib.request.build_opener(NoRedirects)


@dataclass(frozen=True)
class ServerConfig:
    name: str
    command: str
    args: list
    env: dict
    source: str
    url: str = ""
    headers: dict = field(default_factory=dict)
    sign_in: str = ""


@dataclass(frozen=True)
class Tool:
    server: str
    name: str
    exposed: str
    description: str
    schema: dict
    read_only: bool


def safe_part(text: str) -> str:
    return UNSAFE_NAME_CHARS.sub("_", text)


def exposed_name(server: str, tool: str) -> str:
    return f"{PREFIX}{safe_part(server)}__{safe_part(tool)}"[:MAX_TOOL_NAME]


def read_table(path: Path) -> tuple[dict, list[str]]:
    if not path.is_file():
        return {}, []
    try:
        found = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return {}, [f"Couldn't read {path}: {error}"]
    servers = found.get(SERVERS_KEY, {}) if isinstance(found, dict) else None
    if not isinstance(servers, dict):
        return {}, [f"'{SERVERS_KEY}' in {path} should be an object of servers"]
    return servers, []


def with_environment(text: str) -> tuple[str, list[str]]:
    missing = [name for name in ENVIRONMENT_REFERENCE.findall(text) if name not in os.environ]
    return ENVIRONMENT_REFERENCE.sub(lambda found: os.environ.get(found.group(1), ""), text), missing


def web_config_from(name: str, entry: dict, source: str, path: Path) -> tuple[ServerConfig | None, str]:
    url, headers, sign_in = entry.get("url"), entry.get("headers") or {}, entry.get(SIGN_IN_KEY, "")
    if not isinstance(url, str) or urlparse(url).scheme not in WEB_SCHEMES or not urlparse(url).netloc:
        return None, f"MCP server {name} in {path}: url should be an http:// or https:// address"
    if not isinstance(headers, dict):
        return None, f"MCP server {name} in {path}: headers should be an object"
    if sign_in and (not isinstance(sign_in, str) or not SIGN_IN_NAME.match(sign_in)):
        return None, f"MCP server {name} in {path}: {SIGN_IN_KEY} should be the name of a saved sign-in"
    filled = {}
    for header, value in headers.items():
        filled[str(header)], missing = with_environment(str(value))
        if missing:
            return None, (f"MCP server {name} in {path}: the header {header} uses "
                          f"{', '.join('${' + variable + '}' for variable in missing)}, which isn't set")
    return ServerConfig(name, "", [], {}, source, url=url, headers=filled, sign_in=sign_in), ""


def config_from(name: str, entry, source: str, path: Path) -> tuple[ServerConfig | None, str]:
    if not isinstance(entry, dict):
        return None, f"MCP server {name} in {path} should be an object"
    if "url" in entry and not entry.get("command"):
        return web_config_from(name, entry, source, path)
    command = entry.get("command")
    args, env = entry.get("args") or [], entry.get("env") or {}
    if not isinstance(command, str) or not command:
        return None, f"MCP server {name} in {path} needs a command"
    if not isinstance(args, list) or not isinstance(env, dict):
        return None, f"MCP server {name} in {path}: args should be a list and env an object"
    return ServerConfig(name, command, [str(arg) for arg in args], {str(k): str(v) for k, v in env.items()}, source), ""


def configs(personal_folder: Path, workspace, include_project: bool) -> tuple[list[ServerConfig], list[str]]:
    places = [(Path(personal_folder) / SETTINGS_FILE, "yours")]
    if include_project:
        places.append((workspace.root / PROJECT_FILE, "this project"))
    found, problems = {}, []
    for path, source in places:
        servers, trouble = read_table(path)
        problems += trouble
        for name, entry in servers.items():
            config, problem = config_from(str(name), entry, source, path)
            if problem:
                problems.append(problem)
            else:
                found[config.name] = config
    return list(found.values()), problems


def content_text(content: list) -> str:
    parts = []
    for item in content if isinstance(content, list) else []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text":
            parts.append(str(item.get("text", "")))
        elif kind == "resource" and isinstance(item.get("resource"), dict) and "text" in item["resource"]:
            parts.append(str(item["resource"]["text"]))
        else:
            parts.append(f"[{item.get('mimeType') or kind} content]")
    return "\n".join(parts)


def windows_extensions(name: str) -> list[str]:
    if sys.platform != "win32" or Path(name).suffix:
        return [""]
    return [""] + [ext.lower() for ext in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";") if ext]


def is_safe_search_folder(folder: str, workspace) -> bool:
    if not folder or not os.path.isabs(folder):
        return False
    resolved = Path(folder).resolve()
    return not resolved.is_relative_to(workspace.root) and resolved != Path.cwd().resolve()


def program_path(command: str, workspace) -> str | None:
    if os.sep in command or (os.altsep and os.altsep in command):
        found = workspace.resolve(command)
        return str(found) if found.is_file() and os.access(found, os.X_OK) else None
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        if not is_safe_search_folder(folder, workspace):
            continue
        for extension in windows_extensions(command):
            candidate = Path(folder) / f"{command}{extension}"
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    return None


def tools_listed(request, server_name: str, seconds: float) -> list[Tool]:
    found, cursor = [], None
    for _page in range(MAX_PAGES):
        page = request("tools/list", {"cursor": cursor} if cursor else {}, seconds)
        for tool in page.get("tools") or []:
            if isinstance(tool, dict) and tool.get("name"):
                annotations = tool.get("annotations") if isinstance(tool.get("annotations"), dict) else {}
                schema = tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else EMPTY_SCHEMA
                found.append(Tool(server_name, str(tool["name"]), exposed_name(server_name, str(tool["name"])),
                                  str(tool.get("description") or ""), schema, annotations.get("readOnlyHint") is True))
        cursor = page.get("nextCursor")
        if not cursor:
            break
    return found


def call_text(result: dict) -> str:
    text = content_text(result.get("content"))
    return f"Error: {text}" if result.get("isError") else text


def answer_to(server_message: dict) -> dict:
    if server_message.get("method") == "ping":
        return {"jsonrpc": "2.0", "id": server_message["id"], "result": {}}
    return {"jsonrpc": "2.0", "id": server_message["id"],
            "error": {"code": METHOD_NOT_FOUND, "message": "ArtyCode doesn't offer that"}}


def initialize_params() -> dict:
    return {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "arty-cli", "version": __version__}}


def streamed_messages(response):
    data = []
    for raw in response:
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if line.startswith(DATA_PREFIX):
            data.append(line[len(DATA_PREFIX):].lstrip())
        elif not line and data:
            yield "\n".join(data)
            data = []
    if data:
        yield "\n".join(data)


class WebServer:
    def __init__(self, config: ServerConfig, start_seconds=START_SECONDS, call_seconds=CALL_SECONDS,
                 credentials=None):
        self.config = config
        self.start_seconds = start_seconds
        self.call_seconds = call_seconds
        self.credentials = credentials
        self.lock = threading.Lock()
        self.session = ""
        self.next_id = 0
        self.needs_sign_in = False

    def start(self) -> list[Tool]:
        self.request("initialize", initialize_params(), self.start_seconds)
        self.post({"jsonrpc": "2.0", "method": "notifications/initialized"}, self.start_seconds).close()
        return tools_listed(self.request, self.config.name, self.start_seconds)

    def headers(self, renewing: bool = True) -> dict:
        found = {"Content-Type": "application/json", "Accept": f"application/json, {EVENT_STREAM}",
                 "User-Agent": f"arty-cli/{__version__}", **self.config.headers}
        if self.credentials is not None:
            found["Authorization"] = f"Bearer {self.bearer(renewing)}"
        if self.session:
            found.update({SESSION_HEADER: self.session, VERSION_HEADER: PROTOCOL_VERSION})
        return found

    def bearer(self, renewing: bool) -> str:
        try:
            return self.credentials.bearer() if renewing else self.credentials.current()
        except sign_ins.SignInError as error:
            raise self.sign_in_trouble(error) from None

    def sign_in_trouble(self, error: sign_ins.SignInError) -> McpError:
        if not error.needs_sign_in:
            return McpError(str(error))
        self.needs_sign_in = True
        return SignInNeeded(f"{error}. Sign in again with /mcp login {self.config.name} "
                            f"(or arty mcp login {self.config.name})")

    def take_notice(self) -> str:
        return self.credentials.take_notice() if self.credentials is not None else ""

    def opened(self, request, seconds: float):
        if self.credentials is None:
            return urllib.request.urlopen(request, timeout=seconds)
        return SIGNED_IN_OPENER.open(request, timeout=seconds)

    def post(self, message: dict, seconds: float, renewed: bool = False):
        headers = self.headers()
        request = urllib.request.Request(self.config.url, data=json.dumps(message).encode(), headers=headers,
                                         method="POST")
        try:
            response = self.opened(request, seconds)
        except urllib.error.HTTPError as error:
            if error.code == UNAUTHORISED and self.credentials is not None and not renewed:
                self.renew(headers["Authorization"].removeprefix("Bearer "))
                return self.post(message, seconds, renewed=True)
            raise McpError(f"the MCP server {self.config.name} answered {error.code} {error.reason}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, http.client.HTTPException) as error:
            raise McpError(f"couldn't reach the MCP server {self.config.name} at {self.config.url}: {error}") from None
        self.needs_sign_in = False
        return response

    def renew(self, rejected: str) -> None:
        try:
            self.credentials.renew(rejected)
        except sign_ins.SignInError as error:
            raise self.sign_in_trouble(error) from None

    def request(self, method: str, params: dict, seconds: float) -> dict:
        with self.lock:
            self.next_id += 1
            wanted = self.next_id
            with self.post({"jsonrpc": "2.0", "id": wanted, "method": method, "params": params}, seconds) as response:
                self.session = response.headers.get(SESSION_HEADER) or self.session
                return self.answer_in(response, wanted, method, seconds)

    def answer_in(self, response, wanted: int, method: str, seconds: float) -> dict:
        streamed = EVENT_STREAM in (response.headers.get("Content-Type") or "")
        try:
            texts = streamed_messages(response) if streamed else [response.read().decode("utf-8", errors="replace")]
            for text in texts:
                message = json.loads(text) if text.strip() else None
                if not isinstance(message, dict):
                    continue
                if "method" in message and "id" in message:
                    self.post(answer_to(message), seconds).close()
                    continue
                if message.get("id") == wanted:
                    return self.result_of(message, method)
        except (OSError, ValueError, http.client.HTTPException) as error:
            raise McpError(f"the MCP server {self.config.name} sent an unreadable answer to {method}: {error}") from None
        raise McpError(f"the MCP server {self.config.name} didn't answer {method}")

    def result_of(self, message: dict, method: str) -> dict:
        if "error" in message:
            error = message["error"] if isinstance(message["error"], dict) else {}
            raise McpError(f"{self.config.name} refused {method}: {error.get('message', 'no details')}")
        result = message.get("result")
        return result if isinstance(result, dict) else {}

    def call(self, tool: str, arguments: dict) -> str:
        return call_text(self.request("tools/call", {"name": tool, "arguments": arguments}, self.call_seconds))

    def close(self) -> str:
        if not self.session:
            return ""
        try:
            request = urllib.request.Request(self.config.url, headers=self.headers(renewing=False), method="DELETE")
            self.session = ""
            self.opened(request, CLOSE_SECONDS).close()
        except (McpError, urllib.error.URLError, TimeoutError, ConnectionError, OSError,
                http.client.HTTPException) as error:
            self.session = ""
            return f"Couldn't end the session with the MCP server {self.config.name}: {error}"
        return ""


class Server:
    def __init__(self, config: ServerConfig, workspace, start_seconds=START_SECONDS, call_seconds=CALL_SECONDS):
        self.config = config
        self.workspace = workspace
        self.start_seconds = start_seconds
        self.call_seconds = call_seconds
        self.lock = threading.Lock()
        self.answers = queue.Queue()
        self.stderr_tail = ""
        self.next_id = 0
        self.process = None
        self.ended = False

    def start(self) -> list[Tool]:
        program = program_path(self.config.command, self.workspace)
        if program is None:
            raise McpError(f"MCP server {self.config.name}: can't find the program {self.config.command}")
        self.process = subprocess.Popen([program, *self.config.args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, cwd=self.workspace.root, text=True, encoding="utf-8",
                                        errors="replace", env={**os.environ, **self.config.env})
        threading.Thread(target=self.read_answers, daemon=True).start()
        threading.Thread(target=self.read_errors, daemon=True).start()
        self.request("initialize", initialize_params(), self.start_seconds)
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return tools_listed(self.request, self.config.name, self.start_seconds)

    def read_answers(self) -> None:
        for line in self.process.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if isinstance(message, dict):
                self.answers.put(message)
        self.answers.put(ENDED)

    def read_errors(self) -> None:
        for line in self.process.stderr:
            self.stderr_tail = (self.stderr_tail + line)[-STDERR_TAIL_CHARS:]

    def send(self, message: dict) -> None:
        try:
            self.process.stdin.write(json.dumps(message) + "\n")
            self.process.stdin.flush()
        except (OSError, ValueError):
            self.ended = True
            raise McpError(self.stopped_message()) from None

    def stopped_message(self) -> str:
        code = self.process.poll() if self.process else None
        detail = f" (exit code {code})" if code is not None else ""
        tail = f": {self.stderr_tail.strip().splitlines()[-1]}" if self.stderr_tail.strip() else ""
        return f"the MCP server {self.config.name} has stopped{detail}{tail}"

    def answer_server(self, message: dict) -> None:
        self.send(answer_to(message))

    def request(self, method: str, params: dict, seconds: float) -> dict:
        if self.ended:
            raise McpError(self.stopped_message())
        with self.lock:
            self.next_id += 1
            wanted = self.next_id
            self.send({"jsonrpc": "2.0", "id": wanted, "method": method, "params": params})
            return self.awaited(wanted, method, seconds)

    def awaited(self, wanted: int, method: str, seconds: float) -> dict:
        while True:
            try:
                message = self.answers.get(timeout=seconds)
            except queue.Empty:
                raise McpError(f"the MCP server {self.config.name} didn't answer {method} within {seconds:g} s") from None
            if message is ENDED:
                self.ended = True
                raise McpError(self.stopped_message())
            if "method" in message and "id" in message:
                self.answer_server(message)
                continue
            if message.get("id") != wanted:
                continue
            if "error" in message:
                error = message["error"] if isinstance(message["error"], dict) else {}
                raise McpError(f"{self.config.name} refused {method}: {error.get('message', 'no details')}")
            result = message.get("result")
            return result if isinstance(result, dict) else {}

    def call(self, tool: str, arguments: dict) -> str:
        return call_text(self.request("tools/call", {"name": tool, "arguments": arguments}, self.call_seconds))

    def take_notice(self) -> str:
        return ""

    def close(self) -> str:
        if self.process is None or self.process.poll() is not None:
            return ""
        try:
            self.process.stdin.close()
            self.process.wait(timeout=CLOSE_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            self.process.kill()
            self.process.wait()
        return ""


@dataclass(frozen=True)
class Trouble:
    config: ServerConfig
    problem: str
    needs_sign_in: bool


@dataclass(frozen=True)
class State:
    name: str
    source: str
    status: str
    tools: list
    problem: str = ""


class Hub:
    def __init__(self, workspace, start_seconds=START_SECONDS, call_seconds=CALL_SECONDS, sign_ins_store=None):
        self.workspace = workspace
        self.start_seconds = start_seconds
        self.call_seconds = call_seconds
        self.sign_ins = sign_ins_store
        self.servers = {}
        self.tools = {}
        self.trouble = {}

    def made(self, config: ServerConfig):
        if not config.url:
            return Server(config, self.workspace, self.start_seconds, self.call_seconds)
        if not config.sign_in:
            return WebServer(config, self.start_seconds, self.call_seconds)
        if self.sign_ins is None:
            raise sign_ins.SignInError(f"there is no saved sign-in called {config.sign_in}")
        credentials = self.sign_ins.credentials(config.sign_in, config.url)
        return WebServer(config, self.start_seconds, self.call_seconds, credentials)

    def start(self, wanted: list[ServerConfig]) -> list[str]:
        problems = []
        for config in wanted:
            self.trouble.pop(config.name, None)
            try:
                server = self.made(config)
            except sign_ins.SignInError as error:
                problems.append(self.troubled(config, f"MCP server {config.name} wasn't started: {error}",
                                              error.needs_sign_in))
                continue
            try:
                listed = server.start()
            except (McpError, OSError, http.client.HTTPException) as error:
                server.close()
                problems.append(self.troubled(config, f"MCP server {config.name} didn't start: {error}",
                                              isinstance(error, SignInNeeded)))
                continue
            problems += [notice for notice in (server.take_notice(),) if notice]
            self.servers[config.name] = server
            for tool in listed:
                if tool.exposed in self.tools:
                    problems.append(f"Two MCP tools are both called {tool.exposed}; kept the first")
                    continue
                self.tools[tool.exposed] = tool
        return problems

    def troubled(self, config: ServerConfig, problem: str, needs_sign_in: bool) -> str:
        self.trouble[config.name] = Trouble(config, problem, needs_sign_in)
        if needs_sign_in and f"/mcp login {config.name}" not in problem:
            problem += f". Sign in again with /mcp login {config.name}"
        return problem

    def drop(self, name: str) -> list[str]:
        server = self.servers.pop(name, None)
        self.trouble.pop(name, None)
        self.tools = {exposed: tool for exposed, tool in self.tools.items() if tool.server != name}
        problem = server.close() if server is not None else ""
        return [problem] if problem else []

    def restart(self, config: ServerConfig) -> list[str]:
        return self.drop(config.name) + self.start([config])

    def states(self) -> list[State]:
        found = []
        for name, server in self.servers.items():
            listed = [tool.exposed for tool in self.tools.values() if tool.server == name]
            status = NEEDS_SIGN_IN if getattr(server, "needs_sign_in", False) else CONNECTED
            found.append(State(name, server.config.source, status, listed))
        for name, trouble in self.trouble.items():
            if name not in self.servers:
                found.append(State(name, trouble.config.source, NEEDS_SIGN_IN if trouble.needs_sign_in
                                   else NOT_STARTED, [], trouble.problem))
        return found

    def config_of(self, name: str) -> ServerConfig | None:
        server = self.servers.get(name)
        if server is not None:
            return server.config
        trouble = self.trouble.get(name)
        return trouble.config if trouble else None

    def definitions(self) -> list[dict]:
        return [{"type": "function", "function": {
            "name": tool.exposed, "description": tool.description or f"{tool.name} from the {tool.server} MCP server",
            "parameters": {**EMPTY_SCHEMA, **tool.schema}}} for tool in self.tools.values()]

    def tool(self, name: str) -> Tool | None:
        return self.tools.get(name)

    def run(self, name: str, arguments: dict) -> str:
        tool = self.tools.get(name)
        if tool is None:
            return f"Error: there is no MCP tool called {name}"
        server = self.servers[tool.server]
        try:
            answer = server.call(tool.name, arguments)
        except (McpError, OSError, http.client.HTTPException) as error:
            answer = f"Error: {error}"
        notice = server.take_notice()
        return f"{answer}\n[{notice}]" if notice else answer

    def close(self) -> list[str]:
        return [problem for problem in (server.close() for server in self.servers.values()) if problem]
