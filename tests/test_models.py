import io
import json
import time

import pytest

from arty_cli import brain, cli, models, providers, sessions, settings
from tests.fake_servers import serving

LISTED = [("arty/qwen3:14b", "main"), ("arty/qwen2.5-coder:14b", "coder")]


class ListingBrain:
    made = []
    broken = False
    listings = 0
    offered = LISTED

    def __init__(self, config):
        self.model = config.model
        self.calls = []
        ListingBrain.made.append(self)

    def address(self):
        return "brain"

    def search_web(self, query, results):
        return f"Results for {query}"

    def available_models(self):
        ListingBrain.listings += 1
        if ListingBrain.broken:
            raise providers.ProviderError("the brain is down")
        return ListingBrain.offered

    def reply(self, messages, tools, on_text):
        self.calls.append([dict(m) for m in messages])
        return {"role": "assistant", "content": "arty here.", "tool_calls": []}


@pytest.fixture
def arty_set_up(monkeypatch, tmp_path):
    ListingBrain.made, ListingBrain.broken = [], False
    ListingBrain.listings, ListingBrain.offered = 0, LISTED
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


def test_models_arty_gets_from_starfire_are_listed_even_when_the_brain_marks_coding_models():
    listing = {"object": "list", "data": [
        {"id": "arty/qwen3:14b", "description": "14B", "coding": False},
        {"id": "arty/mimo-v2.6:9b", "description": "9B, sees images", "coding": True},
        {"id": "arty/remote/starfire/qwen3.6-35b", "description": "on starfire", "coding": False},
        {"id": "arty/remote/starfire/friend-big", "description": "offline, on starfire", "coding": False,
         "available": False}]}
    offered = {"object": "list", "data": [{"id": "Arty"}]}
    with serving([(200, json.dumps(listing)), (200, json.dumps(offered))], "application/json") as (url, _recorder):
        client = brain.Brain(settings.Settings(remote_url=url, api_key="secret"))
        listed = [model for model, _about in client.available_models()]
    assert listed == ["arty/mimo-v2.6:9b", "arty/remote/starfire/qwen3.6-35b", "arty/remote/starfire/friend-big"]


def test_the_brain_listing_error_is_plain():
    with serving([(500, json.dumps({"error": "the model server is down"}))], "application/json") as (url, _recorder):
        client = brain.Brain(settings.Settings(remote_url=url, api_key="secret"))
        with pytest.raises(brain.BrainError, match="the model server is down"):
            client.available_models()


def test_choices_are_numbered_in_order():
    found = models.Choices([models.Choice("arty/a", ""), models.Choice("arty/b", "")])
    assert found.pick("2").model == "arty/b"
    assert found.pick("0") is None and found.pick("x") is None


def test_chats_ask_the_brain_to_allow_any_downloaded_model():
    client = brain.Brain(settings.Settings(remote_url="http://brain.example/v1", api_key="k"))
    built = client.request(brain.Route("http://brain.example/v1", {}), {"model": "arty/qwen2.5:72b"})
    assert built.get_header("X-arty-any-model") == "1"


class HostileBrain:
    def available_models(self):
        return [("arty/real\n  2. arty/fake", "about\r\n\u2028  3. made up"),
                ("arty/\x1b]8;;http://evil.example\x07link\x1b]8;;\x07", "\x1b[2J\x1b[31mred\x1b[0m"),
                ("\x1b[2J\x07", "nothing")]


def test_model_ids_and_descriptions_from_the_brain_are_shown_as_one_clean_line_each():
    found = models.Choices(models.listed_for(HostileBrain()))
    assert [(choice.model, choice.about) for choice in found.listed] == [
        ("arty/real 2. arty/fake", "about 3. made up"), ("arty/link", "red")]
    shown = found.lines("") + [models.picker_text(choice, "") for choice in found.listed]
    shown += [*models.menu_meanings(found, ""), *models.menu_meanings(found, "").values()]
    assert len(found.lines("")) == 2
    assert not [line for line in shown if any(ord(char) < 32 or 0x7f <= ord(char) <= 0x9f for char in line)]
    assert not [line for line in shown if "\u2028" in line]


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


