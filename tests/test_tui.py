import stat
import sys
import threading
import time
from contextlib import contextmanager

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.data_structures import Point
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
from prompt_toolkit.output import DummyOutput

from arty_cli import permissions, prompt_line, tui

COMMANDS = {"/model": "switch model", "/mode": "how much is asked", "/help": "this list"}
MODELS = {"arty/qwen3:14b": "14B, all tools", "arty/mimo-v2.6:9b": "9B, sees images"}
ESCAPE, CTRL_C, CTRL_D, CTRL_R, SHIFT_TAB, UP, DOWN = "\x1b", "\x03", "\x04", "\x12", "\x1b[Z", "\x1b[A", "\x1b[B"
ALT_ENTER = "\x1b\r"
PASTE_START, PASTE_END = "\x1b[200~", "\x1b[201~"


class Host:
    def __init__(self, root):
        self.root = root
        self.current = permissions.DEFAULT
        self.is_shared = False
        self.details = ("", "", None)

    def footer_details(self):
        return self.details

    def mode(self):
        return self.current

    def cycle_mode(self):
        self.current = prompt_line.next_mode(self.current)

    def shared(self):
        return self.is_shared

    def commands(self):
        return COMMANDS

    def models(self):
        return MODELS

    def paint(self, text, colour):
        return text


