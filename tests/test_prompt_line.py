import io
import sys

import pytest

from arty_cli import cli, permissions, prompt_line


@pytest.mark.parametrize("mode, following", [
    (permissions.DEFAULT, permissions.AUTO_EDIT),
    (permissions.AUTO_EDIT, permissions.PLAN),
    (permissions.PLAN, permissions.DEFAULT),
    (permissions.ALLOW_ALL, permissions.DEFAULT),
])
def test_the_mode_key_cycles_and_never_reaches_allow_all(mode, following):
    assert prompt_line.next_mode(mode) == following


class FakeReadline:
    def __init__(self):
        self.bound, self.hooks, self.inserted = [], [], []
        self.buffer, self.completer, self.display_hook, self.delimiters = "", None, None, None

    def set_completer(self, completer):
        self.completer = completer

    def set_completer_delims(self, delimiters):
        self.delimiters = delimiters

    def set_completion_display_matches_hook(self, hook):
        self.display_hook = hook

    def get_line_buffer(self):
        return self.buffer

    def parse_and_bind(self, line):
        self.bound.append(line)

    def set_startup_hook(self, hook=None):
        self.hooks.append(hook)
        if hook:
            hook()

    def insert_text(self, text):
        self.inserted.append(text)


def readline_reader(*answers, choices=None):
    module, written, given = FakeReadline(), io.StringIO(), list(answers)
    prompts = []

    def read(prompt):
        prompts.append(prompt)
        return given.pop(0)

    reader = prompt_line.ReadlineLine(module, read=read, write=written.write, columns=lambda: 80, choices=choices)
    return reader, module, written, prompts


def test_on_linux_shift_tab_is_bound_to_send_a_marked_line():
    _reader, module, _written, _prompts = readline_reader()
    assert module.bound == [f'"\\e[Z": "{prompt_line.MODE_MARK}\\n"']


def test_on_linux_a_marked_line_means_change_mode_and_keeps_what_was_typed():
    reader, module, written, prompts = readline_reader(f"fix the te{prompt_line.MODE_MARK}", "fix the tests")
    assert reader.read("plan › ", 7, "") == ("fix the te", True)
    assert "\033[F\033[2K" in written.getvalue()
    assert reader.read("default › ", 10, "fix the te") == ("fix the tests", False)
    assert module.inserted == ["fix the te"]
    assert module.hooks[-1] is None
    assert prompts == ["plan › ", "default › "]


def test_on_linux_a_plain_line_is_returned_as_typed():
    reader, _module, written, _prompts = readline_reader("hello")
    assert reader.read("default › ", 10, "") == ("hello", False)
    assert written.getvalue() == ""


def test_readline_prompts_mark_colour_codes_as_taking_no_room():
    painted = "\033[38;5;167mplan\033[0m › "
    assert prompt_line.for_readline(painted) == "\001\033[38;5;167m\002plan\001\033[0m\002 › "


def console_reader(keys, history=None, choices=None):
    given, written = list(keys), io.StringIO()
    reader = prompt_line.ConsoleLine(lambda: given.pop(0), written.write, choices=choices, columns=lambda: 80)
    reader.history = list(history or [])
    return reader, written


def test_on_windows_shift_tab_means_change_mode_and_keeps_what_was_typed():
    reader, written = console_reader(["h", "i", "\x00", "\x0f"])
    assert reader.read("default › ", 10, "") == ("hi", True)
    assert written.getvalue().startswith("default › hi")


def test_on_windows_shift_tab_also_arrives_with_the_extended_key_prefix():
    reader, _written = console_reader(["\xe0", "\x0f"])
    assert reader.read("› ", 2, "") == ("", True)


def test_on_windows_typing_editing_and_enter_give_the_line():
    keys = list("helo") + ["\xe0", "K", "l", "\xe0", "G", "\xe0", "S", "H", "\xe0", "O", "\x08", "o", "!", "\r"]
    reader, _written = console_reader(keys)
    assert reader.read("› ", 2, "") == ("Hello!", False)
    assert reader.history == ["Hello!"]


def test_on_windows_the_line_starts_with_what_was_kept():
    reader, written = console_reader(["s", "\r"])
    assert reader.read("plan › ", 7, "test") == ("tests", False)
    assert written.getvalue().startswith("plan › test")


def test_on_windows_up_brings_back_the_last_line():
    reader, _written = console_reader(["\xe0", "H", "\r"], history=["run the tests"])
    assert reader.read("› ", 2, "") == ("run the tests", False)


