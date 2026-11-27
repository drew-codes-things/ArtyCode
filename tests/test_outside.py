import json
import os
import stat

import pytest

from arty_cli import brain, cli, login_flow, settings
from tests.fake_openai import GOOD_KEY, outside_server
from tests.fake_servers import serving

ARTY_KEY = "arty-key-not-for-outside"
CONNECTION_VARIABLES = ("ARTY_API_KEY", "ARTY_URL", "ARTY_REMOTE_URL", "ARTY_ACCESS_CLIENT_ID", "ARTY_ACCESS_CLIENT_SECRET",
                        "ARTY_OUTSIDE_URL", "ARTY_OUTSIDE_KEY", "ARTY_CLI_MODEL")
ARTY_LIST = json.dumps({"data": [{"id": "arty/qwen3:14b", "description": "home"}]})


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "config"))
    for name in CONNECTION_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def client_for(url="", key=GOOD_KEY, model="outside/Qwen3-14B-Q4_K_M", **arty) -> brain.Brain:
    return brain.Brain(settings.Settings(outside_url=url, outside_key=key, model=model, **arty))


def test_only_an_outside_server_is_enough_to_be_ready():
    assert settings.Settings(outside_url="https://models.example/v1", outside_key="sf_x").is_ready()
    assert not settings.Settings(outside_url="https://models.example/v1").is_ready()


def test_the_outside_address_and_key_come_from_the_environment(home, monkeypatch):
    monkeypatch.setenv("ARTY_OUTSIDE_URL", "https://models.example/v1")
    monkeypatch.setenv("ARTY_OUTSIDE_KEY", "sf_from_env")
    loaded = settings.load()
    assert (loaded.outside_url, loaded.outside_key) == ("https://models.example/v1", "sf_from_env")


def test_an_outside_model_is_asked_of_the_outside_server_with_its_own_key_and_name():
    with outside_server() as (url, server):
        shown, thoughts = [], []
        client = client_for(url, base_url="http://brain.invalid/v1", api_key=ARTY_KEY)
        client.on_thinking = thoughts.append
        reply = client.reply([{"role": "user", "content": "hi"}], [], shown.append)
    sent = server.requests[-1]
    assert reply["content"] == "from outside" and shown == ["from outside"] and thoughts == ["hmm"]
    assert sent["path"] == "/v1/chat/completions"
    assert sent["headers"]["Authorization"] == f"Bearer {GOOD_KEY}"
    assert sent["body"]["model"] == "Qwen3-14B-Q4_K_M"
    assert not any(name.lower().startswith("x-arty") for name in sent["headers"])
    assert ARTY_KEY not in json.dumps(sent["headers"])


def test_an_arty_model_still_goes_to_arty_with_the_arty_key():
    lines = "\n".join(["data: " + json.dumps({"choices": [{"delta": {"content": "arty"}, "finish_reason": "stop"}]}),
                       "data: [DONE]", ""])
    with outside_server() as (outside_url, outside), serving([(200, lines)], "text/event-stream") as (arty_url, arty):
        client = client_for(outside_url, model="arty/qwen3:14b", remote_url=f"{arty_url}/v1", api_key=ARTY_KEY)
        reply = client.reply([{"role": "user", "content": "hi"}], [], lambda text: None)
    assert reply["content"] == "arty"
    assert arty.requests[-1]["headers"]["Authorization"] == f"Bearer {ARTY_KEY}"
    assert outside.requests == []


def test_the_model_list_shows_arty_and_outside_models(monkeypatch):
    with outside_server() as (outside_url, _), \
            serving([(200, ARTY_LIST), (200, ARTY_LIST)], "application/json") as (arty_url, _arty):
        client = client_for(outside_url, remote_url=f"{arty_url}/v1", api_key=ARTY_KEY)
        listed = [model for model, _ in client.available_models()]
    assert listed == ["arty/qwen3:14b", "outside/Qwen3-14B-Q4_K_M", "outside/remote/model"]


def test_with_only_an_outside_server_the_list_is_its_models():
    with outside_server() as (url, _):
        listed = client_for(url).available_models()
    assert [model for model, _ in listed] == ["outside/Qwen3-14B-Q4_K_M", "outside/remote/model"]
    assert all(about.startswith("on 127.0.0.1") for _, about in listed)


def test_a_refused_outside_key_says_so_without_the_key():
    with outside_server() as (url, _):
        client = client_for(url, key="sf_wrong_secret")
        with pytest.raises(brain.SetupProblem, match="refused the key") as raised:
            client.reply([{"role": "user", "content": "hi"}], [], lambda text: None)
        with pytest.raises(brain.SetupProblem, match="refused the key"):
            client.available_models()
    assert "sf_wrong_secret" not in str(raised.value) and "login outside" in str(raised.value)


def test_an_outside_model_the_server_lacks_says_so():
    with outside_server() as (url, _):
        client = client_for(url, model="outside/not-there")
        with pytest.raises(brain.SetupProblem, match="doesn't have the model outside/not-there"):
            client.reply([{"role": "user", "content": "hi"}], [], lambda text: None)


def test_an_outside_model_without_an_outside_server_says_how_to_set_one_up():
    client = brain.Brain(settings.Settings(base_url="http://brain.invalid/v1", api_key=ARTY_KEY, model="outside/x"))
    with pytest.raises(brain.SetupProblem, match="arty-cli login outside"):
        client.reply([{"role": "user", "content": "hi"}], [], lambda text: None)


