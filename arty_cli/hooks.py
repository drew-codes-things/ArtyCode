import json
import re
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from arty_cli import tools

SETTINGS_FILE = "settings.json"
PROJECT_FOLDER = Path(".arty")
EVENTS = ("PreToolUse", "PostToolUse", "Stop", "UserPromptSubmit")
TOOL_EVENTS = ("PreToolUse", "PostToolUse")
BLOCKING_EXIT = 2
DEFAULT_SECONDS = 60
MATCH_EVERYTHING = ("", "*")
COMMAND_TYPE = "command"
ALLOW, DENY, ASK = "allow", "deny", "ask"
DECISION_STRENGTH = {"": 0, ALLOW: 1, ASK: 2, DENY: 3}
LEGACY_DECISIONS = {"approve": ALLOW, "block": DENY}


@dataclass(frozen=True)
class Hook:
    event: str
    matcher: str
    command: str
    seconds: int
    source: str

    def matches(self, tool_name: str) -> bool:
        if self.event not in TOOL_EVENTS or self.matcher in MATCH_EVERYTHING:
            return True
        try:
            return re.fullmatch(self.matcher, tool_name) is not None
        except re.error:
            return self.matcher == tool_name


@dataclass
class Outcome:
    blocked: bool = False
    feedback: str = ""
    context: str = ""
    decision: str = ""
    problems: list[str] = field(default_factory=list)

    def add_feedback(self, text: str) -> None:
        self.feedback = "\n".join(part for part in (self.feedback, str(text or "").strip()) if part)

    def add_context(self, text: str) -> None:
        self.context = "\n".join(part for part in (self.context, str(text or "").strip()) if part)

    def decide(self, decision: str) -> None:
        if DECISION_STRENGTH.get(decision, 0) > DECISION_STRENGTH[self.decision]:
            self.decision = decision
        self.blocked = self.blocked or decision == DENY


def json_output(stdout: str) -> dict | None:
    text = stdout.strip()
    if not text.startswith("{"):
        return None
    try:
        found = json.loads(text)
    except ValueError:
        return None
    return found if isinstance(found, dict) else None


def apply_json(found: dict, event: str, outcome: Outcome) -> None:
    if found.get("continue") is False:
        outcome.blocked = True
        outcome.add_feedback(found.get("stopReason") or found.get("reason") or "")
    specific = found.get("hookSpecificOutput") if isinstance(found.get("hookSpecificOutput"), dict) else {}
    decision = specific.get("permissionDecision") if event == "PreToolUse" else None
    if decision in DECISION_STRENGTH and decision:
        outcome.decide(decision)
        outcome.add_feedback(specific.get("permissionDecisionReason") or "")
    elif found.get("decision") == "block" or (event == "PreToolUse" and found.get("decision") in LEGACY_DECISIONS):
        outcome.decide(LEGACY_DECISIONS.get(found["decision"], DENY) if event == "PreToolUse" else DENY)
        outcome.blocked = outcome.blocked or found["decision"] == "block"
        outcome.add_feedback(found.get("reason") or "")
    outcome.add_context(specific.get("additionalContext") or "")


def read_settings(path: Path) -> tuple[dict, list[str]]:
    if not path.is_file():
        return {}, []
    try:
        found = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return {}, [f"Couldn't read {path}: {error}"]
    if not isinstance(found, dict):
        return {}, [f"{path} should hold a JSON object"]
    return found, []


def hooks_from(table, source: str, path: Path) -> tuple[list[Hook], list[str]]:
    if not isinstance(table, dict):
        return [], [f"'hooks' in {path} should be an object of events"]
    found, problems = [], []
    for event, groups in table.items():
        if event not in EVENTS:
            problems.append(f"Unknown hook event {event!r} in {path}; use one of {', '.join(EVENTS)}")
            continue
        for group in groups if isinstance(groups, list) else []:
            matcher = str(group.get("matcher") or "") if isinstance(group, dict) else ""
            for hook in group.get("hooks") or [] if isinstance(group, dict) else []:
                if not isinstance(hook, dict) or hook.get("type", COMMAND_TYPE) != COMMAND_TYPE or not hook.get("command"):
                    problems.append(f"A {event} hook in {path} needs \"type\": \"command\" and a command")
                    continue
                seconds = hook.get("timeout", DEFAULT_SECONDS)
                seconds = seconds if isinstance(seconds, int) and seconds > 0 else DEFAULT_SECONDS
                found.append(Hook(event, matcher, str(hook["command"]), seconds, source))
    return found, problems


class Hooks:
    def __init__(self, listed: list[Hook], workspace):
        self.hooks = listed
        self.workspace = workspace
        self.session_id = uuid.uuid4().hex

    def listed(self) -> list[Hook]:
        return list(self.hooks)

    def run(self, event: str, payload: dict, tool_name: str = "") -> Outcome:
        outcome = Outcome()
        for hook in self.hooks:
            if hook.event == event and hook.matches(tool_name):
                self.run_one(hook, {"session_id": self.session_id, "hook_event_name": event,
                                    "cwd": str(self.workspace.root), **payload}, outcome)
        return outcome

    def run_one(self, hook: Hook, payload: dict, outcome: Outcome) -> None:
        try:
            finished = finished_hook(hook, payload, self.workspace.root)
        except subprocess.TimeoutExpired:
            outcome.problems.append(f"The {hook.event} hook `{hook.command}` took longer than {hook.seconds} s")
            return
        except OSError as error:
            outcome.problems.append(f"The {hook.event} hook `{hook.command}` couldn't start: {error}")
            return
        if finished.returncode == BLOCKING_EXIT:
            outcome.blocked = True
            outcome.add_feedback(finished.stderr)
        elif finished.returncode == 0:
            found = json_output(finished.stdout)
            if found is not None:
                apply_json(found, hook.event, outcome)
            elif hook.event not in TOOL_EVENTS:
                outcome.add_context(finished.stdout)
        else:
            detail = finished.stderr.strip() or f"exit code {finished.returncode}"
            outcome.problems.append(f"The {hook.event} hook `{hook.command}` failed: {detail}")


def finished_hook(hook: Hook, payload: dict, folder: Path) -> subprocess.CompletedProcess:
    arguments, options = tools.shell_command(hook.command)
    process = subprocess.Popen(arguments, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding="utf-8", errors="replace", cwd=folder, **options)
    try:
        output, errors = process.communicate(json.dumps(payload), timeout=hook.seconds)
    except subprocess.TimeoutExpired:
        tools.stop(process)
        process.wait()
        raise
    except BaseException:
        tools.stop(process)
        process.wait()
        raise
    return subprocess.CompletedProcess(arguments, process.returncode, output, errors)


def load(personal_folder: Path, workspace, include_project: bool) -> tuple[Hooks, list[str]]:
    places = [(Path(personal_folder) / SETTINGS_FILE, "yours")]
    if include_project:
        places.append((workspace.root / PROJECT_FOLDER / SETTINGS_FILE, "this project"))
    found, problems = [], []
    for path, source in places:
        settings, trouble = read_settings(path)
        problems += trouble
        if "hooks" in settings:
            listed, trouble = hooks_from(settings["hooks"], source, path)
            found += listed
            problems += trouble
    return Hooks(found, workspace), problems


def no_hooks(workspace) -> Hooks:
    return Hooks([], workspace)