@pytest.mark.parametrize("key, raised", [("\x03", KeyboardInterrupt), ("\x1a", EOFError), ("\x04", EOFError)])
def test_on_windows_ctrl_c_and_ctrl_z_behave_like_the_normal_prompt(key, raised):
    reader, _written = console_reader([key])
    with pytest.raises(raised):
        reader.read("› ", 2, "")


def test_the_prompt_shows_the_mode_in_arty_red_or_plain():
    coloured, plain = cli.Terminal(), cli.Terminal()
    coloured.colour, plain.colour = True, False
    shown, width = prompt_line.mode_prompt(coloured, permissions.PLAN)
    assert cli.TRUE_COLOUR_ACCENT in shown or cli.PALETTE_ACCENT in shown
    assert width == len("plan › ")
    assert prompt_line.mode_prompt(plain, permissions.AUTO_EDIT) == ("auto-edit › ", len("auto-edit › "))


class CyclingReader:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def read(self, prompt, width, kept):
        self.calls.append((prompt, kept))
        return self.results.pop(0)


def test_the_chat_prompt_cycles_the_session_mode_and_keeps_the_text(ready_session):
    reader = CyclingReader(("fix", True), ("fix", True), ("fix it", False))
    assert cli.read_request(ready_session, reader) == "fix it"
    assert ready_session.gate.mode == permissions.PLAN
    assert [call[0] for call in reader.calls] == ["default › ", "auto-edit › ", "plan › "]
    assert [call[1] for call in reader.calls] == ["", "fix", "fix"]


def test_no_reader_means_the_plain_prompt(ready_session, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": "hello")
    assert cli.read_request(ready_session, None) == "hello"
    assert ready_session.gate.mode == permissions.DEFAULT


def test_the_reader_is_picked_for_the_platform(monkeypatch):
    monkeypatch.setattr(cli, "is_interactive", lambda: False)
    assert prompt_line.line_reader(cli.is_interactive) is None
    monkeypatch.setattr(cli, "is_interactive", lambda: True)
    monkeypatch.setattr(prompt_line.os, "name", "posix")
    monkeypatch.setitem(sys.modules, "readline", FakeReadline())
    assert isinstance(prompt_line.line_reader(cli.is_interactive), prompt_line.ReadlineLine)
    monkeypatch.setattr(prompt_line.os, "name", "nt")
    monkeypatch.setattr(prompt_line, "console_keys", lambda: lambda: "\r")
    assert isinstance(prompt_line.line_reader(cli.is_interactive), prompt_line.ConsoleLine)


@pytest.fixture
def ready_session(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "config"))
    class Client:
        model = "arty/x"

    class Saved:
        messages, id = [], "s1"

    workspace = cli.tools.Workspace(tmp_path)
    terminal = cli.Terminal(out=io.StringIO(), err=io.StringIO())
    terminal.colour = False
    gate = permissions.Gate(workspace, permissions.refuse)
    return cli.Session(Client(), workspace, gate, terminal, Saved())


def test_on_windows_keys_are_polled_so_a_message_from_afar_can_wake_the_prompt():
    ready, slept = [False, False, True], []
    next_key = prompt_line.polled_keys(lambda: ready.pop(0), lambda: "x", slept.append)
    assert next_key() == "x"
    assert slept == [prompt_line.KEY_POLL_SECONDS] * 2


def test_while_remote_control_is_on_the_prompt_says_so(ready_session):
    plain = cli.Terminal()
    plain.colour = False
    assert prompt_line.mode_prompt(plain, permissions.DEFAULT, shared=True) == (
        "default · remote › ", len("default · remote › "))
    ready_session.remote = object()
    reader = CyclingReader(("hi", False))
    cli.read_request(ready_session, reader)
    assert reader.calls[0][0] == "default · remote › "


COMMANDS = {"/model": "switch model", "/mode": "how much is asked", "/help": "this list"}


def test_on_linux_slash_at_the_start_lists_commands_and_tab_completes_them():
    reader, module, written, _prompts = readline_reader("/model", choices=lambda: COMMANDS)
    assert {prompt_line.SLASH_SHOWS_COMMANDS, "tab: complete", "set show-all-if-ambiguous on"} <= set(module.bound)
    assert module.delimiters == prompt_line.COMMAND_DELIMITERS
    reader.read("default › ", 10, "")
    module.buffer = "/mo"
    offered = [module.completer("/mo", state) for state in range(3)]
    assert offered == ["/model", "/mode", None]
    module.display_hook("/mo", ["/model", "/mode"], 6)
    shown = written.getvalue()
    assert "/model  switch model" in shown and "/mode   how much is asked" in shown
    assert shown.endswith("default › /mo")


