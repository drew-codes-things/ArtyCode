import contextlib
import io
import json
import os
import re
import stat
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from arty_cli import cli, mcp, mcp_sign_in, oauth, screen, settings, sign_ins, tools
from tests import fake_http_mcp, fake_oauth_mcp
from tests.scripted import ScriptedBrain, session_for
from tests.test_oauth import no_redirect

SIGN_IN_PAGE = re.compile(r"Sign-in page: (\S+)")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "config"))
    monkeypatch.setenv("NO_COLOR", "1")
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    return project


@pytest.fixture
def provider():
    found = fake_oauth_mcp.Provider()
    server, url = fake_oauth_mcp.serving(found)
    yield found, url
    server.shutdown()
    server.server_close()


def quiet_terminal() -> screen.Terminal:
    return screen.Terminal(io.StringIO(), io.StringIO())


def desktop(opened=None, **changes) -> mcp_sign_in.World:
    opened = [] if opened is None else opened
    return mcp_sign_in.World(**{"environ": {"DISPLAY": ":0"}, "platform": "linux",
                                "open_browser": fake_oauth_mcp.approving_browser(opened),
                                "is_interactive": lambda: True, "wait_seconds": 5, **changes})


def signed_in_through_cli(url: str, name: str = "fake", world=None) -> tuple[int, str]:
    terminal = quiet_terminal()
    code = mcp_sign_in.run(["mcp", "add", url, "--name", name], terminal, world or desktop())
    return code, terminal.err.getvalue()


def personal() -> Path:
    return settings.config_dir()


def stored(name: str = "fake") -> sign_ins.Record:
    return sign_ins.Store(personal()).get(name)


def test_adding_a_service_signs_in_and_saves_an_entry_that_only_names_the_sign_in(home, provider):
    found, url = provider
    opened = []
    code, shown = signed_in_through_cli(url, world=desktop(opened))
    assert code == 0, shown
    entry = json.loads((personal() / "settings.json").read_text())["mcpServers"]["fake"]
    assert entry == {"url": url, "signIn": "fake"}
    record = stored()
    assert record.access_token in found.access_tokens and record.server_url == url and record.issuer == found.base
    assert opened and opened[0].startswith(f"{found.base}/authorize?")
    for step in ("Find", "Register ArtyCode", "Sign in to", "Finish signing in"):
        assert f"● {step}" in shown
    assert "Signed in" in shown and record.access_token not in shown


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_the_sign_in_file_is_private(home, provider):
    _found, url = provider
    signed_in_through_cli(url)
    mode = stat.S_IMODE(os.stat(personal() / sign_ins.SIGN_INS_FILE).st_mode)
    assert mode == 0o600


def test_on_windows_sign_ins_live_in_the_per_user_app_data_folder_like_the_login_key(tmp_path, monkeypatch):
    folder = settings.config_dir(environ={"APPDATA": r"C:\Users\me\AppData\Roaming"}, platform="win32")
    assert sign_ins.Store(folder).path == folder / sign_ins.SIGN_INS_FILE
    assert str(folder).replace("\\", "/").endswith("AppData/Roaming/arty-cli")
    monkeypatch.delattr(os, "fchmod", raising=False)
    store = sign_ins.Store(tmp_path)
    record = sign_ins.Record("https://a/mcp", "https://a/mcp", "https://a", "https://a/token", "c", "", "none",
                             "tok", "ref", 0.0, "")
    store.put("a", record)
    assert store.get("a") == record


def test_tokens_never_land_in_the_project_folder(home, provider):
    _found, url = provider
    signed_in_through_cli(url)
    record = stored()
    for path in home.rglob("*"):
        if path.is_file():
            text = path.read_text(errors="replace")
            assert record.access_token not in text and record.refresh_token not in text


def test_a_sign_in_is_refused_for_another_address(home, provider):
    _found, url = provider
    signed_in_through_cli(url)
    with pytest.raises(sign_ins.SignInError, match="was made for") as raised:
        sign_ins.Store(personal()).credentials("fake", "https://evil.example/mcp")
    assert not raised.value.needs_sign_in


