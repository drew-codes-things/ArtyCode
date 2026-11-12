import ast
import difflib
import io
import re
import shlex
import threading
import tokenize
from dataclasses import dataclass, field
from pathlib import Path

from arty_cli import finish, rules, tools

PYTHON_SUFFIXES = {".py", ".pyi"}
HASH_SUFFIXES = {".sh", ".bash", ".zsh", ".rb", ".pl", ".pm", ".r", ".ps1", ".psm1", ".nim", ".ex", ".exs", ".jl",
                 ".cr"}
C_LIKE_SUFFIXES = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts", ".go", ".rs", ".c", ".h", ".cpp",
                   ".cc", ".cxx", ".hpp", ".hh", ".java", ".kt", ".kts", ".swift", ".cs", ".scala", ".dart", ".php",
                   ".m", ".mm", ".groovy", ".proto", ".css", ".scss", ".less", ".sass", ".zig", ".v", ".sol", ".glsl",
                   ".hlsl"}
PYTHON_DIRECTIVE = re.compile(r"^#\s*(!|-\*-|noqa|type:|pragma|pylint|mypy:|ruff:|flake8:|isort:|fmt:|nosec|"
                              r"pyright:|coding[:=]|SPDX-License-Identifier)")
HASH_DIRECTIVE = re.compile(r"^#\s*(!|shellcheck|frozen_string_literal|rubocop|encoding:|nolint|region|endregion|"
                            r"requires|SPDX-License-Identifier)", re.I)
C_LIKE_DIRECTIVE = re.compile(
    r"^(//|/\*+|#)\s*("
    r"@ts-|eslint|prettier|biome-|istanbul|c8 |v8 |go:|nolint|noinspection|tslint|jshint|jslint|jscs|global |globals |"
    r"cspell|spell-checker|<reference|sourceMappingURL|@__PURE__|@type |@typedef|@ts-check|@jsx|@flow|@refresh|"
    r"#region|#endregion|region\b|endregion\b|swiftlint|clang-format|NOLINT|IWYU|rustfmt|clippy|deno-|webpack|vite-|"
    r"turbo-ignore|prettier-ignore|@license|@preserve|@vite-|coverage:|SPDX-|"
    r"==/?UserScript==|@name\b|@namespace\b|@version\b|@description\b|@author\b|@match\b|@include\b|@exclude\b|"
    r"@grant\b|@connect\b|@run-at\b|@noframes\b|@require\b|@resource\b|@icon\b|@downloadURL\b|@updateURL\b|"
    r"@homepageURL\b|@supportURL\b|@sandbox\b|@antifeature\b|@inject-into\b|@unwrap\b"
    r")",
    re.I,
)
QUOTED = re.compile(r'"(?:\\.|[^"\\\n])*"' r"|'(?:\\.|[^'\\\n])*'" r"|`(?:\\.|[^`\\])*`", re.S)
WEB_ADDRESS_SCHEME = re.compile(r"https?://")
TRAILING_HASH_COMMENT = re.compile(r"(?<![\$\{\\\w])\s#(?!\{)")
LINE_COMMENT, BLOCK_START, BLOCK_END = "//", "/*", "*/"
SHEBANG = "#!"
EM_DASH = "\u2014"
COMMENT, DOCSTRING, EM_DASH_KIND = "comment", "docstring", "em dash"
MAX_SHOWN_CHARS = 120
MAX_COMMENT_SEND_BACKS = 3
EXIT_CODE_AT_THE_END = re.compile(r"\[(?:job \S+ finished with )?exit code (-?\d+)\]\s*$")
STOPPED_AT_THE_END = re.compile(r"\[stopped after \d+ s\b[^\]]*\]\s*$")
NOT_RUN_EXIT_CODES = ("126", "127")
COMMAND_SEPARATORS = re.compile(r"&&|\|\||[;|&\n]")
ENVIRONMENT_SETTING = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
NOT_CHECKING_PROGRAMS = {
    "cd", "pushd", "popd", "echo", "printf", "mkdir", "touch", "cp", "mv", "rm", "ln", "chmod", "ls", "cat", "head",
    "tail", "less", "grep", "rg", "find", "sed", "awk", "wc", "pwd", "which", "true", "false", "sleep", "tee", "sort",
    "uniq", "diff", "tree", "stat", "file", "du", "export", "set", "git", "dir", "type", "write-output",
    "write-host", "get-content", "get-childitem", "select-string", "set-location", "new-item", "remove-item",
}
HITS_LEAD = "Rule check: remove these from the lines you added (the task can't finish while they are there):"
HITS_ADVICE = ("Delete each comment or docstring (one turned into a # comment is still a comment) and say it in "
               "code instead: a clearer name, a smaller function or a named constant; reasons go in the history "
               "entry. Only tool directives may stay (shebangs, encoding lines, lint or type suppressions, license "
               "headers). Replace an em dash with a comma, colon or brackets.")