def test_on_linux_a_slash_inside_a_request_offers_nothing():
    _reader, module, _written, _prompts = readline_reader(choices=lambda: COMMANDS)
    module.buffer = "look in src/"
    assert module.completer("src/", 0) is None


def test_on_linux_without_choices_nothing_extra_is_bound():
    _reader, module, _written, _prompts = readline_reader()
    assert module.completer is None


def test_on_windows_slash_first_shows_the_menu_and_tab_finishes_a_single_match():
    reader, written = console_reader(["/", "h", "\t", "\r"], choices=lambda: COMMANDS)
    assert reader.read("› ", 2, "") == ("/help ", False)
    shown = written.getvalue()
    assert "/model  switch model" in shown and "/help   this list" in shown


def test_on_windows_tab_with_several_matches_shows_only_those():
    reader, written = console_reader(["/", "m", "o", "\t", "\r"], choices=lambda: COMMANDS)
    assert reader.read("› ", 2, "") == ("/mo", False)
    after_tab = written.getvalue().split("› ")[-2]
    assert "/model" in after_tab and "/help" not in after_tab


MODEL_LIST = {"arty/qwen3:14b": "14B, all tools", "own/Coder": "private coder"}


def test_on_linux_tab_after_slash_model_lists_the_models_with_what_they_do():
    reader, module, written, _prompts = readline_reader("/model own/Coder", choices=lambda: COMMANDS)
    reader.models = lambda: MODEL_LIST
    reader.read("default › ", 10, "")
    module.buffer = "/model "
    assert [module.completer("", state) for state in range(3)] == ["arty/qwen3:14b", "own/Coder", None]
    module.display_hook("", ["arty/qwen3:14b", "own/Coder"], 14)
    assert "own/Coder       private coder" in written.getvalue()


def test_on_windows_tab_after_slash_model_finishes_the_one_model_that_fits():
    reader, _written = console_reader(list("/model o") + ["\t", "\r"], choices=lambda: COMMANDS)
    reader.models = lambda: MODEL_LIST
    assert reader.read("› ", 2, "") == ("/model own/Coder ", False)


class Plain:
    def paint(self, text, colour):
        return text


def mode_box(mode=permissions.DEFAULT, shared=False, columns=40):
    return prompt_line.mode_box(Plain(), mode, shared, columns=lambda: columns)


def test_the_box_is_a_rule_above_and_below_the_prompt_row_with_the_mode_bar_under_it():
    box = mode_box()
    assert box.rule == "─" * 39
    assert box.bar.startswith("  default mode (shift+tab to cycle)") and len(box.bar) == 39
    drawn = box.opened()
    assert drawn.startswith(prompt_line.CLEAR_BELOW + box.rule)
    assert drawn.count(box.rule) == 2 and drawn.index(box.bar) > drawn.rindex(box.rule)
    assert drawn.endswith("\033[2A\r")


def test_the_bar_names_the_mode_how_to_change_it_how_to_interrupt_and_remote_control():
    wide = mode_box(permissions.PLAN, shared=True, columns=120)
    assert wide.bar == "  plan mode (shift+tab to cycle) · esc to interrupt · remote"
    assert mode_box(columns=120).bar == "  default mode (shift+tab to cycle) · esc to interrupt"
    assert len(mode_box(columns=30).bar) == 29


def test_on_linux_the_box_is_drawn_first_and_swapped_for_the_typed_line_on_enter():
    reader, _module, written, prompts = readline_reader("fix the tests")
    box = mode_box()
    assert reader.read("› ", 2, "", box) == ("fix the tests", False)
    shown = written.getvalue()
    assert shown.startswith(box.opened())
    assert shown.endswith(prompt_line.UP_A_ROW * 2 + prompt_line.CLEAR_BELOW + "› fix the tests\r\n")
    assert prompts == ["› "]


def test_on_linux_a_line_that_ran_onto_a_second_row_is_cleared_from_the_right_row():
    typed = "x" * 100
    reader, _module, written, _prompts = readline_reader(typed)
    reader.read("› ", 2, "", mode_box())
    assert (prompt_line.UP_A_ROW * 3 + prompt_line.CLEAR_BELOW) in written.getvalue()


