import collections
import fnmatch
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from arty_cli import webfetch

DEFAULT_RESULT_CHARS = 16_000
AVERAGE_LINE_CHARS = 40
MAX_LINE_CHARS = 2000
MAX_LISTED_FILES = 500
MAX_SEARCH_MATCHES = 200
MAX_SEARCHED_FILE_BYTES = 2_000_000
OUTPUT_HEAD_SHARE = 0.4
DEFAULT_COMMAND_SECONDS = 120
MAX_COMMAND_SECONDS = 1800
DEFAULT_OUTPUT_WAIT_SECONDS = 5
JOB_POLL_SECONDS = 0.1
STILL_RUNNING = "still running"
STOPPED_OUTPUT_SECONDS = 3
STRAY_OUTPUT_NOTE = "\n[a process it started is still running and holding the output, so the rest wasn't read]"
MAX_JOB_OUTPUT_CHARS = 200_000
GLOB_CHARACTERS = set("*?[")
ANY_FOLDERS = "**"
WINDOWS_LINE_END = "\r\n"
UNIX_LINE_END = "\n"
SKIPPED_FOLDERS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache",
                   ".pytest_cache", ".gradle", "build", "dist"}
CHANGING_TOOLS = ("write_file", "edit_file", "multi_edit", "notebook_edit")
NOTEBOOK_SUFFIX = ".ipynb"
CELL_TYPES = ("code", "markdown", "raw")
EDIT_MODES = ("replace", "insert", "delete")
CELL_IDS_FROM_MINOR = 5
CELL_ID_CHARS = 8
TEXT_OUTPUT = "text/plain"
TODO_MARKS = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]"}
EXISTING_FILE_REFUSAL = ("{path} already exists, so write_file left it alone. Change it with edit_file: read it, "
                         "copy the exact lines to change with 2 or 3 lines around them as old_text. Set "
                         "overwrite to true only to replace the whole file.")
NOTHING_TO_UNDO = "There is nothing to undo."
MAX_CHECKPOINTS = 100
NEW_FILE_MODE = 0o666
POWERSHELL_EXIT_CODE = "\nif (-not $?) { if ($LASTEXITCODE) { exit $LASTEXITCODE }; exit 1 }"
TEMPORARY_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL
TRUE_WORDS = ("true", "yes", "1")
JSON_TYPES = {"array": list, "object": dict}
KEY_ALIASES = {"file_path": "path", "filepath": "path", "file": "path", "filename": "path", "file_name": "path",
               "old_string": "old_text", "old_str": "old_text", "new_string": "new_text", "new_str": "new_text",
               "cmd": "command", "todos": "items", "content": "text", "task": "text", "title": "text",
               "timeout": "timeout_seconds", "glob_pattern": "glob", "regex": "pattern", "query": "pattern"}
VALUE_ALIASES = {"completed": "done", "complete": "done", "finished": "done", "in-progress": "in_progress",
                 "in progress": "in_progress", "inprogress": "in_progress", "active": "in_progress",
                 "todo": "pending", "to do": "pending", "not_started": "pending", "open": "pending"}
FALSE_WORDS = ("false", "no", "0")


class ToolError(Exception):
    pass


@dataclass(frozen=True)
class Checkpoint:
    path: Path
    before: str | None
    after: str


def digest_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest()


class Job:
    def __init__(self, command: str, process: subprocess.Popen):
        self.command = command
        self.process = process
        self.output = collections.deque()
        self.kept_chars = 0
        self.read_upto = 0
        self.missed = 0
        self.lock = threading.Lock()
        self.collector = threading.Thread(target=self.collect, daemon=True)
        self.collector.start()

    def collect(self) -> None:
        for line in self.process.stdout:
            with self.lock:
                self.keep(line)

    def keep(self, line: str) -> None:
        self.output.append(line)
        self.kept_chars += len(line)
        while self.kept_chars > MAX_JOB_OUTPUT_CHARS and self.output:
            self.kept_chars -= len(self.output.popleft())
            if self.read_upto:
                self.read_upto -= 1
            else:
                self.missed += 1

    def unread(self) -> str:
        with self.lock:
            fresh = "".join(list(self.output)[self.read_upto:])
            self.read_upto = len(self.output)
            missed, self.missed = self.missed, 0
        return f"... {missed} earlier lines dropped ...\n{fresh}" if missed else fresh

    def state(self) -> str:
        code = self.process.poll()
        return STILL_RUNNING if code is None else f"finished with exit code {code}"

    def wait_for_end(self, seconds: float) -> None:
        try:
            self.process.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            return
        self.collector.join(STOPPED_OUTPUT_SECONDS)


