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
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.history import FileHistory, InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout.containers import ConditionalContainer, HSplit, VSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.patch_stdout import StdoutProxy
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import SearchToolbar

from arty_cli import prompt_line, remote, suggest, tools, waiting, wrap

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
WRITE_PAUSE_SECONDS = 0.03
POLL_SECONDS = 0.2
START_SECONDS = 5.0
JOIN_SECONDS = 2.0
QUIT_SECONDS = 2.0
KEY_PREFIX_SECONDS = 0.15
ESCAPE_SECONDS = 0.05
FRAMES_PER_SECOND = 8
REFUSAL_KEY = "n"
LINE_CONTINUES = "\\"
QUEUED_LEAD = "  queued: "
POINTER = "❯"
QUIT_HINT = "  Press Ctrl-C again to quit"
CHOICE_HINT = "  ↑↓ or its letter to choose · enter to confirm · esc to refuse"
LINE_HINT = "  enter to answer · esc to skip"
ACCENT = "#e5383b"
STYLE = Style.from_dict({"rule": "fg:ansibrightblack", "dim": "fg:ansibrightblack", "prompt": f"fg:{ACCENT}",
                         "question": f"fg:{ACCENT}", "selected": "reverse", "meaning": "fg:ansibrightblack"})


class InterfaceError(Exception):
    pass


class Scroll:
    def __init__(self, commit, changed=lambda: None):
        self.commit = commit
        self.changed = changed
        self.pending = ""
        self.lock = threading.Lock()

    def isatty(self) -> bool:
        return True

    def write(self, text: str) -> int:
        with self.lock:
            for piece in PIECES.split(text):
                if piece == "\n":
                    self.commit(self.pending)
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
    selected: int = 0
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
        self.failure = None
        self.started = threading.Event()
        self.stray = None
        self.commit_line = commit
        self.scroll = Scroll(self.committed, self.redraw)
        self.app = None
        self.thread = None

    def committed(self, line: str) -> None:
        if self.commit_line is not None:
            self.commit_line(line)
        else:
            self.stray.write(line + "\n")

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
        search = SearchToolbar()
        typed = BufferControl(buffer=self.buffer, search_buffer_control=search.control)
        menu_open = Condition(self.menu_is_open)
        rows = [
            self.shown_when(self.pending_text, lambda: bool(self.scroll.pending), wraps=True),
            self.shown_when(self.question_text, lambda: self.question is not None, wraps=True),
            self.shown_when(self.queued_text, lambda: bool(self.queued)),
            self.shown_when(self.status_text, lambda: self.status is not None),
            Window(char=prompt_line.RULE, height=1, style="class:rule"),
            VSplit([Window(FormattedTextControl(self.prompt_text), width=len(prompt_line.PROMPT_MARK),
                           dont_extend_height=True),
                    Window(typed, wrap_lines=True, dont_extend_height=True, height=Dimension(min=1))]),
            Window(char=prompt_line.RULE, height=1, style="class:rule"),
            ConditionalContainer(Window(FormattedTextControl(self.menu_text), dont_extend_height=True),
                                 filter=menu_open),
            search,
            ConditionalContainer(Window(FormattedTextControl(self.bar_text), height=1),
                                 filter=~menu_open & ~is_searching),
        ]
        self.app = Application(layout=Layout(HSplit(rows), focused_element=typed), key_bindings=self.bindings(),
                               style=STYLE, full_screen=False, erase_when_done=True,
                               refresh_interval=REFRESH_SECONDS)
        self.app.timeoutlen = KEY_PREFIX_SECONDS
        self.app.ttimeoutlen = ESCAPE_SECONDS

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
        keys.add("up", filter=asking)(lambda event: self.move_choice(-1))
        keys.add("down", filter=asking)(lambda event: self.move_choice(1))
        keys.add("enter", filter=asking)(lambda event: self.confirm_choice())
        keys.add(Keys.Any, filter=asking)(lambda event: self.pick_choice(event.data))
        return keys

    def start(self) -> None:
        try:
            self.build()
            if self.commit_line is None:
                self.stray = StdoutProxy(sleep_between_writes=WRITE_PAUSE_SECONDS, raw=True)
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
        self.scroll.finish()
        if self.stray is not None:
            self.stray.close()

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

    def move_choice(self, step: int) -> None:
        question = self.question
        if question is not None and question.choices:
            question.selected = (question.selected + step) % len(question.choices)

    def pick_choice(self, typed: str) -> None:
        question = self.question
        if question is not None and typed.lower() in question.keys():
            question.selected = question.keys().index(typed.lower())

    def confirm_choice(self) -> None:
        question = self.question
        if question is not None and question.choices:
            question.settle(question.keys()[question.selected])

    def answer_line(self, text: str) -> None:
        question, self.wants_line = self.question, False
        self.buffer.reset()
        self.buffer.insert_text(self.draft)
        self.draft = ""
        if question is not None:
            question.settle(text)

    def open_question(self, text: str, choices) -> None:
        self.question = Question(text, list(choices))
        self.redraw()

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
        first, *rest = text.split("\n")
        shown = [self.host.paint(prompt_line.PROMPT_MARK, "accent") + first, *(f"  {line}" for line in rest)]
        self.scroll.write("\n".join(shown) + "\n")

    def room(self) -> int:
        return wrap.room()

    def prompt_text(self):
        return [("class:prompt", prompt_line.PROMPT_MARK)]

    def pending_text(self):
        return ANSI(self.scroll.pending)

    def queued_text(self):
        room = self.room()
        rows = [f"{QUEUED_LEAD}{text.splitlines()[0] if text.strip() else ''}"[:room] for text in list(self.queued)]
        return [("class:dim", "\n".join(rows))]

    def status_text(self):
        status = self.status
        if status is None:
            return []
        frame = int(self.clock() * FRAMES_PER_SECOND)
        return [("class:dim", waiting.status_text(frame, self.clock() - status.started_at, status.label,
                                                  status.tokens(), self.room()))]

    def question_text(self):
        question = self.question
        if question is None:
            return []
        rows = [("class:question", question.text)]
        for index in window_around(question.selected, len(question.choices), CHOICE_ROWS):
            key, meaning = question.choices[index]
            chosen = index == question.selected
            rows.append(("", "\n"))
            rows.append(("class:selected" if chosen else "", f"{POINTER if chosen else ' '} {key}  {meaning}"))
        return rows

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
        if self.quit_is_armed():
            return [("class:dim", QUIT_HINT)]
        if self.wants_line:
            return [("class:dim", LINE_HINT)]
        if self.question is not None:
            return [("class:dim", CHOICE_HINT)]
        shared = prompt_line.REMOTE_MARK if self.host.shared() else ""
        bar = f"{prompt_line.BAR_INDENT}{self.host.mode()} {prompt_line.BAR_HINTS}{shared}"
        return [("class:dim", bar[:self.room()])]

