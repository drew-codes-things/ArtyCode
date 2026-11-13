import contextlib
import io
import sys

import pytest

from arty_cli import cli, permissions, screen, settings
from tests.test_cli import FakeBrain, ready

assert ready
BLOCK_WIDTH = 80


class Tty(io.StringIO):
    def isatty(self):
        return True


class FakeInterface:
    def __init__(self, *lines):
        self.lines = list(lines)
        self.shown, self.worked, self.opened, self.asked_lines, self.statuses = [], [], [], [], []
        self.answers = []
        self.stopped = False
        self.scroll, self.stray = Tty(), Tty()
        self.question = None
        self.failure = None
        self.blocks = []

    def next_message(self):
        item = self.lines.pop(0) if self.lines else None
        if isinstance(item, BaseException):
            raise item
        return item

    def show_request(self, text):
        self.shown.append(text)

    @contextlib.contextmanager
    def working(self):
        self.worked.append("start")
        yield
        self.worked.append("end")

    @contextlib.contextmanager
    def showing(self, label, started_at, tokens):
        self.statuses.append(label)
        yield self

    def open_question(self, text, choices):
        self.question = (text, list(choices))
        self.opened.append(self.question)

    def wait_answer(self, seconds=None):
        return self.answers.pop(0) if self.answers else None

    def close_question(self):
        self.question = None

    def choose(self, text, choices):
        self.open_question(text, choices)
        self.close_question()
        return self.answers.pop(0) if self.answers else None

    def line(self, text):
        self.asked_lines.append(text)
        return self.answers.pop(0) if self.answers else ""

    def answer_lines(self):
        return "console answers"

    def show_block(self, render):
        self.blocks.append(render)
        self.scroll.write("\n".join(render(BLOCK_WIDTH)) + "\n")

    def stop(self):
        self.stopped = True


@pytest.fixture
def interactive(ready, monkeypatch):
    monkeypatch.setattr(screen, "is_interactive", lambda: True)
    monkeypatch.setenv("NO_COLOR", "1")
    return ready


def session_for(tmp_path, out=None, err=None):
    workspace = cli.tools.Workspace(tmp_path)
    terminal = cli.Terminal(out or io.StringIO(), err or io.StringIO())
    gate = permissions.Gate(workspace, terminal.ask)

    class Saved:
        messages, id, model = [], "s1", ""

        def save(self, messages, model, **kept):
            pass

    return cli.Session(FakeBrain(settings.Settings(base_url="http://b/v1", api_key="k")), workspace, gate, terminal,
                       Saved())


def test_plain_is_a_flag_and_off_by_default():
    assert cli.parse_arguments(["--plain"]).plain and not cli.parse_arguments([]).plain


def test_the_interface_is_only_tried_on_a_real_terminal(interactive, tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "arty_cli.tui", None)
    piped = session_for(tmp_path)
    assert cli.started_interface(piped) is None
    assert piped.terminal.err.getvalue() == ""


def test_without_the_interface_library_the_plain_prompt_is_used_and_it_says_so(interactive, tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "arty_cli.tui", None)
    session = session_for(tmp_path, Tty(), Tty())
    assert cli.started_interface(session) is None
    assert "plain prompt" in session.terminal.err.getvalue()


def test_an_interface_that_cannot_start_falls_back_to_the_plain_prompt(interactive, tmp_path, monkeypatch):
    from arty_cli import tui

    def broken(self):
        raise tui.InterfaceError("no console here")

    monkeypatch.setattr(tui.Interface, "start", broken)
    session = session_for(tmp_path, Tty(), Tty())
    assert cli.started_interface(session) is None
    shown = session.terminal.err.getvalue()
    assert "no console here" in shown and "plain prompt" in shown


def test_asking_for_plain_never_starts_the_interface(interactive, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "started_interface", lambda session: pytest.fail("the interface was started"))
    monkeypatch.setattr("builtins.input", lambda prompt="": "/exit")
    monkeypatch.setattr(cli.prompt_line, "line_reader", lambda *given: None)
    cli.chat(session_for(tmp_path), plain=True)


