import contextlib
import contextvars
import hashlib
import os
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import Completer, Completion, ThreadedCompleter
from prompt_toolkit.filters import Condition, is_searching
from prompt_toolkit.data_structures import Point
from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from prompt_toolkit.history import FileHistory, InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout.containers import ConditionalContainer, HSplit, VSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl, UIContent, UIControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.layout.processors import Processor, Transformation
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import SearchToolbar

from arty_cli import context, look, permissions, prompt_line, remote, suggest, tools, waiting, wrap

HISTORY_FOLDER = "history"
PROJECT_KEY_CHARS = 16
PRIVATE_FILE_MODE = 0o600
PIECES = re.compile(r"(\n|\x08|\r|\033\[K)")
IGNORED_PIECES = ("\r", wrap.CLEAR_TO_LINE_END)
MENTION = re.compile(r"(?:^|\s)@(\S*)$")
MOST_PATHS = 50
MENU_ROWS = 8
CHOICE_ROWS = 10
REFRESH_SECONDS = 0.15
POLL_SECONDS = 0.2
START_SECONDS = 5.0
WHEEL_ROWS = 3
PAGE_OVERLAP_ROWS = 2
TAB_STOP = 8
MAX_KEPT_ENTRIES = 10_000
ZERO_WIDTH_ESCAPE = "[ZeroWidthEscape]"
SCROLLED_HINT = "  ↓ {} more lines below · pgdn or the mouse wheel to scroll down"
JOIN_SECONDS = 2.0
QUIT_SECONDS = 2.0
KEY_PREFIX_SECONDS = 0.15
ESCAPE_SECONDS = 0.05
FRAMES_PER_SECOND = 8
REFUSAL_KEY = "n"
ANSWER_PAUSE_SECONDS = 0.5
LINE_CONTINUES = "\\"
QUEUED_LEAD = "  queued: "
POINTER = "❯"
QUIT_HINT = "  Press Ctrl-C again to quit"
CHOICE_HINT = "  ↑↓ or its letter to choose · enter to confirm · esc to refuse"
LINE_HINT = "  enter to answer · esc to skip"
SHORTCUTS_HINT = "  enter for every command (/help) · esc or ? to close"
PLACEHOLDER = "Ask Arty to fix, explain or build something"
SHORT_PLACEHOLDER = "Ask Arty anything"
SHORT_CHOICE_HINT = "  ↑↓ · enter to confirm · esc to refuse"
SHORT_SHORTCUTS_HINT = "  enter for /help · esc to close"
CHOICE_HANG = 4
SHORTEST_LABEL = 16
BUSY_PLACEHOLDER = "Type your next message; it waits until Arty finishes"
SHORTCUTS_MARK = "?"
SHORTCUTS_LABEL = "? for shortcuts"
HELP_COMMAND = "/help"
MODE_HINT = "(shift+tab to cycle)"
CONTEXT_LEFT = "{}% context left"
CONTEXT_USED = "{} / {}"
BRANCH_MARK = "⎇ "
FOOTER_GAP = "  "
JOIN = " · "
QUESTION_TITLE = " Arty asks "
PANEL_PADDING = 1
PANEL_LEAST_ROWS = 4
ACCENT = "#e5383b"
MODE_STYLES = {permissions.DEFAULT: "class:dim", permissions.AUTO_EDIT: "class:accent", permissions.PLAN: "class:accent",
               permissions.ALLOW_ALL: "class:warning"}
STYLE = Style.from_dict({"rule": "fg:ansibrightblack", "dim": "fg:ansibrightblack", "prompt": f"fg:{ACCENT} bold",
                         "question": "bold", "selected": "reverse", "meaning": "fg:ansibrightblack",
                         "accent": f"fg:{ACCENT}", "border": "fg:ansibrightblack", "panel-border": f"fg:{ACCENT}",
                         "placeholder": "fg:ansibrightblack italic", "spinner": f"fg:{ACCENT} bold",
                         "status-label": "bold", "key": "bold", "warning": "fg:ansiyellow bold"})


class InterfaceError(Exception):
    pass


class Scroll:
    def __init__(self, commit, changed=lambda: None):
        self.commit = commit
        self.changed = changed
        self.pending = ""
        self.last_line = None
        self.lock = threading.Lock()

    def isatty(self) -> bool:
        return True

    def write(self, text: str) -> int:
        with self.lock:
            for piece in PIECES.split(text):
                if piece == "\n":
                    self.commit(self.pending)
                    self.last_line = self.pending
                    self.pending = ""
                elif piece == "\b":
                    self.pending = self.pending[:-1]
                elif piece not in IGNORED_PIECES:
                    self.pending += piece
        self.changed()
        return len(text)

    def flush(self) -> None:
        pass

    def finish(self) -> None:
        if self.pending:
            self.write("\n")