def hub_for(home, **kwargs) -> mcp.Hub:
    return mcp.Hub(tools.Workspace(home), sign_ins_store=sign_ins.Store(personal()), **kwargs)


def signed_in_config(url: str, name: str = "fake", source: str = "yours") -> mcp.ServerConfig:
    return mcp.ServerConfig(name, "", [], {}, source, url=url, sign_in=name)


def test_a_signed_in_server_starts_and_its_tools_work(home, provider):
    found, url = provider
    signed_in_through_cli(url)
    hub = hub_for(home)
    try:
        assert hub.start([signed_in_config(url)]) == []
        assert hub.run("mcp__fake__whoami", {}) == f"signed in as {fake_oauth_mcp.USER}"
        assert hub.states()[0].status == mcp.CONNECTED and hub.states()[0].tools == ["mcp__fake__whoami"]
    finally:
        hub.close()
    without_token = [request["authorization"] for request in found.mcp_requests
                     if request["authorization"] != stored().access_token]
    assert without_token == [""]


def test_a_401_renews_the_token_once_and_tries_again(home, provider):
    found, url = provider
    signed_in_through_cli(url)
    before = stored().access_token
    hub = hub_for(home)
    try:
        hub.start([signed_in_config(url)])
        found.revoke_access()
        assert hub.run("mcp__fake__whoami", {}) == f"signed in as {fake_oauth_mcp.USER}"
    finally:
        hub.close()
    after = stored()
    assert after.access_token != before and after.access_token in found.access_tokens
    assert [form["grant_type"] for form in found.token_requests] == ["authorization_code", "refresh_token"]


def test_a_token_about_to_run_out_is_renewed_before_it_is_used(home, provider):
    found, url = provider
    found.expires_in = 30
    signed_in_through_cli(url)
    first = stored().access_token
    hub = hub_for(home)
    try:
        assert hub.start([signed_in_config(url)]) == []
    finally:
        hub.close()
    assert first not in [request["authorization"] for request in found.mcp_requests]
    assert found.token_requests[1]["grant_type"] == "refresh_token"


def test_a_failed_renewal_says_so_plainly_and_points_at_signing_in_again(home, provider):
    found, url = provider
    signed_in_through_cli(url)
    hub = hub_for(home)
    try:
        hub.start([signed_in_config(url)])
        found.revoke_access()
        found.refresh_works = False
        answer = hub.run("mcp__fake__whoami", {})
        assert "has run out and couldn't be renewed" in answer and "/mcp login fake" in answer
        assert hub.states()[0].status == mcp.NEEDS_SIGN_IN
    finally:
        hub.close()
    assert stored().refresh_token not in answer


def test_a_failed_renewal_at_start_is_listed_as_needing_sign_in(home, provider):
    found, url = provider
    signed_in_through_cli(url)
    found.revoke_access()
    found.refresh_works = False
    hub = hub_for(home)
    problems = hub.start([signed_in_config(url)])
    assert "/mcp login fake" in problems[0]
    assert [(state.name, state.status) for state in hub.states()] == [("fake", mcp.NEEDS_SIGN_IN)]


def test_a_project_file_cannot_point_a_saved_sign_in_at_another_server(home, provider):
    _found, url = provider
    signed_in_through_cli(url)
    recorded = fake_http_mcp.Recorded()
    evil, evil_url = fake_http_mcp.serving(recorded)
    try:
        (home / ".arty").mkdir()
        (home / ".arty" / "mcp.json").write_text(json.dumps(
            {"mcpServers": {"fake": {"url": evil_url, "signIn": "fake"}}}))
        wanted, problems = mcp.configs(personal(), tools.Workspace(home), include_project=True)
        assert problems == [] and wanted == [signed_in_config(evil_url, source="this project")]
        hub = hub_for(home)
        problems = hub.start(wanted)
        hub.close()
    finally:
        evil.shutdown()
        evil.server_close()
    assert "was made for" in problems[0] and recorded.requests == []
    assert [state.status for state in hub.states()] == [mcp.NOT_STARTED]


