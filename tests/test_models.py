import io
import json

import pytest

from arty_cli import brain, cli, models, providers, sessions, settings
from tests.fake_servers import serving

LISTED = [("arty/qwen3:14b", "main"), ("arty/qwen2.5-coder:14b", "coder")]


class ListingBrain:
    made = []
    broken = False

    def __init__(self, config):
        self.model = config.model
        self.calls = []
        ListingBrain.made.append(self)

    def address(self):
        return "brain"

    def search_web(self, query, results):
        return f"Results for {query}"

    def available_models(self):
        if ListingBrain.broken:
            raise providers.ProviderError("the brain is down")
        return LISTED

    def reply(self, messages, tools, on_text):
        self.calls.append([dict(m) for m in messages])
        return {"role": "assistant", "content": "arty here.", "tool_calls": []}


@pytest.fixture
def arty_set_up(monkeypatch, tmp_path):
    ListingBrain.made, ListingBrain.broken = [], False
    monkeypatch.setattr(cli.providers, "make", ListingBrain)
    monkeypatch.setattr(cli.settings, "load", lambda *a, **k: settings.Settings(base_url="http://b/v1", api_key="k"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "config"))
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    return tmp_path / "config" / settings.APP_FOLDER / settings.CONFIG_NAME


def chat(monkeypatch, *lines, arguments=()):
    monkeypatch.setattr("sys.stdin", io.StringIO("".join(f"{line}\n" for line in lines)))
    return cli.main(list(arguments))


def test_slash_model_lists_arty_models_and_marks_the_current_one(arty_set_up, monkeypatch, capsys):
    chat(monkeypatch, "/model", "/exit")
    shown = capsys.readouterr().err
    for model, _about in LISTED:
        assert model in shown
    assert "● 2. arty/qwen2.5-coder:14b" in shown
    assert "/model <number or name>" in shown
    assert "not set up" not in shown


def test_picking_a_number_switches_the_model_and_is_remembered(arty_set_up, monkeypatch, capsys):
    chat(monkeypatch, "/model", "/model 1", "/status", "/exit")
    assert ListingBrain.made[-1].model == "arty/qwen3:14b"
    saved = json.loads(arty_set_up.read_text())
    assert saved["model"] == "arty/qwen3:14b" and "provider" not in saved
    assert "Model: arty/qwen3:14b" in capsys.readouterr().err


def test_a_model_name_switches_to_it(arty_set_up, monkeypatch):
    chat(monkeypatch, "/model arty/qwen3:14b", "hello", "/exit")
    assert len(ListingBrain.made) == 1
    assert ListingBrain.made[0].model == "arty/qwen3:14b"
    assert [m["content"] for m in ListingBrain.made[0].calls[0] if m["role"] == "user"] == ["hello"]
    assert json.loads(arty_set_up.read_text())["model"] == "arty/qwen3:14b"


def test_a_brain_that_cannot_list_is_explained(arty_set_up, monkeypatch, capsys):
    ListingBrain.broken = True
    chat(monkeypatch, "/model", "/exit")
    assert "the brain is down" in capsys.readouterr().err


def test_a_number_out_of_range_changes_nothing(arty_set_up, monkeypatch, capsys):
    chat(monkeypatch, "/model 99", "/exit")
    assert "1-" in capsys.readouterr().err
    assert ListingBrain.made[0].model == settings.DEFAULT_MODEL
    assert not arty_set_up.exists()


def test_remembering_a_choice_never_writes_keys_from_the_environment(arty_set_up, monkeypatch):
    monkeypatch.setenv("ARTY_API_KEY", "from-the-environment")
    chat(monkeypatch, "/model arty/qwen3:14b", "/exit")
    assert "from-the-environment" not in arty_set_up.read_text()


def saved_conversation_with(model: str) -> None:
    store = sessions.Store(settings.config_dir() / cli.SESSIONS_FOLDER)
    store.start(cli.Path.cwd()).save([{"role": "user", "content": "hi"}, {"role": "assistant", "content": "ok"}], model)


def test_carrying_on_reuses_the_saved_arty_model(arty_set_up, monkeypatch):
    saved_conversation_with("arty/qwen3:14b")
    chat(monkeypatch, "/exit", arguments=["-c"])
    assert ListingBrain.made[0].model == "arty/qwen3:14b"


def test_carrying_on_ignores_a_saved_model_that_is_not_one_of_arty_s(arty_set_up, monkeypatch):
    saved_conversation_with("other-vendor-1")
    chat(monkeypatch, "/exit", arguments=["-c"])
    assert ListingBrain.made[0].model == settings.DEFAULT_MODEL


def test_the_brain_lists_its_arty_models_with_the_key():
    listing = {"object": "list", "data": [
        {"id": "arty", "description": "main"},
        {"id": "arty/qwen3:14b", "description": "14B, tools"},
        {"id": "arty/qwen2.5-coder:14b"},
        {"id": "solo/qwen3:14b", "description": "on its own"}]}
    with serving([(200, json.dumps(listing))], "application/json") as (url, recorder):
        client = brain.Brain(settings.Settings(remote_url=url, api_key="secret"))
        listed = client.available_models()
    assert listed == [("arty/qwen3:14b", "14B, tools"), ("arty/qwen2.5-coder:14b", "")]
    assert recorder.requests[0]["path"] == "/models?all=1"
    sent = {name.lower(): value for name, value in recorder.requests[0]["headers"].items()}
    assert sent["authorization"] == "Bearer secret"


def test_the_brain_listing_error_is_plain():
    with serving([(500, json.dumps({"error": "ollama is down"}))], "application/json") as (url, _recorder):
        client = brain.Brain(settings.Settings(remote_url=url, api_key="secret"))
        with pytest.raises(brain.BrainError, match="ollama is down"):
            client.available_models()


def test_choices_are_numbered_in_order():
    found = models.Choices([models.Choice("arty/a", ""), models.Choice("arty/b", "")])
    assert found.pick("2").model == "arty/b"
    assert found.pick("0") is None and found.pick("x") is None


def test_chats_ask_the_brain_to_allow_any_downloaded_model():
    client = brain.Brain(settings.Settings(remote_url="http://brain.example/v1", api_key="k"))
    built = client.request(brain.Route("http://brain.example/v1", {}), {"model": "arty/qwen2.5:72b"})
    assert built.get_header("X-arty-any-model") == "1"


def test_long_lists_keep_their_columns_lined_up():
    found = models.Choices([models.Choice(f"arty/m{n}", "") for n in range(12)])
    shown = found.lines("arty/m0")
    assert shown[0] == "●  1. arty/m0" and shown[11] == "  12. arty/m11"