def history_path(personal_folder: Path, project_root: Path) -> Path:
    key = hashlib.sha256(str(Path(project_root).resolve()).encode()).hexdigest()[:PROJECT_KEY_CHARS]
    path = Path(personal_folder) / HISTORY_FOLDER / f"{key}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, PRIVATE_FILE_MODE))
    return path


def is_inside(folder: Path, root: Path) -> bool:
    return folder == root or root in folder.parents


def project_paths(root: Path, partial: str) -> list[str]:
    inside, _slash, start = partial.rpartition("/")
    folder = (root / inside).resolve() if inside else root
    if not is_inside(folder, root) or not folder.is_dir():
        return []
    found = []
    for entry in sorted(folder.iterdir(), key=lambda entry: entry.name):
        hidden = entry.name.startswith(".") and not start.startswith(".")
        if hidden or entry.name in tools.SKIPPED_FOLDERS or not entry.name.startswith(start):
            continue
        shown = f"{inside}/{entry.name}" if inside else entry.name
        found.append(shown + "/" if entry.is_dir() else shown)
    return found[:MOST_PATHS]


class Suggestions(Completer):
    def __init__(self, host):
        self.host = host

    def get_completions(self, document, complete_event):
        typed = document.text_before_cursor
        if typed.startswith("/") and "\n" not in document.text:
            found, meanings, partial = suggest.suggestions(typed, self.host.commands, self.host.models)
            for name in found:
                yield Completion(name, start_position=-len(partial), display_meta=meanings.get(name) or "")
            return
        mention = MENTION.search(typed)
        if mention is None:
            return
        for path in project_paths(Path(self.host.root), mention.group(1)):
            yield Completion(f"@{path}", start_position=-(len(mention.group(1)) + 1))


@dataclass
class Question:
    text: str
    choices: list
    opened_at: float = 0.0
    selected: int | None = None
    heeding: bool = False
    answer: str | None = None
    answered: threading.Event = field(default_factory=threading.Event)

    def keys(self) -> list[str]:
        return [key for key, _meaning in self.choices]

    def settle(self, answer: str) -> None:
        self.answer = answer
        self.answered.set()


@dataclass(frozen=True)
class Status:
    label: str
    started_at: float
    tokens: object


class Answers:
    def __init__(self, interface):
        self.interface = interface

    def line(self, seconds: float) -> str | None:
        return self.interface.wait_answer(seconds)

    def discard(self) -> None:
        pass


def nothing_written(_text: str) -> None:
    pass


def window_around(selected: int, count: int, rows: int) -> range:
    start = min(max(selected - rows + 1, 0), max(count - rows, 0))
    return range(start, min(start + rows, count))


def with_text_added(row: list, style: str, text: str) -> None:
    if row and row[-1][0] == style:
        row[-1] = (style, row[-1][1] + text)
    else:
        row.append((style, text))


def screen_rows(line: str, width: int) -> list[list]:
    width = max(width, 1)
    rows, row, used = [], [], 0
    for style, text, *_rest in to_formatted_text(ANSI(line)):
        if ZERO_WIDTH_ESCAPE in style:
            continue
        for character in text:
            shown = " " * (TAB_STOP - used % TAB_STOP) if character == "\t" else character
            size = len(shown) if character == "\t" else get_cwidth(character)
            if row and used + size > width:
                rows.append(row)
                row, used = [], 0
            with_text_added(row, style, shown)
            used += size
    rows.append(row)
    return rows


