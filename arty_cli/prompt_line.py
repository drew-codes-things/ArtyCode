import math
import os
import re
import shutil
import sys
import time

from arty_cli import permissions

PROMPT_MARK = "› "
MODE_CYCLE = (permissions.DEFAULT, permissions.AUTO_EDIT, permissions.PLAN)
MODE_COLOURS = {permissions.DEFAULT: "dim", permissions.AUTO_EDIT: "accent", permissions.PLAN: "accent",
                permissions.ALLOW_ALL: "error"}
SHIFT_TAB_SEQUENCE = r"\e[Z"
MODE_MARK = "<arty-next-mode>"
CLEAR_ROW_ABOVE = "\033[F\033[2K"
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


def shown_at_once(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def next_mode(mode: str) -> str:
    if mode not in MODE_CYCLE:
        return permissions.DEFAULT
    return MODE_CYCLE[(MODE_CYCLE.index(mode) + 1) % len(MODE_CYCLE)]


def mode_prompt(terminal, mode: str) -> tuple[str, int]:
    shown = f"{terminal.paint(mode, MODE_COLOURS.get(mode, 'accent'))} {PROMPT_MARK}"
    return shown, len(f"{mode} {PROMPT_MARK}")


def for_readline(prompt: str) -> str:
    return COLOUR_CODE.sub("\001\\1\002", prompt)


class ReadlineLine:
    def __init__(self, module, read=None, write=None, columns=None):
        self.module = module
        self.read_input = read or (lambda prompt: input(prompt))
        self.write = write or shown_at_once
        self.columns = columns or (lambda: shutil.get_terminal_size().columns)
        module.parse_and_bind(f'"{SHIFT_TAB_SEQUENCE}": "{MODE_MARK}\\n"')

    def read(self, prompt: str, width: int, kept: str) -> tuple[str, bool]:
        self.module.set_startup_hook((lambda: self.module.insert_text(kept)) if kept else None)
        try:
            line = self.read_input(for_readline(prompt))
        finally:
            self.module.set_startup_hook(None)
        if MODE_MARK not in line:
            return line, False
        rows = max(1, math.ceil((width + len(line)) / max(1, self.columns())))
        self.write(CLEAR_ROW_ABOVE * rows)
        return line.replace(MODE_MARK, ""), True


class ConsoleLine:
    def __init__(self, key, write=None):
        self.key = key
        self.write = write or shown_at_once
        self.history = []

    def read(self, prompt: str, width: int, kept: str) -> tuple[str, bool]:
        self.write(prompt + kept)
        self.typed, self.cursor, self.recalled = list(kept), len(kept), len(self.history)
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
        self.write(NEW_LINE)
        line = "".join(self.typed)
        if line.strip():
            self.history = (self.history + [line])[-MAX_HISTORY:]
        return line, False

    def left_for_new_mode(self, width: int) -> tuple[str, bool]:
        self.write("\r" + " " * (width + len(self.typed)) + "\r")
        return "".join(self.typed), True

    def edit(self, key: str) -> None:
        if key == BACKSPACE and self.cursor > 0:
            self.cursor -= 1
            self.write("\b")
            self.remove_at_cursor()
        elif key.isprintable():
            self.insert(key)

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


def line_reader(is_interactive):
    if not is_interactive():
        return None
    if os.name == "nt":
        return ConsoleLine(console_keys())
    try:
        import readline
    except ImportError:
        return None
    return ReadlineLine(readline)
