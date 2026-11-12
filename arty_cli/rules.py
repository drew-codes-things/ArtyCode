import datetime
import os
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
    f"For a task that changes files keep {SCRATCHPAD.as_posix()} (never commit it): read it first, then write Task, "
    "Goal, Verification commands, Plan checklist, Assumptions, Verification log (command -> result) and a last line "
    "Status: IN PROGRESS, DONE, PARTIAL or BLOCKED.",
    f"Before finishing such a task append one entry to the end of {HISTORY.as_posix()} (read it first, never change "
    "earlier entries): ## {today} - <task>, then lines Status, Changed, What, Why, Verified by (commands and "
    "results), Not verified, Left open.",
    f"End such a task with a short report: Outcome (DONE, PARTIAL, BLOCKED or {UNVERIFIED}), Changed, Verification "
    "(each command and its result, failures first), Assumptions, Open items.",
)
CHECKED_NOTE = ("ArtyCode checks comments and em dashes after each edit, and sends you back once if you finish "
                "without checking your work or writing the history entry.")
HISTORY_TEMPLATE = (
    "## {today} - <the task in one line>\n"
    f"Status: DONE, PARTIAL, BLOCKED or {UNVERIFIED}\n"
    "Changed: <files>\n"
    "What: <what changed and what it does now>\n"
    "Why: <the choices made and the evidence for them; reasons live here, not in code comments>\n"
    "Verified by: <each command and its result>\n"
    "Not verified: <what wasn't proven and why, or nothing>\n"
    "Left open: <follow-ups and risks>"
)
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


def history_template(today: str) -> str:
    return HISTORY_TEMPLATE.format(today=today)


def instructions(workspace, today: str = "") -> str:
    if not told(workspace):
        return ""
    listed = [f"- {rule.format(today=today or today_text())}" for rule in RULES]
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