class Transcript:
    def __init__(self):
        self.entries = []
        self.lock = threading.Lock()
        self.width = None
        self.rows = []
        self.entry_rows = []
        self.drawn = 0
        self.top = None
        self.height = 1
        self.total = 0

    def add_line(self, line: str) -> None:
        with self.lock:
            self.entries.append(line)
            self.forget_the_oldest()

    def add_block(self, render) -> None:
        with self.lock:
            self.entries.append(render)
            self.forget_the_oldest()

    def forget_the_oldest(self) -> None:
        extra = len(self.entries) - MAX_KEPT_ENTRIES
        if extra <= 0:
            return
        del self.entries[:extra]
        drawn_gone = min(extra, self.drawn)
        rows_gone = sum(self.entry_rows[:drawn_gone])
        del self.entry_rows[:drawn_gone]
        del self.rows[:rows_gone]
        self.drawn -= drawn_gone
        if self.top is not None:
            self.top = max(self.top - rows_gone, 0)

    def rows_for(self, width: int) -> list[list]:
        with self.lock:
            if width != self.width:
                self.width, self.rows, self.entry_rows, self.drawn = width, [], [], 0
            for entry in self.entries[self.drawn:]:
                made = [row for line in (entry(width) if callable(entry) else [entry])
                        for row in screen_rows(line, width)]
                self.rows.extend(made)
                self.entry_rows.append(len(made))
            self.drawn = len(self.entries)
            return list(self.rows)

    def last_top(self) -> int:
        return max(self.total - self.height, 0)

    def shown(self, rows: list[list], height: int) -> list[list]:
        self.height, self.total = max(height, 1), len(rows)
        if self.top is not None and self.top >= self.last_top():
            self.top = None
        top = self.last_top() if self.top is None else self.top
        return rows[top:top + self.height]

    def scroll(self, step: int) -> None:
        current = self.last_top() if self.top is None else self.top
        wanted = min(max(current + step, 0), self.last_top())
        self.top = None if wanted >= self.last_top() else wanted

    def page(self, direction: int) -> None:
        self.scroll(direction * max(self.height - PAGE_OVERLAP_ROWS, 1))

    def follow(self) -> None:
        self.top = None

    def is_scrolled(self) -> bool:
        return self.top is not None

    def rows_below(self) -> int:
        return 0 if self.top is None else max(self.total - self.top - self.height, 0)


class TranscriptControl(UIControl):
    def __init__(self, transcript: Transcript, live_lines):
        self.transcript = transcript
        self.live_lines = live_lines

    def create_content(self, width: int, height: int) -> UIContent:
        rows = self.transcript.rows_for(width)
        for line in self.live_lines():
            rows.extend(screen_rows(line, width))
        shown = self.transcript.shown(rows, height)
        return UIContent(get_line=lambda number: shown[number], line_count=len(shown),
                         cursor_position=Point(x=0, y=max(len(shown) - 1, 0)), show_cursor=False)

    def mouse_handler(self, mouse_event):
        if mouse_event.event_type == MouseEventType.SCROLL_UP:
            self.transcript.scroll(-WHEEL_ROWS)
            return None
        if mouse_event.event_type == MouseEventType.SCROLL_DOWN:
            self.transcript.scroll(WHEEL_ROWS)
            return None
        return NotImplemented


class Placeholder(Processor):
    def __init__(self, text):
        self.text = text

    def apply_transformation(self, transformation_input):
        fragments = transformation_input.fragments
        if transformation_input.document.text or transformation_input.lineno:
            return Transformation(fragments)
        wanted = self.text()
        room = max(transformation_input.width - 1, 0)
        shown = wanted if len(wanted) <= room else SHORT_PLACEHOLDER if len(SHORT_PLACEHOLDER) <= room else ""
        return Transformation(fragments + [("class:placeholder", shown)])


def fitted_hint(full: str, short: str, room: int) -> str:
    return full if len(full) <= room else short[:room]


def status_parts(frame: int, seconds: float, label: str, tokens: int, room: int) -> tuple[str, str, str]:
    spinner = waiting.FRAMES[frame % len(waiting.FRAMES)]
    elapsed = waiting.elapsed_text(seconds)
    counted = f" · {waiting.TOKEN_MARK} {waiting.tokens_text(tokens)} tokens" if tokens > 0 else ""
    choices = (f" ({elapsed}{counted} · {waiting.STOP_HINT})", f" ({elapsed} · {waiting.STOP_HINT})", f" ({elapsed})")
    space = room - len(spinner) - 1
    for details in choices:
        if space - len(details) >= min(len(label), SHORTEST_LABEL):
            return spinner, look.clipped(label, space - len(details)), details
    return spinner, look.clipped(label, space - len(choices[-1])), choices[-1]


def context_used_text(room: context.ContextRoom) -> str:
    return CONTEXT_USED.format(waiting.tokens_text(room.used), waiting.tokens_text(room.limit))


