import json
import posixpath
from dataclasses import dataclass

from arty_cli import context, tools

STUB_LEAD = "[Dropped to save room"
TARGET_SHARE = 0.6
KEPT_ARGUMENT_CHARS = 200
READ_TOOL, COMMAND_TOOL = "read_file", "run_command"
CHANGING_TOOLS = ("write_file", "edit_file", "multi_edit", "notebook_edit")
KEPT_WHOLE = ("path", "command")
TARGET_KEYS = ("path", "command", "pattern", "url", "query", "task", "job", "name")


@dataclass(frozen=True)
class Step:
    index: int
    name: str
    arguments: dict

    def path(self) -> str:
        given = str(self.arguments.get("path") or "")
        return posixpath.normpath(given.replace("\\", "/")) if given else ""

    def command(self) -> str:
        return str(self.arguments.get("command") or "").strip()


def arguments_of(call: dict) -> dict:
    raw = (call.get("function") or {}).get("arguments")
    if isinstance(raw, dict):
        return raw
    try:
        found = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def steps_of(messages: list[dict]) -> list[Step]:
    found, waiting = [], []
    for index, message in enumerate(messages):
        if message.get("tool_calls"):
            waiting = list(message["tool_calls"])
        elif message.get("role") == "tool" and waiting:
            call = waiting.pop(0)
            found.append(Step(index, str((call.get("function") or {}).get("name") or ""), arguments_of(call)))
    return found


def newest_round_start(messages: list[dict]) -> int:
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("tool_calls"):
            return index
    return len(messages)


def is_stub(content) -> bool:
    return str(content or "").startswith(STUB_LEAD)


def stub_for(name: str, arguments: dict) -> str:
    target = next((str(arguments[key]) for key in TARGET_KEYS if arguments.get(key)), "")
    step = f"{name} {target}".strip()[:KEPT_ARGUMENT_CHARS]
    return f"{STUB_LEAD}: the result of {step}. Do it again if you still need it.]"


def stubbed(messages: list[dict], step: Step) -> None:
    messages[step.index] = {**messages[step.index], "content": stub_for(step.name, step.arguments)}


def is_stale(step: Step, later: list[Step]) -> bool:
    if step.name == READ_TOOL and step.path():
        return any(other.path() == step.path() and other.name in (READ_TOOL, *CHANGING_TOOLS) for other in later)
    if step.name == COMMAND_TOOL and step.command():
        return any(other.name == COMMAND_TOOL and other.command() == step.command() for other in later)
    return False


def shortened(value, key: str = ""):
    if isinstance(value, str):
        if key in KEPT_WHOLE or len(value) <= KEPT_ARGUMENT_CHARS:
            return value
        return f"{value[:KEPT_ARGUMENT_CHARS]}... ({len(value) - KEPT_ARGUMENT_CHARS} characters dropped to save room)"
    if isinstance(value, dict):
        return {inner_key: shortened(inner, inner_key) for inner_key, inner in value.items()}
    if isinstance(value, list):
        return [shortened(inner) for inner in value]
    return value


def with_short_arguments(call: dict) -> dict:
    function = call.get("function") or {}
    if function.get("name") not in CHANGING_TOOLS:
        return call
    return {**call, "function": {**function, "arguments": json.dumps(shortened(arguments_of(call)))}}


def shorten_past_changes(messages: list[dict], protected_from: int) -> None:
    for index in range(protected_from):
        calls = messages[index].get("tool_calls")
        if calls:
            messages[index] = {**messages[index], "tool_calls": [with_short_arguments(call) for call in calls]}


def stub_stale(messages: list[dict]) -> None:
    protected_from = newest_round_start(messages)
    steps = steps_of(messages)
    for position, step in enumerate(steps):
        if step.index >= protected_from or is_stub(messages[step.index].get("content")):
            continue
        if is_stale(step, steps[position + 1:]):
            stubbed(messages, step)
    shorten_past_changes(messages, protected_from)


def estimated(messages: list[dict], client) -> int:
    return context.estimated_tokens(messages, client)


def prune(messages: list[dict], limit: int, client) -> None:
    if estimated(messages, client) <= limit:
        return
    stub_stale(messages)
    target = limit * TARGET_SHARE
    protected_from = newest_round_start(messages)
    for step in steps_of(messages):
        if estimated(messages, client) <= target or step.index >= protected_from:
            return
        if not is_stub(messages[step.index].get("content")):
            stubbed(messages, step)


def cut_newest(messages: list[dict], limit: int, client) -> None:
    start = len(messages)
    while start > 0 and messages[start - 1].get("role") == "tool":
        start -= 1
    if start == len(messages) or estimated(messages, client) <= limit:
        return
    share = context.room_chars(limit, estimated(messages[:start], client)) // (len(messages) - start)
    for index in range(start, len(messages)):
        content = str(messages[index].get("content") or "")
        if len(content) > share:
            messages[index] = {**messages[index], "content": tools.clipped(content, share)}


def paths_out_of_view(messages: list[dict]) -> set[str]:
    steps = steps_of(messages)
    hidden = set()
    for position, step in enumerate(steps):
        if step.name != READ_TOOL or not step.path():
            continue
        later = steps[position + 1:]
        same_file = [step] + [other for other in later if other.path() == step.path()]
        reads = [other for other in same_file if other.name == READ_TOOL]
        changed_after = any(other.name in CHANGING_TOOLS for other in same_file)
        if not changed_after and all(is_stub(messages[read.index].get("content")) for read in reads):
            hidden.add(step.path())
    return hidden