def test_requests_typed_in_the_interface_run_one_after_another_and_the_terminal_is_put_back(interactive, tmp_path):
    session = session_for(tmp_path)
    out, err = session.terminal.out, session.terminal.err
    interface = FakeInterface("hello", "and again")
    cli.chat_in_interface(session, interface, share=False)
    assert interface.shown == ["hello", "and again"]
    assert [call[-1]["content"] for call in session.client.calls] == ["hello", "and again"]
    assert "Hi there." in interface.scroll.getvalue()
    assert interface.stopped and session.terminal.console is None
    assert (session.terminal.out, session.terminal.err) == (out, err)
    assert sys.stdout is not interface.stray


def test_slash_exit_leaves_the_interface(interactive, tmp_path):
    session = session_for(tmp_path)
    interface = FakeInterface("/exit", "never asked")
    cli.chat_in_interface(session, interface, share=False)
    assert session.client.calls == [] and interface.stopped


def test_being_woken_at_the_prompt_goes_round_again_instead_of_leaving(interactive, tmp_path):
    session = session_for(tmp_path)
    interface = FakeInterface(KeyboardInterrupt(), "hello")
    cli.chat_in_interface(session, interface, share=False)
    assert interface.shown == ["hello"]


def test_a_turn_runs_marked_as_working_so_typing_is_queued_and_esc_can_stop_it(interactive, tmp_path):
    session = session_for(tmp_path)
    interface = FakeInterface("hello")
    seen = []
    session.client.reply = lambda messages, tools, on_text: (
        seen.append(list(interface.worked)), {"role": "assistant", "content": "ok", "tool_calls": []})[1]
    cli.chat_in_interface(session, interface, share=False)
    assert seen[0][-1] == "start"


def console_terminal():
    terminal = cli.Terminal(Tty(), Tty())
    terminal.console = FakeInterface()
    return terminal, terminal.console


def test_a_permission_question_is_asked_in_the_interface_and_the_answer_is_noted(interactive):
    terminal, console = console_terminal()
    console.answers = ["a"]
    assert terminal.ask("Edit greet.py?", "+new line") == permissions.ALWAYS
    question, choices = console.opened[0]
    assert question == "Edit greet.py?" and [key for key, _meaning in choices] == ["y", "a", "n"]
    assert console.question is None
    shown = terminal.err.getvalue()
    assert "+new line" in shown and "Edit greet.py? → Yes, and don't ask again" in shown
    assert "Choose y" not in shown


def test_refusing_in_the_interface_then_saying_what_to_do_instead_uses_its_box(interactive):
    terminal, console = console_terminal()
    console.answers = ["n", "use tabs"]
    assert terminal.ask("Run: rm -rf build") == permissions.NO
    assert terminal.instead() == "use tabs"
    assert console.asked_lines == [screen.INSTEAD_QUESTION]


def test_a_plan_is_reviewed_in_the_interface(interactive):
    terminal, console = console_terminal()
    console.answers = ["n", "smaller steps"]
    assert terminal.review_plan("1. Do it") == (permissions.NO, "smaller steps")
    assert console.opened[0][0] == screen.PLAN_QUESTION and console.asked_lines == [screen.PLAN_FEEDBACK_QUESTION]


def test_with_the_interface_the_status_goes_to_it_and_no_key_watcher_is_started(interactive):
    terminal, console = console_terminal()
    with terminal.working("Run pytest -q"):
        pass
    with terminal.thinking():
        pass
    assert console.statuses == ["Run pytest -q", screen.waiting.LABEL]
    with terminal.interruptible():
        assert terminal.stop_key is None
    assert terminal.err.getvalue() == ""


class Listing(FakeBrain):
    def available_models(self):
        return [("arty/qwen3:14b", "14B"), ("arty/mimo-v2.6:9b", "9B, sees images")]


def test_slash_model_picks_from_a_list_in_the_interface(interactive, tmp_path, monkeypatch):
    session = session_for(tmp_path)
    session.client = Listing(settings.Settings(base_url="http://b/v1", api_key="k"))
    session.terminal.console = console = FakeInterface()
    console.answers = ["2"]
    monkeypatch.setattr(cli.settings, "remember", lambda **changes: None)
    session.command("/model")
    question, choices = console.opened[0]
    assert choices[1] == ("2", "arty/mimo-v2.6:9b  9B, sees images")
    assert session.client.model == "arty/mimo-v2.6:9b"


class Bridge:
    title = "laptop"

    def __init__(self):
        self.write_prompt = print
        self.waker = cli.remote.Waker(lambda: None)

    def send_history(self, events):
        pass

    def stop(self):
        pass

    def take_notices(self):
        return []


