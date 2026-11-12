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


def test_each_tick_redraws_one_line_with_the_time_so_far_and_how_to_interrupt():
    clock = Clock()
    line, written = thinking_line(clock)
    line.tick()
    clock.now += 3.4
    line.tick()
    assert all(text.startswith("\r") and "\n" not in text for text in written)
    assert "Thinking… (0s" in written[0] and "Thinking… (3s" in written[1]
    assert all("esc to interrupt" in text for text in written)
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


def test_the_line_never_runs_past_the_room_it_is_given():
    clock = Clock()
    written = []
    line = waiting.ThinkingLine(written.append, clock=clock, label="Run " + "x" * 200, room=lambda: 50)
    clock.now += 59 * 60
    line.tick()
    assert len(written[-1]) - 1 <= 50
    assert written[-1].endswith("esc to interrupt)")


def test_as_a_context_it_ticks_on_its_own_and_is_cleared_on_the_way_out():
    drawn = threading.Event()
    written = []

    def write(text):
        written.append(text)
        drawn.set()

    with waiting.ThinkingLine(write, tick_seconds=0.01):
        assert drawn.wait(2)
    assert written[-1].replace(" ", "") == "\r\r"


def test_the_line_names_what_is_happening_with_minutes_and_tokens_so_far():
    clock = Clock()
    written = []
    line = waiting.ThinkingLine(written.append, clock=clock, label="Run npm install", started_at=clock.now - 65,
                                tokens=lambda: 1234, room=lambda: 200)
    line.tick()
    assert written[-1] == "\r· Run npm install (1m 05s · ↓ 1.2k tokens · esc to interrupt)"


def test_a_shorter_redraw_covers_what_the_longer_one_left():
    clock = Clock()
    written = []
    labels = iter(["Run a long command", "Read"])
    line = waiting.ThinkingLine(written.append, clock=clock, room=lambda: 200)
    line.label = next(labels)
    line.tick()
    line.label = next(labels)
    line.tick()
    assert len(written[-1]) == len(written[-2])


def test_elapsed_time_and_tokens_read_naturally():
    assert waiting.elapsed_text(9.7) == "9s"
    assert waiting.elapsed_text(3600 + 61) == "61m 01s"
    assert waiting.tokens_text(999) == "999" and waiting.tokens_text(25_400) == "25.4k"


def test_the_thinking_line_reads_like_a_spinner_with_time_tokens_and_how_to_interrupt():
    assert waiting.status_text(0, 12, tokens=340, room=200) == "· Thinking… (12s · ↓ 340 tokens · esc to interrupt)"
