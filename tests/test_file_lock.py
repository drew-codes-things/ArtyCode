import os
import sys
import threading
import time
import types

import pytest

from arty_cli import file_lock

HELD_SECONDS = 0.3


def flock_backed_msvcrt():
    import fcntl
    calls = []

    def locking(descriptor: int, mode: int, count: int) -> None:
        calls.append((mode, count, os.lseek(descriptor, 0, os.SEEK_CUR)))
        if mode == module.LK_UNLCK:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            return
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise OSError(36, "Resource deadlock avoided") from None

    module = types.SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=locking, calls=calls)
    return module


@pytest.fixture(params=["this system", "windows"])
def platform(request, monkeypatch):
    if request.param == "windows" and sys.platform != "win32":
        monkeypatch.setitem(sys.modules, "msvcrt", flock_backed_msvcrt())
    return "win32" if request.param == "windows" else sys.platform


def holding(path, released: threading.Event, taken: threading.Event, platform: str):
    def hold():
        with file_lock.held(path, platform=platform):
            taken.set()
            released.wait(5)

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    assert taken.wait(5)
    return thread


def test_a_second_holder_waits_until_the_first_lets_go(tmp_path, platform):
    path = tmp_path / "config" / "sign-ins.lock"
    released, taken = threading.Event(), threading.Event()
    holder = holding(path, released, taken, platform)
    threading.Timer(HELD_SECONDS, released.set).start()
    started = time.monotonic()
    with file_lock.held(path, wait_seconds=5, platform=platform):
        waited = time.monotonic() - started
    holder.join(5)
    assert waited >= HELD_SECONDS * 0.8


def test_a_lock_held_too_long_gives_up_with_an_os_error(tmp_path, platform):
    path = tmp_path / "sign-ins.lock"
    released, taken = threading.Event(), threading.Event()
    holder = holding(path, released, taken, platform)
    try:
        with pytest.raises(OSError, match="another ArtyCode has held"):
            with file_lock.held(path, wait_seconds=0.2, platform=platform):
                pytest.fail("took a lock someone else holds")
    finally:
        released.set()
        holder.join(5)
    with file_lock.held(path, wait_seconds=1, platform=platform):
        pass


def test_the_lock_is_let_go_when_the_work_inside_fails(tmp_path, platform):
    path = tmp_path / "sign-ins.lock"
    with pytest.raises(RuntimeError):
        with file_lock.held(path, platform=platform):
            raise RuntimeError("failed inside")
    with file_lock.held(path, wait_seconds=0.5, platform=platform):
        pass


@pytest.mark.skipif(sys.platform == "win32", reason="checks the stand-in used off Windows")
def test_on_windows_one_byte_at_the_start_of_the_lock_file_is_locked_and_unlocked(tmp_path, monkeypatch):
    stand_in = flock_backed_msvcrt()
    monkeypatch.setitem(sys.modules, "msvcrt", stand_in)
    with file_lock.held(tmp_path / "sign-ins.lock", platform="win32"):
        pass
    assert stand_in.calls == [(stand_in.LK_NBLCK, 1, 0), (stand_in.LK_UNLCK, 1, 0)]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_the_lock_file_is_private(tmp_path):
    path = tmp_path / "sign-ins.lock"
    with file_lock.held(path):
        pass
    assert os.stat(path).st_mode & 0o777 == file_lock.LOCK_FILE_MODE