def test_sharing_from_the_interface_answers_through_it_and_writes_no_prompt_of_its_own(interactive, tmp_path, monkeypatch):
    made = []

    def connect(client, details, lines):
        made.append((Bridge(), lines))
        return made[0][0]

    monkeypatch.setattr(cli.remote, "connect", connect)
    session = session_for(tmp_path)
    session.terminal.console = FakeInterface()
    session.start_remote()
    bridge, lines = made[0]
    assert lines == "console answers"
    assert bridge.write_prompt("Choose y: ") is None and bridge.write_prompt is not print


def test_in_the_interface_the_header_comes_first_with_the_logo_details_and_its_own_keys(interactive, tmp_path):
    session = session_for(tmp_path)
    interface = FakeInterface()
    cli.chat_in_interface(session, interface, share=False)
    shown = interface.scroll.getvalue()
    assert shown.startswith("  " + cli.look.LOGO[0] + "\n") and "ArtyCode " + cli.__version__ in shown
    assert "  model    arty/qwen3:14b\n" in shown and "  context  " in shown
    assert "Esc stops a request" in shown and "Enter while Arty works queues" in shown
    assert "Ctrl-C stops a request" not in shown and "╭" not in shown


def test_the_header_says_rules_off_once(interactive, tmp_path):
    session = session_for(tmp_path)
    interface = FakeInterface()
    cli.chat_in_interface(session, interface, share=False)
    shown = interface.scroll.getvalue()
    assert "  rules    off\n" in shown and "off: off" not in shown


def test_the_header_is_handed_over_as_a_block_drawn_for_each_width(interactive, tmp_path):
    session = session_for(tmp_path)
    interface = FakeInterface()
    cli.chat_in_interface(session, interface, share=False)
    render = interface.blocks[0]
    assert render(110)[0] == "  " + cli.look.LOGO[0] and render(30)[0].startswith("  ◆ ArtyCode")


def test_a_crash_in_the_interface_still_puts_the_terminal_back(interactive, tmp_path):
    session = session_for(tmp_path)
    out, err = session.terminal.out, session.terminal.err
    interface = FakeInterface(RuntimeError("boom"))
    with pytest.raises(RuntimeError, match="boom"):
        cli.chat_in_interface(session, interface, share=False)
    assert interface.stopped and (session.terminal.out, session.terminal.err) == (out, err)
    assert sys.stdout is not interface.stray


def test_help_names_the_keys_of_the_interface(interactive, tmp_path):
    session = session_for(tmp_path)
    session.command("/help")
    shown = session.terminal.err.getvalue()
    for wanted in ("Alt+Enter", "Ctrl-R", "queue", "twice to leave"):
        assert wanted in shown


def test_help_and_the_shortcut_panel_say_how_to_scroll_the_conversation(interactive, tmp_path):
    session = session_for(tmp_path)
    session.command("/help")
    row = next(line for line in session.terminal.err.getvalue().splitlines() if "PageUp, PageDown" in line)
    assert "scroll the conversation, as the mouse wheel does" in row
    assert ("pgup pgdn", "scroll the chat") in cli.look.SHORTCUTS


def test_the_answer_is_noted_after_an_arrow(interactive):
    terminal, console = console_terminal()
    console.answers = ["y"]
    terminal.ask("Run: python3 calc.py")
    assert "Run: python3 calc.py → Yes" in terminal.err.getvalue()


class RingingInterface(FakeInterface):
    def __init__(self, *lines):
        super().__init__(*lines)
        self.rung, self.partials = [], []

    def ring(self):
        self.rung.append(True)

    def show_partial(self, text):
        self.partials.append(text)


def test_answers_in_the_interface_are_drawn_as_markdown_under_one_mark(interactive, tmp_path):
    session = session_for(tmp_path)
    interface = RingingInterface("hello")

    def reply(messages, tools, on_text):
        on_text("Use **bold** and `code`.\n- one\n")
        return {"role": "assistant", "content": "Use **bold** and `code`.\n- one\n", "tool_calls": []}

    session.client.reply = reply
    cli.chat_in_interface(session, interface, share=False)
    shown = interface.scroll.getvalue()
    assert "● Use bold and code.\n  • one\n" in shown and "**" not in shown
    assert session.terminal.view is None


