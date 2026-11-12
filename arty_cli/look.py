import functools
import os
import re
from dataclasses import dataclass
from pathlib import Path

from arty_cli import wrap

RESET = "\033[0m"
TRUE_COLOUR_NAMES = ("truecolor", "24bit")
WINDOWS_TERMINAL_MARK = "WT_SESSION"
LIGHT_BACKGROUND_CODES = ("7", "15")
TRUE, PALETTE, BASIC = "true", "256", "basic"
BASIC_TERMINALS = ("linux", "vt100", "vt220", "ansi")
ROLE_CODES = {
    "accent": {TRUE: "38;2;229;56;59", PALETTE: "38;5;167", BASIC: "31"},
    "dim": "2", "bold": "1", "italic": "3", "underline": "4", "reverse": "7",
    "added": "32", "removed": "31", "success": "32", "failure": "31", "warning": "33",
    "code": "36", "keyword": "35", "string": "32", "number": "36", "function": "34", "comment": "2;3",
}
USER_CODES = {
    (TRUE, False): "38;2;236;236;236;48;2;52;52;52", (TRUE, True): "38;2;24;24;24;48;2;228;228;228",
    (PALETTE, False): "38;5;255;48;5;237", (PALETTE, True): "38;5;234;48;5;254",
    (BASIC, False): "7", (BASIC, True): "7",
}
MARK = "◆"
STEP_MARK = "●"
DONE, FAILED, WAITING = "ok", "failed", "waiting"
STATE_ROLES = {DONE: "success", FAILED: "failure"}
RESULT_MARK = "└"
USER_MARK = "› "
HANG = "  "
RESULT_HANG = "    "
CODE_GUTTER = "│ "
QUOTE_GUTTER = "│ "
BULLET = "•"
ELLIPSIS = "…"
HUNK_GAP = "⋮"
BOX = {"top_left": "╭", "top_right": "╮", "bottom_left": "╰", "bottom_right": "╯", "side": "│", "line": "─"}
CARD_PADDING = 1
CARD_LABEL_GAP = 2
FENCE = re.compile(r"^\s*(```+|~~~+)\s*([\w+#.-]*)")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
RULE_LINE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
QUOTE = re.compile(r"^\s*>\s?(.*)$")
BULLET_ITEM = re.compile(r"^(\s*)[-*+]\s+(?:\[([ xX])\]\s+)?(.*)$")
NUMBERED_ITEM = re.compile(r"^(\s*)(\d{1,3}[.)])\s+(.*)$")
CODE_SPAN = re.compile(r"(`+)(.+?)\1")
STRONG = re.compile(r"\*\*(.+?)\*\*|(?<!\w)__(.+?)__(?!\w)")
EMPHASIS = re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])|(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)")
LINK = re.compile(r"\[([^\]]+)\]\((\S+?)\)")
TASK_BOXES = {" ": "☐", "x": "☑", "X": "☑"}
RULE_MOST = 40
HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
DIFF_FILE_LEADS = ("--- ", "+++ ")
DIFF_INDENT = "    "
DIFF_CUT_LEAD = "..."
TOKEN_ROLES = (("Comment", "comment"), ("Literal.String", "string"), ("Literal.Number", "number"),
               ("Keyword", "keyword"), ("Name.Function", "function"), ("Name.Class", "function"),
               ("Name.Builtin", "code"), ("Name.Decorator", "keyword"), ("Operator.Word", "keyword"))
SHORTCUTS = (("/", "commands"), ("@", "attach a file"), ("!", "run a command yourself"), ("#", "save a project note"),
             ("enter", "send, or queue while Arty works"), ("alt+enter", "new line"), ("\\ enter", "new line"),
             ("tab", "complete"), ("shift+tab", "change mode"), ("esc", "stop a request"),
             ("ctrl+o", "the last step in full"), ("ctrl+r", "search earlier requests"),
             ("ctrl+c", "clear, twice to quit"), ("ctrl+d", "quit"))
GIT_FOLDER = ".git"
GIT_LINK = "gitdir:"
HEAD_REFERENCE = "ref: refs/heads/"
SHORT_COMMIT = 7
SHORTCUT_GAP = 4
MOST_SHORTCUT_COLUMNS = 3


