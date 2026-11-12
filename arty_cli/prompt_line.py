import math
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass

from arty_cli import permissions, suggest

PROMPT_MARK = "› "
REMOTE_MARK = " · remote"
MODE_CYCLE = (permissions.DEFAULT, permissions.AUTO_EDIT, permissions.PLAN)
MODE_COLOURS = {permissions.DEFAULT: "dim", permissions.AUTO_EDIT: "accent", permissions.PLAN: "accent",
                permissions.ALLOW_ALL: "error"}
SHIFT_TAB_SEQUENCE = r"\e[Z"
MODE_MARK = "<arty-next-mode>"
CLEAR_ROW_ABOVE = "\033[F\033[2K"
UP_A_ROW = "\033[F"
CLEAR_BELOW = "\033[J"
BACK_TO_PROMPT_ROW = "\033[2A\r"
RULE = "─"
BAR_INDENT = "  "
BAR_HINTS = "mode (shift+tab to cycle) · esc to interrupt"
COLOUR_CODE = re.compile(r"(\033\[[0-9;]*m)")
EXTENDED_KEYS = ("\x00", "\xe0")
SHIFT_TAB_CODE = "\x0f"
LEFT, RIGHT, HOME, END, DELETE, UP, DOWN = "K", "M", "G", "O", "S", "H", "P"
ENTER_KEYS = ("\r", "\n")
CTRL_C = "\x03"
END_OF_INPUT_KEYS = ("\x1a", "\x04")
BACKSPACE = "\x08"
NEW_LINE = "\r\n"
MAX_HISTORY = 200
KEY_POLL_SECONDS = 0.03
TAB = "\t"
SLASH = "/"
COMMAND_DELIMITERS = " \t\n"
SLASH_SHOWS_COMMANDS = '"/": "\\C-v/\\t"'
COMPLETION_SETTINGS = ("tab: complete", "set show-all-if-ambiguous on", "set bell-style none", SLASH_SHOWS_COMMANDS)