def test_outside_models_are_remembered_like_arty_models():
    assert brain.is_known_model("outside/Qwen3-14B-Q4_K_M") and brain.is_known_model("arty/qwen3:14b")
    assert not brain.is_known_model("qwen3:14b")


def logged_in_outside(monkeypatch, address, key, words=("login", "outside"), answers=()):
    asked = []
    replies = [*answers, address]
    monkeypatch.setattr("builtins.input", lambda prompt="": (asked.append(prompt), replies.pop(0))[1])
    monkeypatch.setattr(login_flow.getpass, "getpass", lambda prompt="": (asked.append(prompt), key)[1])
    return cli.main(list(words)), asked


def test_login_outside_checks_the_key_and_saves_it_privately(home, monkeypatch):
    with outside_server() as (url, server):
        result, asked = logged_in_outside(monkeypatch, url, GOOD_KEY)
    saved = settings.load()
    assert result == 0
    assert asked == [login_flow.OUTSIDE_ADDRESS_PROMPT, login_flow.OUTSIDE_KEY_PROMPT]
    assert (saved.outside_url, saved.outside_key, saved.model) == (url, GOOD_KEY, "outside/Qwen3-14B-Q4_K_M")
    assert server.requests[-1]["path"] == "/v1/models"
    if os.name != "nt":
        assert stat.S_IMODE(settings.config_path().stat().st_mode) == settings.PRIVATE_FILE_MODE


def test_a_refused_key_at_login_saves_nothing(home, monkeypatch, capsys):
    with outside_server() as (url, _):
        result, _ = logged_in_outside(monkeypatch, url, "sf_wrong_secret")
    shown = capsys.readouterr().err
    assert result == cli.FAILED and "refused the key" in shown and "sf_wrong_secret" not in shown
    assert not settings.config_path().exists()


def test_a_public_plain_http_address_is_refused_before_the_key_is_asked(home, monkeypatch, capsys):
    monkeypatch.setattr(login_flow, "reaches_the_internet", lambda url: True)
    result, asked = logged_in_outside(monkeypatch, "http://models.example.com/v1", GOOD_KEY)
    assert result == cli.FAILED and asked == [login_flow.OUTSIDE_ADDRESS_PROMPT]
    assert "plain http" in capsys.readouterr().err and not settings.config_path().exists()


def test_the_login_menu_offers_an_outside_server_and_keeps_the_arty_model(home, monkeypatch, capsys):
    settings.save(settings.Settings(base_url="http://brain.lan:8081/v1", api_key=ARTY_KEY))
    with outside_server() as (url, _):
        result, asked = logged_in_outside(monkeypatch, url, GOOD_KEY, words=("login",), answers=("3",))
    saved = settings.load()
    assert result == 0 and asked[0] == "Choose 1-3: "
    assert "3. Another OpenAI-compatible server" in capsys.readouterr().err
    assert (saved.api_key, saved.model, saved.outside_key) == (ARTY_KEY, settings.DEFAULT_MODEL, GOOD_KEY)


def test_login_without_an_arty_address_points_to_login_outside(home, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("nothing should be asked"))
    assert cli.main(["login"]) == cli.FAILED
    assert "arty-cli login outside" in capsys.readouterr().err


def test_an_outside_model_takes_the_set_window_and_never_arty_sizes():
    unset = client_for("http://127.0.0.1:9/v1", base_url="http://brain.invalid/v1", api_key=ARTY_KEY)
    chosen = client_for("http://127.0.0.1:9/v1", context_tokens="32768")
    assert unset.context_tokens() is None and unset.reply_tokens() is None
    assert chosen.context_tokens() == 32768


READ_FILE = [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}]


def test_a_tool_call_sent_in_pieces_is_put_back_together():
    with outside_server() as (url, _):
        reply = client_for(url).reply([{"role": "user", "content": "read a.txt"}], READ_FILE, lambda text: None)
    assert reply["tool_calls"] == [{"id": "call_1", "type": "function",
                                    "function": {"name": "read_file", "arguments": "{\"path\": \"a.txt\"}"}}]


def test_an_offline_outside_model_is_marked_in_the_model_list():
    with outside_server(offline={"remote/model"}) as (url, _):
        listed = dict(client_for(url).available_models())
    assert listed["outside/remote/model"].endswith("(offline)")
    assert "offline" not in listed["outside/Qwen3-14B-Q4_K_M"]


def test_an_offline_outside_model_says_it_is_offline_without_waiting(monkeypatch):
    monkeypatch.setattr(brain.retry, "show_wait", lambda *args: pytest.fail("an offline model is not retried"))
    with outside_server(offline={"Qwen3-14B-Q4_K_M"}) as (url, _):
        with pytest.raises(brain.BrainError, match="is offline right now"):
            client_for(url).reply([{"role": "user", "content": "hi"}], [], lambda text: None)


def test_an_offline_arty_model_is_marked_in_the_model_list():
    listed = json.dumps({"data": [{"id": "arty/remote/starfire/model", "description": "offline · all tools", "available": False},
                                  {"id": "arty/qwen3:14b", "description": "14.8B · all tools"}]})
    with serving([(200, listed), (200, listed)], "application/json") as (arty_url, _):
        found = dict(brain.Brain(settings.Settings(remote_url=f"{arty_url}/v1", api_key=ARTY_KEY)).available_models())
    assert found["arty/remote/starfire/model"] == "all tools (offline)"
    assert found["arty/qwen3:14b"] == "14.8B · all tools"