def footer_pieces(mode: str, shared: bool, model: str, branch: str, room: context.ContextRoom | None, width: int):
    mode_part = [(MODE_STYLES.get(mode, "class:accent"), f"{mode} mode")]
    hint = [("class:dim", f" {MODE_HINT}")]
    remote_part = [("class:accent", prompt_line.REMOTE_MARK)] if shared else []
    shortcuts = [("class:dim", JOIN + SHORTCUTS_LABEL)]
    context_part = [("class:dim", CONTEXT_LEFT.format(room.left_percent))] if room else []
    used_part = [("class:dim", context_used_text(room))] if room else []
    branch_part = [("class:dim", BRANCH_MARK + branch)] if branch else []
    model_part = [("class:dim", model)] if model else []
    optional = {"model": model_part, "branch": branch_part, "used": used_part, "shortcuts": shortcuts, "hint": hint}
    for dropped in (None, "hint", "model", "branch", "used", "shortcuts"):
        if dropped:
            optional[dropped] = []
        left = [("", prompt_line.BAR_INDENT), *mode_part, *optional["hint"], *remote_part, *optional["shortcuts"]]
        right = joined([optional["model"], optional["branch"], optional["used"], context_part])
        if fragment_width(left) + len(FOOTER_GAP) + fragment_width(right) <= width:
            return left, right
    return left, []


def joined(parts: list[list]) -> list:
    pieces = []
    for part in (part for part in parts if part):
        if pieces:
            pieces.append(("class:dim", JOIN))
        pieces.extend(part)
    return pieces


def fragment_width(fragments) -> int:
    return sum(len(text) for _style, text, *_rest in fragments)


def panel_lines(title: str, body: list, width: int) -> list:
    inner = max(width - 2 - 2 * PANEL_PADDING, 1)
    head = look.clipped(QUESTION_TITLE, inner)
    rows = [[("class:panel-border", f"{look.BOX['top_left']}{look.BOX['line']}{head}"
              f"{look.BOX['line'] * max(inner + 2 * PANEL_PADDING - len(head) - 1, 0)}{look.BOX['top_right']}")]]
    pad = " " * PANEL_PADDING
    for row in body:
        used = fragment_width(row)
        clipped_row = row if used <= inner else clipped_fragments(row, inner)
        filler = " " * max(inner - fragment_width(clipped_row), 0)
        rows.append([("class:panel-border", look.BOX["side"]), ("", pad), *clipped_row, ("", filler + pad),
                     ("class:panel-border", look.BOX["side"])])
    rows.append([("class:panel-border",
                  f"{look.BOX['bottom_left']}{look.BOX['line'] * (inner + 2 * PANEL_PADDING)}{look.BOX['bottom_right']}")])
    return rows


def clipped_fragments(fragments, room: int) -> list:
    kept, used = [], 0
    for style, text, *_rest in fragments:
        if used + len(text) >= room:
            kept.append((style, text[:max(room - used - 1, 0)] + look.ELLIPSIS))
            return kept
        kept.append((style, text))
        used += len(text)
    return kept


def lines_to_fragments(rows: list) -> list:
    fragments = []
    for number, row in enumerate(rows):
        if number:
            fragments.append(("", "\n"))
        fragments.extend(row)
    return fragments


