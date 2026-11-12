import os
import random
import sys
import time

from arty_cli import waiting

PATIENCE_SECONDS = 15 * 60
FIRST_WAIT_SECONDS = 1.0
MAX_WAIT_SECONDS = 30.0
MAX_RETRY_AFTER_SECONDS = 60.0
JITTER = 0.25
BUSY_REASON = "busy"
DIM, RESET = "\033[2m", "\033[0m"
sleep = time.sleep
clock = time.monotonic


class Busy:
    def __init__(self, retry_after: float | None = None, reason: str = BUSY_REASON, tries: int | None = None):
        self.retry_after = retry_after
        self.reason = reason
        self.tries = tries


def wait_before(attempt: int, retry_after: float | None) -> float:
    if retry_after is not None and retry_after >= 0:
        return min(retry_after, MAX_RETRY_AFTER_SECONDS)
    base = min(FIRST_WAIT_SECONDS * 2 ** attempt, MAX_WAIT_SECONDS)
    return base * (1 - JITTER + random.random() * 2 * JITTER)


def seconds_in(value) -> float | None:
    try:
        return float(str(value).strip().removesuffix("s"))
    except (TypeError, ValueError):
        return None


def retry_after_of(headers) -> float | None:
    if headers is None:
        return None
    try:
        return seconds_in(headers.get("retry-after") or headers.get("Retry-After"))
    except AttributeError:
        return None


def show_dim(text: str) -> None:
    on_terminal = sys.stderr.isatty()
    shown = text.ljust(waiting.line_room()) if on_terminal else text
    painted = on_terminal and not os.environ.get("NO_COLOR")
    sys.stderr.write(("\r" if on_terminal else "") + (f"{DIM}{shown}{RESET}" if painted else shown) + "\n")
    sys.stderr.flush()


def show_wait(seconds: float, reason: str = BUSY_REASON) -> None:
    show_dim(f"  · {reason}, retrying in {max(round(seconds), 1)} s")


def out_of_tries(busy: Busy, attempt: int) -> bool:
    return busy.tries is not None and attempt + 1 >= busy.tries


def with_retries(attempt_once, busy_of, patience_seconds: float | None = None):
    patience_seconds = PATIENCE_SECONDS if patience_seconds is None else patience_seconds
    first_failure, attempt = None, 0
    while True:
        try:
            return attempt_once()
        except Exception as error:
            busy = busy_of(error)
            if busy is None or out_of_tries(busy, attempt):
                raise
            now = clock()
            first_failure = now if first_failure is None else first_failure
            seconds = wait_before(attempt, busy.retry_after)
            if now + seconds - first_failure > patience_seconds:
                raise
            show_wait(seconds, busy.reason)
            sleep(seconds)
            attempt += 1
