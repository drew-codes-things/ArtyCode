import collections
import fnmatch
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from arty_cli import webfetch

DEFAULT_READ_LINES = 2000
MAX_LINE_CHARS = 2000
MAX_LISTED_FILES = 500
MAX_SEARCH_MATCHES = 200
MAX_SEARCHED_FILE_BYTES = 2_000_000
MAX_OUTPUT_CHARS = 20_000
MAX_READ_CHARS = 60_000
OUTPUT_HEAD_SHARE = 0.4
DEFAULT_COMMAND_SECONDS = 120
MAX_COMMAND_SECONDS = 600
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
NOTHING_TO_UNDO = "There is nothing to undo."
MAX_CHECKPOINTS = 100


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
        threading.Thread(target=self.collect, daemon=True).start()

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
        return "still running" if code is None else f"finished with exit code {code}"


class Workspace:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.todos = []
        self.jobs = {}
        self.seen = {}
        self.checkpoints = []

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


def write_text(path: Path, text: str) -> None:
    mode = path.stat().st_mode & 0o7777 if path.exists() else None
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".arty-tmp", delete=False)
    try:
        with handle:
            handle.write(text)
        if mode is not None:
            os.chmod(handle.name, mode)
        os.replace(handle.name, path)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
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


def rendered_notebook(notebook: dict) -> str:
    cells = [rendered_cell(number, cell) for number, cell in enumerate(notebook["cells"], start=1) if isinstance(cell, dict)]
    return clipped("\n\n".join(cells)) if cells else "The notebook has no cells."


def is_notebook(path: Path) -> bool:
    return path.suffix.lower() == NOTEBOOK_SUFFIX


def read_file(workspace, path, offset=1, limit=DEFAULT_READ_LINES):
    target = workspace.resolve(path)
    if is_notebook(target):
        shown = rendered_notebook(read_notebook(target))
        workspace.note_seen(target, read_text(target))
        return shown
    whole = read_text(target)
    workspace.note_seen(target, whole)
    lines = whole.splitlines()
    start = max(int(offset), 1)
    chosen = lines[start - 1:start - 1 + line_count(limit)]
    if not chosen and lines:
        raise ToolError(f"{path} has only {len(lines)} lines")
    shown, used = [], 0
    for number, line in enumerate(chosen, start=start):
        entry = f"{number}\t{line[:MAX_LINE_CHARS]}"
        if shown and used + len(entry) > MAX_READ_CHARS:
            shown.append(f"... stopped at about {MAX_READ_CHARS} characters; read on with offset={number}")
            return "\n".join(shown)
        shown.append(entry)
        used += len(entry) + 1
    next_line = start + len(chosen)
    lines_left = len(lines) - (next_line - 1)
    if lines_left > 0:
        shown.append(f"... {lines_left} more lines; read on with offset={next_line}")
    return "\n".join(shown)


def line_count(limit) -> int:
    asked = int(limit)
    return asked if asked > 0 else DEFAULT_READ_LINES


WRITTEN_OUT_ESCAPES = {"\\n": "\n", "\\t": "\t", '\\"': '"'}


def unescaped(text: str) -> str:
    for written, real in WRITTEN_OUT_ESCAPES.items():
        text = text.replace(written, real)
    return text


def edited_text(before: str, old_text: str, new_text: str, replace_all: bool) -> str:
    if not old_text:
        raise ToolError("old_text is empty; use write_file to create a file")
    found = before.count(old_text)
    if found == 0 and before.count(unescaped(old_text)) > 0:
        old_text, new_text = unescaped(old_text), unescaped(new_text)
        found = before.count(old_text)
    if found == 0:
        raise ToolError("old_text was not found; read the file again and copy the text exactly")
    if found > 1 and not replace_all:
        raise ToolError(f"old_text is in {found} places; add more surrounding text or set replace_all")
    return before.replace(old_text, new_text)


def multi_edited_text(before: str, edits) -> str:
    if not isinstance(edits, list) or not edits:
        raise ToolError("edits needs at least one {old_text, new_text} change")
    after = before
    for number, edit in enumerate(edits, start=1):
        if not isinstance(edit, dict):
            raise ToolError(f"edit {number} should be an object with old_text and new_text; nothing was changed")
        old_text = in_line_style_of(str(edit.get("old_text", "")), before)
        new_text = in_line_style_of(str(edit.get("new_text", "")), before)
        try:
            after = edited_text(after, old_text, new_text, bool(edit.get("replace_all", False)))
        except ToolError as error:
            raise ToolError(f"edit {number}: {error}; nothing was changed") from None
    return after


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


def write_file(workspace, path, content):
    target, before, after = changed_file(workspace, "write_file", {"path": path, "content": content})
    verb = "Updated" if before else "Created"
    return f"{verb} {workspace.shown(target)} ({len(after.splitlines())} lines)"


def edit_file(workspace, path, old_text, new_text, replace_all=False):
    arguments = {"path": path, "old_text": old_text, "new_text": new_text, "replace_all": replace_all}
    target, _before, _after = changed_file(workspace, "edit_file", arguments)
    return f"Edited {workspace.shown(target)}"


