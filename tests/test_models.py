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
    assert "● 1. arty/qwen3:14b" in shown and "  2. arty/qwen2.5-coder:14b" in shown
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


def test_carrying_on_reuses_a_saved_model_from_the_brain_s_own_list(arty_set_up, monkeypatch):
    saved_conversation_with("own/Coder")
    chat(monkeypatch, "/exit", arguments=["-c"])
    assert ListingBrain.made[0].model == "own/Coder"


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
    offered = {"object": "list", "data": [{"id": "Arty"}, {"id": "arty/qwen3:14b"}, {"id": "own/Coder"}]}
    with serving([(200, json.dumps(listing)), (200, json.dumps(offered))], "application/json") as (url, recorder):
        client = brain.Brain(settings.Settings(remote_url=url, api_key="secret"))
        listed = client.available_models()
    assert listed == [("arty/qwen3:14b", "14B, tools"), ("arty/qwen2.5-coder:14b", ""), ("own/Coder", "")]
    assert [sent["path"] for sent in recorder.requests] == ["/models?all=1", "/models"]
    sent = {name.lower(): value for name, value in recorder.requests[0]["headers"].items()}
    assert sent["authorization"] == "Bearer secret"


def test_a_brain_that_marks_coding_models_gets_only_those_listed():
    listing = {"object": "list", "data": [
        {"id": "arty/qwen3:14b", "description": "14B", "coding": False},
        {"id": "arty/mimo-v2.6:9b", "description": "9B, sees images", "coding": True}]}
    offered = {"object": "list", "data": [{"id": "Arty"}, {"id": "arty/qwen3:14b"}, {"id": "own/Persona"},
                                          {"id": "own/Coder", "description": "private coder", "coding": True}]}
    with serving([(200, json.dumps(listing)), (200, json.dumps(offered))], "application/json") as (url, _recorder):
        client = brain.Brain(settings.Settings(remote_url=url, api_key="secret"))
        listed = client.available_models()
    assert listed == [("arty/mimo-v2.6:9b", "9B, sees images"), ("own/Coder", "private coder")]


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


def coding_model_from(*entries):
    listing = {"object": "list", "data": list(entries)}
    with serving([(200, json.dumps(listing))], "application/json") as (url, recorder):
        found = brain.Brain(settings.Settings(remote_url=url, api_key="secret")).coding_model()
    assert [sent["path"] for sent in recorder.requests] == ["/models?all=1"]
    return found


def test_the_brains_coding_model_is_its_first_arty_entry_marked_for_coding():
    assert coding_model_from({"id": "arty/qwen3:14b", "coding": False}, {"id": "own/Coder", "coding": True},
                             {"id": "arty/mimo-v2.6:9b", "coding": True},
                             {"id": "arty/other:7b", "coding": True}) == "arty/mimo-v2.6:9b"


def test_a_brain_that_marks_no_coding_model_names_none():
    assert coding_model_from({"id": "arty/qwen3:14b"}, {"id": "arty/b", "coding": False}) is None


def test_a_brain_that_cannot_be_reached_or_listed_names_no_coding_model():
    assert brain.Brain(settings.Settings(remote_url="http://127.0.0.1:9/v1", api_key="k")).coding_model() is None
    with serving([(500, json.dumps({"error": "down"}))], "application/json") as (url, _recorder):
        assert brain.Brain(settings.Settings(remote_url=url, api_key="k")).coding_model() is None


class CodingBrain(ListingBrain):
    def coding_model(self):
        return "arty/mimo-v2.6:9b"


@pytest.fixture
def nothing_chosen(arty_set_up, monkeypatch):
    monkeypatch.setattr(cli.providers, "make", CodingBrain)
    monkeypatch.delenv("ARTY_CLI_MODEL", raising=False)
    return cli.settings.config_path()


def test_with_no_model_ever_picked_it_starts_on_the_brains_coding_model_without_saving_it(nothing_chosen, monkeypatch,
                                                                                         capsys):
    chat(monkeypatch, "/exit")
    assert ListingBrain.made[0].model == "arty/mimo-v2.6:9b"
    assert "arty/mimo-v2.6:9b" in capsys.readouterr().err
    assert not nothing_chosen.exists() or "mimo" not in nothing_chosen.read_text()


