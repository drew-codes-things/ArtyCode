import contextlib
import http.client
import io
import json
import os
import re
import signal
import stat
import subprocess
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
from tests.test_oauth import cut_short_server, no_redirect

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
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
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
    assert not mcp_sign_in.handles(["mcp servers?"]) and not mcp_sign_in.handles(["fix", "mcp"])


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


def test_two_sessions_share_a_sign_in_whose_refresh_token_rotates(home, provider):
    found, url = provider
    signed_in_through_cli(url)
    first, second = hub_for(home), hub_for(home)
    try:
        assert first.start([signed_in_config(url)]) == [] and second.start([signed_in_config(url)]) == []
        found.revoke_access()
        assert first.run("mcp__fake__whoami", {}) == f"signed in as {fake_oauth_mcp.USER}"
        assert second.run("mcp__fake__whoami", {}) == f"signed in as {fake_oauth_mcp.USER}"
        assert [state.status for state in second.states()] == [mcp.CONNECTED]
    finally:
        first.close()
        second.close()
    assert [form["grant_type"] for form in found.token_requests].count("refresh_token") == 1
    assert stored().refresh_token in found.refresh_tokens and stored().access_token in found.access_tokens


def flaky_credentials(store: sign_ins.Store, network: dict, clock: dict):
    real = store.credentials

    def fetch(*args, **kwargs):
        if not network["up"]:
            raise oauth.Unavailable("Couldn't reach the token address: [Errno -3] Temporary failure in name resolution")
        return oauth.fetched(*args, **kwargs)

    return lambda name, server_url: real(name, server_url, fetch, lambda: clock["now"])


def test_a_brief_network_failure_is_not_reported_as_a_lost_sign_in(home, provider):
    found, url = provider
    signed_in_through_cli(url)
    store = sign_ins.Store(personal())
    network, clock = {"up": True}, {"now": time.time()}
    store.credentials = flaky_credentials(store, network, clock)
    hub = mcp.Hub(tools.Workspace(home), sign_ins_store=store)
    try:
        assert hub.start([signed_in_config(url)]) == []
        clock["now"] += found.expires_in - 30
        network["up"] = False
        assert hub.run("mcp__fake__whoami", {}) == f"signed in as {fake_oauth_mcp.USER}"
        found.revoke_access()
        answer = hub.run("mcp__fake__whoami", {})
        assert "couldn't reach the sign-in server" in answer and "has run out" not in answer
        assert [state.status for state in hub.states()] == [mcp.CONNECTED]
        network["up"] = True
        assert hub.run("mcp__fake__whoami", {}) == f"signed in as {fake_oauth_mcp.USER}"
    finally:
        hub.close()


def test_needs_sign_in_clears_once_the_sign_in_works_again(home, provider):
    found, url = provider
    signed_in_through_cli(url)
    hub = hub_for(home)
    try:
        hub.start([signed_in_config(url)])
        found.revoke_access()
        found.refresh_works = False
        assert "has run out" in hub.run("mcp__fake__whoami", {})
        assert [state.status for state in hub.states()] == [mcp.NEEDS_SIGN_IN]
        found.refresh_works = True
        assert hub.run("mcp__fake__whoami", {}) == f"signed in as {fake_oauth_mcp.USER}"
        assert [state.status for state in hub.states()] == [mcp.CONNECTED]
    finally:
        hub.close()


def test_a_renewal_that_cannot_be_saved_still_answers_and_says_so(home, provider, monkeypatch):
    found, url = provider
    signed_in_through_cli(url)
    hub = hub_for(home)
    try:
        hub.start([signed_in_config(url)])
        found.revoke_access()

        def unwritable(target, data, indent=None):
            raise PermissionError(13, "Permission denied", str(target))

        monkeypatch.setattr(sign_ins.settings, "write_private_json", unwritable)
        answer = hub.run("mcp__fake__whoami", {})
    finally:
        hub.close()
    assert answer.startswith(f"signed in as {fake_oauth_mcp.USER}") and "couldn't save it" in answer


