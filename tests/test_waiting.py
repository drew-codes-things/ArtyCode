import threading

from arty_cli import waiting


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def thinking_line(clock=None, tick_seconds=waiting.TICK_SECONDS):
    written = []
    return waiting.ThinkingLine(written.append, clock=clock or Clock(), tick_seconds=tick_seconds), written


def test_each_tick_redraws_one_line_with_the_seconds_waited_and_how_to_stop():
    clock = Clock()
    line, written = thinking_line(clock)
    line.tick()
    clock.now += 3.4
    line.tick()
    assert all(text.startswith("\r") and "\n" not in text for text in written)
    assert "Thinking 0s" in written[0] and "Thinking 3s" in written[1]
    assert all("Ctrl-C to stop" in text for text in written)
    assert written[0][1] != written[1][1]


def test_stopping_blanks_the_line_with_spaces_and_returns_to_its_start():
    line, written = thinking_line()
    line.tick()
    shown = len(written[-1]) - 1
    line.stop()
    assert written[-1] == "\r" + " " * shown + "\r"


def test_stopping_twice_or_before_anything_was_shown_writes_nothing_more():
    line, written = thinking_line()
    line.stop()
    line.stop()
    assert written == []


def test_no_tick_is_drawn_after_it_stopped():
    line, written = thinking_line()
    line.tick()
    line.stop()
    before = list(written)
    line.tick()
    assert written == before


def test_the_line_fits_the_room_other_status_lines_clear():
    clock = Clock()
    line, written = thinking_line(clock)
    clock.now += 59 * 60
    line.tick()
    assert len(written[-1]) - 1 <= waiting.LINE_ROOM


def test_as_a_context_it_ticks_on_its_own_and_is_cleared_on_the_way_out():
    drawn = threading.Event()
    written = []

    def write(text):
        written.append(text)
        drawn.set()

    with waiting.ThinkingLine(write, tick_seconds=0.01):
        assert drawn.wait(2)
    assert written[-1].replace(" ", "") == "\r\r"