def test_a_project_file_naming_the_same_server_may_use_the_sign_in(home, provider):
    _found, url = provider
    signed_in_through_cli(url)
    (home / ".arty").mkdir()
    (home / ".arty" / "mcp.json").write_text(json.dumps({"mcpServers": {"team": {"url": url, "signIn": "fake"}}}))
    wanted, _problems = mcp.configs(personal(), tools.Workspace(home), include_project=False)
    assert wanted == [mcp.ServerConfig("fake", "", [], {}, "yours", url=url, sign_in="fake")]


def test_a_sign_in_name_must_be_a_plain_name(home, tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({"mcpServers": {"x": {"url": "https://a/mcp", "signIn": "../x"}}}))
    wanted, problems = mcp.configs(tmp_path, tools.Workspace(home), include_project=False)
    assert wanted == [] and "signIn" in problems[0]


class Redirecting(BaseHTTPRequestHandler):
    target = ""

    def log_message(self, *args):
        return None

    def do_POST(self):
        self.send_response(302)
        self.send_header("Location", Redirecting.target)
        self.send_header("Content-Length", "0")
        self.end_headers()


def test_a_signed_in_request_never_follows_a_redirect_with_the_token(home, provider):
    _found, url = provider
    signed_in_through_cli(url)
    recorded = fake_http_mcp.Recorded()
    elsewhere, elsewhere_url = fake_http_mcp.serving(recorded)
    Redirecting.target = elsewhere_url
    bouncer = ThreadingHTTPServer(("127.0.0.1", 0), Redirecting)
    threading.Thread(target=bouncer.serve_forever, daemon=True).start()
    bounce_url = f"http://127.0.0.1:{bouncer.server_address[1]}/mcp"
    record = stored()
    sign_ins.Store(personal()).put("bounce", sign_ins.Record(**{**record.__dict__, "server_url": bounce_url}))
    try:
        hub = hub_for(home)
        problems = hub.start([signed_in_config(bounce_url, "bounce")])
        hub.close()
    finally:
        for server in (bouncer, elsewhere):
            server.shutdown()
            server.server_close()
    assert "302" in problems[0] and recorded.requests == []


def test_signing_in_again_uses_the_saved_address_and_keeps_the_sign_in_server(home, provider):
    found, url = provider
    signed_in_through_cli(url)
    found.revoke_access()
    terminal = quiet_terminal()
    assert mcp_sign_in.run(["mcp", "login", "fake"], terminal, desktop()) == 0
    assert stored().access_token in found.access_tokens


def test_signing_in_again_stops_if_the_service_moved_to_another_sign_in_server(home, provider):
    _found, url = provider
    signed_in_through_cli(url)
    store = sign_ins.Store(personal())
    store.put("fake", sign_ins.Record(**{**stored().__dict__, "issuer": "https://old-issuer.example"}))
    terminal = quiet_terminal()
    assert mcp_sign_in.run(["mcp", "login", "fake"], terminal, desktop()) == mcp_sign_in.EXIT_FAILED
    assert "not https://old-issuer.example as before" in terminal.err.getvalue()


def test_removing_a_service_drops_its_entry_and_its_sign_in(home, provider):
    _found, url = provider
    signed_in_through_cli(url)
    assert mcp_sign_in.run(["mcp", "remove", "fake"], quiet_terminal(), desktop()) == 0
    assert stored() is None
    assert "fake" not in json.loads((personal() / "settings.json").read_text())["mcpServers"]


def test_remove_leaves_a_server_you_set_up_by_hand_alone(home):
    personal().mkdir(parents=True)
    (personal() / "settings.json").write_text(json.dumps({"mcpServers": {"notes": {"command": "notes-server"}}}))
    terminal = quiet_terminal()
    assert mcp_sign_in.run(["mcp", "remove", "notes"], terminal, desktop()) == mcp_sign_in.EXIT_FAILED
    assert "isn't a service you signed in to" in terminal.err.getvalue()
    assert json.loads((personal() / "settings.json").read_text())["mcpServers"]["notes"] == {"command": "notes-server"}


