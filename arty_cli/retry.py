import os
import random
import sys
import time

MAX_TRIES = 4
FIRST_WAIT_SECONDS = 1.0
MAX_WAIT_SECONDS = 30.0
MAX_RETRY_AFTER_SECONDS = 60.0
JITTER = 0.25
DIM, RESET = "\033[2m", "\033[0m"
sleep = time.sleep


class Busy:
    def __init__(self, retry_after: float | None = None):
        self.retry_after = retry_after


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
    painted = sys.stderr.isatty() and not os.environ.get("NO_COLOR")
    sys.stderr.write((f"{DIM}{text}{RESET}" if painted else text) + "\n")
    sys.stderr.flush()


def show_wait(seconds: float) -> None:
    show_dim(f"  · busy, retrying in {max(round(seconds), 1)} s")


def with_retries(attempt_once, busy_of):
    for attempt in range(MAX_TRIES):
        try:
            return attempt_once()
        except Exception as error:
            busy = busy_of(error)
            if busy is None or attempt == MAX_TRIES - 1:
                raise
            seconds = wait_before(attempt, busy.retry_after)
            show_wait(seconds)
            sleep(seconds)
    raise RuntimeError("with_retries ran out of tries without an answer")
