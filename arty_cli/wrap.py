import re
import shutil

DEFAULT_COLUMNS = 80
TERMINAL_ROWS = 24
COLOUR_CODE = re.compile(r"\033\[[0-9;]*m")
SPACE_RUN = re.compile(r"( +)")
FENCE = "```"
LINE_SPACES = " \t"
MOST_HANG_SHARE = 2
CLEAR_TO_LINE_END = "\033[K"


def columns() -> int:
    return shutil.get_terminal_size((DEFAULT_COLUMNS, TERMINAL_ROWS)).columns


def room() -> int:
    return max(columns() - 1, 1)


def visible_length(text: str) -> int:
    return len(COLOUR_CODE.sub("", text))


def leading_spaces(text: str) -> str:
    return text[:len(text) - len(text.lstrip(" "))]


def wrapped_line(text: str, width: int, hang: str | None = None) -> list[str]:
    if visible_length(text) <= width:
        return [text]
    indent = leading_spaces(text)
    hang = indent if hang is None else hang
    if len(hang) * MOST_HANG_SHARE > width:
        hang = ""
    lines, current, gap = [], indent, ""
    for piece in SPACE_RUN.split(text[len(indent):]):
        if not piece:
            continue
        if piece.isspace():
            gap = piece
            continue
        has_words = bool(current.strip())
        if has_words and visible_length(current) + len(gap) + visible_length(piece) > width:
            lines.append(current)
            current, gap = hang, ""
        current += (gap if has_words or current == indent and not lines else "") + piece
        gap = ""
    return lines + [current]


class Flow:
    def __init__(self, width=room):
        self.width = width
        self.reset()

    def reset(self) -> None:
        self.column = 0
        self.word = ""
        self.in_code = False
        self.line_is_blank = True

    def feed(self, text: str) -> str:
        return "".join(self.shown(character) for character in text)

    def shown(self, character: str) -> str:
        if character == "\n":
            self.column, self.word, self.line_is_blank = 0, "", True
            return character
        if character in LINE_SPACES:
            return self.space(character)
        return self.letter(character)

    def space(self, character: str) -> str:
        self.line_is_blank = self.line_is_blank and not self.word
        self.word = ""
        if not self.in_code and self.column >= self.width():
            return ""
        self.column += 1
        return character

    def letter(self, character: str) -> str:
        moved = self.moved_down() if self.overflows() else ""
        self.word += character
        self.column += 1
        if self.line_is_blank and self.word == FENCE:
            self.in_code = not self.in_code
        return moved + character

    def overflows(self) -> bool:
        starts_the_line = self.line_is_blank or self.column == len(self.word)
        return not self.in_code and not starts_the_line and self.column + 1 > self.width()

    def moved_down(self) -> str:
        erased = "\b" * len(self.word) + CLEAR_TO_LINE_END if self.word else ""
        self.column, self.line_is_blank = len(self.word), True
        return f"{erased}\n{self.word}"