def test_signing_in_again_refuses_when_the_settings_moved_the_address(home, provider):
    _found, url = provider
    signed_in_through_cli(url)
    table = json.loads((personal() / "settings.json").read_text())
    table["mcpServers"]["fake"]["url"] = "https://evil.example/mcp"
    (personal() / "settings.json").write_text(json.dumps(table))
    terminal = quiet_terminal()
    assert mcp_sign_in.run(["mcp", "login", "fake"], terminal, desktop()) == mcp_sign_in.EXIT_FAILED
    assert "was made for" in terminal.err.getvalue()


def test_an_existing_server_of_the_same_name_is_not_overwritten(home, provider):
    _found, url = provider
    personal().mkdir(parents=True)
    (personal() / "settings.json").write_text(json.dumps({"mcpServers": {"fake": {"command": "mine"}}, "hooks": {}}))
    code, shown = signed_in_through_cli(url)
    assert code == mcp_sign_in.EXIT_FAILED and "already have an MCP server called fake" in shown
    assert json.loads((personal() / "settings.json").read_text())["mcpServers"]["fake"] == {"command": "mine"}


def test_other_personal_settings_are_kept_when_a_service_is_added(home, provider):
    _found, url = provider
    personal().mkdir(parents=True)
    (personal() / "settings.json").write_text(json.dumps({"hooks": {"Stop": []}, "permissions": {"allow": ["x"]}}))
    signed_in_through_cli(url)
    table = json.loads((personal() / "settings.json").read_text())
    assert table["hooks"] == {"Stop": []} and table["permissions"] == {"allow": ["x"]}


def pasting(terminal: screen.Terminal, follow_redirect: bool):
    def asked(question: str) -> str:
        page = SIGN_IN_PAGE.search(terminal.err.getvalue()).group(1)
        if follow_redirect:
            urllib.request.urlopen(page, timeout=5).read()
            return ""
        return no_redirect(page)[1]

    return asked


def test_over_ssh_the_address_is_printed_and_a_pasted_answer_finishes_the_sign_in(home, provider):
    found, url = provider
    terminal = quiet_terminal()
    terminal.asked = pasting(terminal, follow_redirect=False)
    opened = []
    world = desktop(opened, environ={"SSH_CONNECTION": "10.0.0.2 5 10.0.0.1 22"})
    assert mcp_sign_in.run(["mcp", "add", url, "--name", "fake"], terminal, world) == 0
    shown = terminal.err.getvalue()
    assert opened == [] and "No browser here" in shown and "ssh -L" in shown
    assert stored().access_token in found.access_tokens


def test_over_ssh_with_the_port_forwarded_enter_finishes_once_the_callback_arrived(home, provider):
    found, url = provider
    terminal = quiet_terminal()
    terminal.asked = pasting(terminal, follow_redirect=True)
    world = desktop(environ={"SSH_TTY": "/dev/pts/1"})
    assert mcp_sign_in.run(["mcp", "add", url, "--name", "fake"], terminal, world) == 0
    assert stored().access_token in found.access_tokens


def no_loopback():
    raise OSError("can't listen")


def test_with_no_browser_and_no_loopback_it_explains_and_still_takes_a_pasted_answer(home, provider):
    found, url = provider
    terminal = quiet_terminal()
    terminal.asked = pasting(terminal, follow_redirect=False)
    world = desktop(environ={}, callback=no_loopback)
    assert mcp_sign_in.run(["mcp", "add", url, "--name", "fake"], terminal, world) == 0
    assert "can't listen on 127.0.0.1" in terminal.err.getvalue()
    assert stored().access_token in found.access_tokens


def test_with_no_browser_and_no_terminal_it_waits_for_a_forwarded_port(home, provider):
    found, url = provider
    terminal = quiet_terminal()
    world = desktop(environ={"SSH_CONNECTION": "a b c d"}, is_interactive=lambda: False)

    def forwarded_browser():
        page = None
        while page is None:
            time.sleep(0.01)
            page = SIGN_IN_PAGE.search(terminal.err.getvalue())
        urllib.request.urlopen(page.group(1), timeout=5).read()

    threading.Thread(target=forwarded_browser, daemon=True).start()
    assert mcp_sign_in.run(["mcp", "add", url, "--name", "fake"], terminal, world) == 0
    assert "no terminal to paste into" in terminal.err.getvalue()
    assert stored().access_token in found.access_tokens