@dataclass(frozen=True)
class Theme:
    colour: bool = True
    depth: str = PALETTE
    light: bool = False

    def codes(self, roles) -> str:
        found = []
        for role in roles:
            code = ROLE_CODES[role]
            found.append(code[self.depth] if isinstance(code, dict) else code)
        return ";".join(found)

    def paint(self, text: str, *roles: str) -> str:
        if not self.colour or not text or not roles:
            return text
        return f"\033[{self.codes(roles)}m{text}{RESET}"

    def block(self, text: str, width: int) -> str:
        if not self.colour:
            return text
        padding = " " * max(width - wrap.visible_length(text), 0)
        return f"\033[{USER_CODES[(self.depth, self.light)]}m{text}{padding}{RESET}"


def colour_depth(environ=os.environ) -> str:
    if environ.get("COLORTERM", "").lower() in TRUE_COLOUR_NAMES or environ.get(WINDOWS_TERMINAL_MARK):
        return TRUE
    if environ.get("TERM", "") in BASIC_TERMINALS:
        return BASIC
    return PALETTE


def has_light_background(environ=os.environ) -> bool:
    return environ.get("COLORFGBG", "").rpartition(";")[2] in LIGHT_BACKGROUND_CODES


def theme_for(colour: bool, environ=os.environ) -> Theme:
    return Theme(colour=colour, depth=colour_depth(environ), light=has_light_background(environ))


def clipped(text: str, room: int) -> str:
    if room <= 0:
        return ""
    return text if len(text) <= room else text[:room - 1] + ELLIPSIS


def card(title: str, rows: list[tuple[str, str]], width: int, theme: Theme) -> list[str]:
    label_width = max((len(label) for label, _value in rows), default=0) + CARD_LABEL_GAP
    widest = max([len(title) + 2] + [label_width + len(value) for _label, value in rows])
    inner = max(min(widest, width - 2 - 2 * CARD_PADDING), 1)
    side = theme.paint(BOX["side"], "dim")
    pad = " " * CARD_PADDING
    shown_title = clipped(title, inner - 1)
    top_line = BOX["line"] * max(inner + 2 * CARD_PADDING - len(shown_title) - 3, 0)
    top = (theme.paint(f"{BOX['top_left']}{BOX['line']} ", "dim") + titled(shown_title, theme)
           + theme.paint(f" {top_line}{BOX['top_right']}", "dim"))
    lines = [top]
    for label, value in rows:
        shown_label = clipped(label, inner).ljust(min(label_width, inner))
        shown_value = clipped(value, inner - len(shown_label))
        filler = " " * (inner - len(shown_label) - len(shown_value))
        lines.append(f"{side}{pad}{theme.paint(shown_label, 'dim')}{shown_value}{filler}{pad}{side}")
    lines.append(theme.paint(f"{BOX['bottom_left']}{BOX['line'] * (inner + 2 * CARD_PADDING)}{BOX['bottom_right']}",
                             "dim"))
    return lines


def titled(title: str, theme: Theme) -> str:
    if not title.startswith(MARK):
        return theme.paint(title, "bold")
    name, _space, version = title[len(MARK) + 1:].partition(" ")
    return (theme.paint(MARK, "accent") + " " + theme.paint(name, "bold")
            + (" " + theme.paint(version, "dim") if version else ""))


def user_block(text: str, width: int, theme: Theme) -> list[str]:
    room = max(width - 1, len(USER_MARK) + 1)
    lines = []
    for number, written in enumerate(text.split("\n")):
        lead = USER_MARK if number == 0 else HANG
        lines.extend(wrap.wrapped_line(lead + written, room, HANG))
    return [theme.block(line + " ", room + 1) if theme.colour else line for line in lines]


@dataclass(frozen=True)
class Rendered:
    text: str
    hang: str = ""
    wraps: bool = True