def shown_at_once(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def next_mode(mode: str) -> str:
    if mode not in MODE_CYCLE:
        return permissions.DEFAULT
    return MODE_CYCLE[(MODE_CYCLE.index(mode) + 1) % len(MODE_CYCLE)]


def mode_prompt(terminal, mode: str, shared: bool = False) -> tuple[str, int]:
    marker = REMOTE_MARK if shared else ""
    shown = f"{terminal.paint(mode, MODE_COLOURS.get(mode, 'accent'))}{terminal.paint(marker, 'accent')} {PROMPT_MARK}"
    return shown, len(f"{mode}{marker} {PROMPT_MARK}")


def terminal_columns() -> int:
    return shutil.get_terminal_size().columns


@dataclass(frozen=True)
class Box:
    rule: str
    bar: str

    def opened(self) -> str:
        return f"{CLEAR_BELOW}{self.rule}{NEW_LINE}{NEW_LINE}{self.rule}{NEW_LINE}{self.bar}{BACK_TO_PROMPT_ROW}"

    def closed(self, rows: int) -> str:
        return UP_A_ROW * (rows + 1) + CLEAR_BELOW


def mode_box(terminal, mode: str, shared: bool = False, columns=terminal_columns) -> Box:
    room = max(columns() - 1, 1)
    bar = f"{BAR_INDENT}{mode} {BAR_HINTS}{REMOTE_MARK if shared else ''}"
    return Box(terminal.paint(RULE * room, "dim"), terminal.paint(bar[:room], "dim"))


def box_prompt(terminal) -> tuple[str, int]:
    return terminal.paint(PROMPT_MARK, "accent"), len(PROMPT_MARK)


def rows_of(width: int, typed: int, columns: int) -> int:
    return max(1, math.ceil((width + typed) / max(1, columns)))


def for_readline(prompt: str) -> str:
    return COLOUR_CODE.sub("\001\\1\002", prompt)


def readline_safe(prompt: str) -> str:
    return for_readline(prompt) if "readline" in sys.modules else prompt


class ReadlineLine:
    def __init__(self, module, read=None, write=None, columns=None, choices=None, models=None):
        self.module = module
        self.read_input = read or (lambda prompt: input(prompt))
        self.write = write or shown_at_once
        self.columns = columns or terminal_columns
        self.choices = choices
        self.models = models
        self.prompt, self.box = "", None
        self.found, self.meanings = [], {}
        module.parse_and_bind(f'"{SHIFT_TAB_SEQUENCE}": "{MODE_MARK}\\n"')
        if choices:
            self.offer_commands()

    def offer_commands(self) -> None:
        for setting in COMPLETION_SETTINGS:
            self.module.parse_and_bind(setting)
        self.module.set_completer_delims(COMMAND_DELIMITERS)
        self.module.set_completer(self.complete)
        self.module.set_completion_display_matches_hook(self.show_matches)

    def complete(self, _text: str, state: int) -> str | None:
        if state == 0:
            self.found, self.meanings, _partial = suggest.suggestions(self.module.get_line_buffer(), self.choices,
                                                                      self.models)
        return self.found[state] if state < len(self.found) else None

    def show_matches(self, _substitution, matches, _longest) -> None:
        lines = suggest.menu_lines(list(matches), self.meanings, self.columns())
        lead, redrawn = ("\r" + UP_A_ROW + CLEAR_BELOW, self.box.opened()) if self.box else ("\n", "")
        self.write(lead + "\n".join(lines) + "\n" + redrawn + self.prompt + self.module.get_line_buffer())

    def read(self, prompt: str, width: int, kept: str, box: Box | None = None) -> tuple[str, bool]:
        self.prompt, self.box = prompt, box
        if box:
            self.write(box.opened())
        self.module.set_startup_hook((lambda: self.module.insert_text(kept)) if kept else None)
        try:
            line = self.read_input(for_readline(prompt))
        except BaseException:
            if box:
                self.write(CLEAR_BELOW)
            raise
        finally:
            self.module.set_startup_hook(None)
        rows = rows_of(width, len(line), self.columns())
        wants_next_mode = MODE_MARK in line
        if box:
            self.write(box.closed(rows) + ("" if wants_next_mode else prompt + line + NEW_LINE))
        elif wants_next_mode:
            self.write(CLEAR_ROW_ABOVE * rows)
        return line.replace(MODE_MARK, ""), wants_next_mode


class ConsoleLine:
    def __init__(self, key, write=None, choices=None, columns=None, models=None):
        self.key = key
        self.write = write or shown_at_once
        self.choices = choices
        self.models = models
        self.columns = columns or terminal_columns
        self.history = []
        self.prompt, self.width, self.box = "", 0, None

    def read(self, prompt: str, width: int, kept: str, box: Box | None = None) -> tuple[str, bool]:
        self.prompt, self.width, self.box = prompt, width, box
        self.write((box.opened() if box else "") + prompt + kept)
        self.typed, self.cursor, self.recalled = list(kept), len(kept), len(self.history)
        try:
            return self.read_keys(width)
        except BaseException:
            if box:
                self.write(CLEAR_BELOW)
            raise

    def rows_to_cursor(self) -> int:
        return rows_of(self.width, self.cursor + 1, self.columns())

    def read_keys(self, width: int) -> tuple[str, bool]:
        while True:
            key = self.key()
            if key in ENTER_KEYS:
                return self.finished()
            if key == CTRL_C:
                self.write(NEW_LINE)
                raise KeyboardInterrupt
            if key in END_OF_INPUT_KEYS and not self.typed:
                self.write(NEW_LINE)
                raise EOFError
            if key in EXTENDED_KEYS:
                code = self.key()
                if code == SHIFT_TAB_CODE:
                    return self.left_for_new_mode(width)
                self.extended(code)
                continue
            self.edit(key)

    def finished(self) -> tuple[str, bool]:
        rows = self.rows_to_cursor()
        self.write(NEW_LINE)
        line = "".join(self.typed)
        if self.box:
            self.write(self.box.closed(rows) + self.prompt + line + NEW_LINE)
        if line.strip():
            self.history = (self.history + [line])[-MAX_HISTORY:]
        return line, False

    def left_for_new_mode(self, width: int) -> tuple[str, bool]:
        if self.box:
            self.write("\r" + UP_A_ROW * self.rows_to_cursor() + CLEAR_BELOW)
        else:
            self.write("\r" + " " * (width + len(self.typed)) + "\r")
        return "".join(self.typed), True

    def edit(self, key: str) -> None:
        if key == BACKSPACE and self.cursor > 0:
            self.cursor -= 1
            self.write("\b")
            self.remove_at_cursor()
        elif key == TAB:
            self.complete()
        elif key.isprintable():
            self.insert(key)
            if key == SLASH and self.typed == [SLASH]:
                self.complete()

    def complete(self) -> None:
        if not self.choices:
            return
        found, meanings, partial = suggest.suggestions("".join(self.typed), self.choices, self.models)
        if len(found) == 1:
            self.move_end()
            for letter in found[0][len(partial):] + " ":
                self.insert(letter)
        elif found:
            self.show_menu(found, meanings)

    def show_menu(self, found: list[str], meanings: dict) -> None:
        lines = suggest.menu_lines(found, meanings, self.columns())
        tail = len(self.typed) - self.cursor
        lead, redrawn = NEW_LINE, ""
        if self.box:
            lead, redrawn = "\r" + UP_A_ROW * self.rows_to_cursor() + CLEAR_BELOW, self.box.opened()
        self.write(lead + NEW_LINE.join(lines) + NEW_LINE + redrawn + self.prompt + "".join(self.typed) + "\b" * tail)

    def extended(self, code: str) -> None:
        moves = {LEFT: self.move_left, RIGHT: self.move_right, HOME: self.move_home, END: self.move_end,
                 DELETE: self.remove_at_cursor, UP: self.older, DOWN: self.newer}
        if code in moves:
            moves[code]()

    def insert(self, key: str) -> None:
        tail = "".join(self.typed[self.cursor:])
        self.typed.insert(self.cursor, key)
        self.cursor += 1
        self.write(key + tail + "\b" * len(tail))

    def remove_at_cursor(self) -> None:
        if self.cursor >= len(self.typed):
            return
        del self.typed[self.cursor]
        tail = "".join(self.typed[self.cursor:])
        self.write(tail + " " + "\b" * (len(tail) + 1))

    def move_left(self) -> None:
        if self.cursor > 0:
            self.cursor -= 1
            self.write("\b")

    def move_right(self) -> None:
        if self.cursor < len(self.typed):
            self.write(self.typed[self.cursor])
            self.cursor += 1

    def move_home(self) -> None:
        self.write("\b" * self.cursor)
        self.cursor = 0

    def move_end(self) -> None:
        self.write("".join(self.typed[self.cursor:]))
        self.cursor = len(self.typed)

    def older(self) -> None:
        if self.recalled > 0:
            self.recalled -= 1
            self.replace_line(self.history[self.recalled])

    def newer(self) -> None:
        if self.recalled < len(self.history):
            self.recalled += 1
            self.replace_line(self.history[self.recalled] if self.recalled < len(self.history) else "")

    def replace_line(self, text: str) -> None:
        old_length = len(self.typed)
        self.move_home()
        padding = max(0, old_length - len(text))
        self.write(text + " " * padding + "\b" * padding)
        self.typed, self.cursor = list(text), len(text)


def polled_keys(has_key, key, sleep=time.sleep):
    def next_key() -> str:
        while not has_key():
            sleep(KEY_POLL_SECONDS)
        return key()

    return next_key


def console_keys():
    import msvcrt
    return polled_keys(msvcrt.kbhit, msvcrt.getwch)


def line_reader(is_interactive, choices=None, models=None):
    if not is_interactive():
        return None
    if os.name == "nt":
        return ConsoleLine(console_keys(), choices=choices, models=models)
    try:
        import readline
    except ImportError:
        return None
    return ReadlineLine(readline, choices=choices, models=models)
