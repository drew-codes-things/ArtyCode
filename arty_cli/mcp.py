import json
import os
import queue
import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from arty_cli import __version__

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


class McpError(Exception):
    pass


@dataclass(frozen=True)
class ServerConfig:
    name: str
    command: str
    args: list
    env: dict
    source: str


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


def config_from(name: str, entry, source: str, path: Path) -> tuple[ServerConfig | None, str]:
    if not isinstance(entry, dict):
        return None, f"MCP server {name} in {path} should be an object"
    if entry.get("url") and not entry.get("command"):
        return None, f"MCP server {name} in {path} uses a URL; only local (stdio) servers with a command are supported"
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
        self.request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                    "clientInfo": {"name": "arty-cli", "version": __version__}}, self.start_seconds)
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return self.listed_tools()

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
        if message.get("method") == "ping":
            self.send({"jsonrpc": "2.0", "id": message["id"], "result": {}})
        else:
            self.send({"jsonrpc": "2.0", "id": message["id"],
                       "error": {"code": METHOD_NOT_FOUND, "message": "arty-cli doesn't offer that"}})

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

    def listed_tools(self) -> list[Tool]:
        found, cursor = [], None
        for _page in range(MAX_PAGES):
            page = self.request("tools/list", {"cursor": cursor} if cursor else {}, self.start_seconds)
            for tool in page.get("tools") or []:
                if isinstance(tool, dict) and tool.get("name"):
                    annotations = tool.get("annotations") if isinstance(tool.get("annotations"), dict) else {}
                    schema = tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else EMPTY_SCHEMA
                    found.append(Tool(self.config.name, str(tool["name"]), exposed_name(self.config.name, str(tool["name"])),
                                      str(tool.get("description") or ""), schema, annotations.get("readOnlyHint") is True))
            cursor = page.get("nextCursor")
            if not cursor:
                break
        return found

    def call(self, tool: str, arguments: dict) -> str:
        result = self.request("tools/call", {"name": tool, "arguments": arguments}, self.call_seconds)
        text = content_text(result.get("content"))
        return f"Error: {text}" if result.get("isError") else text

    def close(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        try:
            self.process.stdin.close()
            self.process.wait(timeout=CLOSE_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            self.process.kill()
            self.process.wait()


class Hub:
    def __init__(self, workspace, start_seconds=START_SECONDS, call_seconds=CALL_SECONDS):
        self.workspace = workspace
        self.start_seconds = start_seconds
        self.call_seconds = call_seconds
        self.servers = {}
        self.tools = {}

    def start(self, wanted: list[ServerConfig]) -> list[str]:
        problems = []
        for config in wanted:
            server = Server(config, self.workspace, self.start_seconds, self.call_seconds)
            try:
                listed = server.start()
            except (McpError, OSError) as error:
                server.close()
                problems.append(f"MCP server {config.name} didn't start: {error}")
                continue
            self.servers[config.name] = server
            for tool in listed:
                if tool.exposed in self.tools:
                    problems.append(f"Two MCP tools are both called {tool.exposed}; kept the first")
                    continue
                self.tools[tool.exposed] = tool
        return problems

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
        try:
            return self.servers[tool.server].call(tool.name, arguments)
        except McpError as error:
            return f"Error: {error}"

    def close(self) -> None:
        for server in self.servers.values():
            server.close()