FINISH_LEAD = "Before you finish (ArtyCode's built-in rules):"
VERIFY_REQUEST = ("You changed files but ran nothing to check them after the last change. Run the project's tests "
                  "now with run_command (or a build, a lint or the program itself) and read the result. If it can't "
                  f"be checked, write {rules.UNVERIFIED} in your report with what would check it.")
HISTORY_REQUEST = (f"Now append one entry for this task to the end of {rules.HISTORY.as_posix()}: create it with "
                   "write_file if it is missing, otherwise read it and add the entry after its last line with "
                   "edit_file. Never change earlier entries. The shape:\n{template}")
FAILING_LEAD = "Your last test run still fails:"
FAILING_REQUEST = (FAILING_LEAD + " {summary}\nFix the code and run the tests again. If they can't pass, write "
                   f"{rules.UNVERIFIED} in your report and say why.")
REPORT_REMINDER = (f"Then finish with your report: Outcome (DONE, PARTIAL, BLOCKED or {rules.UNVERIFIED}), Changed, "
                   "Verification (each command and its result), Assumptions, Open items.")
VERIFY_STEP, HISTORY_STEP = "verify", "history"
SENT_BACK_NOTE = "Rule check: sent back to"
LEFT_IN_NOTE = "Rule check: still in the lines added:"


@dataclass(frozen=True)
class Hit:
    line: int
    kind: str
    text: str


@dataclass
class Turn:
    history_before: str | None
    baselines: dict = field(default_factory=dict)
    steps: int = 0
    changed_at: int = 0
    checked_at: int = 0
    comment_send_backs: int = 0
    failing_run: finish.TestRun | None = None
    asked: set = field(default_factory=set)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def next_step(self) -> int:
        self.steps += 1
        return self.steps


@dataclass(frozen=True)
class Watched:
    name: str
    command: str
    path: Path | None
    before: str | None


def shortened(text: str) -> str:
    stripped = text.strip()
    return stripped if len(stripped) <= MAX_SHOWN_CHARS else stripped[:MAX_SHOWN_CHARS - 3] + "..."


def python_token_hits(text: str) -> list[Hit]:
    found = []
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type != tokenize.COMMENT:
            continue
        is_shebang = token.start[0] <= 2 and token.string.startswith(SHEBANG)
        if is_shebang or PYTHON_DIRECTIVE.match(token.string):
            continue
        found.append(Hit(token.start[0], COMMENT, token.line.strip() or token.string))
    return found


def docstring_hits(text: str) -> list[Hit]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    documented = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    return [Hit(node.body[0].lineno, DOCSTRING, ast.get_docstring(node, clean=False) or "")
            for node in ast.walk(tree)
            if isinstance(node, documented) and node.body and ast.get_docstring(node, clean=False) is not None]


def python_hits(text: str) -> list[Hit]:
    try:
        comments = python_token_hits(text)
    except (tokenize.TokenError, SyntaxError):
        return hash_hits(text)
    return comments + docstring_hits(text)


def hash_hits(text: str) -> list[Hit]:
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            allowed = (number == 1 and stripped.startswith(SHEBANG)) or HASH_DIRECTIVE.match(stripped)
            if not allowed:
                found.append(Hit(number, COMMENT, stripped))
            continue
        if TRAILING_HASH_COMMENT.search(QUOTED.sub('""', line)):
            found.append(Hit(number, COMMENT, stripped))
    return found


def opens_block(fragment: str) -> bool:
    return fragment.startswith(BLOCK_START) and BLOCK_END not in fragment