class Markdown:
    def __init__(self, theme: Theme):
        self.theme = theme
        self.fence = ""
        self.language = ""

    def in_code(self) -> bool:
        return bool(self.fence)

    def line(self, source: str) -> Rendered | None:
        fence = FENCE.match(source)
        if fence and (not self.fence or fence.group(1).startswith(self.fence)):
            self.fence, self.language = ("", "") if self.fence else (fence.group(1), fence.group(2))
            return None
        if self.fence:
            return Rendered(self.theme.paint(CODE_GUTTER, "dim") + highlighted(source, self.language, self.theme),
                            wraps=False)
        return self.prose(source)

    def prose(self, source: str) -> Rendered:
        theme = self.theme
        heading = HEADING.match(source)
        if heading:
            roles = ("bold", "underline") if len(heading.group(1)) == 1 else ("bold",)
            return Rendered(inline(heading.group(2), theme, roles))
        if RULE_LINE.match(source):
            return Rendered(theme.paint(BOX["line"] * RULE_MOST, "dim"), wraps=False)
        quote = QUOTE.match(source)
        if quote:
            gutter = theme.paint(QUOTE_GUTTER, "dim")
            return Rendered(gutter + inline(quote.group(1), theme, ("italic",)), QUOTE_GUTTER)
        item = BULLET_ITEM.match(source)
        if item:
            indent, task, text = item.groups()
            mark = TASK_BOXES[task] if task else BULLET
            return Rendered(f"{indent}{theme.paint(mark, 'dim')} {inline(text, theme)}", indent + HANG)
        numbered = NUMBERED_ITEM.match(source)
        if numbered:
            indent, number, text = numbered.groups()
            return Rendered(f"{indent}{theme.paint(number, 'dim')} {inline(text, theme)}",
                            indent + " " * (len(number) + 1))
        return Rendered(inline(source, theme), wrap.leading_spaces(source))


def inline(text: str, theme: Theme, base: tuple = ()) -> str:
    pieces, last = [], 0
    for span in CODE_SPAN.finditer(text):
        pieces.append(styled(text[last:span.start()], theme, base))
        pieces.append(theme.paint(span.group(2), *base, "code"))
        last = span.end()
    pieces.append(styled(text[last:], theme, base))
    return "".join(pieces)


def styled(text: str, theme: Theme, base: tuple) -> str:
    if not text:
        return ""
    for pattern, role in ((LINK, "underline"), (STRONG, "bold"), (EMPHASIS, "italic")):
        found = pattern.search(text)
        if found is None:
            continue
        inner = next(group for group in found.groups() if group is not None)
        shown = theme.paint(inner, *base, role)
        if pattern is LINK:
            shown += theme.paint(f" ({found.group(2)})", *base, "dim")
        return styled(text[:found.start()], theme, base) + shown + styled(text[found.end():], theme, base)
    return theme.paint(text, *base)


@functools.lru_cache(maxsize=32)
def lexer_for(language: str):
    try:
        from pygments.lexers import get_lexer_by_name
        from pygments.util import ClassNotFound
    except ImportError:
        return None
    try:
        return get_lexer_by_name(language, stripnl=False, ensurenl=False)
    except ClassNotFound:
        return None


def token_role(kind) -> str | None:
    name = str(kind).removeprefix("Token.")
    return next((role for prefix, role in TOKEN_ROLES if name == prefix or name.startswith(prefix + ".")), None)


def highlighted(code: str, language: str, theme: Theme) -> str:
    lexer = lexer_for(language.lower()) if theme.colour and language else None
    if lexer is None:
        return theme.paint(code, "code") if theme.colour and not language else code
    parts = []
    for kind, value in lexer.get_tokens(code):
        role = token_role(kind)
        parts.append(theme.paint(value, role) if role else value)
    return "".join(parts).rstrip("\n")


def diff_counts(preview: str) -> tuple[int, int]:
    added = removed = 0
    for line in preview.splitlines():
        if line.startswith(DIFF_FILE_LEADS):
            continue
        added += line.startswith("+")
        removed += line.startswith("-")
    return added, removed


def counts_text(added: int, removed: int, theme: Theme) -> str:
    return f"{theme.paint(f'+{added}', 'added')} {theme.paint(f'-{removed}', 'removed')}"