def test_on_linux_changing_mode_takes_the_box_away_so_the_next_one_shows_the_new_mode():
    reader, _module, written, _prompts = readline_reader(f"fix{prompt_line.MODE_MARK}")
    assert reader.read("› ", 2, "", mode_box()) == ("fix", True)
    assert written.getvalue().endswith(prompt_line.UP_A_ROW * 2 + prompt_line.CLEAR_BELOW)


@pytest.mark.parametrize("stopped", [KeyboardInterrupt, EOFError])
def test_on_linux_leaving_the_prompt_takes_the_whole_box_away(stopped):
    module, written = FakeReadline(), io.StringIO()

    def read(prompt):
        raise stopped

    reader = prompt_line.ReadlineLine(module, read=read, write=written.write, columns=lambda: 80)
    with pytest.raises(stopped):
        reader.read("› ", 2, "", mode_box())
    assert written.getvalue().endswith("\r" + prompt_line.UP_A_ROW + prompt_line.CLEAR_BELOW)


def test_on_linux_the_command_menu_goes_above_a_fresh_box():
    reader, module, written, _prompts = readline_reader("/model", choices=lambda: COMMANDS)
    box = mode_box()
    reader.read("› ", 2, "", box)
    written.truncate(0)
    written.seek(0)
    module.buffer = "/mo"
    module.completer("/mo", 0)
    module.display_hook("/mo", ["/model", "/mode"], 6)
    shown = written.getvalue()
    assert shown.startswith("\r" + prompt_line.UP_A_ROW + prompt_line.CLEAR_BELOW)
    assert shown.index("/model  switch model") < shown.index(box.rule)
    assert shown.endswith(box.opened() + "› /mo")


def test_on_windows_the_box_is_drawn_first_and_swapped_for_the_typed_line_on_enter():
    reader, written = console_reader(["h", "i", "\r"])
    box = mode_box()
    assert reader.read("› ", 2, "", box) == ("hi", False)
    shown = written.getvalue()
    assert shown.startswith(box.opened() + "› ")
    assert shown.endswith(prompt_line.UP_A_ROW * 2 + prompt_line.CLEAR_BELOW + "› hi\r\n")


def test_on_windows_changing_mode_takes_the_box_away():
    reader, written = console_reader(["h", "\x00", "\x0f"])
    assert reader.read("› ", 2, "", mode_box()) == ("h", True)
    assert written.getvalue().endswith("\r" + prompt_line.UP_A_ROW + prompt_line.CLEAR_BELOW)


def test_on_windows_ctrl_c_takes_the_whole_box_away():
    reader, written = console_reader(["\x03"])
    box = mode_box()
    with pytest.raises(KeyboardInterrupt):
        reader.read("› ", 2, "", box)
    assert written.getvalue() == box.opened() + "› " + "\r" + prompt_line.UP_A_ROW + prompt_line.CLEAR_BELOW


def test_on_windows_the_command_menu_goes_above_a_fresh_box():
    reader, written = console_reader(["/", "m", "o", "\t", "\r"], choices=lambda: COMMANDS)
    box = mode_box()
    reader.read("› ", 2, "", box)
    shown = written.getvalue()
    assert shown.count(box.opened()) == 3
    assert shown.index("/model  switch model") < shown.rindex(box.opened())


class Tty(io.StringIO):
    def isatty(self):
        return True


class BoxReader:
    def __init__(self, *results):
        self.results, self.calls = list(results), []

    def read(self, prompt, width, kept, box=None):
        self.calls.append((prompt, width, kept, box))
        return self.results.pop(0)


def test_on_a_terminal_the_prompt_is_boxed_with_the_mode_in_the_bar(ready_session):
    ready_session.terminal.out, ready_session.terminal.err = Tty(), Tty()
    reader = BoxReader(("fix", True), ("fix it", False))
    assert cli.read_request(ready_session, reader) == "fix it"
    (prompt, width, _kept, box), (_prompt, _width, kept, second) = reader.calls
    assert (prompt, width, kept) == ("› ", 2, "fix")
    assert "default mode" in box.bar and "auto-edit mode" in second.bar


def test_into_a_pipe_the_prompt_stays_plain(ready_session):
    reader = BoxReader(("hi", False))
    cli.read_request(ready_session, reader)
    assert reader.calls == [("default › ", 10, "", None)]