SIZED_LISTING = {"data": [{"id": "arty/qwen3:14b", "context_tokens": 24576, "reply_tokens": 4096},
                          {"id": "arty/mimo-v2.6:9b", "context_tokens": 49152, "reply_tokens": 6144}]}


def test_the_window_and_reply_room_are_those_of_the_model_requests_go_to():
    with serving([(200, json.dumps(SIZED_LISTING))], "application/json") as (url, recorder):
        client = brain.Brain(settings.Settings(remote_url=url, api_key="k", model="arty/mimo-v2.6:9b"))
        assert (client.context_tokens(), client.reply_tokens()) == (49152, 6144)
        client.model = "arty/qwen3:14b"
        assert (client.context_tokens(), client.reply_tokens()) == (24576, 4096)
    assert len(recorder.requests) == 1


def test_a_model_named_without_latest_finds_its_listed_window():
    listing = {"data": [{"id": "arty/qwen3:14b", "context_tokens": 24576},
                        {"id": "arty/coder:latest", "context_tokens": 65536}]}
    with serving([(200, json.dumps(listing))], "application/json") as (url, _recorder):
        assert brain.Brain(settings.Settings(remote_url=url, api_key="k", model="arty/coder")).context_tokens() == 65536


def test_a_model_the_list_does_not_size_gets_the_smallest_window():
    with serving([(200, json.dumps(SIZED_LISTING))], "application/json") as (url, _recorder):
        client = brain.Brain(settings.Settings(remote_url=url, api_key="k", model="Arty"))
        assert (client.context_tokens(), client.reply_tokens()) == (24576, 4096)


FOUND = [models.Choice(model, about) for model, about in LISTED]


class Counting:
    def __init__(self, *results):
        self.results, self.calls = list(results), 0

    def __call__(self):
        self.calls += 1
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def listing_with(*results):
    from tests.conftest import PretendTime
    time_now, fetch = PretendTime(), Counting(*results)
    return models.Listing(fetch, clock=time_now.clock), fetch, time_now


def test_a_listing_is_fetched_once_and_kept_fresh_for_a_minute():
    listing, fetch, time_now = listing_with(FOUND, FOUND)
    assert listing.fresh() is None
    first = listing.load()
    assert [choice.model for choice in first.listed] == [model for model, _about in LISTED]
    assert listing.fresh() is first and fetch.calls == 1
    time_now.now += models.STALE_SECONDS - 1
    assert listing.fresh() is first
    time_now.now += 1
    assert listing.fresh() is None and listing.available().listed == first.listed


def test_loading_again_straight_away_reuses_what_another_load_just_fetched():
    listing, fetch, _time_now = listing_with(FOUND, FOUND)
    assert listing.load() is listing.load() and fetch.calls == 1


def test_everyone_waiting_for_the_list_is_told_when_it_arrives():
    listing, _fetch, time_now = listing_with(FOUND, FOUND)
    told = []
    listing.when_loaded(lambda: told.append(listing.choices))
    first = listing.load()
    assert told == [first]
    listing.load()
    assert len(told) == 1
    time_now.now += models.STALE_SECONDS
    again = listing.load()
    assert told == [first, again]


def test_waiting_for_the_list_ends_as_soon_as_it_arrives_or_nothing_is_loading():
    import threading
    release = threading.Event()

    def slow():
        release.wait(5)
        return FOUND

    listing = models.Listing(slow)
    listing.wait(0.5)
    assert listing.available() is None
    threading.Timer(0.2, release.set).start()
    started = time.monotonic()
    listing.wait(5)
    assert listing.choices is not None and time.monotonic() - started < 3
    idle = models.Listing(slow)
    started = time.monotonic()
    idle.wait(5)
    assert time.monotonic() - started < 1


def test_waiting_for_a_list_that_never_comes_gives_up_at_the_limit():
    import threading
    release = threading.Event()
    listing = models.Listing(lambda: release.wait(5) or FOUND)
    listing.available()
    started = time.monotonic()
    listing.wait(0.3)
    assert listing.choices is None and 0.25 < time.monotonic() - started < 2
    release.set()


