import datetime
import os
import re
from dataclasses import dataclass
from pathlib import Path

from arty_cli import switches, tools

SWITCH = "builtInRules"
ON, TELL_ONLY, OFF = "on", "tell-only", "off"
LEVELS = (ON, TELL_ONLY, OFF)
STRENGTH = {OFF: 0, TELL_ONLY: 1, ON: 2}
MEANINGS = {ON: "told to the model and checked", TELL_ONLY: "told to the model, not checked", OFF: "off"}
RECORDS_FOLDER = Path(".arty")
SCRATCHPAD = RECORDS_FOLDER / "scratchpad.md"
HISTORY = RECORDS_FOLDER / "history.md"
RECORD_FILES = (SCRATCHPAD, HISTORY)
UNVERIFIED = "UNVERIFIED"
EM_DASH = "\u2014"
HEADING = "Working rules (built into ArtyCode; where the project's notes below say otherwise, the notes win):"
RULES = (
    "Before changing code, read it and its callers, and match the project's existing patterns.",
    "Make the smallest change that does the job: nothing the request didn't ask for, no tidying nearby code.",
    "Never make anything up (functions, options, files, results): if you didn't read it or run it, you don't know it.",
    "Code you write has no comments and no docstrings; use clear names, small functions and named constants "
    "instead. Only text a tool reads may stay: shebangs, encoding lines, lint or type suppressions, license headers.",
    "Never write the em dash character anywhere; use a comma, colon or brackets.",
    "Ask before anything destructive (deleting files or data, force-pushing, rewriting git history, deleting "
    "branches). Otherwise pick what the project already does, note it as an assumption and carry on; ask only when "
    "you must, all questions at once.",
    "Nothing is done until it has run: after your last change run the tests, a build, a lint or the program and "
    f"read the result. If you can't, write {UNVERIFIED} in the report with what would check it.",
    f"End a task that changes files with a short report: Outcome (DONE, PARTIAL, BLOCKED or {UNVERIFIED}), Changed, "
    "Verification (each command and its result, failures first), Assumptions (with the reasons for your choices), "
    f"Open items. ArtyCode adds it to {HISTORY.as_posix()} itself, so don't write that file or a scratchpad.",
)
CHECKED_NOTE = ("ArtyCode checks comments and em dashes after each edit, and sends you back once if you finish "
                "without checking your work.")
STATUSES = ("DONE", "PARTIAL", "BLOCKED", UNVERIFIED)
REPORT_HEADING = re.compile(r"^[\s#*_>-]*(outcome|changed|verification|assumptions|open items|not verified)[*_\s]*:"
                            r"[*_\s]*(.*)$", re.I)
MAX_FIELD_CHARS = 600
MAX_TASK_CHARS = 100
NOTHING_RUN = "nothing was run"
UNCHECKED = "no command checked the last change"
APPEND_ONLY = (f"Error: {HISTORY.as_posix()} is append-only: keep every earlier entry exactly as it is and add the "
               "new entry at the end (edit_file with its last lines as old_text, and those lines plus the entry as "
               "new_text).")


@dataclass
class Rules:
    level: str = ON
    turn: object = None

    def is_told(self) -> bool:
        return self.level != OFF

    def is_checked(self) -> bool:
        return self.level == ON


def level_of(value) -> str:
    if value is False or value == OFF:
        return OFF
    return TELL_ONLY if value == TELL_ONLY else ON


def chosen(personal_folder: Path, project_root: Path, include_project: bool, asked: str | None) -> str:
    if asked:
        return asked
    places = [Path(personal_folder) / switches.SETTINGS_FILE]
    if include_project:
        places.append(Path(project_root) / switches.PROJECT_SETTINGS)
    return min((level_of(switches.value_in(path, SWITCH)) for path in places), key=STRENGTH.get)


def rules_of(workspace) -> Rules | None:
    return getattr(workspace, "rules", None)