def test_a_token_answer_cut_short_does_not_stop_artycode_starting(home):
    listener, token_endpoint = cut_short_server()
    store = sign_ins.Store(personal())
    url = "http://127.0.0.1:9/mcp"
    store.put("fake", sign_ins.Record(url, url, "http://127.0.0.1:9", token_endpoint, "client", "", "none", "old",
                                      "refresh", 1.0, ""))
    hub = mcp.Hub(None, sign_ins_store=store)
    try:
        problems = hub.start([signed_in_config(url)])
    finally:
        listener.close()
    assert len(problems) == 1 and "couldn't reach the sign-in server" in problems[0]


def test_a_tool_call_that_fails_at_the_network_answers_with_an_error(home, provider, monkeypatch):
    _found, url = provider
    signed_in_through_cli(url)
    hub = hub_for(home)
    try:
        hub.start([signed_in_config(url)])

        def broken(*args, **kwargs):
            raise http.client.IncompleteRead(b"{")

        monkeypatch.setattr(hub.servers["fake"], "opened", broken)
        assert hub.run("mcp__fake__whoami", {}).startswith("Error: ")
        monkeypatch.setattr(hub.servers["fake"], "call", lambda *args: (_ for _ in ()).throw(OSError("gone")))
        assert hub.run("mcp__fake__whoami", {}) == "Error: gone"
    finally:
        hub.close()


def test_closing_never_renews_a_token(home, provider):
    found, url = provider
    signed_in_through_cli(url)
    hub = hub_for(home)
    hub.start([signed_in_config(url)])
    credentials = hub.servers["fake"].credentials
    credentials.clock = lambda: credentials.record.expires_at + 1
    before = len(found.token_requests)
    hub.close()
    assert len(found.token_requests) == before


HOSTILE_SERVER = "https://mcp.hostile.example/mcp"
HOSTILE_ISSUER = "https://mcp.hostile.example"
ESCAPE = "\x1b"


def canned_world(resource: dict, metadata: dict, extra: dict | None = None, **changes) -> mcp_sign_in.World:
    answers = {
        HOSTILE_SERVER: (401, {}, b""),
        f"{HOSTILE_ISSUER}/.well-known/oauth-protected-resource/mcp": (200, {}, json.dumps(resource).encode()),
        f"{HOSTILE_ISSUER}/.well-known/oauth-authorization-server": (200, {}, json.dumps(metadata).encode()),
        f"{HOSTILE_ISSUER}/register": (201, {}, json.dumps({"client_id": "c"}).encode()),
        **(extra or {}),
    }
    return mcp_sign_in.World(**{"environ": {"DISPLAY": ":0"}, "platform": "linux", "open_browser": lambda url: True,
                                "fetch": lambda url, *rest: answers.get(url, (404, {}, b"{}")),
                                "is_interactive": lambda: True, "wait_seconds": 0.2, **changes})


def hostile_metadata(**changes) -> dict:
    return {"issuer": HOSTILE_ISSUER, "code_challenge_methods_supported": ["S256"],
            "authorization_endpoint": f"{HOSTILE_ISSUER}/authorize", "token_endpoint": f"{HOSTILE_ISSUER}/token",
            "registration_endpoint": f"{HOSTILE_ISSUER}/register", **changes}


def test_hostile_metadata_in_slash_mcp_add_is_a_note_not_the_end_of_the_chat(home, monkeypatch):
    world = canned_world({"resource": HOSTILE_SERVER, "authorization_servers": [HOSTILE_ISSUER],
                          "scopes_supported": [1]}, hostile_metadata())
    session_world(monkeypatch, world)
    session = session_for(home, ScriptedBrain([]))
    session.extras = cli.extras_module.Extras(hub=hub_for(home))
    session.command(f"/mcp add {HOSTILE_SERVER} hostile")
    assert "isn't a list of text" in session.terminal.err.getvalue() and stored("hostile") is None


@pytest.mark.parametrize("problem", [oauth.OAuthError("odd answer"), mcp.McpError("odd answer"),
                                     OSError(28, "No space left on device")])
def test_any_sign_in_or_mcp_failure_in_arty_mcp_add_is_a_note(home, monkeypatch, problem):
    def failing(*args, **kwargs):
        raise problem

    monkeypatch.setattr(mcp_sign_in, "added", failing)
    code, shown = signed_in_through_cli("https://mcp.example/mcp")
    assert code == mcp_sign_in.EXIT_FAILED and str(problem) in shown and "Traceback" not in shown