class Interface:
    def __init__(self, host, history_file: Path | None = None, commit=None, interrupt=remote.interrupt_main_thread,
                 clock=time.monotonic):
        self.host = host
        self.history_file = history_file
        self.interrupt = interrupt
        self.clock = clock
        self.messages = queue.Queue()
        self.queued = []
        self.lock = threading.Lock()
        self.busy = False
        self.leaving = False
        self.status = None
        self.question = None
        self.wants_line = False
        self.draft = ""
        self.quit_armed_at = None
        self.last_key_at = None
        self.failure = None
        self.started = threading.Event()
        self.commit_line = commit
        self.transcript = Transcript()
        self.scroll = Scroll(self.committed, self.redraw)
        self.stray = Scroll(self.committed, self.redraw)
        self.partial = ""
        self.shortcuts_open = False
        self.app = None
        self.thread = None

    def committed(self, line: str) -> None:
        self.transcript.add_line(line)
        if self.commit_line is not None:
            self.commit_line(line)

    def show_block(self, render) -> None:
        self.scroll.finish()
        self.transcript.add_block(render)
        self.redraw()

    def redraw(self) -> None:
        if self.app is not None and self.app.is_running:
            self.app.invalidate()

    def in_loop(self, action) -> None:
        if self.app is not None and self.app.is_running:
            self.app.loop.call_soon_threadsafe(action)

    def build(self) -> None:
        history = FileHistory(str(self.history_file)) if self.history_file else InMemoryHistory()
        self.buffer = Buffer(multiline=True, completer=ThreadedCompleter(Suggestions(self.host)),
                             complete_while_typing=True, history=history)
        self.buffer.on_text_changed += lambda _buffer: self.close_shortcuts()
        search = SearchToolbar()
        typed = BufferControl(buffer=self.buffer, search_buffer_control=search.control,
                              input_processors=[Placeholder(self.placeholder)])
        menu_open = Condition(self.menu_is_open)
        choosing = Condition(self.is_choosing)
        shortcuts_open = Condition(lambda: self.shortcuts_open)
        rows = [
            Window(TranscriptControl(self.transcript, self.live_lines)),
            self.shown_when(self.scrolled_text, self.transcript.is_scrolled),
            self.shown_when(self.queued_text, lambda: bool(self.queued)),
            self.shown_when(self.status_text, lambda: self.status is not None),
            self.shown_when(self.question_text, lambda: self.question is not None, wraps=True),
            ConditionalContainer(self.input_box(typed), filter=~choosing),
            ConditionalContainer(Window(FormattedTextControl(self.menu_text), dont_extend_height=True),
                                 filter=menu_open),
            ConditionalContainer(Window(FormattedTextControl(self.shortcuts_text), dont_extend_height=True),
                                 filter=shortcuts_open & ~menu_open),
            search,
            ConditionalContainer(Window(FormattedTextControl(self.bar_text), height=1),
                                 filter=~menu_open & ~is_searching),
        ]
        self.app = Application(layout=Layout(HSplit(rows), focused_element=typed), key_bindings=self.bindings(),
                               style=STYLE, full_screen=True, mouse_support=True,
                               enable_page_navigation_bindings=False, refresh_interval=REFRESH_SECONDS)
        self.app.timeoutlen = KEY_PREFIX_SECONDS
        self.app.ttimeoutlen = ESCAPE_SECONDS
        self.app.key_processor.after_key_press += self.note_key_press

    def note_key_press(self, _processor) -> None:
        self.last_key_at = self.clock()

    def input_box(self, typed):
        side = look.BOX["side"]
        edge = "class:border"
        return HSplit([
            VSplit([Window(char=look.BOX["top_left"], width=1, height=1, style=edge),
                    Window(char=look.BOX["line"], height=1, style=edge),
                    Window(char=look.BOX["top_right"], width=1, height=1, style=edge)]),
            VSplit([Window(char=side, width=1, style=edge),
                    Window(FormattedTextControl(self.prompt_text), width=len(prompt_line.PROMPT_MARK) + 1,
                           dont_extend_height=True),
                    Window(typed, wrap_lines=True, dont_extend_height=True, height=Dimension(min=1)),
                    Window(width=1),
                    Window(char=side, width=1, style=edge)]),
            VSplit([Window(char=look.BOX["bottom_left"], width=1, height=1, style=edge),
                    Window(char=look.BOX["line"], height=1, style=edge),
                    Window(char=look.BOX["bottom_right"], width=1, height=1, style=edge)]),
        ])

    def shown_when(self, text, wanted, wraps: bool = False):
        return ConditionalContainer(Window(FormattedTextControl(text), wrap_lines=wraps, dont_extend_height=True),
                                    filter=Condition(wanted))

    def bindings(self) -> KeyBindings:
        keys = KeyBindings()
        asking = Condition(self.is_choosing)
        typing = ~is_searching & ~asking
        menu_open = Condition(self.menu_is_open)
        nothing_typed = Condition(lambda: not self.buffer.text)
        keys.add("enter", filter=typing)(lambda event: self.enter())
        keys.add("escape", "enter", filter=typing)(lambda event: self.buffer.insert_text("\n"))
        keys.add("c-j", filter=typing)(lambda event: self.buffer.insert_text("\n"))
        keys.add("tab", filter=typing)(lambda event: self.complete())
        keys.add("s-tab", filter=typing)(lambda event: self.host.cycle_mode())
        keys.add("up", filter=typing & menu_open)(lambda event: self.move_menu(-1))
        keys.add("down", filter=typing & menu_open)(lambda event: self.move_menu(1))
        keys.add("escape", filter=~is_searching)(lambda event: self.escape())
        keys.add("c-c", filter=~is_searching)(lambda event: self.control_c())
        keys.add("c-d", filter=typing & nothing_typed)(lambda event: self.quit())
        keys.add("c-o", filter=~is_searching)(lambda event: self.expand())
        keys.add("pageup", filter=~is_searching)(lambda event: self.transcript.page(-1))
        keys.add("pagedown", filter=~is_searching)(lambda event: self.transcript.page(1))
        shortcuts_open = Condition(lambda: self.shortcuts_open)
        composing = typing & Condition(lambda: not self.wants_line)
        keys.add(SHORTCUTS_MARK, filter=composing & nothing_typed & ~shortcuts_open)(lambda event: self.open_shortcuts())
        keys.add(SHORTCUTS_MARK, filter=typing & shortcuts_open)(lambda event: self.close_shortcuts())
        keys.add("escape", filter=typing & shortcuts_open)(lambda event: self.close_shortcuts())
        keys.add("enter", filter=typing & shortcuts_open)(lambda event: self.ask_for_help())
        keys.add("up", filter=asking)(lambda event: self.heeds_key() and self.move_choice(-1))
        keys.add("down", filter=asking)(lambda event: self.heeds_key() and self.move_choice(1))
        keys.add("enter", filter=asking)(lambda event: self.heeds_key() and self.confirm_choice())
        keys.add(Keys.Any, filter=asking)(lambda event: self.heeds_key() and self.pick_choice(event.data))
        return keys

    def start(self) -> None:
        try:
            self.build()
        except Exception as error:
            raise InterfaceError(str(error) or type(error).__name__) from None
        context = contextvars.copy_context()
        self.thread = threading.Thread(target=lambda: context.run(self.run_app), daemon=True)
        self.thread.start()
        if not self.started.wait(START_SECONDS):
            raise InterfaceError("it took too long to start")
        if self.failure is not None:
            raise InterfaceError(str(self.failure) or type(self.failure).__name__)

    def run_app(self) -> None:
        try:
            self.app.run(pre_run=self.started.set, handle_sigint=False)
        except Exception as error:
            self.failure = error
        finally:
            self.leaving = True
            self.started.set()

    def stop(self) -> None:
        self.leaving = True
        self.in_loop(self.leave)
        if self.thread is not None:
            self.thread.join(JOIN_SECONDS)
            if self.thread.is_alive():
                self.put_terminal_back()
        self.scroll.finish()
        self.stray.finish()

    def put_terminal_back(self) -> None:
        output = self.app.output
        output.disable_mouse_support()
        output.quit_alternate_screen()
        output.reset_attributes()
        output.show_cursor()
        output.flush()

    def leave(self) -> None:
        if self.app.is_running and not self.app.is_done:
            self.app.exit()

    def is_choosing(self) -> bool:
        question = self.question
        return question is not None and bool(question.choices)

    def menu_is_open(self) -> bool:
        state = self.buffer.complete_state
        return state is not None and bool(state.completions) and not self.wants_line

    def enter(self) -> None:
        if self.menu_is_open() and self.accepts_completion():
            return
        text = self.buffer.text
        if self.buffer.document.text_before_cursor.endswith(LINE_CONTINUES):
            self.buffer.delete_before_cursor(1)
            self.buffer.insert_text("\n")
            return
        if self.wants_line:
            self.answer_line(text)
            return
        if not text.strip():
            return
        self.buffer.reset(append_to_history=True)
        self.transcript.follow()
        self.submit(text)

    def accepts_completion(self) -> bool:
        state = self.buffer.complete_state
        if state.current_completion is None and self.typed_is_listed(state):
            return False
        self.buffer.apply_completion(state.current_completion or state.completions[0])
        return True

    def typed_is_listed(self, state) -> bool:
        before = self.buffer.document.text_before_cursor
        return any(before.endswith(completion.text) and len(completion.text) == -completion.start_position
                   for completion in state.completions)

    def complete(self) -> None:
        state = self.buffer.complete_state
        if state is not None and state.completions:
            self.buffer.apply_completion(state.current_completion or state.completions[0])
        else:
            self.buffer.start_completion(select_first=True)

    def move_menu(self, step: int) -> None:
        state = self.buffer.complete_state
        self.buffer.go_to_completion(((state.complete_index or 0) + step) % len(state.completions))

    def open_shortcuts(self) -> None:
        self.shortcuts_open = True

    def close_shortcuts(self) -> None:
        self.shortcuts_open = False

    def ask_for_help(self) -> None:
        self.close_shortcuts()
        self.submit(HELP_COMMAND)

    def expand(self) -> None:
        shown = getattr(self.host, "expand", None)
        if shown is not None:
            threading.Thread(target=shown, daemon=True).start()

    def ring(self) -> None:
        self.in_loop(self.bell)

    def bell(self) -> None:
        self.app.output.bell()
        self.app.output.flush()

    def show_partial(self, text: str) -> None:
        self.partial = text
        self.redraw()

    def submit(self, text: str) -> None:
        with self.lock:
            if self.busy:
                self.queued.append(text)
        self.messages.put(text)

    def escape(self) -> None:
        if self.wants_line:
            self.answer_line("")
        elif self.question is not None:
            self.refuse()
        elif self.buffer.complete_state is not None:
            self.buffer.cancel_completion()
        elif self.busy:
            self.interrupt()

    def control_c(self) -> None:
        if self.wants_line:
            self.answer_line("")
        elif self.question is not None:
            self.refuse()
        elif self.buffer.text:
            self.buffer.reset()
            self.quit_armed_at = None
        elif self.quit_is_armed():
            self.quit()
        else:
            self.quit_armed_at = self.clock()

    def quit_is_armed(self) -> bool:
        return self.quit_armed_at is not None and self.clock() - self.quit_armed_at <= QUIT_SECONDS

    def quit(self) -> None:
        self.leaving = True
        self.refuse()
        if self.busy:
            self.interrupt()
        self.leave()

    def refuse(self) -> None:
        question = self.question
        if question is not None:
            question.settle(REFUSAL_KEY if REFUSAL_KEY in question.keys() else "")

    def heeds_key(self) -> bool:
        question, now = self.question, self.clock()
        if question is None:
            return False
        settled = now - question.opened_at >= ANSWER_PAUSE_SECONDS
        paused = self.last_key_at is None or now - self.last_key_at >= ANSWER_PAUSE_SECONDS
        question.heeding = settled and (question.heeding or paused)
        return question.heeding

    def move_choice(self, step: int) -> None:
        question = self.question
        if question is None or not question.choices:
            return
        start = -1 if step > 0 else 0
        question.selected = ((start if question.selected is None else question.selected) + step) % len(question.choices)

    def pick_choice(self, typed: str) -> None:
        question = self.question
        if question is None:
            return
        if typed.lower() in question.keys():
            question.selected = question.keys().index(typed.lower())
            return
        question.selected, question.heeding = None, False

    def confirm_choice(self) -> None:
        question = self.question
        if question is not None and question.choices and question.selected is not None:
            question.settle(question.keys()[question.selected])

    def answer_line(self, text: str) -> None:
        question, self.wants_line = self.question, False
        self.buffer.reset()
        self.buffer.insert_text(self.draft)
        self.draft = ""
        if question is not None:
            question.settle(text)

    def open_question(self, text: str, choices) -> None:
        self.question = Question(text, list(choices), opened_at=self.clock())
        self.in_loop(self.drop_typed_ahead)
        self.transcript.follow()
        self.redraw()

    def drop_typed_ahead(self) -> None:
        self.app.input.flush_keys()
        self.app.key_processor.reset()

    def wait_answer(self, seconds: float | None = None) -> str | None:
        question = self.question
        if question is None:
            return None
        deadline = None if seconds is None else time.monotonic() + seconds
        while not question.answered.is_set():
            if self.leaving:
                return REFUSAL_KEY if REFUSAL_KEY in question.keys() else ""
            left = POLL_SECONDS if deadline is None else min(POLL_SECONDS, deadline - time.monotonic())
            if left <= 0:
                return None
            question.answered.wait(left)
        return question.answer

    def close_question(self) -> None:
        self.question = None
        self.redraw()

    def choose(self, text: str, choices) -> str | None:
        self.open_question(text, choices)
        try:
            return self.wait_answer() or None
        finally:
            self.close_question()

    def line(self, text: str) -> str:
        self.question = Question(text, [])
        self.in_loop(self.take_the_box)
        try:
            return self.wait_answer() or ""
        finally:
            self.close_question()

    def take_the_box(self) -> None:
        self.draft = self.buffer.text
        self.buffer.reset()
        self.wants_line = True

    def answer_lines(self) -> Answers:
        return Answers(self)

    @contextlib.contextmanager
    def showing(self, label: str, started_at: float, tokens):
        before, self.status = self.status, Status(label, started_at, tokens)
        self.redraw()
        try:
            yield self
        finally:
            self.status = before
            self.redraw()

    @contextlib.contextmanager
    def working(self):
        with self.lock:
            self.busy = True
        try:
            yield
        finally:
            with self.lock:
                self.busy = False

    def take_queued(self, wanted) -> list[str]:
        with self.lock:
            waiting = []
            while not self.messages.empty():
                waiting.append(self.messages.get_nowait())
            taken = [text for text in waiting if wanted(text)]
            for text in waiting:
                if text not in taken:
                    self.messages.put(text)
        for text in taken:
            self.show_request(text)
        return taken

    def next_message(self) -> str | None:
        while not self.leaving:
            try:
                return self.messages.get(timeout=POLL_SECONDS)
            except queue.Empty:
                continue
        return None

    def show_request(self, text: str) -> None:
        with self.lock:
            if text in self.queued:
                self.queued.remove(text)
        theme = getattr(self.host, "theme", look.Theme(colour=False))
        shown = look.user_block(text, self.room(), theme)
        gap = [""] if self.scroll.last_line else []
        self.scroll.write("\n".join(gap + shown) + "\n")

    def room(self) -> int:
        if self.app is not None and self.app.is_running:
            return max(self.app.output.get_size().columns - 1, 1)
        return wrap.room()

    def prompt_text(self):
        return [("", " "), ("class:prompt", prompt_line.PROMPT_MARK)]

    def placeholder(self) -> str:
        if self.wants_line:
            return ""
        return BUSY_PLACEHOLDER if self.busy else PLACEHOLDER

    def live_lines(self) -> list[str]:
        live = self.scroll.pending or self.partial
        return [live] if live else []

    def scrolled_text(self):
        return [("class:dim", SCROLLED_HINT.format(self.transcript.rows_below())[:self.room()])]

    def queued_text(self):
        room = self.room()
        rows = [f"{QUEUED_LEAD}{text.splitlines()[0] if text.strip() else ''}"[:room] for text in list(self.queued)]
        return [("class:dim", "\n".join(rows))]

    def status_text(self):
        status = self.status
        if status is None:
            return []
        frame = int(self.clock() * FRAMES_PER_SECOND)
        spinner, label, details = status_parts(frame, self.clock() - status.started_at, status.label,
                                               status.tokens(), self.room())
        return [("class:spinner", spinner), ("", " "), ("class:status-label", label), ("class:dim", details)]

    def question_text(self):
        question = self.question
        if question is None:
            return []
        if not question.choices:
            return [("class:accent", question.text)]
        width = self.room()
        inner = max(width - 2 - 2 * PANEL_PADDING, 1)
        body = [[("class:question", line)] for line in wrap.wrapped_line(question.text, inner)]
        body.append([])
        for index in window_around(question.selected or 0, len(question.choices), CHOICE_ROWS):
            key, meaning = question.choices[index]
            chosen = index == question.selected
            choice = f"{POINTER if chosen else ' '} {key}  {meaning}"
            hang = " " * (len(key) + CHOICE_HANG)
            body.extend([("class:selected" if chosen else "", line)] for line in wrap.wrapped_line(choice, inner, hang))
        return lines_to_fragments(panel_lines(QUESTION_TITLE, body, width))

    def menu_text(self):
        state = self.buffer.complete_state
        if state is None or not state.completions:
            return []
        selected = state.complete_index or 0
        shown = window_around(selected, len(state.completions), MENU_ROWS)
        width = max(len(state.completions[index].display_text) for index in shown) + suggest.NAME_GAP
        rows = []
        for index in shown:
            completion = state.completions[index]
            name = f"{suggest.MENU_INDENT}{completion.display_text:{width}}"
            rows.append(("class:selected" if index == selected else "", name))
            rows.append(("class:meaning", f"{completion.display_meta_text}"[:max(self.room() - len(name), 0)]))
            rows.append(("", "\n"))
        return rows[:-1]

    def bar_text(self):
        room = self.room()
        if self.quit_is_armed():
            return [("class:dim", QUIT_HINT)]
        if self.wants_line:
            return [("class:dim", LINE_HINT)]
        if self.question is not None:
            return [("class:dim", fitted_hint(CHOICE_HINT, SHORT_CHOICE_HINT, room))]
        if self.shortcuts_open:
            return [("class:dim", fitted_hint(SHORTCUTS_HINT, SHORT_SHORTCUTS_HINT, room))]
        details = getattr(self.host, "footer_details", lambda: ("", "", None))
        model, branch, context_room = details()
        width = self.room()
        left, right = footer_pieces(self.host.mode(), self.host.shared(), model, branch, context_room, width)
        filler = " " * max(width - fragment_width(left) - fragment_width(right), 0)
        return left + [("", filler)] + right

    def shortcuts_text(self):
        columns = look.shortcut_columns(self.room())
        key_width = max(len(key) for key, _meaning in look.SHORTCUTS) + 1
        meaning_width = max(len(meaning) for _key, meaning in look.SHORTCUTS) + look.SHORTCUT_GAP
        rows = []
        for row in range(len(columns[0])):
            line = [("", prompt_line.BAR_INDENT)]
            for column in columns:
                if row < len(column):
                    key, meaning = column[row]
                    line += [("class:key", key.ljust(key_width)), ("class:dim", meaning.ljust(meaning_width))]
            rows.append(line)
        return lines_to_fragments(rows)

