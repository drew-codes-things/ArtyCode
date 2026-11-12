import threading
import time

FRAMES = ("·", "•", "●", "•")
TICK_SECONDS = 0.12
LINE_ROOM = 40
LABEL = "Thinking"
STOP_HINT = "Ctrl-C to stop"


def thinking_text(frame: int, seconds: float) -> str:
    return f"{FRAMES[frame % len(FRAMES)]} {LABEL} {int(seconds)}s · {STOP_HINT}"[:LINE_ROOM]


class ThinkingLine:
    def __init__(self, write, paint=lambda text: text, clock=time.monotonic, tick_seconds=TICK_SECONDS):
        self.write = write
        self.paint = paint
        self.clock = clock
        self.tick_seconds = tick_seconds
        self.started_at = clock()
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.frame = 0
        self.shown_width = 0

    def tick(self) -> None:
        with self.lock:
            if self.stopped.is_set():
                return
            text = thinking_text(self.frame, self.clock() - self.started_at)
            self.write("\r" + self.paint(text))
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