def test_a_sign_in_address_with_escape_codes_never_reaches_the_terminal(home):
    world = canned_world({"resource": HOSTILE_SERVER, "authorization_servers": [HOSTILE_ISSUER]},
                         hostile_metadata(authorization_endpoint=f"{HOSTILE_ISSUER}/authorize{ESCAPE}]0;owned\x07"
                                                                 f"{ESCAPE}[2J"))
    terminal = quiet_terminal()
    assert mcp_sign_in.run(["mcp", "add", HOSTILE_SERVER, "--name", "hostile"], terminal, world) == 1
    assert ESCAPE not in terminal.err.getvalue() and "\x07" not in terminal.err.getvalue()


def test_a_refusal_with_escape_codes_from_the_server_is_shown_without_them(home):
    refusal = json.dumps({"error_description": f"{ESCAPE}[2J{ESCAPE}[Hfake prompt"}).encode()
    world = canned_world({"resource": HOSTILE_SERVER, "authorization_servers": [HOSTILE_ISSUER]}, hostile_metadata(),
                         {f"{HOSTILE_ISSUER}/register": (400, {}, refusal)})
    terminal = quiet_terminal()
    assert mcp_sign_in.run(["mcp", "add", HOSTILE_SERVER, "--name", "hostile"], terminal, world) == 1
    shown = terminal.err.getvalue()
    assert ESCAPE not in shown and "fake prompt Nothing was saved." in shown


def signing_in_elsewhere(provider_and_url, answer: str):
    found, url = provider_and_url
    found.sign_in_base = found.base.replace("127.0.0.1", "localhost")
    terminal = quiet_terminal()
    asked = []
    terminal.asked = lambda question: asked.append(question) or answer
    code = mcp_sign_in.run(["mcp", "add", url, "--name", "fake"], terminal, desktop())
    return code, terminal.err.getvalue(), asked


def test_an_address_that_signs_in_on_another_host_asks_first_naming_both(home, provider):
    code, shown, asked = signing_in_elsewhere(provider, "y")
    assert code == 0 and len(asked) == 1
    assert "127.0.0.1 sends you to localhost to sign in. Sign in there?" in shown
    assert "  y  Yes, open the sign-in page at localhost" in shown and "  n  No, stop here" in shown
    assert stored() is not None


def test_saying_no_to_signing_in_on_another_host_stops_before_registering(home, provider):
    found, _url = provider
    code, shown, _asked = signing_in_elsewhere(provider, "n")
    assert code == mcp_sign_in.EXIT_FAILED and "chose not to sign in at localhost" in shown
    assert found.registrations == [] and stored() is None


def test_a_catalogue_service_signing_in_on_another_host_does_not_ask(home):
    discovery = oauth.Discovery("https://mcp.stripe.com", "https://mcp.stripe.com", "https://access.stripe.com/mcp",
                                "https://access.stripe.com/mcp/authorize", "", "", "")
    terminal = quiet_terminal()
    terminal.asked = lambda question: pytest.fail("asked about a catalogue service")
    assert mcp_sign_in.agreed_to_sign_in_elsewhere(terminal, "https://mcp.stripe.com", discovery, desktop())


def test_the_signed_in_account_is_shown_when_the_service_says_which_it_is(home, provider):
    found, url = provider
    found.account = "drew@example.com"
    code, shown = signed_in_through_cli(url)
    assert code == 0 and "Signed in as drew@example.com" in shown


def test_ctrl_c_while_waiting_for_the_browser_ends_cleanly(home, provider):
    _found, url = provider

    def interrupted(address):
        raise KeyboardInterrupt

    code, shown = signed_in_through_cli(url, world=desktop(open_browser=interrupted))
    assert code == mcp_sign_in.EXIT_INTERRUPTED and "Stopped." in shown and "Traceback" not in shown
    assert stored() is None