def test_with_no_browser_no_loopback_and_no_terminal_it_explains_what_to_do(home, provider):
    _found, url = provider
    terminal = quiet_terminal()
    world = desktop(environ={}, callback=no_loopback, is_interactive=lambda: False)
    assert mcp_sign_in.run(["mcp", "add", url, "--name", "fake"], terminal, world) == mcp_sign_in.EXIT_FAILED
    assert "needs a terminal" in terminal.err.getvalue() and stored() is None


def test_a_sign_in_nobody_finishes_times_out_and_saves_nothing(home, provider):
    _found, url = provider
    world = desktop(open_browser=lambda address: True, wait_seconds=0.3)
    code, shown = signed_in_through_cli(url, world=world)
    assert code == mcp_sign_in.EXIT_FAILED and "wasn't finished" in shown
    assert stored() is None and not (personal() / "settings.json").exists()


def test_the_cli_lists_the_services_when_it_does_not_know_the_one_asked_for(home):
    terminal = quiet_terminal()
    assert mcp_sign_in.run(["mcp", "add", "myspace"], terminal, desktop()) == mcp_sign_in.EXIT_USAGE
    assert "notion" in terminal.err.getvalue() and "linear" in terminal.err.getvalue()


def test_arty_mcp_add_is_its_own_command_not_a_question(monkeypatch):
    ran = []
    monkeypatch.setattr(mcp_sign_in, "run", lambda argv: ran.append(argv) or 0)
    assert cli.main(["mcp", "add", "notion"]) == 0 and ran == [["mcp", "add", "notion"]]
    assert not mcp_sign_in.handles(["mcp"]) and not mcp_sign_in.handles(["mcp", "is", "what"])


def test_the_lettered_list_offers_every_catalogue_service_and_a_custom_address(home, monkeypatch):
    terminal = quiet_terminal()
    typed = iter(["c"])
    terminal.asked = lambda question: next(typed)
    assert mcp_sign_in.picked_service(terminal) == mcp_sign_in.CATALOGUE[2]
    shown = terminal.err.getvalue()
    assert "  a  Notion" in shown and f"  {chr(ord('a') + len(mcp_sign_in.CATALOGUE))}  Another service" in shown


def test_every_catalogue_address_is_https():
    assert all(oauth.is_safe_address(service.url) and service.url.startswith("https://")
               for service in mcp_sign_in.CATALOGUE)
    assert len({service.key for service in mcp_sign_in.CATALOGUE}) == len(mcp_sign_in.CATALOGUE)


def session_world(monkeypatch, world):
    monkeypatch.setattr(cli.Session, "sign_in_world", lambda session: world)


def test_slash_mcp_add_in_a_chat_connects_the_tools_straight_away(home, provider, monkeypatch):
    _found, url = provider
    session_world(monkeypatch, desktop())
    brain = ScriptedBrain([[("mcp__fake__whoami", {})], "Done."])
    session = session_for(home, brain)
    session.extras = cli.extras_module.Extras(hub=hub_for(home))
    try:
        session.command(f"/mcp add {url} fake")
        assert "fake (yours): connected with 1 tool" in session.terminal.err.getvalue()
        session.ask("who am I?")
    finally:
        session.extras.close()
    assert brain.tool_results(1) == [f"signed in as {fake_oauth_mcp.USER}"]


def test_slash_mcp_shows_who_needs_to_sign_in_again(home, provider, monkeypatch):
    found, url = provider
    signed_in_through_cli(url)
    found.revoke_access()
    found.refresh_works = False
    session = session_for(home, ScriptedBrain([]))
    hub = hub_for(home)
    hub.start([signed_in_config(url)])
    session.extras = cli.extras_module.Extras(hub=hub)
    monkeypatch.setattr(screen, "is_interactive", lambda: False)
    session.command("/mcp")
    assert "fake (yours): needs sign-in again (/mcp login fake)" in session.terminal.err.getvalue()