class Workspace:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.todos = []
        self.max_command_seconds = MAX_COMMAND_SECONDS
        self.jobs = {}
        self.seen = {}
        self.checkpoints = []
        self.lint = None
        self.repo_map = None
        self.sandbox = None
        self.rules = None
        self.result_chars = DEFAULT_RESULT_CHARS

    def lint_note(self, path: Path, text: str) -> str:
        if self.lint is None:
            return ""
        found = self.lint.problems(path, text)
        return f"\n{found}" if found else ""

    def note_seen(self, path: Path, text: str) -> None:
        self.seen[path] = digest_of(text)

    def check_fresh(self, path: Path, current: str) -> None:
        if path not in self.seen:
            raise ToolError(f"read {self.shown(path)} with read_file before changing it")
        if self.seen[path] != digest_of(current):
            raise ToolError(f"{self.shown(path)} changed on disk since you last read it; read it again first")

    def record_change(self, path: Path, before: str | None, after: str) -> None:
        self.checkpoints = (self.checkpoints + [Checkpoint(path, before, after)])[-MAX_CHECKPOINTS:]
        self.note_seen(path, after)

    def undo_last(self) -> str:
        if not self.checkpoints:
            return NOTHING_TO_UNDO
        last = self.checkpoints[-1]
        shown = self.shown(last.path)
        current = read_text(last.path) if last.path.is_file() else None
        if current != last.after:
            return f"Didn't undo the change to {shown}: it changed again after Arty changed it."
        self.checkpoints.pop()
        if last.before is None:
            last.path.unlink()
            self.seen.pop(last.path, None)
            return f"Undid the creation of {shown} (removed it)."
        write_text(last.path, last.before)
        self.note_seen(last.path, last.before)
        return f"Undid the last change to {shown}."

    def resolve(self, path: str) -> Path:
        if not path:
            raise ToolError("a path is needed")
        given = Path(os.path.expanduser(path))
        return (given if given.is_absolute() else self.root / given).resolve()

    def contains(self, path: str) -> bool:
        return self.resolve(path).is_relative_to(self.root)

    def shown(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix() if path.is_relative_to(self.root) else str(path)

    def stop_jobs(self) -> None:
        for job in self.jobs.values():
            if job.process.poll() is None:
                stop(job.process)
                job.process.wait()


def read_text(path: Path) -> str:
    if not path.is_file():
        raise ToolError(f"{path} is not a file")
    try:
        with open(path, encoding="utf-8", newline="") as handle:
            return handle.read()
    except UnicodeDecodeError:
        raise ToolError(f"{path} is not a text file") from None


def created_beside(path: Path) -> Path:
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.arty-tmp"
    os.close(os.open(temporary, TEMPORARY_FILE_FLAGS, NEW_FILE_MODE))
    return temporary


def write_text(path: Path, text: str) -> None:
    mode = path.stat().st_mode & 0o7777 if path.exists() else None
    temporary = created_beside(path)
    try:
        with open(temporary, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def uses_windows_line_ends(text: str) -> bool:
    return WINDOWS_LINE_END in text


def in_line_style_of(text: str, existing: str) -> str:
    if not uses_windows_line_ends(existing):
        return text
    return text.replace(WINDOWS_LINE_END, UNIX_LINE_END).replace(UNIX_LINE_END, WINDOWS_LINE_END)


def read_notebook(path: Path) -> dict:
    try:
        found = json.loads(read_text(path))
    except ValueError as error:
        raise ToolError(f"{path.name} isn't a readable notebook: {error}") from None
    if not isinstance(found, dict) or not isinstance(found.get("cells"), list):
        raise ToolError(f"{path.name} isn't a notebook (no list of cells)")
    return found


def joined_source(source) -> str:
    return "".join(source) if isinstance(source, list) else str(source or "")


def output_text(output: dict) -> str:
    kind = output.get("output_type")
    if kind == "stream":
        return joined_source(output.get("text"))
    if kind == "error":
        return f"{output.get('ename', 'Error')}: {output.get('evalue', '')}"
    data = output.get("data") or {}
    text = joined_source(data[TEXT_OUTPUT]) if TEXT_OUTPUT in data else ""
    others = " ".join(f"[{kind_of_data} output]" for kind_of_data in data if kind_of_data != TEXT_OUTPUT)
    return "\n".join(part for part in (text, others) if part)


def rendered_cell(number: int, cell: dict) -> str:
    shown = f"Cell {number} ({cell.get('cell_type', 'code')})\n{joined_source(cell.get('source'))}"
    outputs = [output_text(output) for output in cell.get("outputs") or [] if isinstance(output, dict)]
    outputs = [text.rstrip() for text in outputs if text.strip()]
    if outputs:
        shown += "\nOutput:\n" + "\n".join(outputs)
    return shown


def rendered_notebook(notebook: dict, limit: int) -> str:
    cells = [rendered_cell(number, cell) for number, cell in enumerate(notebook["cells"], start=1) if isinstance(cell, dict)]
    return clipped("\n\n".join(cells), limit) if cells else "The notebook has no cells."


def is_notebook(path: Path) -> bool:
    return path.suffix.lower() == NOTEBOOK_SUFFIX


def read_file(workspace, path, offset=1, limit=0):
    target = workspace.resolve(path)
    if is_notebook(target):
        shown = rendered_notebook(read_notebook(target), workspace.result_chars)
        workspace.note_seen(target, read_text(target))
        return shown
    whole = read_text(target)
    workspace.note_seen(target, whole)
    lines = whole.splitlines()
    start = max(int(offset), 1)
    chosen = lines[start - 1:start - 1 + line_count(limit, workspace.result_chars)]
    if not chosen and lines:
        raise ToolError(f"{path} has only {len(lines)} lines")
    shown, used = [], 0
    for number, line in enumerate(chosen, start=start):
        entry = f"{number}\t{line[:MAX_LINE_CHARS]}"
        if shown and used + len(entry) > workspace.result_chars:
            shown.append(f"... stopped at about {workspace.result_chars} characters; read on with offset={number}")
            return "\n".join(shown)
        shown.append(entry)
        used += len(entry) + 1
    next_line = start + len(chosen)
    lines_left = len(lines) - (next_line - 1)
    if lines_left > 0:
        shown.append(f"... {lines_left} more lines; read on with offset={next_line}")
    return "\n".join(shown)


def line_count(limit, result_chars: int) -> int:
    asked = int(limit or 0)
    return asked if asked > 0 else max(result_chars // AVERAGE_LINE_CHARS, 1)


WRITTEN_OUT_ESCAPES = {"\\n": "\n", "\\t": "\t", '\\"': '"'}


def unescaped(text: str) -> str:
    for written, real in WRITTEN_OUT_ESCAPES.items():
        text = text.replace(written, real)
    return text


WRITTEN_OUT_ESCAPE = re.compile(r'\\[nt"]')
CONTEXT_STOP = re.compile(r'\s|\\[nt"]')


def written_context(text: str, start: int, end: int) -> str:
    stops_before = [stop.end() for stop in CONTEXT_STOP.finditer(text, 0, start)]
    stop_after = CONTEXT_STOP.search(text, end)
    left = stops_before[-1] if stops_before else 0
    right = stop_after.start() if stop_after else len(text)
    return text[left:right]


def unescaped_new_text(new_text: str, before: str) -> str:
    def replaced(escape: re.Match) -> str:
        context = written_context(new_text, escape.start(), escape.end())
        file_has_this_escape = len(context) > len(escape.group()) and context in before
        return escape.group() if file_has_this_escape else WRITTEN_OUT_ESCAPES[escape.group()]

    return WRITTEN_OUT_ESCAPE.sub(replaced, new_text)


TYPOGRAPHIC_MARKS = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'", "\u2032": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"', "\u2033": '"',
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-", "\u2015": "-", "\u2212": "-",
    "\u00a0": " ", "\u2007": " ", "\u2009": " ", "\u200a": " ", "\u202f": " ", "\u3000": " ",
})
FILE_LINE = re.compile(r"[^\n]*\n|[^\n]+$")


def in_plain_marks(line: str) -> str:
    return line.translate(TYPOGRAPHIC_MARKS).strip()


@dataclass(frozen=True)
class Looseness:
    how: str
    key: Callable[[str], str]
    keeps_file_indentation: bool


LOOSER_MATCHES = (
    Looseness("ignoring spaces at line ends", str.rstrip, False),
    Looseness("ignoring indentation and spaces at line ends, keeping the file's indentation", str.strip, True),
    Looseness("after turning curly quotes, long dashes and odd spaces into plain ones, keeping the file's "
              "indentation", in_plain_marks, True),
)


@dataclass(frozen=True)
class AppliedEdit:
    after: str
    how: str = ""


def file_lines(text: str) -> list[str]:
    return FILE_LINE.findall(text)


def content_of(line: str) -> str:
    return line.removesuffix("\n").removesuffix("\r")


def pattern_lines(text: str) -> list[str]:
    return [line.removesuffix("\r") for line in text.removesuffix("\n").removesuffix("\r").split("\n")]


def leading_space(line: str) -> str:
    return line[:len(line) - len(line.lstrip())]


def loose_starts(lines: list[str], pattern: list[str], looseness: Looseness) -> list[int]:
    wanted = [looseness.key(line) for line in pattern]
    keys = [looseness.key(content_of(line)) for line in lines]
    size = len(wanted)
    return [start for start in range(len(keys) - size + 1) if keys[start:start + size] == wanted]


def indentation_pairs(pattern: list[str], matched: list[str]) -> list[tuple[str, str]]:
    pairs = {leading_space(line): leading_space(original) for line, original in zip(pattern, matched) if line.strip()}
    return sorted(pairs.items(), key=lambda pair: len(pair[0]), reverse=True)


def reindented_line(line: str, pairs: list[tuple[str, str]]) -> str:
    if not line.strip():
        return ""
    for model_indent, file_indent in pairs:
        if line.startswith(model_indent):
            return file_indent + line[len(model_indent):]
    return line


def with_file_indentation(new_lines: list[str], pattern: list[str], matched: list[str]) -> list[str]:
    unchanged = {line: original for line, original in zip(pattern, matched) if line.strip()}
    pairs = indentation_pairs(pattern, matched)
    return [unchanged.get(line) or reindented_line(line, pairs) for line in new_lines]


def spliced(lines: list[str], start: int, pattern: list[str], new_text: str, looseness: Looseness) -> str:
    matched = lines[start:start + len(pattern)]
    line_end = "\r\n" if matched[0].endswith("\r\n") else "\n"
    new_lines = pattern_lines(new_text) if new_text else []
    if looseness.keeps_file_indentation:
        new_lines = with_file_indentation(new_lines, pattern, [content_of(line) for line in matched])
    last_end = matched[-1][len(content_of(matched[-1])):]
    replacement = line_end.join(new_lines) + last_end if new_lines else ""
    return "".join(lines[:start]) + replacement + "".join(lines[start + len(pattern):])


def loosely_edited(before: str, old_text: str, new_text: str) -> AppliedEdit | None:
    pattern = pattern_lines(old_text)
    if not any(line.strip() for line in pattern):
        return None
    lines = file_lines(before)
    for looseness in LOOSER_MATCHES:
        starts = loose_starts(lines, pattern, looseness)
        if len(starts) > 1:
            raise ToolError(f"old_text is in {len(starts)} places when matched {looseness.how}; "
                            "add more surrounding text")
        if starts:
            return AppliedEdit(spliced(lines, starts[0], pattern, new_text, looseness), looseness.how)
    return None


def applied_edit(before: str, old_text: str, new_text: str, replace_all: bool) -> AppliedEdit:
    if not old_text:
        raise ToolError("old_text is empty; use write_file to create a file")
    found = before.count(old_text)
    if found == 0 and before.count(unescaped(old_text)) > 0:
        old_text, new_text = unescaped(old_text), unescaped_new_text(new_text, before)
        found = before.count(old_text)
    if found == 0:
        loose = loosely_edited(before, old_text, new_text)
        if loose is None:
            raise ToolError("old_text was not found; read the file again and copy the text exactly")
        return loose
    if found > 1 and not replace_all:
        raise ToolError(f"old_text is in {found} places; add more surrounding text or set replace_all")
    return AppliedEdit(before.replace(old_text, new_text))


def edited_text(before: str, old_text: str, new_text: str, replace_all: bool) -> str:
    return applied_edit(before, old_text, new_text, replace_all).after


def applied_edits(before: str, edits) -> tuple[str, list[str]]:
    if not isinstance(edits, list) or not edits:
        raise ToolError("edits needs at least one {old_text, new_text} change")
    after, notes = before, []
    for number, edit in enumerate(edits, start=1):
        if not isinstance(edit, dict):
            raise ToolError(f"edit {number} should be an object with old_text and new_text; nothing was changed")
        old_text = in_line_style_of(str(edit.get("old_text", "")), before)
        new_text = in_line_style_of(str(edit.get("new_text", "")), before)
        try:
            applied = applied_edit(after, old_text, new_text, bool(edit.get("replace_all", False)))
        except ToolError as error:
            raise ToolError(f"edit {number}: {error}; nothing was changed") from None
        after = applied.after
        notes += [f"edit {number} matched {applied.how}"] if applied.how else []
    return after, notes


def multi_edited_text(before: str, edits) -> str:
    return applied_edits(before, edits)[0]


def match_note(name: str, arguments: dict, before: str) -> str:
    if name == "multi_edit":
        notes = applied_edits(before, arguments.get("edits"))[1]
        return f" ({'; '.join(notes)})" if notes else ""
    old_text = in_line_style_of(str(arguments.get("old_text", "")), before)
    new_text = in_line_style_of(str(arguments.get("new_text", "")), before)
    how = applied_edit(before, old_text, new_text, bool(arguments.get("replace_all", False))).how
    return f" (old_text matched {how})" if how else ""


def source_lines(text: str) -> list[str]:
    return str(text).splitlines(keepends=True)


def new_cell(cell_type: str, source: str, notebook: dict) -> dict:
    cell = {"cell_type": cell_type, "metadata": {}, "source": source_lines(source)}
    if cell_type == "code":
        cell.update(execution_count=None, outputs=[])
    if notebook.get("nbformat", 4) > 4 or notebook.get("nbformat_minor", 0) >= CELL_IDS_FROM_MINOR:
        cell["id"] = uuid.uuid4().hex[:CELL_ID_CHARS]
    return cell


def replaced_cell(cell: dict, source: str, cell_type: str) -> dict:
    changed = {**cell, "source": source_lines(source), "cell_type": cell_type or cell.get("cell_type", "code")}
    if changed["cell_type"] == "code":
        changed.update(execution_count=None, outputs=[])
    else:
        changed.pop("outputs", None)
        changed.pop("execution_count", None)
    return changed


def edited_notebook(notebook: dict, cell, source: str, cell_type: str, edit_mode: str) -> dict:
    if edit_mode not in EDIT_MODES:
        raise ToolError(f"edit_mode must be one of {', '.join(EDIT_MODES)}")
    if cell_type and cell_type not in CELL_TYPES:
        raise ToolError(f"cell_type must be one of {', '.join(CELL_TYPES)}")
    cells = list(notebook["cells"])
    number = int(cell)
    highest = len(cells) + 1 if edit_mode == "insert" else len(cells)
    if not 1 <= number <= highest:
        raise ToolError(f"cell must be from 1 to {highest}; the notebook has {len(cells)} cells")
    if edit_mode == "insert":
        cells.insert(number - 1, new_cell(cell_type or "code", source, notebook))
    elif edit_mode == "delete":
        del cells[number - 1]
    else:
        cells[number - 1] = replaced_cell(cells[number - 1], source, cell_type)
    return {**notebook, "cells": cells}


def notebook_json(notebook: dict) -> str:
    return json.dumps(notebook, indent=1, ensure_ascii=False) + "\n"


def planned_change(name: str, arguments: dict, workspace) -> tuple[Path, str, str]:
    target = workspace.resolve(arguments.get("path", ""))
    if name == "write_file":
        if target.exists() and not arguments.get("overwrite"):
            raise ToolError(EXISTING_FILE_REFUSAL.format(path=workspace.shown(target)))
        before = read_text(target) if target.exists() else ""
        return target, before, in_line_style_of(str(arguments.get("content", "")), before)
    if name == "edit_file":
        before = read_text(target)
        old_text = in_line_style_of(str(arguments.get("old_text", "")), before)
        new_text = in_line_style_of(str(arguments.get("new_text", "")), before)
        after = edited_text(before, old_text, new_text, bool(arguments.get("replace_all", False)))
        return target, before, after
    if name == "multi_edit":
        before = read_text(target)
        return target, before, multi_edited_text(before, arguments.get("edits"))
    if name == "notebook_edit":
        if not is_notebook(target):
            raise ToolError("notebook_edit only changes .ipynb notebooks; use edit_file for other files")
        before = read_text(target)
        after = edited_notebook(read_notebook(target), arguments.get("cell", 0), str(arguments.get("source", "")),
                                str(arguments.get("cell_type", "")), str(arguments.get("edit_mode", "replace")))
        return target, before, in_line_style_of(notebook_json(after), before)
    raise ToolError(f"{name} doesn't change files")


def changed_file(workspace, name: str, arguments: dict) -> tuple[Path, str, str]:
    target, before, after = planned_change(name, arguments, workspace)
    existed = target.exists()
    if existed:
        workspace.check_fresh(target, before)
    target.parent.mkdir(parents=True, exist_ok=True)
    write_text(target, after)
    workspace.record_change(target, before if existed else None, after)
    return target, before, after


def change_problem(workspace, name: str, arguments: dict) -> str:
    try:
        target, before, _after = planned_change(name, arguments, workspace)
        if target.exists():
            workspace.check_fresh(target, before)
    except (ToolError, OSError, ValueError, TypeError) as error:
        return f"Error: {error}"
    return ""


def write_file(workspace, path, content, overwrite=False):
    arguments = {"path": path, "content": content, "overwrite": overwrite}
    target, before, after = changed_file(workspace, "write_file", arguments)
    verb = "Updated" if before else "Created"
    return f"{verb} {workspace.shown(target)} ({len(after.splitlines())} lines)" + workspace.lint_note(target, after)


def edit_file(workspace, path, old_text, new_text, replace_all=False):
    arguments = {"path": path, "old_text": old_text, "new_text": new_text, "replace_all": replace_all}
    target, before, after = changed_file(workspace, "edit_file", arguments)
    note = match_note("edit_file", arguments, before)
    return f"Edited {workspace.shown(target)}{note}" + workspace.lint_note(target, after)


def multi_edit(workspace, path, edits):
    arguments = {"path": path, "edits": edits}
    target, before, after = changed_file(workspace, "multi_edit", arguments)
    note = match_note("multi_edit", arguments, before)
    return f"Made {len(edits)} edits to {workspace.shown(target)}{note}" + workspace.lint_note(target, after)


def notebook_edit(workspace, path, cell, source="", cell_type="", edit_mode="replace"):
    arguments = {"path": path, "cell": cell, "source": source, "cell_type": cell_type, "edit_mode": edit_mode}
    target, _before, _after = changed_file(workspace, "notebook_edit", arguments)
    done = {"insert": "Inserted", "delete": "Deleted"}.get(edit_mode, "Replaced")
    return f"{done} cell {cell} in {workspace.shown(target)}"


def split_pattern(pattern: str) -> tuple[str, list[str]]:
    parts = pattern.replace("\\", "/").split("/")
    fixed = []
    for index, part in enumerate(parts):
        if GLOB_CHARACTERS & set(part):
            return "/".join(fixed) or ("/" if fixed else ""), parts[index:]
        fixed.append(part)
    return "/".join(fixed[:-1]) or ("/" if len(fixed) > 1 else ""), fixed[-1:]


def listing_folder(workspace, pattern: str, path: str = ".") -> tuple[Path, list[str]]:
    prefix, rest = split_pattern(pattern or "**/*")
    if ".." in rest:
        raise ToolError("'..' can only come before the first wildcard in a pattern")
    folder = workspace.resolve(path or ".")
    return ((folder / os.path.expanduser(prefix)).resolve() if prefix else folder), rest


def matches(parts: tuple, pattern: list[str]) -> bool:
    if not pattern:
        return not parts
    head, rest = pattern[0], pattern[1:]
    if head == ANY_FOLDERS:
        return any(matches(parts[skipped:], rest) for skipped in range(len(parts) + 1))
    return bool(parts) and fnmatch.fnmatch(parts[0], head) and matches(parts[1:], rest)


def is_linked_out(workspace, folder: Path, candidate: Path) -> bool:
    return folder.is_relative_to(workspace.root) and not candidate.resolve().is_relative_to(workspace.root)


def project_files(workspace, folder: Path, pattern: list[str]) -> list[Path]:
    deepest = None if ANY_FOLDERS in pattern else len(pattern) - 1
    found = []
    for current, folders, files in os.walk(folder):
        depth = len(Path(current).relative_to(folder).parts)
        folders[:] = [] if deepest is not None and depth >= deepest else [
            name for name in folders if name not in SKIPPED_FOLDERS]
        for name in files:
            candidate = Path(current) / name
            if matches(candidate.relative_to(folder).parts, pattern) and not is_linked_out(workspace, folder, candidate):
                found.append(candidate)
    return sorted(found)


def list_files(workspace, pattern="**/*", path="."):
    folder, rest = listing_folder(workspace, str(pattern), str(path))
    found = [workspace.shown(candidate) for candidate in project_files(workspace, folder, rest)]
    if not found:
        return f"No files match {pattern} in {workspace.shown(folder) or '.'}"
    extra = len(found) - MAX_LISTED_FILES
    listed = "\n".join(found[:MAX_LISTED_FILES])
    return listed + (f"\n... and {extra} more" if extra > 0 else "")


ANY_FOLDERS_PREFIX = ANY_FOLDERS + "/"
ANY_FOLDERS_BETWEEN = "/" + ANY_FOLDERS + "/"


def glob_spellings(glob: str) -> set[str]:
    return {glob, glob.removeprefix(ANY_FOLDERS_PREFIX), glob.replace(ANY_FOLDERS_BETWEEN, "/")}


def matches_glob(name: str, relative: str, glob: str) -> bool:
    return any(fnmatch.fnmatch(name, spelling) or fnmatch.fnmatch(relative, spelling) for spelling in glob_spellings(glob))


def searched_files(workspace, target: Path) -> list[Path]:
    return [target] if target.is_file() else project_files(workspace, target, [ANY_FOLDERS, "*"])


def search(workspace, pattern, path=".", glob="*"):
    try:
        expression = re.compile(pattern)
    except re.error as error:
        raise ToolError(f"bad pattern: {error}") from None
    matches, used = [], 0
    for candidate in searched_files(workspace, workspace.resolve(path)):
        if not matches_glob(candidate.name, workspace.shown(candidate), glob) or candidate.stat().st_size > MAX_SEARCHED_FILE_BYTES:
            continue
        try:
            lines = candidate.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(lines, start=1):
            if not expression.search(line):
                continue
            found = f"{workspace.shown(candidate)}:{number}:{line[:MAX_LINE_CHARS]}"
            if used + len(found) > workspace.result_chars:
                return "\n".join(matches) + f"\n... stopped at about {workspace.result_chars} characters of matches"
            matches.append(found)
            used += len(found) + 1
            if len(matches) >= MAX_SEARCH_MATCHES:
                return "\n".join(matches) + f"\n... stopped at {MAX_SEARCH_MATCHES} matches"
    return "\n".join(matches) if matches else f"No matches for {pattern}"


def clipped(output: str, limit: int) -> str:
    if len(output) <= limit:
        return output
    head = int(limit * OUTPUT_HEAD_SHARE)
    return f"{output[:head]}\n... {len(output) - limit} characters cut ...\n{output[-(limit - head):]}"


def windows_system_program(*parts: str) -> str:
    root = os.environ.get("SystemRoot", "C:\\Windows")
    return "\\".join([root.rstrip("\\"), "System32", *parts])


def windows_powershell() -> str:
    return windows_system_program("WindowsPowerShell", "v1.0", "powershell.exe")


def powershell_arguments(script: str) -> list[str]:
    return [windows_powershell(), "-NoProfile", "-NonInteractive", "-Command", script + POWERSHELL_EXIT_CODE]


def shell_command(command: str) -> tuple[list[str] | str, dict]:
    if sys.platform == "win32":
        return powershell_arguments(command), {}
    bash = "/bin/bash" if Path("/bin/bash").exists() else None
    return command, {"shell": True, "executable": bash, "start_new_session": True}


def stop(process: subprocess.Popen) -> None:
    end_tree = getattr(process, "end_tree", None)
    if end_tree is not None:
        end_tree()
    elif sys.platform == "win32":
        subprocess.run([windows_system_program("taskkill.exe"), "/F", "/T", "/PID", str(process.pid)],
                       capture_output=True)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def start_process(workspace, command: str) -> subprocess.Popen:
    pipes = {"cwd": workspace.root, "stdout": subprocess.PIPE, "stderr": subprocess.STDOUT,
             "stdin": subprocess.DEVNULL, "text": True, "encoding": "utf-8", "errors": "replace"}
    if workspace.sandbox is not None:
        sandboxed = workspace.sandbox.start(command, **pipes)
        if sandboxed is not None:
            return sandboxed
    arguments, options = shell_command(command)
    return subprocess.Popen(arguments, **pipes, **options)


def start_job(workspace, command: str) -> str:
    job_id = str(len(workspace.jobs) + 1)
    workspace.jobs[job_id] = Job(command, start_process(workspace, command))
    return f"Job {job_id} started in the background. Read it with command_output, end it with stop_command."


def job_of(workspace, job) -> Job:
    found = workspace.jobs.get(str(job))
    if found is None:
        raise ToolError(f"there is no background job {job}")
    return found


def command_output(workspace, job, wait_seconds=DEFAULT_OUTPUT_WAIT_SECONDS):
    running = job_of(workspace, job)
    running.wait_for_end(min(max(float(wait_seconds), 0), workspace.max_command_seconds))
    return clipped(running.unread(), workspace.result_chars) + f"\n[job {job} {running.state()}]"


def stop_command(workspace, job):
    running = job_of(workspace, job)
    if running.process.poll() is None:
        stop(running.process)
        running.process.wait()
    return clipped(running.unread(), workspace.result_chars) + f"\n[job {job} stopped]"


def todo_write(workspace, items):
    if not isinstance(items, list):
        raise ToolError("items should be a list")
    for item in items:
        if not isinstance(item, dict) or item.get("status") not in TODO_MARKS or not str(item.get("text", "")).strip():
            raise ToolError("each item needs text and a status of pending, in_progress or done")
    workspace.todos = items
    return "\n".join(f"{TODO_MARKS[item['status']]} {item['text']}" for item in items) or "The list is empty."


def web_fetch(workspace, url):
    try:
        return webfetch.fetch(str(url))
    except webfetch.FetchError as error:
        raise ToolError(str(error)) from None


def text_of(output) -> str:
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace")
    return output or ""


def output_after_stop(process: subprocess.Popen) -> str:
    try:
        return process.communicate(timeout=STOPPED_OUTPUT_SECONDS)[0]
    except subprocess.TimeoutExpired as error:
        process.wait()
        return text_of(error.output) + STRAY_OUTPUT_NOTE


STOPPED_LEAD = "[stopped after"


def timed_out_note(seconds: int, most: int) -> str:
    return (f"{STOPPED_LEAD} {seconds} s while still running, so it is not running now. A server or watcher "
            f"should run with background: true, then command_output reads it; a long install or build can run "
            f"again with a bigger timeout_seconds (up to {most}). Its output before it was stopped:]\n")


def run_command(workspace, command, timeout_seconds=DEFAULT_COMMAND_SECONDS, background=False):
    if background:
        return start_job(workspace, command)
    most = workspace.max_command_seconds
    seconds = min(max(int(timeout_seconds), 1), most)
    process = start_process(workspace, command)
    try:
        output, _ = process.communicate(timeout=seconds)
    except subprocess.TimeoutExpired:
        stop(process)
        return timed_out_note(seconds, most) + clipped(output_after_stop(process), workspace.result_chars)
    except KeyboardInterrupt:
        stop(process)
        process.wait()
        raise
    return clipped(output, workspace.result_chars) + f"\n[exit code {process.returncode}]"


HANDLERS = {
    "read_file": read_file,
    "write_file": write_file,
    "edit_file": edit_file,
    "multi_edit": multi_edit,
    "notebook_edit": notebook_edit,
    "list_files": list_files,
    "search": search,
    "run_command": run_command,
    "command_output": command_output,
    "stop_command": stop_command,
    "todo_write": todo_write,
    "web_fetch": web_fetch,
}


def function(name, description, properties, required):
    return {"type": "function", "function": {"name": name, "description": description,
                                             "parameters": {"type": "object", "properties": properties,
                                                            "required": required}}}


TEXT = {"type": "string"}
NUMBER = {"type": "integer"}
DEFINITIONS = [
    function("read_file", "Read a text file in the project. Lines come back numbered. A Jupyter notebook (.ipynb) "
                          "comes back as numbered cells with their text output.",
             {"path": TEXT, "offset": {**NUMBER, "description": "first line, from 1"},
              "limit": {**NUMBER, "description": "how many lines"}}, ["path"]),
    function("write_file", "Create a new file. An existing file is refused: change it with edit_file, or set "
                           "overwrite to true only when the whole file really has to be replaced.",
             {"path": TEXT, "content": TEXT,
              "overwrite": {"type": "boolean", "description": "replace an existing file completely"}},
             ["path", "content"]),
    function("edit_file", "Replace old_text with new_text in a file. old_text must match exactly once "
                          "(copy it from read_file without the line numbers) unless replace_all is true.",
             {"path": TEXT, "old_text": TEXT, "new_text": TEXT, "replace_all": {"type": "boolean"}},
             ["path", "old_text", "new_text"]),
    function("multi_edit", "Make several edit_file changes to one file in one step, in order, each applied to the "
                           "result of the one before. Each old_text must match exactly once unless its replace_all "
                           "is true. All or nothing: if any edit fails, the file is left as it was.",
             {"path": TEXT, "edits": {"type": "array", "items": {"type": "object", "properties": {
                 "old_text": TEXT, "new_text": TEXT, "replace_all": {"type": "boolean"}},
                 "required": ["old_text", "new_text"]}}}, ["path", "edits"]),
    function("notebook_edit", "Change one cell of a Jupyter notebook (.ipynb); never edit notebooks with edit_file. "
                              "cell is the number read_file shows. edit_mode: replace (default; a code cell's old "
                              "output is cleared), insert (the new cell takes that number) or delete.",
             {"path": TEXT, "cell": NUMBER, "source": TEXT,
              "cell_type": {"type": "string", "enum": list(CELL_TYPES)},
              "edit_mode": {"type": "string", "enum": list(EDIT_MODES)}}, ["path", "cell"]),
    function("list_files", "List files in the project by glob pattern, e.g. **/*.py.",
             {"pattern": TEXT, "path": {**TEXT, "description": "folder to look in, default the project"}}, []),
    function("search", "Search file contents with a regular expression. Returns file:line:text.",
             {"pattern": TEXT, "path": TEXT, "glob": {**TEXT, "description": "file name filter, e.g. *.py"}},
             ["pattern"]),
    function("run_command", "Run a shell command in the project folder (bash on Linux, PowerShell on Windows), "
                            "e.g. tests, builds, git. Returns the output and exit code. It is stopped after "
                            f"timeout_seconds (default {DEFAULT_COMMAND_SECONDS}); give long installs and builds "
                            "more. Servers and watchers never end, so start them with background: true.",
             {"command": TEXT, "timeout_seconds": NUMBER,
              "background": {"type": "boolean", "description": "start it and return at once, e.g. a dev server"}},
             ["command"]),
    function("command_output", "Read new output from a background job started by run_command. Waits up to "
                               f"wait_seconds (default {DEFAULT_OUTPUT_WAIT_SECONDS}) for the job to finish first; "
                               "pass more to wait for a long build.",
             {"job": TEXT, "wait_seconds": NUMBER}, ["job"]),
    function("stop_command", "Stop a background job.", {"job": TEXT}, ["job"]),
    function("todo_write", "Keep a short checklist for a job with several steps; send the whole list each time "
                           "with each item's status (pending, in_progress, done). The user sees it.",
             {"items": {"type": "array", "items": {"type": "object", "properties": {
                 "text": TEXT, "status": {"type": "string", "enum": ["pending", "in_progress", "done"]}},
                 "required": ["text", "status"]}}}, ["items"]),
    function("web_fetch", "Fetch a web page (documentation, an issue, an API reference) as text.",
             {"url": TEXT}, ["url"]),
]

PROPERTIES = {definition["function"]["name"]: definition["function"]["parameters"]["properties"]
              for definition in DEFINITIONS}


def strict_boolean(key: str, value) -> bool:
    if value is None or isinstance(value, bool):
        return bool(value)
    word = str(value).strip().lower()
    if word in TRUE_WORDS:
        return True
    if word in FALSE_WORDS:
        return False
    raise ToolError(f"{key} should be true or false, not {value!r}")


def from_json_text(schema: dict, value):
    wanted = JSON_TYPES.get(schema.get("type"))
    if wanted is None or not isinstance(value, str):
        return value
    try:
        found = json.loads(value)
    except ValueError:
        return value
    return found if isinstance(found, wanted) else value


def enum_value(schema: dict, value):
    allowed = schema.get("enum")
    if not allowed or not isinstance(value, str) or value in allowed:
        return value
    plain = value.strip().lower()
    plain = VALUE_ALIASES.get(plain, plain)
    return plain if plain in allowed else value


def typed_value(key: str, schema: dict, value):
    if schema.get("type") == "boolean":
        return strict_boolean(key, value)
    value = enum_value(schema, from_json_text(schema, value))
    item_properties = (schema.get("items") or {}).get("properties")
    if schema.get("type") == "array" and item_properties and isinstance(value, list):
        return [typed_values(item_properties, item) if isinstance(item, dict) else item for item in value]
    return value


def with_right_names(properties: dict, values: dict) -> dict:
    renamed = dict(values)
    for key in values:
        right = KEY_ALIASES.get(key)
        if key not in properties and right in properties and right not in renamed:
            renamed[right] = renamed.pop(key)
    return renamed


def typed_values(properties: dict, values: dict) -> dict:
    named = with_right_names(properties, values) if properties else values
    return {key: typed_value(key, properties.get(key, {}), value) for key, value in named.items()}


def typed_arguments(name: str, arguments: dict) -> dict:
    return typed_values(PROPERTIES.get(name, {}), arguments)


def parameter_note(name: str) -> str:
    definition = next((found["function"] for found in DEFINITIONS if found["function"]["name"] == name), None)
    if definition is None:
        return ""
    names = ", ".join(definition["parameters"]["properties"])
    needed = ", ".join(definition["parameters"]["required"])
    return f". Its parameters are {names}" + (f" ({needed} needed)" if needed else "")


def run(name: str, arguments: dict, workspace: Workspace) -> str:
    handler = HANDLERS.get(name)
    if handler is None:
        return f"Error: there is no tool called {name}"
    try:
        return handler(workspace, **arguments)
    except TypeError as error:
        return f"Error: wrong arguments for {name}: {error}{parameter_note(name)}"
    except (ToolError, OSError, ValueError) as error:
        return f"Error: {error}"
