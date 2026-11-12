import _thread
import contextlib
import os
import select
import signal
import sys
import threading
import time

ESCAPE = "\x1b"
POLL_SECONDS = 0.1
KEY_POLL_SECONDS = 0.03
READ_BYTES = 64
JOIN_SECONDS = 1.0


class PosixKeys:
    def __init__(self, descriptor: int):
        import termios
        self.termios = termios
        self.descriptor = descriptor
        self.saved = None

    def begin(self) -> None:
        self.saved = self.termios.tcgetattr(self.descriptor)
        quiet = self.termios.tcgetattr(self.descriptor)
        quiet[3] &= ~(self.termios.ICANON | self.termios.ECHO)
        quiet[6][self.termios.VMIN], quiet[6][self.termios.VTIME] = 1, 0
        self.termios.tcsetattr(self.descriptor, self.termios.TCSANOW, quiet)

    def end(self) -> None:
        if self.saved is not None:
            self.termios.tcsetattr(self.descriptor, self.termios.TCSANOW, self.saved)
            self.saved = None

    def read(self, seconds: float) -> str:
        ready, _writable, _failed = select.select([self.descriptor], [], [], seconds)
        return os.read(self.descriptor, READ_BYTES).decode("utf-8", errors="replace") if ready else ""


class ConsoleKeys:
    def __init__(self, has_key, key, clock=time.monotonic, sleep=time.sleep):
        self.has_key = has_key
        self.key = key
        self.clock = clock
        self.sleep = sleep

    def begin(self) -> None:
        pass

    def end(self) -> None:
        pass

    def read(self, seconds: float) -> str:
        deadline = self.clock() + seconds
        while not self.has_key():
            if self.clock() >= deadline:
                return ""
            self.sleep(KEY_POLL_SECONDS)
        typed = []
        while self.has_key():
            typed.append(self.key())
        return "".join(typed)


def interrupt_main_thread() -> None:
    if hasattr(signal, "pthread_kill"):
        signal.pthread_kill(threading.main_thread().ident, signal.SIGINT)
    else:
        _thread.interrupt_main()


class StopKey:
    def __init__(self, keys, interrupt=interrupt_main_thread, poll_seconds: float = POLL_SECONDS):
        self.keys = keys
        self.interrupt = interrupt
        self.poll_seconds = poll_seconds
        self.lock = threading.Lock()
        self.ended = threading.Event()
        self.pauses = 0
        self.pressed = False
        self.thread = threading.Thread(target=self.watch, daemon=True)

    def __enter__(self) -> "StopKey":
        self.keys.begin()
        self.thread.start()
        return self

    def __exit__(self, *details) -> None:
        self.ended.set()
        with self.lock:
            if not self.pauses:
                self.keys.end()
        self.thread.join(JOIN_SECONDS)

    def typed_now(self) -> str | None:
        with self.lock:
            if self.pauses or self.ended.is_set():
                return None
            return self.keys.read(self.poll_seconds)

    def watch(self) -> None:
        while not self.ended.is_set():
            typed = self.typed_now()
            if typed is None:
                self.ended.wait(self.poll_seconds)
                continue
            if typed == ESCAPE and not self.ended.is_set():
                self.pressed = True
                self.interrupt()
                return

    @contextlib.contextmanager
    def paused(self):
        with self.lock:
            self.pauses += 1
            if self.pauses == 1:
                self.keys.end()
        try:
            yield
        finally:
            with self.lock:
                self.pauses -= 1
                if not self.pauses and not self.ended.is_set():
                    self.keys.begin()


def keys_here():
    if os.name == "nt":
        import msvcrt
        return ConsoleKeys(msvcrt.kbhit, msvcrt.getwch)
    try:
        return PosixKeys(sys.stdin.fileno())
    except (ImportError, OSError, ValueError):
        return None