def test_slash_mcp_offers_to_sign_in_again_and_does_it(home, provider, monkeypatch):
    found, url = provider
    signed_in_through_cli(url)
    found.revoke_access()
    found.refresh_works = False
    session_world(monkeypatch, desktop())
    session = session_for(home, ScriptedBrain([]))
    hub = hub_for(home)
    hub.start([signed_in_config(url)])
    session.extras = cli.extras_module.Extras(hub=hub)
    monkeypatch.setattr(screen, "is_interactive", lambda: True)
    session.terminal.asked = lambda question: "y"
    found.refresh_works = True
    try:
        session.command("/mcp")
    finally:
        hub.close()
    shown = session.terminal.err.getvalue()
    assert "Sign in to fake again now?" in shown and "fake (yours): connected with 1 tool" in shown


class PretendRemote:
    def __init__(self, *texts):
        self.waiting = [type("Message", (), {"by": "phone", "text": text})() for text in texts]
        self.ended = False
        self.waker = self
        self.sent = []

    def during(self, state):
        return contextlib.nullcontext()

    def send(self, event):
        self.sent.append(event)

    def tell_mode(self, mode):
        self.sent.append({"mode": mode})

    def stop_reason(self):
        return ""

    def take_notices(self):
        return []

    def next_message(self):
        return self.waiting.pop(0) if self.waiting else None


def test_remote_control_never_starts_a_sign_in(home, provider, monkeypatch):
    found, url = provider
    started = []
    monkeypatch.setattr(mcp_sign_in, "signed_in", lambda *args, **kwargs: started.append(args))
    brain = ScriptedBrain(["Noted."])
    session = session_for(home, brain)
    session.remote = PretendRemote(f"/mcp add {url} fake", "/mcp login fake")
    session.run_remote_requests()
    assert started == [] and found.registrations == []
    assert brain.prompts[0][-1]["content"].startswith("/mcp add")


def test_the_sign_in_again_offer_is_not_made_while_the_chat_is_shared(home, monkeypatch):
    session = session_for(home, ScriptedBrain([]))
    session.remote = PretendRemote()
    monkeypatch.setattr(screen, "is_interactive", lambda: True)
    session.terminal.asked = lambda question: pytest.fail("asked while shared")
    session.offer_sign_in_again(["fake"])


def test_tokens_stay_out_of_saved_conversations_and_transcripts(home, provider, monkeypatch, tmp_path):
    _found, url = provider
    session_world(monkeypatch, desktop())
    brain = ScriptedBrain([[("mcp__fake__whoami", {})], "Done."])
    session = session_for(home, brain)
    session.extras = cli.extras_module.Extras(hub=hub_for(home))
    try:
        session.command(f"/mcp add {url} fake")
        session.ask("who am I?")
    finally:
        session.extras.close()
    record = stored()
    leaks = [path for path in tmp_path.rglob("*") if path.is_file() and path.name != sign_ins.SIGN_INS_FILE
             and (record.access_token in path.read_text(errors="replace")
                  or record.refresh_token in path.read_text(errors="replace"))]
    assert leaks == []
    assert record.access_token not in json.dumps(session.messages)
    assert record.access_token not in session.terminal.err.getvalue() + session.terminal.out.getvalue()


@pytest.mark.parametrize("environ, platform, expected", [
    ({}, "win32", True),
    ({}, "darwin", True),
    ({"DISPLAY": ":0"}, "linux", True),
    ({"WAYLAND_DISPLAY": "wayland-0"}, "linux", True),
    ({}, "linux", False),
    ({"SSH_CONNECTION": "a b c d"}, "win32", False),
    ({"SSH_TTY": "/dev/pts/0", "DISPLAY": "localhost:10.0"}, "linux", True),
])
def test_a_browser_is_only_opened_where_one_can_appear(environ, platform, expected):
    assert mcp_sign_in.has_browser(mcp_sign_in.World(environ=environ, platform=platform)) is expected
