import hashlib
import hmac
import http.client
import json
import re
import secrets
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlparse

from arty_cli import __version__, retry

CONNECT_SECONDS = 3
ANSWER_SECONDS = 600
BUSY_CODES = (429, 503)
DEVICE_KEY_PREFIX = "arty_dev_"
LIST_SECONDS = 30
MODEL_PREFIX = "arty/"
SKIPPED_GROUPS = "browser,screen,windows,computer,documents"
DATA_PREFIX = "data: "
DONE = "data: [DONE]"
CUT_OFF = "the reply stopped before its end, so it was not used"
DEFAULT_PORTS = {"http": 80, "https": 443}
IDENTITY_PREFIX = "arty-identity:"
NONCE_BYTES = 16
MAX_IDENTITY_BYTES = 64_000
PLAIN_HTTP = "http"
SEARCH_TOOL = "research__web_search"
SEARCH_SECONDS = 60
NOT_FOUND = 404
REFUSED_KEY = 401
MISSING_MODEL_CODES = (400, 404)
MISSING_MODEL = re.compile(r"isn't downloaded|model\b.*\bnot found|no such model", re.I)
ERRNO_LEAD = re.compile(r"^\[Errno -?\d+\]\s*")


SHARED_TURN_KEYS = ("role", "content", "tool_calls", "tool_call_id")
FENCED_BLOCK = re.compile(r"```[a-zA-Z]*\s*\n?(.*?)```", re.S)
TAGGED_CALL = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.S)
LINE_START_JSON = re.compile(r"^[ \t]*([\[{])", re.M)
WRITTEN_CALL_ID = "written_call_{}"


class BrainError(Exception):
    pass


class SetupProblem(BrainError):
    pass


def missing_model_message(model: str) -> str:
    return (f"Arty's brain doesn't have the model {model}. /model lists the ones it has; arty-cli --model <name> "
            "picks one for a single run.")


def plain_reason(error: Exception) -> str:
    reason = getattr(error, "reason", None) or error
    return ERRNO_LEAD.sub("", str(getattr(reason, "strerror", None) or reason)) or type(reason).__name__


@dataclass(frozen=True)
class Route:
    url: str
    headers: dict
    must_prove: bool = False


def routes(settings) -> list[Route]:
    found = []
    if settings.base_url:
        home = settings.base_url.rstrip("/")
        found.append(Route(home, {}, must_prove=urlparse(home).scheme == PLAIN_HTTP))
    if settings.remote_url:
        access = {}
        if settings.access_client_id:
            access = {"CF-Access-Client-Id": settings.access_client_id,
                      "CF-Access-Client-Secret": settings.access_client_secret}
        found.append(Route(settings.remote_url.rstrip("/"), access))
    return found


def is_reachable(url: str) -> bool:
    parsed = urlparse(url)
    port = parsed.port or DEFAULT_PORTS.get(parsed.scheme, 80)
    try:
        with socket.create_connection((parsed.hostname, port), timeout=CONNECT_SECONDS):
            return True
    except OSError:
        return False