def c_like_line_hit(line: str) -> tuple[bool, bool]:
    stripped = line.strip()
    if stripped.startswith((LINE_COMMENT, BLOCK_START)):
        return not C_LIKE_DIRECTIVE.match(stripped), opens_block(stripped)
    cleaned = WEB_ADDRESS_SCHEME.sub("", QUOTED.sub('""', line))
    line_at, block_at = cleaned.find(LINE_COMMENT), cleaned.find(BLOCK_START)
    if line_at >= 0 and cleaned[:line_at].count("`") % 2 == 0 and not C_LIKE_DIRECTIVE.match(cleaned[line_at:].strip()):
        return True, False
    if block_at >= 0 and not C_LIKE_DIRECTIVE.match(cleaned[block_at:].strip()):
        return True, BLOCK_END not in cleaned[block_at:]
    return False, False


def c_like_hits(text: str) -> list[Hit]:
    found, in_block = [], False
    for number, line in enumerate(text.splitlines(), 1):
        if in_block:
            found.append(Hit(number, COMMENT, line.strip()))
            in_block = BLOCK_END not in line
            continue
        is_comment, in_block = c_like_line_hit(line)
        if is_comment:
            found.append(Hit(number, COMMENT, line.strip()))
    return found


def comment_hits(path: Path, text: str) -> list[Hit]:
    suffix = Path(path).suffix.lower()
    if suffix in PYTHON_SUFFIXES:
        return python_hits(text)
    if suffix in HASH_SUFFIXES:
        return hash_hits(text)
    if suffix in C_LIKE_SUFFIXES:
        return c_like_hits(text)
    return []


def em_dash_hits(text: str) -> list[Hit]:
    return [Hit(number, EM_DASH_KIND, line.strip()) for number, line in enumerate(text.splitlines(), 1)
            if EM_DASH in line]


def added_line_numbers(before: str, after: str) -> set[int]:
    matcher = difflib.SequenceMatcher(None, before.splitlines(), after.splitlines(), autojunk=False)
    return {number + 1 for tag, _start, _end, added_from, added_to in matcher.get_opcodes()
            if tag in ("replace", "insert") for number in range(added_from, added_to)}


def added_hits(path: Path, before: str, after: str) -> list[Hit]:
    added = added_line_numbers(before, after)
    if not added:
        return []
    found = [hit for hit in comment_hits(path, after) + em_dash_hits(after) if hit.line in added]
    return sorted(found, key=lambda hit: (hit.line, hit.kind))


def hits_note(workspace, found: dict) -> str:
    listed = [f"{workspace.shown(path)}:{hit.line}: {hit.kind}: {shortened(hit.text)}"
              for path, hits in found.items() for hit in hits]
    return "\n".join([HITS_LEAD, *listed, HITS_ADVICE])


def program_of(part: str) -> str:
    try:
        words = shlex.split(part)
    except ValueError:
        words = part.split()
    words = [word for word in words if not ENVIRONMENT_SETTING.match(word)]
    return Path(words[0]).name.lower() if words else ""


def is_checking_command(command: str) -> bool:
    programs = [program_of(part) for part in COMMAND_SEPARATORS.split(command or "")]
    return any(program and program not in NOT_CHECKING_PROGRAMS for program in programs)


def text_or_none(path: Path) -> str | None:
    try:
        return tools.read_text(path)
    except (tools.ToolError, OSError):
        return None


def current_turn(workspace) -> Turn | None:
    if not rules.checked(workspace):
        return None
    turn = workspace.rules.turn
    return turn if isinstance(turn, Turn) else None


def start_turn(workspace) -> None:
    if rules.checked(workspace):
        workspace.rules.turn = Turn(text_or_none(workspace.root / rules.HISTORY))


def watch(workspace, name: str, arguments: dict) -> Watched | None:
    if current_turn(workspace) is None:
        return None
    if name == "run_command":
        return Watched(name, str(arguments.get("command") or ""), None, None)
    if name == "command_output":
        job = workspace.jobs.get(str(arguments.get("job")))
        return Watched(name, job.command if job is not None else "", None, None)
    if name not in tools.CHANGING_TOOLS:
        return None
    try:
        path = workspace.resolve(str(arguments.get("path") or ""))
    except (tools.ToolError, OSError, ValueError):
        return None
    return Watched(name, "", path, text_or_none(path))