def test_a_failed_load_tells_nobody():
    listing, _fetch, _time_now = listing_with(providers.ProviderError("down"))
    told = []
    listing.when_loaded(lambda: told.append(True))
    with pytest.raises(providers.ProviderError):
        listing.load()
    assert told == []


def test_a_failed_load_raises_and_keeps_the_last_good_list():
    listing, _fetch, time_now = listing_with(FOUND, providers.ProviderError("the brain is down"))
    good = listing.load()
    time_now.now += models.STALE_SECONDS
    with pytest.raises(providers.ProviderError, match="the brain is down"):
        listing.load()
    assert listing.available() is good


def wait_for(condition, seconds=3.0):
    import time
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not condition():
        time.sleep(0.01)
    return condition()


def test_asking_for_the_menu_list_never_waits_for_the_brain():
    import threading
    release, started = threading.Event(), threading.Event()

    def slow():
        started.set()
        release.wait(5)
        return FOUND

    listing = models.Listing(slow)
    assert listing.available() is None
    assert started.wait(3)
    assert listing.available() is None
    release.set()
    assert wait_for(lambda: listing.available() is not None)
    assert listing.available().listed[0].model == "arty/qwen3:14b"


def test_a_failing_brain_is_asked_again_by_the_menu_only_once_a_minute():
    listing, fetch, time_now = listing_with(providers.ProviderError("down"), FOUND)
    assert listing.available() is None
    assert wait_for(lambda: listing.failure is not None) and fetch.calls == 1
    for _typed in range(5):
        assert listing.available() is None
    assert fetch.calls == 1
    time_now.now += models.STALE_SECONDS
    listing.available()
    assert wait_for(lambda: listing.fresh() is not None) and fetch.calls == 2 and listing.failure is None


def test_the_menu_marks_the_current_model_and_keeps_what_each_one_does():
    found = models.Choices([models.Choice("arty/a", "14B, tools"), models.Choice("arty/b", ""),
                            models.Choice("own/c", "own/c")])
    assert models.menu_meanings(found, "arty/a") == {"arty/a": "● current, 14B, tools", "arty/b": "", "own/c": ""}
    assert models.menu_meanings(found, "arty/b")["arty/b"] == "● current"
    assert models.menu_meanings(found, "own/other")["arty/a"] == "14B, tools"


class PickerClient:
    def __init__(self, listed, failure=None):
        self.model, self.listed, self.failure, self.asked_for_models = "arty/qwen3:14b", listed, failure, 0

    def available_models(self):
        self.asked_for_models += 1
        if self.failure:
            raise self.failure
        return self.listed


class Picker:
    def __init__(self, answer=None):
        self.answer, self.asked = answer, []

    def choose(self, text, choices):
        self.asked.append((text, list(choices)))
        return self.answer


def picking_session(tmp_path, monkeypatch, listed, failure=None, answer=None):
    import io
    from arty_cli import permissions
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "config"))

    class Saved:
        messages, id = [], "s1"

    terminal = cli.Terminal(out=io.StringIO(), err=io.StringIO())
    terminal.colour = False
    terminal.console = Picker(answer)
    workspace = cli.tools.Workspace(tmp_path)
    client = PickerClient(listed, failure)
    session = cli.Session(client, workspace, permissions.Gate(workspace, permissions.refuse), terminal, Saved())
    return session, client, terminal


def test_a_bare_slash_model_says_it_is_loading_then_offers_every_model_with_the_current_one_marked(tmp_path, monkeypatch):
    session, _client, terminal = picking_session(tmp_path, monkeypatch, [("arty/qwen3:14b", "14B, tools"),
                                                                       ("arty/mimo-v2.6:9b", "9B, sees images")])
    session.command("/model")
    shown = terminal.err.getvalue()
    assert shown.splitlines()[0] == "Loading models..."
    question, offered = terminal.console.asked[0]
    assert "arty/qwen3:14b" in question
    assert offered == [("1", "arty/qwen3:14b  14B, tools  (current)"), ("2", "arty/mimo-v2.6:9b  9B, sees images")]