@pytest.mark.skipif(sys.platform == "win32", reason="sends a POSIX SIGINT")
def test_ctrl_c_in_a_real_arty_mcp_add_exits_without_a_traceback(home, provider, tmp_path):
    _found, url = provider
    environ = {**os.environ, "XDG_CONFIG_HOME": str(tmp_path / "config"), "APPDATA": str(tmp_path / "config"),
               "NO_COLOR": "1", "DISPLAY": "", "WAYLAND_DISPLAY": ""}
    environ["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    child = subprocess.Popen([sys.executable, "-m", "arty_cli", "mcp", "add", url, "--name", "fake"], env=environ,
                             cwd=home, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True)
    try:
        waited = time.monotonic() + 30
        while time.monotonic() < waited and not (tmp_path / "config").exists():
            time.sleep(0.05)
        time.sleep(1)
        child.send_signal(signal.SIGINT)
        _out, err = child.communicate(timeout=30)
    finally:
        if child.poll() is None:
            child.kill()
    assert child.returncode == mcp_sign_in.EXIT_INTERRUPTED and "Traceback" not in err and "Stopped." in err


def test_a_mistyped_paste_is_asked_for_again(home, provider):
    found, url = provider
    terminal = quiet_terminal()
    real = pasting(terminal, follow_redirect=False)
    typed = iter(["http://127.0.0.1:1/callback?code=x&state=typo", "not even an address"])
    terminal.asked = lambda question: next(typed, None) or real(question)
    world = desktop(environ={"SSH_CONNECTION": "10.0.0.2 5 10.0.0.1 22"})
    assert mcp_sign_in.run(["mcp", "add", url, "--name", "fake"], terminal, world) == 0
    shown = terminal.err.getvalue()
    assert "2 more tries" in shown and "1 more try" in shown and stored().access_token in found.access_tokens


def test_three_mistyped_pastes_stop_the_sign_in(home, provider):
    _found, url = provider
    terminal = quiet_terminal()
    terminal.asked = lambda question: "http://127.0.0.1:1/callback?code=x&state=typo"
    world = desktop(environ={"SSH_CONNECTION": "10.0.0.2 5 10.0.0.1 22"})
    assert mcp_sign_in.run(["mcp", "add", url, "--name", "fake"], terminal, world) == mcp_sign_in.EXIT_FAILED
    assert "doesn't match" in terminal.err.getvalue() and stored() is None


@pytest.mark.parametrize("argv, expected", [(["mcp"], mcp_sign_in.EXIT_GOOD), (["mcp", "list"], mcp_sign_in.EXIT_GOOD),
                                            (["mcp", "logn", "notion"], mcp_sign_in.EXIT_USAGE),
                                            (["mcp", "is", "what"], mcp_sign_in.EXIT_USAGE)])
def test_arty_mcp_without_a_known_action_shows_how_to_use_it(home, monkeypatch, argv, expected):
    monkeypatch.setattr(cli, "main_session", lambda arguments: pytest.fail("went to the model"))
    terminal = quiet_terminal()
    monkeypatch.setattr(mcp_sign_in, "Terminal", lambda: terminal)
    assert cli.main(argv) == expected
    assert "arty mcp add" in terminal.err.getvalue()


def test_removing_a_service_tells_it_to_forget_the_sign_in(home, provider):
    found, url = provider
    found.offers_revocation = True
    signed_in_through_cli(url)
    refresh = stored().refresh_token
    terminal = quiet_terminal()
    assert mcp_sign_in.run(["mcp", "remove", "fake"], terminal, desktop()) == 0
    assert stored() is None and "told to forget the sign-in too" in terminal.err.getvalue()
    assert [(form["token"], form["token_type_hint"]) for form in found.revocations] == [(refresh, "refresh_token")]


def test_a_service_that_will_not_forget_the_sign_in_is_still_removed(home, provider):
    found, url = provider
    found.offers_revocation = True
    found.revocation_status = 503
    signed_in_through_cli(url)
    terminal = quiet_terminal()
    assert mcp_sign_in.run(["mcp", "remove", "fake"], terminal, desktop()) == 0
    assert stored() is None and "removed here anyway" in terminal.err.getvalue()
    assert "fake" not in json.loads((personal() / "settings.json").read_text())["mcpServers"]


def test_an_unreadable_address_given_to_arty_mcp_add_is_a_usage_note(home):
    terminal = quiet_terminal()
    assert mcp_sign_in.run(["mcp", "add", "https://["], terminal, desktop()) == mcp_sign_in.EXIT_USAGE
    assert "arty mcp add" in terminal.err.getvalue()