def identity_secret(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest() if key.startswith(DEVICE_KEY_PREFIX) else key


def expected_proof(key: str, nonce: str) -> str:
    return hmac.new(identity_secret(key).encode(), f"{IDENTITY_PREFIX}{nonce}".encode(), hashlib.sha256).hexdigest()


def proves_identity(url: str, key: str) -> bool:
    nonce = secrets.token_hex(NONCE_BYTES)
    request = urllib.request.Request(f"{url}/identity?nonce={nonce}",
                                     headers={"Accept": "application/json", "User-Agent": f"arty-cli/{__version__}"})
    try:
        with urllib.request.urlopen(request, timeout=CONNECT_SECONDS) as response:
            proofs = json.loads(response.read(MAX_IDENTITY_BYTES)).get("proofs")
    except (urllib.error.URLError, OSError, ValueError, AttributeError):
        return False
    if not isinstance(proofs, list):
        return False
    expected = expected_proof(key, nonce)
    return any(isinstance(proof, str) and proof.isascii() and hmac.compare_digest(proof, expected) for proof in proofs)


def is_arty_model(name: str) -> bool:
    return name.startswith(MODEL_PREFIX)


def shared_messages(messages: list[dict]) -> list[dict]:
    return [{key: value for key, value in message.items() if key in SHARED_TURN_KEYS} for message in messages]


def json_values(text: str) -> list | None:
    decoder, values, position, text = json.JSONDecoder(), [], 0, text.strip()
    while position < len(text):
        try:
            value, position = decoder.raw_decode(text, position)
        except ValueError:
            return None
        values.append(value)
        while position < len(text) and text[position].isspace():
            position += 1
    return values


def ends_its_line(content: str, position: int) -> bool:
    line_end = content.find("\n", position)
    return not content[position:line_end if line_end >= 0 else len(content)].strip()


def json_on_own_lines(content: str) -> list[str]:
    decoder, found, position = json.JSONDecoder(), [], 0
    for opening in LINE_START_JSON.finditer(content):
        start = opening.start(1)
        if start < position:
            continue
        try:
            _value, end = decoder.raw_decode(content, start)
        except ValueError:
            continue
        if ends_its_line(content, end):
            found.append(content[start:end])
            position = end
    return found


def written_blocks(content: str) -> list[str]:
    return FENCED_BLOCK.findall(content) + TAGGED_CALL.findall(content) or json_on_own_lines(content)


def written_call(entry, offered_names: set[str]) -> dict | None:
    if not isinstance(entry, dict) or entry.get("name") not in offered_names:
        return None
    arguments = entry.get("arguments", entry.get("parameters", {}))
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return None
    if not isinstance(arguments, dict):
        return None
    return {"name": entry["name"], "arguments": json.dumps(arguments)}


def calls_in_block(block: str, offered_names: set[str]) -> list[dict] | None:
    values = json_values(block)
    if not values:
        return None
    entries = [entry for value in values for entry in (value if isinstance(value, list) else [value])]
    found = [written_call(entry, offered_names) for entry in entries]
    return found if entries and all(found) else None


def written_tool_calls(content: str, offered_names: set[str]) -> list[dict]:
    found = []
    for block in written_blocks(content or ""):
        calls = calls_in_block(block, offered_names)
        if calls is None:
            return []
        found += calls
    return [{"id": WRITTEN_CALL_ID.format(number), "type": "function", "function": call}
            for number, call in enumerate(found, 1)]


def with_written_calls(reply: dict, tools: list[dict]) -> dict:
    if reply.get("tool_calls") or not tools:
        return reply
    offered_names = {tool.get("function", {}).get("name") for tool in tools}
    calls = written_tool_calls(reply.get("content") or "", offered_names)
    return {**reply, "content": "", "tool_calls": calls} if calls else reply


def read_reply(lines, on_text) -> dict:
    content, calls, finished = [], {}, False
    for line in lines:
        if not line.startswith(DATA_PREFIX):
            continue
        if line == DONE:
            finished = True
            break
        try:
            choice = (json.loads(line[len(DATA_PREFIX):]).get("choices") or [{}])[0]
        except ValueError as error:
            raise BrainError(f"Arty's brain sent something unreadable: {error}") from None
        finished = finished or bool(choice.get("finish_reason"))
        delta = choice.get("delta") or {}
        if delta.get("content"):
            content.append(delta["content"])
            on_text(delta["content"])
        for call in delta.get("tool_calls") or []:
            index = call.pop("index", len(calls))
            calls[index] = call
    if not finished:
        raise ConnectionError(CUT_OFF)
    return {"role": "assistant", "content": "".join(content), "tool_calls": [calls[i] for i in sorted(calls)]}


def error_detail(error: urllib.error.HTTPError) -> str:
    try:
        return json.loads(error.read() or b"{}").get("error", {}).get("message", "")
    except (ValueError, AttributeError, OSError):
        return ""


def error_message(error: urllib.error.HTTPError) -> str:
    try:
        found = json.loads(error.read() or b"{}").get("error", "")
    except (ValueError, AttributeError, OSError):
        return ""
    return found.get("message", "") if isinstance(found, dict) else str(found)


def busy_of(error):
    if isinstance(error, urllib.error.HTTPError) and error.code in BUSY_CODES:
        return retry.Busy(retry.retry_after_of(error.headers))
    return None


def decoded_lines(response):
    for raw in response:
        yield raw.decode("utf-8", errors="replace").rstrip("\r\n")


class Brain:
    name = "arty"

    def __init__(self, settings):
        self.settings = settings
        self.model = settings.model
        self.route = None

    def address(self) -> str:
        return self.route.url if self.route else ""

    def pick_route(self) -> Route:
        if self.route:
            return self.route
        candidates = routes(self.settings)
        if not candidates:
            raise BrainError("No brain address is set. Run `arty-cli login`.")
        unproven = []
        for candidate in candidates:
            if not is_reachable(candidate.url):
                continue
            if candidate.must_prove and not proves_identity(candidate.url, self.settings.api_key):
                unproven.append(candidate.url)
                continue
            self.route = candidate
            return candidate
        if unproven:
            raise BrainError(f"Something answered at {', '.join(unproven)} but couldn't prove it is your Arty brain, "
                             "so the API key wasn't sent there. Away from home, use the address from anywhere "
                             "(arty-cli setup); at home, make sure the brain is up to date.")
        tried = ", ".join(candidate.url for candidate in candidates)
        raise BrainError(f"Can't reach Arty's brain (tried {tried}). Is the server on, and is this computer online?")

    def request(self, route: Route, body: dict) -> urllib.request.Request:
        headers = {"Authorization": f"Bearer {self.settings.api_key}", "Content-Type": "application/json",
                   "Accept": "text/event-stream", "User-Agent": f"arty-cli/{__version__}",
                   "X-Arty-Client-Tools": "1", "X-Arty-Agent": "1", "X-Arty-Any-Model": "1", "X-Arty-Skip-Groups": SKIPPED_GROUPS,
                   **route.headers}
        return urllib.request.Request(f"{route.url}/chat/completions", data=json.dumps(body).encode(),
                                      headers=headers, method="POST")

    def available_models(self) -> list[tuple[str, str]]:
        route = self.pick_route()
        request = urllib.request.Request(f"{route.url}/models?all=1", headers={
            "Authorization": f"Bearer {self.settings.api_key}", "User-Agent": f"arty-cli/{__version__}", **route.headers})
        try:
            with urllib.request.urlopen(request, timeout=LIST_SECONDS) as response:
                listed = json.loads(response.read() or b"{}").get("data") or []
        except urllib.error.HTTPError as error:
            raise BrainError(f"Arty's brain couldn't list its models: {error_message(error) or error.code}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError) as error:
            raise BrainError(f"Couldn't get Arty's models from {route.url}: {error}") from None
        return [(entry["id"], str(entry.get("description") or "")) for entry in listed
                if isinstance(entry, dict) and is_arty_model(str(entry.get("id", "")))]

    def search_web(self, query: str, results: int) -> str:
        route = self.pick_route()
        body = json.dumps({"arguments": {"query": query, "results": results}}).encode()
        request = urllib.request.Request(f"{route.url}/tools/{SEARCH_TOOL}", data=body, method="POST", headers={
            "Authorization": f"Bearer {self.settings.api_key}", "Content-Type": "application/json",
            "Accept": "application/json", "User-Agent": f"arty-cli/{__version__}", **route.headers})
        try:
            with urllib.request.urlopen(request, timeout=SEARCH_SECONDS) as response:
                found = json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            if error.code == NOT_FOUND:
                raise BrainError(f"This Arty brain has no web search ({SEARCH_TOOL} isn't one of its tools).") from None
            raise BrainError(f"Arty's brain couldn't search: {error_message(error) or error.code}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError) as error:
            raise BrainError(f"Couldn't search through Arty's brain at {route.url}: {error}") from None
        result = found.get("result") if isinstance(found, dict) else None
        if not isinstance(result, str):
            raise BrainError("Arty's brain didn't send search results.")
        return result

    def streamed_reply(self, route: Route, body: dict, on_text) -> dict:
        with urllib.request.urlopen(self.request(route, body), timeout=ANSWER_SECONDS) as response:
            if "html" in (response.headers.get("Content-Type") or ""):
                raise BrainError("The remote address answered with a web page: Cloudflare Access turned the "
                                 "request away. Check access_client_id and access_client_secret.")
            return read_reply(decoded_lines(response), on_text)

    def reply(self, messages, tools, on_text) -> dict:
        route = self.pick_route()
        body = {"model": self.model, "messages": shared_messages(messages), "stream": True}
        if tools:
            body["tools"] = tools
        try:
            reply = retry.with_retries(lambda: self.streamed_reply(route, body, on_text), busy_of)
        except urllib.error.HTTPError as error:
            if error.code == REFUSED_KEY:
                raise SetupProblem("Arty's brain refused the API key. Run `arty-cli login` to log in again.") from None
            detail = error_detail(error)
            if error.code in MISSING_MODEL_CODES and MISSING_MODEL.search(detail):
                raise SetupProblem(missing_model_message(self.model)) from None
            if error.code in BUSY_CODES:
                raise BrainError(f"Arty's brain is busy right now ({detail or error.code}). "
                                 "Try again in a minute.") from None
            raise BrainError(f"Arty's brain hit an error: {detail or error.code}") from None
        except (OSError, http.client.HTTPException) as error:
            self.route = None
            raise BrainError(f"Lost the connection to Arty's brain at {route.url} ({plain_reason(error)}). "
                             "Is the server still on?") from None
        return with_written_calls(reply, tools)