def test_a_second_slash_model_within_a_minute_reuses_the_list_without_loading_again(tmp_path, monkeypatch):
    session, client, terminal = picking_session(tmp_path, monkeypatch, [("arty/qwen3:14b", "")])
    session.command("/model")
    first = terminal.err.getvalue().count("Loading models...")
    session.command("/model")
    assert first == 1 and terminal.err.getvalue().count("Loading models...") == 1
    assert client.asked_for_models == 1 and len(terminal.console.asked) == 2


def test_a_slash_model_after_a_minute_loads_the_list_again(tmp_path, monkeypatch):
    session, client, terminal = picking_session(tmp_path, monkeypatch, [("arty/qwen3:14b", "")])
    session.command("/model")
    session.listing.loaded_at -= models.STALE_SECONDS
    session.command("/model")
    assert client.asked_for_models == 2 and terminal.err.getvalue().count("Loading models...") == 2


@pytest.mark.parametrize("failure, said", [
    (providers.ProviderError("Couldn't get Arty's models from http://b: timed out"), "timed out"),
    (providers.ProviderError("Can't reach Arty's brain (tried http://b)"), "Can't reach Arty's brain"),
])
def test_a_brain_that_cannot_list_gives_one_clear_line_and_no_picker(tmp_path, monkeypatch, failure, said):
    session, _client, terminal = picking_session(tmp_path, monkeypatch, [], failure)
    session.command("/model")
    lines = terminal.err.getvalue().splitlines()
    assert lines[0] == "Loading models..." and said in lines[1] and len(lines) == 2
    assert terminal.console.asked == []


def test_a_brain_with_no_models_says_so_and_opens_no_empty_picker(tmp_path, monkeypatch):
    session, _client, terminal = picking_session(tmp_path, monkeypatch, [])
    session.command("/model")
    assert "listed no models" in terminal.err.getvalue() and terminal.console.asked == []


def test_picking_in_the_list_switches_and_remembers_the_model(tmp_path, monkeypatch):
    session, client, terminal = picking_session(tmp_path, monkeypatch, [("arty/qwen3:14b", ""), ("own/Coder", "")],
                                                answer="2")
    session.command("/model")
    assert client.model == "own/Coder" and "Model: own/Coder" in terminal.err.getvalue()


def test_the_menu_list_is_the_cached_one_with_the_current_model_marked(tmp_path, monkeypatch):
    session, client, _terminal = picking_session(tmp_path, monkeypatch, [("arty/qwen3:14b", "14B"), ("own/Coder", "")])
    session.listing.load()
    assert session.model_choices() == {"arty/qwen3:14b": "● current, 14B", "own/Coder": ""}
    client.model = "own/Coder"
    assert session.model_choices()["own/Coder"] == "● current"


def test_the_plain_prompt_says_it_is_loading_then_lists_and_reuses_the_list(arty_set_up, monkeypatch, capsys):
    chat(monkeypatch, "/model", "/model", "/exit")
    shown = capsys.readouterr().err
    assert shown.count("Loading models...") == 1 and shown.count("● 1. arty/qwen3:14b") == 2
    assert ListingBrain.listings == 1


def test_the_plain_prompt_names_a_brain_that_cannot_list_in_one_line(arty_set_up, monkeypatch, capsys):
    ListingBrain.broken = True
    chat(monkeypatch, "/model", "/exit")
    lines = [line for line in capsys.readouterr().err.splitlines() if "Loading" in line or "the brain is down" in line]
    assert lines == ["Loading models...", "the brain is down"]


def test_the_plain_prompt_names_a_brain_with_no_models(arty_set_up, monkeypatch, capsys):
    ListingBrain.offered = []
    chat(monkeypatch, "/model", "/exit")
    assert "listed no models" in capsys.readouterr().err


def test_a_background_load_that_crashes_is_kept_as_the_failure_and_a_direct_load_raises_it():
    listing = models.Listing(Counting(AttributeError("no listing"), AttributeError("no listing")))
    assert listing.available() is None
    assert wait_for(lambda: listing.failure is not None)
    assert isinstance(listing.failure, AttributeError) and listing.available() is None
    with pytest.raises(AttributeError, match="no listing"):
        listing.load()