def test_a_step_in_the_interface_is_drawn_with_its_result_and_kept_for_slash_last(interactive, tmp_path):
    session = session_for(tmp_path)
    interface = RingingInterface("count", "/last")
    command = {"id": "c1", "type": "function",
               "function": {"name": "run_command", "arguments": '{"command": "seq 1 9"}'}}
    replies = [{"role": "assistant", "content": "", "tool_calls": [command]},
               {"role": "assistant", "content": "Done.", "tool_calls": []}]
    session.client.reply = lambda messages, tools, on_text: replies.pop(0)
    session.gate.mode = permissions.ALLOW_ALL
    cli.chat_in_interface(session, interface, share=False)
    shown = interface.scroll.getvalue()
    assert "● Run seq 1 9\n  └ 9 (exit code 0)\n    1\n    2\n    3\n    … +6 lines" in shown
    assert shown.rstrip().endswith("● Run seq 1 9\n    1\n    2\n    3\n    4\n    5\n    6\n    7\n    8\n    9")


def test_slash_last_at_the_plain_prompt_prints_the_whole_output(interactive, tmp_path):
    session = session_for(tmp_path)
    session.command("/last")
    assert cli.view.NO_STEP_YET in session.terminal.err.getvalue()
    session.terminal.last_step = cli.view.Step("Run ls", "run_command", {"command": "ls"}, "a.py\nb.py\n[exit code 0]")
    session.command("/last")
    assert session.terminal.err.getvalue().endswith("● Run ls\n    a.py\n    b.py\n")


def test_a_permission_question_in_the_interface_rings_and_its_diff_is_numbered(interactive):
    terminal, console = console_terminal()
    console.ring = lambda: console.rung.append(True)
    console.rung = []
    terminal.view = cli.view.View(terminal.err, cli.look.Theme(colour=False),
                                  notifier=cli.notify.Notifier(console.ring, command=lambda title, text: None))
    console.answers = ["y"]
    terminal.ask("Edit calc.py?", "--- calc.py\n+++ calc.py\n@@ -1,1 +1,1 @@\n-x = 1\n+x = 2")
    assert console.rung == [True]
    assert "    1 -x = 1\n    1 +x = 2\n" in terminal.err.getvalue()


def test_notices_follow_the_setting(interactive, tmp_path):
    session = session_for(tmp_path)
    assert cli.interface_notifier(session, RingingInterface()) is not None
    assert cli.interface_notifier(session, FakeInterface()) is None
    session.client.settings = settings.Settings(notify="off")
    assert cli.interface_notifier(session, RingingInterface()) is None


def test_the_footer_gets_the_model_branch_and_context_left(interactive, tmp_path):
    session = session_for(tmp_path)
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    host = cli.SessionHost(session)
    assert host.footer_details() == ("arty/qwen3:14b", "main", None)
    host.measure()
    model, branch, room = host.footer_details()
    assert 50 <= room.left_percent < 100
    assert 0 < room.used < room.limit == session.compact_limit()
    session.messages.append({"role": "user", "content": "x" * 200_000})
    host.measure()
    assert host.footer_details()[2].left_percent == 0


def test_slash_last_prefers_the_last_step_with_output_over_a_later_checklist(interactive, tmp_path):
    session = session_for(tmp_path)
    terminal = session.terminal
    run = {"id": "c1", "function": {"name": "run_command", "arguments": '{"command": "ls"}'}}
    checklist = {"id": "c2", "function": {"name": "todo_write", "arguments": '{"items": []}'}}
    terminal.show_tool_result(run, "a.py\n[exit code 0]")
    terminal.show_tool_result(checklist, "No items")
    session.command("/last")
    assert terminal.err.getvalue().endswith("● Run ls\n    a.py\n")


def test_a_bad_notify_setting_still_puts_the_terminal_back_and_stops_the_interface(interactive, tmp_path):
    session = session_for(tmp_path)
    session.client.settings = settings.Settings(notify="sometimes")
    interface = RingingInterface()
    terminal = session.terminal
    before = (terminal.out, terminal.err)
    with pytest.raises(settings.SettingsError):
        cli.chat_in_interface(session, interface, share=False)
    assert interface.stopped and (terminal.out, terminal.err) == before and terminal.view is None
