import stat
import sys
import threading
import time
from contextlib import contextmanager

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
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


@contextmanager
def running(root, history_file=None):
    committed, interrupted = [], []
    with create_pipe_input() as typed, create_app_session(input=typed, output=DummyOutput()):
        interface = tui.Interface(Host(root), history_file, commit=committed.append,
                                  interrupt=lambda: interrupted.append(True))
        interface.start()
        interface.sent, interface.interrupted = committed, interrupted
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
    assert plain(interface.bar_text()) == "  default mode (shift+tab to cycle) · esc to interrupt"
    typed.send_text("keep this" + SHIFT_TAB)
    assert until(lambda: interface.host.current == permissions.AUTO_EDIT)
    assert plain(interface.bar_text()).startswith("  auto-edit mode") and typed_text(interface) == "keep this"
    interface.host.is_shared = True
    assert plain(interface.bar_text()).endswith("· remote")


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
    typed.send_text(DOWN + DOWN + UP + "\r")
    assert interface.wait_answer() == "a"
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
