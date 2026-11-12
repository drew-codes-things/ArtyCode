import threading

from arty_cli import stopkey


class Keys:
    def __init__(self, *typed):
        self.typed = list(typed)
        self.events = []
        self.read_while_quiet = threading.Event()

    def begin(self):
        self.events.append("begin")

    def end(self):
        self.events.append("end")

    def read(self, seconds):
        self.read_while_quiet.set()
        if self.typed:
            return self.typed.pop(0)
        threading.Event().wait(seconds)
        return ""


def watched(keys):
    pressed = threading.Event()
    return stopkey.StopKey(keys, interrupt=pressed.set, poll_seconds=0.01), pressed


def test_escape_interrupts_the_request():
    keys = Keys("a", stopkey.ESCAPE)
    watcher, pressed = watched(keys)
    with watcher:
        assert pressed.wait(2)
    assert watcher.pressed and keys.events == ["begin", "end"]


def test_arrow_keys_and_other_escape_sequences_do_not_interrupt():
    keys = Keys("\x1b[A", "\x1bOB", "x")
    watcher, pressed = watched(keys)
    with watcher:
        assert not pressed.wait(0.3)
    assert not watcher.pressed


def test_while_paused_for_a_question_the_keys_are_left_alone_and_the_terminal_is_normal():
    keys = Keys()
    watcher, pressed = watched(keys)
    with watcher:
        assert keys.read_while_quiet.wait(2)
        with watcher.paused():
            keys.read_while_quiet.clear()
            keys.typed.append(stopkey.ESCAPE)
            assert not keys.read_while_quiet.wait(0.2)
            assert keys.events == ["begin", "end"]
        assert pressed.wait(2)
    assert keys.events == ["begin", "end", "begin", "end"]


def test_leaving_while_paused_does_not_take_the_terminal_twice():
    keys = Keys()
    watcher, _pressed = watched(keys)
    with watcher:
        with watcher.paused():
            pass
    assert keys.events == ["begin", "end", "begin", "end"]


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_console_keys_give_up_quietly_when_nothing_is_typed_and_collect_what_is():
    clock = Clock()
    waiting = []
    keys = stopkey.ConsoleKeys(lambda: bool(waiting), lambda: waiting.pop(0), clock=clock, sleep=clock.sleep)
    assert keys.read(0.5) == "" and clock.now >= 0.5
    waiting.extend([stopkey.ESCAPE])
    assert keys.read(0.5) == stopkey.ESCAPE
