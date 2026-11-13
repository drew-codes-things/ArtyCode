import contextlib
import os
import sys
import time
from pathlib import Path

WAIT_SECONDS = 30
POLL_SECONDS = 0.05
LOCKED_BYTES = 1
LOCK_FILE_MODE = 0o600


class LockTimeout(TimeoutError):
    pass


def took_posix_lock(descriptor: int) -> bool:
    import fcntl
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def released_posix_lock(descriptor: int) -> None:
    import fcntl
    fcntl.flock(descriptor, fcntl.LOCK_UN)


def took_windows_lock(descriptor: int) -> bool:
    import msvcrt
    os.lseek(descriptor, 0, os.SEEK_SET)
    try:
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, LOCKED_BYTES)
    except OSError:
        return False
    return True


def released_windows_lock(descriptor: int) -> None:
    import msvcrt
    os.lseek(descriptor, 0, os.SEEK_SET)
    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, LOCKED_BYTES)


def lockers(platform: str):
    if platform == "win32":
        return took_windows_lock, released_windows_lock
    return took_posix_lock, released_posix_lock


@contextlib.contextmanager
def held(path: Path, wait_seconds: float = WAIT_SECONDS, platform: str = sys.platform):
    take, release = lockers(platform)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, LOCK_FILE_MODE)
    try:
        deadline = time.monotonic() + wait_seconds
        while not take(descriptor):
            if time.monotonic() >= deadline:
                raise LockTimeout(f"another ArtyCode has held {path} for over {wait_seconds:g} seconds")
            time.sleep(POLL_SECONDS)
        try:
            yield
        finally:
            release(descriptor)
    finally:
        os.close(descriptor)
