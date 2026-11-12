import io

import pytest

from arty_cli import authorise, brain, cli, providers, settings


class FakeBrain:
    made = []

    def __init__(self, config):
        self.model = config.model
        self.calls = []
        FakeBrain.made.append(self)

    def address(self):
        return "brain"

    def search_web(self, query, results):
        return f"Results for {query}"

    def reply(self, messages, tools, on_text):
        self.calls.append([dict(m) for m in messages])
        on_text("arty here.")
        return {"role": "assistant", "content": "arty here.", "tool_calls": []}


@pytest.fixture
def connected(monkeypatch, tmp_path):
    FakeBrain.made = []
    monkeypatch.setattr(cli.providers, "make", FakeBrain)
    monkeypatch.setattr(cli.settings, "load", lambda *a, **k: settings.Settings(base_url="http://b/v1", api_key="k"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "config"))
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    return project


def test_make_builds_the_arty_brain():
    assert isinstance(providers.make(settings.Settings(base_url="http://b/v1", api_key="k")), brain.Brain)


def test_arty_without_a_key_is_refused_before_anything_is_sent():
    with pytest.raises(providers.ProviderError, match="arty-cli login"):
        providers.make(settings.Settings(base_url="http://b/v1"))


def test_the_brain_only_ever_receives_the_shared_parts_of_a_turn():
    cleaned = brain.shared_messages([{"role": "assistant", "content": "x", "tool_calls": [],
                                      "native_content": [{"type": "thinking"}], "native_model": "old"},
                                     {"role": "tool", "tool_call_id": "c1", "content": "done", "extra": 1}])
    assert cleaned == [{"role": "assistant", "content": "x", "tool_calls": []},
                       {"role": "tool", "tool_call_id": "c1", "content": "done"}]


def test_the_old_slash_commands_for_other_models_are_gone(connected, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("/provider\n/effort high\n/exit\n"))
    cli.main([])
    shown = capsys.readouterr().err
    assert "Unknown command /provider" in shown and "Unknown command /effort" in shown


@pytest.mark.parametrize("flags", [["--provider", "arty"], ["--web-search"]])
def test_the_old_command_line_switches_are_gone(flags):
    with pytest.raises(SystemExit):
        cli.parse_arguments(flags)


def test_a_json_answer_names_the_model(connected, capsys):
    assert cli.main(["-p", "hello", "--output-format", "json"]) == 0
    printed = capsys.readouterr().out
    assert '"model": "arty/qwen3:14b"' in printed and '"provider"' not in printed


CONNECTION_VARIABLES = ("ARTY_API_KEY", "ARTY_URL", "ARTY_REMOTE_URL", "ARTY_ACCESS_CLIENT_ID", "ARTY_ACCESS_CLIENT_SECRET")


def login_with(monkeypatch, tmp_path, answers, secrets, expected=0, **connection):
    asked = []
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "config"))
    for name in CONNECTION_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    for name, value in connection.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("builtins.input", lambda prompt="": (asked.append(prompt), answers.pop(0))[1])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": (asked.append(prompt), secrets.pop(0))[1])
    assert cli.main(["login"]) == expected
    return asked


HOME_BRAIN = {"ARTY_URL": "http://brain.lan:8081/v1"}
REMOTE_BRAIN = {"ARTY_REMOTE_URL": "https://arty.example/v1", "ARTY_ACCESS_CLIENT_ID": "cf-id",
                "ARTY_ACCESS_CLIENT_SECRET": "cf-secret"}


def test_login_offers_exactly_an_api_key_or_a_browser_login(monkeypatch, tmp_path, capsys):
    asked = login_with(monkeypatch, tmp_path, ["1"], ["app-key"], **HOME_BRAIN)
    shown = capsys.readouterr().err
    assert "1. API key" in shown and "2. Browser login" in shown and "3." not in shown
    assert asked[0] == "Choose 1-2: "


def test_a_choice_other_than_the_two_is_asked_again(monkeypatch, tmp_path):
    login_with(monkeypatch, tmp_path, ["3", "0", "1"], ["app-key"], **HOME_BRAIN)
    assert settings.Settings(**settings.read_file(settings.config_path())).api_key == "app-key"


def test_login_with_an_api_key_asks_for_the_key_and_nothing_else(monkeypatch, tmp_path, capsys):
    asked = login_with(monkeypatch, tmp_path, ["1"], ["app-key"], **HOME_BRAIN)
    assert asked == ["Choose 1-2: ", "Arty API key: "]
    shown = capsys.readouterr().err.lower()
    assert "address" not in shown and "cloudflare" not in shown