def until(condition, seconds=3.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


def plain(fragments):
    return "".join(text for _style, text, *_rest in fragments)


def rows_text(rows):
    return [plain(row) for row in rows]


def shown_rows(interface, width, height):
    content = tui.TranscriptControl(interface.transcript, interface.live_lines).create_content(width, height)
    return [plain(content.get_line(number)) for number in range(content.line_count)]


def wheel(direction):
    return MouseEvent(Point(x=0, y=0), direction, MouseButton.NONE, frozenset())


def transcript_of(count):
    transcript = tui.Transcript()
    transcript.add_block(lambda width: [f"logo at {width}"])
    for number in range(1, count + 1):
        transcript.add_line(f"line {number}")
    return transcript


def shown_by(transcript, width=40, height=5):
    return rows_text(tui.TranscriptControl(transcript, list).create_content(width, height).get_line(number)
                     for number in range(min(height, len(transcript.rows_for(width)))))


class Clock:
    def __init__(self):
        self.ahead = 0.0

    def __call__(self):
        return time.monotonic() + self.ahead

    def advance(self, seconds):
        self.ahead += seconds


@contextmanager
def running(root, history_file=None):
    committed, interrupted, clock = [], [], Clock()
    with create_pipe_input() as typed, create_app_session(input=typed, output=DummyOutput()):
        interface = tui.Interface(Host(root), history_file, commit=committed.append,
                                  interrupt=lambda: interrupted.append(True), clock=clock)
        interface.start()
        interface.sent, interface.interrupted, interface.test_clock = committed, interrupted, clock
        try:
            yield interface, typed
        finally:
            interface.stop()


@pytest.fixture
def started(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x = 1\n")
    (tmp_path / "README.md").write_text("hi\n")
    (tmp_path / ".git").mkdir()
    with running(tmp_path) as (interface, typed):
        yield interface, typed


def typed_text(interface):
    return interface.buffer.text


def test_complete_lines_go_to_the_scrollback_and_the_unfinished_one_stays_live():
    lines, redrawn = [], []
    scroll = tui.Scroll(lines.append, lambda: redrawn.append(1))
    scroll.write("Read a.py\n  └ 2 lines\nThe test fa")
    assert lines == ["Read a.py", "  └ 2 lines"] and scroll.pending == "The test fa" and redrawn
    scroll.write("ils")
    scroll.finish()
    assert lines[-1] == "The test fails" and scroll.pending == "" and scroll.isatty()


def test_a_word_moved_to_the_next_line_by_wrapping_is_taken_off_the_live_line():
    lines = []
    scroll = tui.Scroll(lines.append)
    scroll.write("it supports addi" + "\b" * 4 + "\033[K" + "\n" + "addition")
    assert lines == ["it supports "] and scroll.pending == "addition"


def test_a_line_typed_while_idle_is_the_next_message(started):
    interface, typed = started
    typed.send_text("fix the tests\r")
    assert interface.next_message() == "fix the tests"
    assert interface.queued == [] and typed_text(interface) == ""


def test_a_line_typed_while_arty_works_is_queued_and_sent_when_the_turn_ends(started):
    interface, typed = started
    with interface.working():
        typed.send_text("also run the linter\r")
        assert until(lambda: interface.queued == ["also run the linter"])
        assert plain(interface.queued_text()) == "  queued: also run the linter"
    assert interface.next_message() == "also run the linter"
    interface.show_request("also run the linter")
    assert interface.queued == [] and interface.sent == ["› also run the linter"]


def test_a_request_of_several_lines_is_shown_under_one_mark(started):
    interface, _typed = started
    interface.show_request("first line\nsecond line")
    assert interface.sent == ["› first line", "  second line"]


def test_escape_interrupts_only_while_a_turn_runs(started):
    interface, typed = started
    typed.send_text(ESCAPE)
    time.sleep(0.4)
    assert interface.interrupted == []
    with interface.working():
        typed.send_text(ESCAPE)
        assert until(lambda: interface.interrupted == [True])


def test_ctrl_c_clears_the_line_then_asks_again_before_quitting(started):
    interface, typed = started
    typed.send_text("half a thought")
    assert until(lambda: typed_text(interface) == "half a thought")
    typed.send_text(CTRL_C)
    assert until(lambda: typed_text(interface) == "")
    assert not interface.leaving
    typed.send_text(CTRL_C)
    assert until(lambda: plain(interface.bar_text()) == tui.QUIT_HINT)
    typed.send_text(CTRL_C)
    assert interface.next_message() is None and interface.leaving


def test_ctrl_d_quits_only_on_an_empty_line(started):
    interface, typed = started
    typed.send_text("abc" + CTRL_D)
    time.sleep(0.3)
    assert not interface.leaving
    typed.send_text(CTRL_C + CTRL_D)
    assert interface.next_message() is None


def test_quitting_while_a_turn_runs_interrupts_it_and_drops_what_was_queued(started):
    interface, typed = started
    with interface.working():
        typed.send_text("later\r")
        assert until(lambda: interface.queued == ["later"])
        typed.send_text(CTRL_D)
        assert until(lambda: interface.interrupted == [True])
    assert interface.next_message() is None


def test_a_slash_opens_a_menu_that_narrows_as_you_type_with_what_each_command_does(started):
    interface, typed = started
    typed.send_text("/")
    assert until(lambda: "/help" in plain(interface.menu_text()))
    typed.send_text("mo")
    assert until(lambda: "/help" not in plain(interface.menu_text()) and interface.menu_is_open())
    shown = plain(interface.menu_text())
    assert "/model  switch model" in shown and "/mode   how much is asked" in shown


def test_enter_takes_the_highlighted_command_and_a_second_enter_sends_it(started):
    interface, typed = started
    typed.send_text("/he")
    assert until(interface.menu_is_open)
    typed.send_text("\r")
    assert until(lambda: typed_text(interface) == "/help")
    assert interface.messages.empty()
    typed.send_text("\r")
    assert interface.next_message() == "/help"


def test_arrows_move_through_the_menu_and_tab_takes_the_choice(started):
    interface, typed = started
    typed.send_text("/mo")
    assert until(interface.menu_is_open)
    typed.send_text(DOWN)
    assert until(lambda: typed_text(interface) == "/mode")
    typed.send_text("\t")
    assert until(lambda: typed_text(interface) == "/mode")
    typed.send_text(CTRL_C + "/mo")
    assert until(lambda: typed_text(interface) == "/mo" and interface.menu_is_open())
    typed.send_text("\t")
    assert until(lambda: typed_text(interface) == "/model")


def test_after_slash_model_the_menu_lists_the_models(started):
    interface, typed = started
    typed.send_text("/model arty/m")
    assert until(lambda: "arty/mimo-v2.6:9b  9B, sees images" in plain(interface.menu_text()))
    assert "qwen3" not in plain(interface.menu_text())


def test_an_at_sign_completes_file_paths_in_the_project(started):
    interface, typed = started
    typed.send_text("look at @")
    assert until(lambda: "@README.md" in plain(interface.menu_text()))
    shown = plain(interface.menu_text())
    assert "@src/" in shown and ".git" not in shown
    typed.send_text("src/a")
    assert until(lambda: plain(interface.menu_text()).strip() == "@src/app.py")
    typed.send_text("\t")
    assert until(lambda: typed_text(interface) == "look at @src/app.py")


def test_paths_outside_the_project_are_never_offered(tmp_path):
    (tmp_path / "project").mkdir()
    (tmp_path / "secret.txt").write_text("x")
    assert tui.project_paths(tmp_path / "project", "../") == []
    assert tui.project_paths(tmp_path / "project", "/etc/") == []


def test_a_trailing_backslash_or_alt_enter_adds_a_line_instead_of_sending(started):
    interface, typed = started
    typed.send_text("one\\\rtwo" + ALT_ENTER + "three\r")
    assert interface.next_message() == "one\ntwo\nthree"


def test_pasted_text_of_several_lines_stays_one_message(started):
    interface, typed = started
    typed.send_text(PASTE_START + "def f():\n    return 1" + PASTE_END)
    assert until(lambda: typed_text(interface) == "def f():\n    return 1")
    typed.send_text("\r")
    assert interface.next_message() == "def f():\n    return 1"


def test_shift_tab_cycles_the_mode_in_place_and_the_bar_follows(started):
    interface, typed = started
    assert plain(interface.bar_text()).startswith("  default mode (shift+tab to cycle) · ? for shortcuts")
    assert "esc to interrupt" not in plain(interface.bar_text())
    typed.send_text("keep this" + SHIFT_TAB)
    assert until(lambda: interface.host.current == permissions.AUTO_EDIT)
    assert plain(interface.bar_text()).startswith("  auto-edit mode") and typed_text(interface) == "keep this"
    interface.host.is_shared = True
    assert "· remote" in plain(interface.bar_text())


def test_the_status_line_names_the_step_time_and_tokens_while_it_is_shown(started):
    interface, _typed = started
    assert interface.status_text() == []
    with interface.showing("Run pytest -q", time.monotonic() - 65, lambda: 1234):
        shown = plain(interface.status_text())
        assert "Run pytest -q (1m 05s · ↓ 1.2k tokens · esc to interrupt)" in shown
    assert interface.status is None


CHOICES = [("y", "Yes"), ("a", "Yes, and don't ask again"), ("n", "No")]


def test_a_question_is_answered_with_its_letter_then_enter_and_typing_stays_out_of_the_box(started):
    interface, typed = started
    interface.open_question("Edit greet.py?", CHOICES)
    assert plain(interface.bar_text()) == tui.CHOICE_HINT
    assert interface.wait_answer(0.05) is None
    interface.test_clock.advance(tui.ANSWER_PAUSE_SECONDS)
    typed.send_text("a")
    assert until(lambda: interface.question.selected == 1)
    assert f"{tui.POINTER} a  Yes, and don't ask again" in plain(interface.question_text())
    typed.send_text("\r")
    assert interface.wait_answer() == "a"
    interface.close_question()
    assert typed_text(interface) == "" and interface.messages.empty()


def test_arrows_choose_too_and_escape_refuses(started):
    interface, typed = started
    interface.open_question("Run: pytest -q", CHOICES)
    interface.test_clock.advance(tui.ANSWER_PAUSE_SECONDS)
    typed.send_text(DOWN + DOWN + DOWN + UP + "\r")
    assert interface.wait_answer(3) == "a"
    interface.close_question()
    interface.open_question("Run: pytest -q", CHOICES)
    typed.send_text(ESCAPE)
    assert interface.wait_answer() == "n"
    assert interface.interrupted == []


def test_choosing_from_a_list_with_no_refusal_gives_nothing_on_escape(started):
    interface, typed = started
    answers = []
    asker = threading.Thread(target=lambda: answers.append(interface.choose("Which model?", [("1", "a"), ("2", "b")])))
    asker.start()
    assert until(interface.is_choosing)
    typed.send_text(ESCAPE)
    asker.join(3)
    assert answers == [None] and interface.question is None


def test_a_typed_answer_uses_the_box_and_gives_back_what_was_being_written(started):
    interface, typed = started
    typed.send_text("my draft")
    assert until(lambda: typed_text(interface) == "my draft")
    answers = []
    asker = threading.Thread(target=lambda: answers.append(interface.line("What should Arty do instead?")))
    asker.start()
    assert until(lambda: interface.wants_line and typed_text(interface) == "")
    assert plain(interface.bar_text()) == tui.LINE_HINT
    typed.send_text("use tabs\r")
    asker.join(3)
    assert answers == ["use tabs"] and interface.messages.empty()
    assert until(lambda: typed_text(interface) == "my draft")


def test_answers_for_a_shared_chat_are_polled_from_the_question(started):
    interface, typed = started
    lines = interface.answer_lines()
    interface.open_question("Edit a.py?", CHOICES)
    assert lines.line(0.05) is None
    interface.test_clock.advance(tui.ANSWER_PAUSE_SECONDS)
    typed.send_text("y\r")
    assert until(lambda: interface.question.answered.is_set())
    assert lines.line(0.05) == "y"
    lines.discard()


def test_up_brings_back_earlier_requests_kept_for_the_project_between_sessions(tmp_path):
    kept = tui.history_path(tmp_path / "config", tmp_path)
    with running(tmp_path, kept) as (interface, typed):
        typed.send_text("first request\r")
        assert interface.next_message() == "first request"
    with running(tmp_path, kept) as (interface, typed):
        assert until(lambda: "first request" in interface.buffer.history.get_strings(), 5)
        typed.send_text(UP)
        assert until(lambda: typed_text(interface) == "first request")
        typed.send_text("\r")
        assert interface.next_message() == "first request"


def test_ctrl_r_searches_the_earlier_requests(tmp_path):
    kept = tui.history_path(tmp_path / "config", tmp_path)
    with running(tmp_path, kept) as (interface, typed):
        typed.send_text("fix the login test\r")
        typed.send_text("add a dry run flag\r")
        assert [interface.next_message(), interface.next_message()] == ["fix the login test", "add a dry run flag"]
        typed.send_text(CTRL_R)
        assert until(lambda: interface.app.layout.is_searching)
        typed.send_text("login\r")
        assert until(lambda: typed_text(interface) == "fix the login test")
        typed.send_text("\r")
        assert interface.next_message() == "fix the login test"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_the_kept_requests_are_readable_only_by_you_and_separate_per_project(tmp_path):
    here = tui.history_path(tmp_path / "config", tmp_path / "a")
    there = tui.history_path(tmp_path / "config", tmp_path / "b")
    assert here != there and here.parent == tmp_path / "config" / tui.HISTORY_FOLDER
    assert stat.S_IMODE(here.stat().st_mode) == 0o600


def test_an_interface_that_cannot_start_says_why(tmp_path, monkeypatch):
    monkeypatch.setattr(tui.Interface, "build", lambda self: (_ for _ in ()).throw(OSError("no console here")))
    with pytest.raises(tui.InterfaceError, match="no console here"):
        tui.Interface(Host(tmp_path), commit=lambda line: None).start()


def test_queued_lines_can_be_taken_mid_turn_leaving_commands_for_later(started):
    interface, typed = started
    with interface.working():
        typed.send_text("also handle division by zero\r")
        typed.send_text("/status\r")
        assert until(lambda: len(interface.queued) == 2)
        taken = interface.take_queued(lambda text: not text.startswith("/"))
        assert taken == ["also handle division by zero"]
        assert interface.queued == ["/status"] and interface.sent == ["› also handle division by zero"]
    assert interface.next_message() == "/status"


def test_the_footer_names_the_model_branch_and_context_left_and_drops_extras_when_narrow():
    left, right = tui.footer_pieces(permissions.DEFAULT, False, "arty/qwen3:14b", "main", 62, 110)
    assert plain(left) == "  default mode (shift+tab to cycle) · ? for shortcuts"
    assert plain(right) == "arty/qwen3:14b · ⎇ main · 62% context left"
    left, right = tui.footer_pieces(permissions.AUTO_EDIT, True, "arty/qwen3:14b", "main", 62, 46)
    assert plain(left) + "  " + plain(right) == "  auto-edit mode · remote  62% context left"
    assert tui.fragment_width(left) + tui.fragment_width(right) <= 46
    left, right = tui.footer_pieces(permissions.DEFAULT, False, "arty/demo-coder", "shell-look", 90, 79)
    assert plain(left) == "  default mode · ? for shortcuts" and plain(right) == "⎇ shell-look · 90% context left"


def test_the_footer_shows_what_the_host_reports(started, monkeypatch):
    interface, _typed = started
    monkeypatch.setattr(interface, "room", lambda: 110)
    interface.host.details = ("arty/mimo-v2.6:9b", "shell-look", 87)
    shown = plain(interface.bar_text())
    assert shown.rstrip().endswith("arty/mimo-v2.6:9b · ⎇ shell-look · 87% context left")


def test_a_question_mark_in_an_empty_box_opens_the_shortcuts_and_escape_closes_them(started):
    interface, typed = started
    typed.send_text("?")
    assert until(lambda: interface.shortcuts_open)
    assert typed_text(interface) == "" and "shift+tab" in plain(interface.shortcuts_text())
    assert plain(interface.bar_text()) == tui.SHORTCUTS_HINT
    typed.send_text(ESCAPE)
    assert until(lambda: not interface.shortcuts_open)
    typed.send_text("why?")
    assert until(lambda: typed_text(interface) == "why?") and not interface.shortcuts_open


def test_enter_on_the_shortcuts_asks_for_the_full_help(started):
    interface, typed = started
    typed.send_text("?")
    assert until(lambda: interface.shortcuts_open)
    typed.send_text("\r")
    assert interface.next_message() == "/help" and not interface.shortcuts_open


def test_typing_closes_the_shortcuts_and_keeps_what_was_typed(started):
    interface, typed = started
    typed.send_text("?")
    assert until(lambda: interface.shortcuts_open)
    typed.send_text("f")
    assert until(lambda: typed_text(interface) == "f") and not interface.shortcuts_open


def test_the_empty_box_shows_a_placeholder_that_changes_while_arty_works(started):
    interface, _typed = started
    assert interface.placeholder() == tui.PLACEHOLDER
    with interface.working():
        assert interface.placeholder() == tui.BUSY_PLACEHOLDER


def test_the_placeholder_is_only_drawn_on_an_empty_first_line():
    placeholder = tui.Placeholder(lambda: "Ask")

    class Typed:
        def __init__(self, text, lineno=0, width=80):
            self.document = type("Document", (), {"text": text})()
            self.fragments, self.lineno, self.width = [("", text)], lineno, width

    assert plain(placeholder.apply_transformation(Typed("")).fragments) == "Ask"
    assert plain(placeholder.apply_transformation(Typed("x")).fragments) == "x"
    assert plain(placeholder.apply_transformation(Typed("", lineno=1)).fragments) == ""
    narrow = tui.Placeholder(lambda: tui.PLACEHOLDER)
    assert plain(narrow.apply_transformation(Typed("", width=30)).fragments) == tui.SHORT_PLACEHOLDER
    assert plain(narrow.apply_transformation(Typed("", width=5)).fragments) == ""


def test_a_permission_question_is_a_bordered_panel_with_its_choices(started):
    interface, _typed = started
    interface.open_question("Run: pytest -q?", CHOICES)
    rows = plain(interface.question_text()).split("\n")
    assert rows[0].startswith("╭─ Arty asks ") and rows[-1].startswith("╰") and rows[-1].endswith("╯")
    assert rows[1].startswith("│ Run: pytest -q?") and rows[1].endswith("│")
    assert any("  y  Yes" in row for row in rows) and tui.POINTER not in plain(interface.question_text())
    assert len({len(row) for row in rows}) == 1
    interface.pick_choice("y")
    assert f"{tui.POINTER} y  Yes" in plain(interface.question_text())


def test_a_long_question_wraps_inside_the_panel_at_phone_width(started, monkeypatch):
    interface, _typed = started
    monkeypatch.setattr(interface, "room", lambda: 45)
    interface.open_question("Run: python3 -c \"for n in range(1, 41): print(n)\"?", CHOICES)
    rows = plain(interface.question_text()).split("\n")
    assert all(len(row) == 45 for row in rows) and len(rows) > len(CHOICES) + 3
    assert "…" not in plain(interface.question_text())
    assert plain(interface.bar_text()) == tui.SHORT_CHOICE_HINT


def test_the_status_line_has_a_spinner_a_label_and_dim_details(started):
    interface, _typed = started
    with interface.showing("Run pytest -q", time.monotonic() - 3, lambda: 0):
        styles = [style for style, _text, *_rest in interface.status_text()]
        assert styles[0] == "class:spinner" and "class:status-label" in styles and styles[-1] == "class:dim"


def test_ctrl_o_asks_the_host_for_the_full_last_step(started):
    interface, typed = started
    asked = []
    interface.host.expand = lambda: asked.append(True)
    typed.send_text("\x0f")
    assert until(lambda: asked == [True])


def test_a_request_after_earlier_output_gets_a_blank_line_before_it(started):
    interface, _typed = started
    interface.scroll.write("● Read a.py\n")
    interface.show_request("next")
    assert interface.sent == ["● Read a.py", "", "› next"]


def test_a_live_answer_line_is_shown_until_it_is_finished(started):
    interface, _typed = started
    interface.show_partial("● The fix is")
    assert interface.live_lines() == ["● The fix is"]
    assert rows_text(interface.transcript.rows_for(80)) == [] and shown_rows(interface, 80, 10)[-1] == "● The fix is"


def test_the_bell_rings_through_the_interface_output(started):
    interface, _typed = started
    rung = []
    interface.app.output.bell = lambda: rung.append(True)
    interface.ring()
    assert until(lambda: rung == [True])


def test_a_narrow_status_line_drops_details_before_it_cuts_the_label():
    spinner, label, details = tui.status_parts(0, 65, "Run python3 -m pytest -q tests", 1234, 45)
    assert label == "Run python3 -m pytest -q tests" and details == " (1m 05s)"
    spinner, label, details = tui.status_parts(0, 65, "Run pytest -q", 1234, 100)
    assert details == " (1m 05s · ↓ 1.2k tokens · esc to interrupt)"
    assert len(spinner) + 1 + len(label) + len(details) <= 100


def test_the_interface_takes_the_alternate_screen_with_the_mouse_and_puts_it_back_when_it_stops(started):
    interface, _typed = started
    assert interface.app.full_screen and interface.app.mouse_support()


def test_a_short_conversation_starts_at_the_top_with_its_header_first():
    transcript = transcript_of(2)
    assert shown_by(transcript, height=10) == ["logo at 40", "line 1", "line 2"]


def test_a_long_conversation_follows_the_newest_lines_so_the_header_scrolls_away():
    transcript = transcript_of(20)
    assert shown_by(transcript) == ["line 16", "line 17", "line 18", "line 19", "line 20"]
    transcript.add_line("line 21")
    assert shown_by(transcript)[-1] == "line 21" and not transcript.is_scrolled()


def test_paging_up_holds_the_view_while_new_lines_arrive_and_paging_down_returns_to_the_newest():
    transcript = transcript_of(20)
    shown_by(transcript)
    transcript.page(-1)
    assert shown_by(transcript) == ["line 13", "line 14", "line 15", "line 16", "line 17"]
    transcript.add_line("line 21")
    assert shown_by(transcript)[0] == "line 13" and transcript.rows_below() == 4
    transcript.page(1)
    transcript.page(1)
    assert shown_by(transcript)[-1] == "line 21" and not transcript.is_scrolled()


def test_scrolling_stops_at_the_header_and_never_goes_past_it():
    transcript = transcript_of(20)
    shown_by(transcript)
    for _page in range(20):
        transcript.page(-1)
    assert shown_by(transcript)[0] == "logo at 40"


def test_the_mouse_wheel_scrolls_the_conversation_three_rows_at_a_time():
    transcript = transcript_of(20)
    control = tui.TranscriptControl(transcript, list)
    control.create_content(40, 5)
    assert control.mouse_handler(wheel(MouseEventType.SCROLL_UP)) is None
    assert shown_by(transcript)[-1] == "line 17"
    control.mouse_handler(wheel(MouseEventType.SCROLL_DOWN))
    assert shown_by(transcript)[-1] == "line 20"
    assert control.mouse_handler(wheel(MouseEventType.MOUSE_UP)) is NotImplemented


def test_a_block_is_drawn_again_for_the_new_width_and_long_lines_wrap_to_it():
    transcript = transcript_of(0)
    transcript.add_line("x" * 30)
    assert rows_text(transcript.rows_for(40)) == ["logo at 40", "x" * 30]
    assert rows_text(transcript.rows_for(12)) == ["logo at 12", "x" * 12, "x" * 12, "x" * 6]


def test_colours_and_wide_characters_and_tabs_are_kept_when_a_line_is_cut():
    rows = tui.screen_rows("\033[31mred\033[0m\tend", 40)
    assert plain(rows[0]) == "red     end" and rows[0][0][0] != ""
    assert rows_text(tui.screen_rows("日本語", 4)) == ["日本", "語"]


def test_page_keys_scroll_the_conversation_and_enter_brings_it_back_to_the_newest(started):
    interface, typed = started
    for number in range(60):
        interface.scroll.write(f"line {number}\n")
    assert until(lambda: interface.transcript.total == 60)
    typed.send_text("\x1b[5~")
    assert until(interface.transcript.is_scrolled)
    assert until(lambda: "more lines below" in plain(interface.scrolled_text()))
    typed.send_text("next\r")
    assert until(lambda: not interface.transcript.is_scrolled())


def test_a_question_brings_the_conversation_back_to_the_newest_lines(started):
    interface, _typed = started
    interface.transcript.top = 0
    interface.open_question("Edit a.py?", [("y", "Yes"), ("n", "No")])
    assert not interface.transcript.is_scrolled()
    interface.close_question()


def test_printed_output_and_the_header_land_in_the_conversation(started):
    interface, _typed = started
    interface.show_block(lambda width: [f"header {width}"])
    interface.stray.write("printed by a library\n")
    assert rows_text(interface.transcript.rows_for(50)) == ["header 50", "printed by a library"]


def test_a_stuck_interface_still_leaves_the_alternate_screen(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(tui, "JOIN_SECONDS", 0.05)
    with create_pipe_input() as typed, create_app_session(input=typed, output=DummyOutput()):
        interface = tui.Interface(Host(tmp_path), commit=lambda line: None)
        interface.start()
        output = interface.app.output
        for name in ("disable_mouse_support", "quit_alternate_screen", "reset_attributes", "show_cursor", "flush"):
            monkeypatch.setattr(output, name, lambda name=name: calls.append(name))
        monkeypatch.setattr(interface, "leave", lambda: None)
        interface.stop()
        assert {"disable_mouse_support", "quit_alternate_screen", "show_cursor", "flush"} <= set(calls)
        interface.app.loop.call_soon_threadsafe(interface.app.exit)
        interface.thread.join(2)


KEYS_LAND_SECONDS = 0.15
GIT_PUSH_CHOICES = [("y", "Yes"), ("a", "Yes, and don't ask again for `git push`"),
                    ("s", "Yes, and save a rule for `git push`"), ("n", "No")]


def opened_while_typing(interface, typed, text="Run: git push --force origin main"):
    typed.send_text("and al")
    assert until(lambda: typed_text(interface) == "and al")
    interface.open_question(text, GIT_PUSH_CHOICES)


def keys_land(interface, typed, keys):
    typed.send_text(keys)
    time.sleep(KEYS_LAND_SECONDS)


@pytest.mark.parametrize("typed_ahead", ["\r", "and also fix the tests\r", "thanks\r", "no wait\r", "y\r", "a\r"])
def test_keys_typed_straight_after_a_question_opens_never_answer_it(started, typed_ahead):
    interface, typed = started
    with interface.working():
        opened_while_typing(interface, typed)
        keys_land(interface, typed, typed_ahead)
        assert interface.wait_answer(0.2) is None
        assert interface.question.selected is None
    interface.close_question()


def test_keys_typed_without_a_pause_after_the_question_settles_still_do_not_answer_it(started):
    interface, typed = started
    opened_while_typing(interface, typed)
    keys_land(interface, typed, "and also")
    interface.test_clock.advance(tui.ANSWER_PAUSE_SECONDS)
    keys_land(interface, typed, " fix it")
    interface.test_clock.advance(tui.ANSWER_PAUSE_SECONDS / 4)
    keys_land(interface, typed, "s\r")
    assert interface.wait_answer(0.2) is None and interface.question.selected is None


def test_enter_alone_never_picks_a_choice_even_after_a_pause(started):
    interface, typed = started
    interface.open_question("Run: git push --force origin main", GIT_PUSH_CHOICES)
    interface.test_clock.advance(tui.ANSWER_PAUSE_SECONDS)
    keys_land(interface, typed, "\r")
    assert interface.wait_answer(0.2) is None
    assert f"{tui.POINTER} y" not in plain(interface.question_text())


def test_a_letter_that_is_not_a_choice_drops_the_selection_so_typing_yes_does_not_save(started):
    interface, typed = started
    interface.open_question("Run: git push --force origin main", GIT_PUSH_CHOICES)
    interface.test_clock.advance(tui.ANSWER_PAUSE_SECONDS)
    keys_land(interface, typed, "yes\r")
    assert interface.wait_answer(0.2) is None and interface.question.selected is None


def test_after_a_pause_a_letter_then_enter_answers_and_escape_always_refuses(started):
    interface, typed = started
    opened_while_typing(interface, typed)
    keys_land(interface, typed, "thanks")
    interface.test_clock.advance(tui.ANSWER_PAUSE_SECONDS)
    keys_land(interface, typed, "n")
    assert until(lambda: interface.question.selected == 3)
    typed.send_text("\r")
    assert interface.wait_answer(3) == "n"
    interface.close_question()
    interface.open_question("Run: git push --force origin main", GIT_PUSH_CHOICES)
    typed.send_text(ESCAPE)
    assert interface.wait_answer(3) == "n"
    interface.close_question()
    assert typed_text(interface) == "and al"