def numbered_diff(preview: str, theme: Theme, most: int | None = None) -> list[str]:
    rows, old, new, hunks = [], 0, 0, 0
    for line in preview.splitlines():
        hunk = HUNK.match(line)
        if hunk:
            old, new = int(hunk.group(1)), int(hunk.group(2))
            if hunks:
                rows.append((HUNK_GAP, "", ""))
            hunks += 1
        elif line.startswith(DIFF_FILE_LEADS):
            continue
        elif line.startswith("-"):
            rows.append((str(old) if hunks else "", "-" + line[1:], "removed"))
            old += 1
        elif line.startswith("+"):
            rows.append((str(new) if hunks else "", "+" + line[1:], "added"))
            new += 1
        elif line.startswith(DIFF_CUT_LEAD):
            rows.append(("", line, "dim"))
        else:
            rows.append((str(new) if hunks else "", " " + line[1:], ""))
            old, new = old + 1, new + 1
    width = max((len(number) for number, _text, _role in rows), default=0)
    shown = [DIFF_INDENT + theme.paint(number.rjust(width), "dim") + (" " + (theme.paint(text, role) if role else text) if text else "")
             for number, text, role in rows]
    return collapsed(shown, most, theme)[0] if most is not None else shown


def collapsed(lines: list[str], keep: int, theme: Theme, hint: str = "") -> tuple[list[str], int]:
    if len(lines) <= keep + 1:
        return list(lines), 0
    hidden = len(lines) - keep
    more = f"{RESULT_HANG}{ELLIPSIS} +{hidden} lines{f' ({hint})' if hint else ''}"
    return lines[:keep] + [theme.paint(more, "dim")], hidden


def step_line(label: str, state: str, width: int, theme: Theme) -> list[str]:
    role = STATE_ROLES.get(state, "dim")
    first, *more = wrap.wrapped_line(f"{STEP_MARK} {label}", max(width, 1), HANG)
    verb, space, rest = first[len(STEP_MARK) + 1:].partition(" ")
    return [theme.paint(STEP_MARK, role) + " " + theme.paint(verb, "bold") + space + rest, *more]


def result_lines(text: str, width: int, theme: Theme, is_problem: bool = False) -> list[str]:
    shown = wrap.wrapped_line(f"{HANG}{RESULT_MARK} {text}", max(width, 1), RESULT_HANG)
    return [theme.paint(line, "failure" if is_problem else "dim") for line in shown]


def output_lines(text: str, width: int) -> list[str]:
    room = max(width - len(RESULT_HANG), 1)
    rows = []
    for line in text.rstrip("\n").splitlines():
        rows.append(RESULT_HANG + clipped(line.expandtabs(), room))
    return rows


def shortcut_columns(width: int) -> list[list[tuple[str, str]]]:
    key_width = max(len(key) for key, _meaning in SHORTCUTS)
    meaning_width = max(len(meaning) for _key, meaning in SHORTCUTS)
    column_width = key_width + 1 + meaning_width + SHORTCUT_GAP
    count = max(1, min(MOST_SHORTCUT_COLUMNS, (width - len(HANG)) // column_width))
    rows = -(-len(SHORTCUTS) // count)
    return [list(SHORTCUTS[start:start + rows]) for start in range(0, len(SHORTCUTS), rows)]


def git_folder(start: Path) -> Path | None:
    for folder in (start, *start.parents):
        found = folder / GIT_FOLDER
        if found.is_dir():
            return found
        if found.is_file():
            linked = found.read_text(encoding="utf-8", errors="replace").strip()
            if linked.startswith(GIT_LINK):
                return (folder / linked[len(GIT_LINK):].strip()).resolve()
            return None
    return None


def git_branch(root) -> str | None:
    try:
        folder = git_folder(Path(root).resolve())
        head = (folder / "HEAD").read_text(encoding="utf-8", errors="replace").strip() if folder else ""
    except OSError:
        return None
    if head.startswith(HEAD_REFERENCE):
        return head[len(HEAD_REFERENCE):]
    return head[:SHORT_COMMIT] or None
