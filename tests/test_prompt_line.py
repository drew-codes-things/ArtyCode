import io

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

    def parse_and_bind(self, line):
        self.bound.append(line)

    def set_startup_hook(self, hook=None):
        self.hooks.append(hook)
        if hook:
            hook()

    def insert_text(self, text):
        self.inserted.append(text)


def readline_reader(*answers):
    module, written, given = FakeReadline(), io.StringIO(), list(answers)
    prompts = []

    def read(prompt):
        prompts.append(prompt)
        return given.pop(0)

    reader = prompt_line.ReadlineLine(module, read=read, write=written.write, columns=lambda: 80)
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


def console_reader(keys, history=None):
    given, written = list(keys), io.StringIO()
    reader = prompt_line.ConsoleLine(lambda: given.pop(0), written.write)
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