def multi_edit(workspace, path, edits):
    target, _before, _after = changed_file(workspace, "multi_edit", {"path": path, "edits": edits})
    return f"Made {len(edits)} edits to {workspace.shown(target)}"


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


def search(workspace, pattern, path=".", glob="*"):
    try:
        expression = re.compile(pattern)
    except re.error as error:
        raise ToolError(f"bad pattern: {error}") from None
    matches, used = [], 0
    for candidate in project_files(workspace, workspace.resolve(path), [ANY_FOLDERS, "*"]):
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
            if used + len(found) > MAX_OUTPUT_CHARS:
                return "\n".join(matches) + f"\n... stopped at about {MAX_OUTPUT_CHARS} characters of matches"
            matches.append(found)
            used += len(found) + 1
            if len(matches) >= MAX_SEARCH_MATCHES:
                return "\n".join(matches) + f"\n... stopped at {MAX_SEARCH_MATCHES} matches"
    return "\n".join(matches) if matches else f"No matches for {pattern}"


def clipped(output: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(output) <= limit:
        return output
    head = int(limit * OUTPUT_HEAD_SHARE)
    return f"{output[:head]}\n... {len(output) - limit} characters cut ...\n{output[-(limit - head):]}"


def windows_system_program(*parts: str) -> str:
    root = os.environ.get("SystemRoot", "C:\\Windows")
    return "\\".join([root.rstrip("\\"), "System32", *parts])


def windows_powershell() -> str:
    return windows_system_program("WindowsPowerShell", "v1.0", "powershell.exe")


def shell_command(command: str) -> tuple[list[str] | str, dict]:
    if sys.platform == "win32":
        return [windows_powershell(), "-NoProfile", "-NonInteractive", "-Command", command], {}
    bash = "/bin/bash" if Path("/bin/bash").exists() else None
    return command, {"shell": True, "executable": bash, "start_new_session": True}


def stop(process: subprocess.Popen) -> None:
    if sys.platform == "win32":
        subprocess.run([windows_system_program("taskkill.exe"), "/F", "/T", "/PID", str(process.pid)],
                       capture_output=True)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def start_process(workspace, command: str) -> subprocess.Popen:
    arguments, options = shell_command(command)
    return subprocess.Popen(arguments, cwd=workspace.root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", **options)


def start_job(workspace, command: str) -> str:
    job_id = str(len(workspace.jobs) + 1)
    workspace.jobs[job_id] = Job(command, start_process(workspace, command))
    return f"Job {job_id} started in the background. Read it with command_output, end it with stop_command."


def job_of(workspace, job) -> Job:
    found = workspace.jobs.get(str(job))
    if found is None:
        raise ToolError(f"there is no background job {job}")
    return found


def command_output(workspace, job):
    running = job_of(workspace, job)
    return clipped(running.unread()) + f"\n[job {job} {running.state()}]"


def stop_command(workspace, job):
    running = job_of(workspace, job)
    if running.process.poll() is None:
        stop(running.process)
        running.process.wait()
    return clipped(running.unread()) + f"\n[job {job} stopped]"


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


def run_command(workspace, command, timeout_seconds=DEFAULT_COMMAND_SECONDS, background=False):
    if background:
        return start_job(workspace, command)
    seconds = min(max(int(timeout_seconds), 1), MAX_COMMAND_SECONDS)
    process = start_process(workspace, command)
    try:
        output, _ = process.communicate(timeout=seconds)
    except subprocess.TimeoutExpired:
        stop(process)
        output, _ = process.communicate()
        return clipped(output) + f"\n[stopped after {seconds} s]"
    except KeyboardInterrupt:
        stop(process)
        process.wait()
        raise
    return clipped(output) + f"\n[exit code {process.returncode}]"


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
    function("write_file", "Create a file or replace all of it. Prefer edit_file for changes to an existing file.",
             {"path": TEXT, "content": TEXT}, ["path", "content"]),
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
                            "e.g. tests, builds, git. Returns the output and exit code.",
             {"command": TEXT, "timeout_seconds": NUMBER,
              "background": {"type": "boolean", "description": "start it and return at once, e.g. a dev server"}},
             ["command"]),
    function("command_output", "Read new output from a background job started by run_command.",
             {"job": TEXT}, ["job"]),
    function("stop_command", "Stop a background job.", {"job": TEXT}, ["job"]),
    function("todo_write", "Keep a short checklist for a job with several steps; send the whole list each time "
                           "with each item's status (pending, in_progress, done). The user sees it.",
             {"items": {"type": "array", "items": {"type": "object", "properties": {
                 "text": TEXT, "status": {"type": "string", "enum": ["pending", "in_progress", "done"]}},
                 "required": ["text", "status"]}}}, ["items"]),
    function("web_fetch", "Fetch a web page (documentation, an issue, an API reference) as text.",
             {"url": TEXT}, ["url"]),
]


def run(name: str, arguments: dict, workspace: Workspace) -> str:
    handler = HANDLERS.get(name)
    if handler is None:
        return f"Error: there is no tool called {name}"
    try:
        return handler(workspace, **arguments)
    except TypeError as error:
        return f"Error: wrong arguments for {name}: {error}"
    except (ToolError, OSError, ValueError) as error:
        return f"Error: {error}"
