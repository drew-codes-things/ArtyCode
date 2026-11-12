import shutil
import threading
import time

FRAMES = ("·", "•", "●", "•")
TICK_SECONDS = 0.12
DEFAULT_COLUMNS = 80
TERMINAL_ROWS = 24
LABEL = "Thinking…"
STOP_HINT = "esc to interrupt"
TOKEN_MARK = "↓"
THOUSAND = 1000
SECONDS_PER_MINUTE = 60
ELLIPSIS = "…"


def line_room() -> int:
    return max(shutil.get_terminal_size((DEFAULT_COLUMNS, TERMINAL_ROWS)).columns - 1, 1)


def elapsed_text(seconds: float) -> str:
    whole = max(int(seconds), 0)
    if whole < SECONDS_PER_MINUTE:
        return f"{whole}s"
    minutes, rest = divmod(whole, SECONDS_PER_MINUTE)
    return f"{minutes}m {rest:02d}s"


def tokens_text(tokens: int) -> str:
    if tokens < THOUSAND:
        return f"{tokens}"
    return f"{tokens / THOUSAND:.1f}k"


def fitted(label: str, details: str, room: int) -> str:
    space_for_label = room - len(details)
    if space_for_label >= len(label):
        return label + details
    if space_for_label > len(ELLIPSIS):
        return label[:space_for_label - len(ELLIPSIS)] + ELLIPSIS + details
    return (label + details)[:room]


def status_text(frame: int, seconds: float, label: str = LABEL, tokens: int = 0, room: int | None = None) -> str:
    counted = f" · {TOKEN_MARK} {tokens_text(tokens)} tokens" if tokens > 0 else ""
    details = f" ({elapsed_text(seconds)}{counted} · {STOP_HINT})"
    return fitted(f"{FRAMES[frame % len(FRAMES)]} {label}", details, line_room() if room is None else room)


class ThinkingLine:
    def __init__(self, write, paint=lambda text: text, clock=time.monotonic, tick_seconds=TICK_SECONDS,
                 label=LABEL, started_at=None, tokens=lambda: 0, room=line_room):
        self.write = write
        self.paint = paint
        self.clock = clock
        self.tick_seconds = tick_seconds
        self.label = label
        self.started_at = clock() if started_at is None else started_at
        self.tokens = tokens
        self.room = room
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.frame = 0
        self.shown_width = 0

    def tick(self) -> None:
        with self.lock:
            if self.stopped.is_set():
                return
            text = status_text(self.frame, self.clock() - self.started_at, self.label, self.tokens(), self.room())
            padding = " " * max(self.shown_width - len(text), 0)
            self.write("\r" + self.paint(text) + padding)
            self.frame += 1
            self.shown_width = len(text)

    def stop(self) -> None:
        with self.lock:
            if self.stopped.is_set():
                return
            self.stopped.set()
            if self.shown_width:
                self.write("\r" + " " * self.shown_width + "\r")

    def keep_ticking(self) -> None:
        while not self.stopped.wait(self.tick_seconds):
            self.tick()

    def __enter__(self) -> "ThinkingLine":
        threading.Thread(target=self.keep_ticking, daemon=True).start()
        return self

    def __exit__(self, *details) -> None:
        self.stop()