def test_a_model_given_for_this_run_wins_over_the_brains_coding_model(nothing_chosen, monkeypatch):
    chat(monkeypatch, "/exit", arguments=["--model", "arty/qwen3:14b"])
    assert ListingBrain.made[0].model == "arty/qwen3:14b"


def test_a_model_the_user_saved_or_set_in_the_environment_wins(nothing_chosen, monkeypatch):
    nothing_chosen.parent.mkdir(parents=True, exist_ok=True)
    nothing_chosen.write_text(json.dumps({"model": "arty/qwen3:14b"}))
    chat(monkeypatch, "/exit")
    nothing_chosen.unlink()
    monkeypatch.setenv("ARTY_CLI_MODEL", "arty/env:1b")
    chat(monkeypatch, "/exit")
    assert [made.model for made in ListingBrain.made] == ["arty/qwen3:14b", settings.DEFAULT_MODEL]


def test_a_brain_with_no_coding_model_leaves_the_default(nothing_chosen, monkeypatch):
    monkeypatch.setattr(CodingBrain, "coding_model", lambda self: None)
    chat(monkeypatch, "/exit")
    assert ListingBrain.made[0].model == settings.DEFAULT_MODEL


def test_a_saved_default_that_was_retired_does_not_count_as_a_choice(tmp_path):
    path = tmp_path / "config.json"
    assert not settings.model_was_chosen(path, environ={})
    path.write_text(json.dumps({"model": settings.RETIRED_DEFAULT_MODELS[0]}))
    assert not settings.model_was_chosen(path, environ={})
    path.write_text(json.dumps({"model": "arty/qwen3:14b"}))
    assert settings.model_was_chosen(path, environ={})
    assert settings.model_was_chosen(tmp_path / "none.json", environ={"ARTY_CLI_MODEL": "arty/x"})


def test_the_context_size_comes_from_the_brain_and_is_asked_for_once():
    listing = {"object": "list", "data": [{"id": "arty/qwen3:14b", "context_tokens": 24576},
                                          {"id": "arty/mimo-v2.6:9b", "context_tokens": 24576}]}
    with serving([(200, json.dumps(listing))], "application/json") as (url, recorder):
        client = brain.Brain(settings.Settings(remote_url=url, api_key="secret"))
        sizes = [client.context_tokens(), client.context_tokens()]
    assert sizes == [24576, 24576]
    assert [sent["path"] for sent in recorder.requests] == ["/models?all=1"]


def test_a_context_size_set_by_hand_wins_over_the_brain():
    client = brain.Brain(settings.Settings(remote_url="http://127.0.0.1:9/v1", api_key="k", context_tokens="16384"))
    assert client.context_tokens() == 16384


def test_an_older_brain_or_one_that_cannot_be_reached_leaves_the_default():
    with serving([(200, json.dumps({"data": [{"id": "arty/qwen3:14b"}]}))], "application/json") as (url, _recorder):
        assert brain.Brain(settings.Settings(remote_url=url, api_key="k")).context_tokens() is None
    assert brain.Brain(settings.Settings(remote_url="http://127.0.0.1:9/v1", api_key="k")).context_tokens() is None


def test_a_failed_look_up_is_tried_again_next_time():
    listing = {"data": [{"id": "arty/qwen3:14b", "context_tokens": 24576}]}
    with serving([(500, json.dumps({"error": "down"})), (200, json.dumps(listing))], "application/json") as (url, _recorder):
        client = brain.Brain(settings.Settings(remote_url=url, api_key="k"))
        assert client.context_tokens() is None
        assert client.context_tokens() == 24576


def test_the_reply_room_comes_from_the_brain_when_it_reports_one():
    listing = {"data": [{"id": "arty/qwen3:14b", "context_tokens": 24576, "reply_tokens": 4096},
                        {"id": "arty/mimo-v2.6:9b", "context_tokens": 24576, "reply_tokens": 6144}]}
    with serving([(200, json.dumps(listing))], "application/json") as (url, recorder):
        client = brain.Brain(settings.Settings(remote_url=url, api_key="k"))
        assert client.reply_tokens() == 4096 and client.context_tokens() == 24576
    assert len(recorder.requests) == 1


def test_without_reply_tokens_in_the_list_the_reply_room_is_unknown():
    with serving([(200, json.dumps({"data": [{"id": "arty/qwen3:14b", "context_tokens": 24576}]}))],
                 "application/json") as (url, _recorder):
        assert brain.Brain(settings.Settings(remote_url=url, api_key="k")).reply_tokens() is None