def told(workspace) -> bool:
    found = rules_of(workspace)
    return found is not None and found.is_told()


def checked(workspace) -> bool:
    found = rules_of(workspace)
    return found is not None and found.is_checked()


def today_text() -> str:
    return datetime.date.today().isoformat()


def instructions(workspace, today: str = "") -> str:
    if not told(workspace):
        return ""
    listed = [f"- {rule}" for rule in RULES]
    lines = [HEADING, *listed] + ([CHECKED_NOTE] if checked(workspace) else [])
    return "\n".join(lines)


def record_path(workspace, path) -> Path | None:
    if not told(workspace) or not path:
        return None
    root = workspace.root
    as_written = Path(os.path.normpath(root / os.path.expanduser(str(path))))
    try:
        resolved = workspace.resolve(str(path))
    except (tools.ToolError, OSError, ValueError):
        return None
    for record in RECORD_FILES:
        wanted = root / record
        if as_written == wanted and resolved == wanted:
            return wanted
    return None


def is_record_change(workspace, name: str, arguments: dict) -> bool:
    return name in tools.CHANGING_TOOLS and record_path(workspace, arguments.get("path")) is not None


def is_record_path(workspace, path: Path) -> bool:
    return any(Path(path) == workspace.root / record for record in RECORD_FILES)


def append_only_refusal(workspace, name: str, arguments: dict) -> str | None:
    if record_path(workspace, arguments.get("path")) != workspace.root / HISTORY:
        return None
    try:
        _target, before, after = tools.planned_change(name, arguments, workspace)
    except (tools.ToolError, OSError, ValueError, TypeError):
        return None
    return None if after.startswith(before) else APPEND_ONLY


def one_line(text: str, limit: int = MAX_FIELD_CHARS) -> str:
    flat = " ".join(str(text).replace(EM_DASH, ",").split())
    return flat if len(flat) <= limit else flat[:limit - 3] + "..."


def report_sections(report: str) -> dict[str, str]:
    sections, current = {}, None
    for line in report.splitlines():
        heading = REPORT_HEADING.match(line)
        if heading:
            current = heading.group(1).lower()
            sections[current] = heading.group(2)
        elif current is not None:
            sections[current] = f"{sections[current]} {line}"
    return {name: one_line(text) for name, text in sections.items() if text.strip()}


def report_status(sections: dict[str, str]) -> str:
    words = sections.get("outcome", "").replace(":", " ").replace(",", " ").split()
    return next((word.upper() for word in words[:3] if word.upper() in STATUSES), "")


@dataclass(frozen=True)
class Record:
    task: str
    changed: list
    commands: list
    checked: bool
    report: str
    stopped: str = ""


def history_entry(record: Record, today: str) -> str:
    sections = report_sections(record.report)
    status = "PARTIAL" if record.stopped else report_status(sections) or ("DONE" if record.checked else UNVERIFIED)
    what = sections.get("changed") or one_line(record.report) or record.stopped
    not_verified = sections.get("not verified") or ("nothing listed" if record.checked else UNCHECKED)
    lines = [f"## {today} - {one_line(record.task, MAX_TASK_CHARS)}",
             f"Status: {status}",
             f"Changed: {', '.join(record.changed)}",
             f"What: {what or 'not described'}",
             f"Why: {sections.get('assumptions') or 'not given in the report'}",
             f"Verified by: {one_line('; '.join(record.commands)) or NOTHING_RUN}",
             f"Not verified: {not_verified}",
             f"Left open: {sections.get('open items') or record.stopped or 'nothing listed'}"]
    return "\n".join(lines) + "\n"


def append_history(workspace, record: Record, today: str = "") -> Path:
    path = workspace.root / HISTORY
    tools.refuse_linked_place(workspace.root, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    earlier = tools.read_text(path) if path.is_file() else ""
    entry = history_entry(record, today or today_text())
    tools.write_text(path, f"{earlier.rstrip()}\n\n{entry}" if earlier.strip() else entry)
    return path