def ran_to_the_end(result: str) -> bool:
    ended = EXIT_CODE_AT_THE_END.search(result)
    if ended is not None:
        return ended.group(1) not in NOT_RUN_EXIT_CODES
    return STOPPED_AT_THE_END.search(result) is not None


def after_command(turn: Turn, watched: Watched, result: str) -> None:
    if not (is_checking_command(watched.command) and ran_to_the_end(result)):
        return
    run = finish.test_run_of(watched.command, result)
    with turn.lock:
        turn.failing_run = run if run is not None and run.failed else None
        if turn.failing_run is None:
            turn.checked_at = turn.next_step()


def checked_result(workspace, watched: Watched | None, result: str) -> str:
    turn = current_turn(workspace)
    if turn is None or watched is None:
        return result
    if watched.path is None:
        after_command(turn, watched, result)
        return result
    after = text_or_none(watched.path)
    if after is None or after == watched.before:
        return result
    with turn.lock:
        baseline = turn.baselines.setdefault(watched.path, watched.before or "")
        step = turn.next_step()
        if not rules.is_record_path(workspace, watched.path):
            turn.changed_at = step
    hits = added_hits(watched.path, baseline, after)
    return f"{result}\n\n{hits_note(workspace, {watched.path: hits})}" if hits else result


def outstanding_hits(turn: Turn) -> dict:
    found = {}
    for path, baseline in list(turn.baselines.items()):
        current = text_or_none(path)
        hits = added_hits(path, baseline, current) if current is not None else []
        if hits:
            found[path] = hits
    return found


def history_appended(workspace, turn: Turn) -> bool:
    now = text_or_none(workspace.root / rules.HISTORY) or ""
    before = turn.history_before or ""
    return len(now.strip()) > len(before.strip()) and now.startswith(before)


def verify_request(workspace, turn: Turn) -> str:
    if turn.failing_run is None:
        return VERIFY_REQUEST
    return "" if finish.is_on(workspace) else FAILING_REQUEST.format(summary=turn.failing_run.summary)


def missing_steps(workspace, turn: Turn, answer: str) -> list[tuple[str, str]]:
    missing = []
    request = verify_request(workspace, turn)
    if turn.checked_at <= turn.changed_at and rules.UNVERIFIED not in answer and request:
        missing.append((VERIFY_STEP, request))
    if not history_appended(workspace, turn):
        missing.append((HISTORY_STEP, HISTORY_REQUEST.format(template=rules.history_template(rules.today_text()))))
    return [(step, request) for step, request in missing if step not in turn.asked]


def show(screen, line: str) -> None:
    shown = getattr(screen, "show_activity", None)
    if shown is not None:
        shown(line)


def finish_reasons(workspace, turn: Turn, answer: str, screen) -> list[str]:
    reasons = []
    outstanding = outstanding_hits(turn)
    if outstanding and turn.comment_send_backs < MAX_COMMENT_SEND_BACKS:
        turn.comment_send_backs += 1
        reasons.append(hits_note(workspace, outstanding))
    elif outstanding:
        show(screen, f"{LEFT_IN_NOTE} {', '.join(workspace.shown(path) for path in outstanding)}")
    missing = missing_steps(workspace, turn, answer)
    if missing:
        step, request = missing[0]
        turn.asked.add(step)
        reasons.append(request)
    return reasons


def sent_back(workspace, messages: list[dict], answer: str, screen=None) -> bool:
    turn = current_turn(workspace)
    if turn is None or not turn.changed_at:
        return False
    reasons = finish_reasons(workspace, turn, answer, screen)
    if not reasons:
        return False
    show(screen, f"{SENT_BACK_NOTE} {', '.join(sent_for(reason) for reason in reasons)}")
    messages.append({"role": "user", "content": "\n\n".join([FINISH_LEAD, *reasons, REPORT_REMINDER])})
    return True


def sent_for(reason: str) -> str:
    if reason.startswith(HITS_LEAD):
        return "remove comments or em dashes"
    if reason.startswith(FAILING_LEAD):
        return "fix the failing tests"
    return "check the work" if reason == VERIFY_REQUEST else "write the history entry"