def test_browser_login_asks_nothing_after_the_choice(monkeypatch, tmp_path, capsys):
    opened, waited = [], []
    request = authorise.Request("device-secret", "WXYZ-1234", "https://arty.example/ios/?authorise=WXYZ-1234", 3, 600)
    monkeypatch.setattr(cli.authorise, "start", lambda url, headers, name="": (waited.append((url, headers)), request)[1])
    monkeypatch.setattr(cli.authorise, "wait_for_key", lambda url, headers, started: "approved-key")
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: opened.append(url) or True)
    asked = login_with(monkeypatch, tmp_path, ["2"], [], **REMOTE_BRAIN)
    assert asked == ["Choose 1-2: "]
    assert settings.Settings(**settings.read_file(settings.config_path())).api_key == "approved-key"
    assert waited == [("https://arty.example/v1", {"CF-Access-Client-Id": "cf-id", "CF-Access-Client-Secret": "cf-secret"})]
    assert opened == [request.verify_url]
    assert "WXYZ-1234" in capsys.readouterr().err


def test_a_saved_address_is_used_without_asking(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "config"))
    settings.save(settings.Settings(base_url="http://brain.lan:8081/v1"))
    asked = login_with(monkeypatch, tmp_path, ["1"], ["app-key"])
    saved = settings.load()
    assert asked == ["Choose 1-2: ", "Arty API key: "]
    assert (saved.base_url, saved.api_key) == ("http://brain.lan:8081/v1", "app-key")


def test_login_without_a_known_address_says_how_to_set_one_and_asks_nothing(monkeypatch, tmp_path, capsys):
    asked = login_with(monkeypatch, tmp_path, [], [], expected=cli.FAILED)
    shown = capsys.readouterr().err
    assert asked == []
    assert "ARTY_REMOTE_URL" in shown and "ARTY_URL" in shown
    assert not settings.config_path().exists()


@pytest.mark.parametrize("windows, home, anywhere, keep", [
    (False, "export ARTY_URL=http://", "export ARTY_REMOTE_URL=https://", "~/.bashrc"),
    (True, '$env:ARTY_URL = "http://', '$env:ARTY_REMOTE_URL = "https://', "setx ARTY_URL"),
])
def test_the_address_help_shows_lines_to_copy_for_this_kind_of_shell(windows, home, anywhere, keep):
    shown = cli.address_help(windows)
    assert home in shown and anywhere in shown and keep in shown
    assert "arty-cli login" in shown.splitlines()[0]


def test_login_without_a_known_address_shows_the_lines_for_this_computer(monkeypatch, tmp_path, capsys):
    login_with(monkeypatch, tmp_path, [], [], expected=cli.FAILED)
    assert cli.address_help(cli.os.name == "nt") in capsys.readouterr().err.replace(cli.COLOURS["reset"], "")


def test_a_first_run_without_an_address_says_it_once(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "config"))
    for name in CONNECTION_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cli, "is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail(f"asked {prompt!r}"))
    assert cli.main([]) == cli.NOT_SET_UP
    shown = capsys.readouterr().err
    assert "ARTY_URL" in shown
    assert shown.count("arty-cli login") == 1
    assert "isn't logged in" not in shown


def test_a_failed_browser_login_saves_nothing(monkeypatch, tmp_path, capsys):
    def refused(url, headers, name=""):
        raise authorise.AuthoriseError("This Arty is too old to authorise devices.")
    monkeypatch.setattr(cli.authorise, "start", refused)
    login_with(monkeypatch, tmp_path, ["2"], [], expected=cli.FAILED, **HOME_BRAIN)
    assert "too old" in capsys.readouterr().err
    assert not settings.config_path().exists()


def test_browser_login_over_plain_http_to_a_public_address_is_refused(monkeypatch, tmp_path, capsys):
    started = []
    monkeypatch.setattr(cli.authorise, "start", lambda url, headers, name="": started.append(url))
    login_with(monkeypatch, tmp_path, ["2"], [], expected=cli.FAILED, ARTY_URL="http://8.8.8.8:8081/v1")
    assert started == [] and "https" in capsys.readouterr().err


def test_resume_lists_this_projects_conversations_and_how_to_carry_one_on(connected, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("hello there\n/resume\n/exit\n"))
    cli.main([])
    shown = capsys.readouterr().err
    listed = [line for line in shown.splitlines() if "hello there" in line and not line.startswith("›")]
    assert listed
    assert cli.RESUME_HINT in shown and cli.THIS_SESSION in shown
