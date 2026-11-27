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
from tests import fake_http_mcp, fake_oauth_mcp, fake_probe_mcp
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
    assert "arty mcp login" not in shown
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


@pytest.fixture
def open_server():
    recorded = fake_http_mcp.Recorded()
    server, url = fake_http_mcp.serving(recorded, token=None)
    yield recorded, url
    server.shutdown()
    server.server_close()


def test_adding_a_server_that_needs_no_sign_in_saves_a_plain_entry(home, open_server):
    recorded, url = open_server
    opened = []
    code, shown = signed_in_through_cli(url, name="open", world=desktop(opened))
    assert code == 0, shown
    entry = json.loads((personal() / "settings.json").read_text())["mcpServers"]["open"]
    assert entry == {"type": "http", "url": url}
    assert stored("open") is None and opened == []
    assert "needs no sign-in" in shown
    assert recorded.deleted == [fake_http_mcp.SESSION, fake_http_mcp.SESSION]


def test_slash_mcp_add_connects_a_server_that_needs_no_sign_in(home, open_server, monkeypatch):
    _recorded, url = open_server
    session_world(monkeypatch, desktop())
    session = session_for(home, ScriptedBrain([]))
    session.extras = cli.extras_module.Extras(hub=hub_for(home))
    try:
        session.command(f"/mcp add {url} open")
        shown = session.terminal.err.getvalue()
        assert "open (yours): connected with 2 tools" in shown
        assert session.extras.hub.run("mcp__open__lookup", {"q": "x"}) == "found x"
    finally:
        session.extras.close()


def test_a_server_that_answers_without_sign_in_but_cannot_be_used_saves_nothing(home, open_server, monkeypatch):
    _recorded, url = open_server
    def failing(self):
        raise mcp.McpError("the tool list was unreadable")

    monkeypatch.setattr(mcp.WebServer, "start", failing)
    code, shown = signed_in_through_cli(url, name="open")
    assert code == mcp_sign_in.EXIT_FAILED and "couldn't use it: the tool list was unreadable" in shown
    assert not (personal() / "settings.json").exists()


@pytest.fixture
def probe_server():
    started = []

    def start(answer=fake_probe_mcp.healthy):
        server, holder = fake_probe_mcp.serving(answer)
        started.append(server)
        return holder

    yield start
    for server in started:
        server.shutdown()
        server.server_close()


def settings_table() -> dict:
    return json.loads((personal() / "settings.json").read_text())


def test_a_server_that_works_anonymously_but_publishes_a_sign_in_is_signed_in_to(home, provider):
    found, url = provider
    found.anonymous = True
    opened = []
    code, shown = signed_in_through_cli(url, world=desktop(opened))
    assert code == 0, shown
    assert settings_table()["mcpServers"]["fake"] == {"url": url, "signIn": "fake"}
    assert stored().access_token in found.access_tokens and len(opened) == 1


def test_a_server_that_works_anonymously_but_publishes_a_sign_in_can_be_signed_in_to_again(home, provider):
    found, url = provider
    found.anonymous = True
    signed_in_through_cli(url)
    terminal = quiet_terminal()
    assert mcp_sign_in.run(["mcp", "login", "fake"], terminal, desktop()) == 0, terminal.err.getvalue()


def test_a_catalogue_service_is_never_saved_without_a_sign_in(home, monkeypatch):
    service = mcp_sign_in.CATALOGUE[0]
    monkeypatch.setattr(mcp_sign_in, "first_answer", lambda url, world: (200, {}, ""))
    monkeypatch.setattr(mcp_sign_in, "opened_or_offer", lambda *args: pytest.fail("tried an anonymous connection"))
    with pytest.raises(mcp_sign_in.SignInStopped):
        mcp_sign_in.added(quiet_terminal(), personal(), service, "notion", sign_ins.Store(personal()),
                          canned_world({}, {}, {service.url: (200, {}, b"")}))


def in_band_auth_error(handler, message) -> None:
    if message and message.get("method") == "initialize":
        return fake_probe_mcp.rpc(handler, message, error={"code": 401, "message": "Authentication required"})
    fake_probe_mcp.send(handler, 404)


def forbidden(handler, message) -> None:
    if message and message.get("method") == "tools/list":
        return fake_probe_mcp.send(handler, 403, b"no")
    fake_probe_mcp.healthy(handler, message)


@pytest.mark.parametrize("answer, said", [(in_band_auth_error, "Authentication required"),
                                          (forbidden, "answered 403 Forbidden")])
def test_an_answer_that_asks_for_auth_without_a_401_reports_what_the_server_said(home, probe_server, answer, said):
    holder = probe_server(answer)
    code, shown = signed_in_through_cli(f"{holder.address}/mcp", name="odd")
    assert code == mcp_sign_in.EXIT_FAILED and said in shown and "publishes no sign-in" in shown
    assert "couldn't use it" not in shown and not (personal() / "settings.json").exists()
    discovery_requests = [path for command, path, _headers, _body in holder.requests if command == "GET"]
    assert len(discovery_requests) == len(set(discovery_requests))


def test_a_signed_in_server_is_never_replaced_by_adding_it_again(home, provider):
    found, url = provider
    signed_in_through_cli(url)
    before = (settings_table(), stored())
    found.anonymous = True
    code, shown = signed_in_through_cli(url)
    assert code == mcp_sign_in.EXIT_FAILED and "already have an MCP server called fake" in shown
    assert "remove it first" in shown and "arty mcp login fake" in shown
    assert (settings_table(), stored()) == before


def test_adding_a_plain_entry_again_is_refused_and_remove_then_makes_it_work(home, open_server):
    _recorded, url = open_server
    assert signed_in_through_cli(url, name="open")[0] == 0
    code, shown = signed_in_through_cli(url, name="open")
    assert code == mcp_sign_in.EXIT_FAILED and "remove it first with arty mcp remove open" in shown
    assert "arty mcp login open" in shown
    terminal = quiet_terminal()
    assert mcp_sign_in.run(["mcp", "remove", "open"], terminal, desktop()) == 0, terminal.err.getvalue()
    assert "open" not in settings_table()["mcpServers"]
    assert signed_in_through_cli(url, name="open")[0] == 0


def test_slash_mcp_remove_takes_a_plain_entry_away_and_stops_its_tools(home, open_server, monkeypatch):
    _recorded, url = open_server
    session_world(monkeypatch, desktop())
    session = session_for(home, ScriptedBrain([]))
    session.extras = cli.extras_module.Extras(hub=hub_for(home))
    try:
        session.command(f"/mcp add {url} open")
        session.command("/mcp remove open")
        assert "open" not in settings_table()["mcpServers"]
        assert session.extras.hub.tool("mcp__open__lookup") is None
    finally:
        session.extras.close()


def test_a_plain_entry_with_headers_is_still_left_alone_by_remove(home):
    personal().mkdir(parents=True)
    entry = {"type": "http", "url": "https://a.example/mcp", "headers": {"X-Key": "1"}}
    (personal() / "settings.json").write_text(json.dumps({"mcpServers": {"mine": entry}}))
    assert mcp_sign_in.run(["mcp", "remove", "mine"], quiet_terminal(), desktop()) == mcp_sign_in.EXIT_FAILED
    assert settings_table()["mcpServers"]["mine"] == entry


def test_logging_in_to_a_plain_entry_that_now_asks_for_a_sign_in_converts_it(home, provider):
    found, url = provider
    personal().mkdir(parents=True)
    (personal() / "settings.json").write_text(json.dumps({"mcpServers": {"fake": {"type": "http", "url": url}}}))
    terminal = quiet_terminal()
    assert mcp_sign_in.run(["mcp", "login", "fake"], terminal, desktop()) == 0, terminal.err.getvalue()
    assert settings_table()["mcpServers"]["fake"] == {"url": url, "signIn": "fake"}
    assert stored().access_token in found.access_tokens


def test_a_plain_entry_whose_server_now_wants_a_sign_in_says_how_to_sign_in(home, provider):
    _found, url = provider
    config = mcp.ServerConfig("fake", "", [], {}, "yours", url=url, plain=True)
    hub = mcp.Hub(tools.Workspace(home))
    problems = hub.start([config])
    assert len(problems) == 1 and "fake now asks for a sign-in: run arty mcp login fake" in problems[0]
    assert problems[0].count("login") == 1
    assert hub.states()[0].status == mcp.NEEDS_SIGN_IN


def test_a_plain_entry_whose_tool_call_gets_a_401_says_how_to_sign_in(home, probe_server):
    def refusing_calls(handler, message):
        if message and message.get("method") == "tools/call":
            return fake_probe_mcp.send(handler, 401, headers={"WWW-Authenticate": "Bearer"})
        fake_probe_mcp.healthy(handler, message)

    holder = probe_server(refusing_calls)
    hub = mcp.Hub(tools.Workspace(home))
    hub.start([mcp.ServerConfig("odd", "", [], {}, "yours", url=f"{holder.address}/mcp", plain=True)])
    try:
        assert hub.run("mcp__odd__lookup", {}).startswith("Error: odd now asks for a sign-in: run arty mcp login odd")
    finally:
        hub.close()


def test_a_hostile_deeply_nested_answer_is_a_note_not_a_traceback(home, probe_server):
    def nested(handler, message):
        if message and message.get("method") == "tools/list":
            return fake_probe_mcp.send(handler, 200, b"[" * 200_000, {"Content-Type": "application/json"})
        fake_probe_mcp.healthy(handler, message)

    holder = probe_server(nested)
    code, shown = signed_in_through_cli(f"{holder.address}/mcp", name="deep")
    assert code == mcp_sign_in.EXIT_FAILED and "Traceback" not in shown and "Nothing was saved" in shown


def test_a_hostile_deeply_nested_answer_in_slash_mcp_add_is_a_note(home, probe_server, monkeypatch):
    def nested(handler, message):
        if message and message.get("method") == "tools/list":
            return fake_probe_mcp.send(handler, 200, b"[" * 200_000, {"Content-Type": "application/json"})
        fake_probe_mcp.healthy(handler, message)

    holder = probe_server(nested)
    session_world(monkeypatch, desktop())
    session = session_for(home, ScriptedBrain([]))
    session.extras = cli.extras_module.Extras(hub=hub_for(home))
    session.command(f"/mcp add {holder.address}/mcp deep")
    assert "Nothing was saved" in session.terminal.err.getvalue()


def test_the_anonymous_check_never_follows_a_redirect(home, probe_server):
    other = probe_server(lambda handler, message: fake_probe_mcp.send(handler, 200, b"{}"))

    def redirecting(handler, message):
        if message and message.get("method") == "initialize" and len(holder.methods()) > 1:
            return fake_probe_mcp.send(handler, 302, headers={"Location": f"{other.address}/internal"})
        fake_probe_mcp.healthy(handler, message)

    holder = probe_server(redirecting)
    code, _shown = signed_in_through_cli(f"{holder.address}/mcp", name="hop")
    assert code == mcp_sign_in.EXIT_FAILED and other.requests == []


def test_a_server_that_keeps_the_anonymous_check_going_is_stopped_at_the_deadline(home, probe_server, monkeypatch):
    def slow_list(handler, message):
        if message and message.get("method") == "tools/list":
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.end_headers()
            for _beat in range(20):
                handler.wfile.write(b": keep going\n\n")
                handler.wfile.flush()
                time.sleep(0.2)
            return None
        fake_probe_mcp.healthy(handler, message)

    holder = probe_server(slow_list)
    monkeypatch.setattr(mcp_sign_in, "OPEN_DEADLINE_SECONDS", 1)
    started = time.monotonic()
    code, shown = signed_in_through_cli(f"{holder.address}/mcp", name="slow")
    assert code == mcp_sign_in.EXIT_FAILED and "took longer than" in shown and time.monotonic() - started < 3


def test_an_oversized_anonymous_answer_is_refused(home, probe_server, monkeypatch):
    def huge(handler, message):
        if message and message.get("method") == "tools/list":
            return fake_probe_mcp.send(handler, 200, b" " * 5_000, {"Content-Type": "application/json"})
        fake_probe_mcp.healthy(handler, message)

    holder = probe_server(huge)
    monkeypatch.setattr(mcp_sign_in, "OPEN_MAX_ANSWER_BYTES", 1_000)
    code, shown = signed_in_through_cli(f"{holder.address}/mcp", name="big")
    assert code == mcp_sign_in.EXIT_FAILED and "more than" in shown and not (personal() / "settings.json").exists()


def test_a_401_after_an_anonymous_start_keeps_the_challenge_for_the_sign_in(home, probe_server):
    def late_401(handler, message):
        if message and message.get("method") == "tools/list":
            challenge = f'Bearer scope="read", resource_metadata="{holder.address}/custom"'
            return fake_probe_mcp.send(handler, 401, headers={"WWW-Authenticate": challenge})
        fake_probe_mcp.healthy(handler, message)

    holder = probe_server(late_401)
    offered = mcp_sign_in.opened_or_offer("Odd", "odd", f"{holder.address}/mcp", desktop())
    assert isinstance(offered, mcp_sign_in.SignInOffer) and offered.hints["scope"] == "read"


def test_a_403_with_a_challenge_does_not_fetch_the_metadata_it_names(home):
    fetched = []
    challenge = 'Bearer resource_metadata="https://meta.example/custom"'
    answers = {HOSTILE_SERVER: (403, {"WWW-Authenticate": challenge}, b"")}

    def fetch(url, *rest):
        fetched.append(url)
        return answers.get(url, (404, {}, b"{}"))

    world = canned_world({}, {}, fetch=fetch)
    mcp_sign_in.run(["mcp", "add", HOSTILE_SERVER, "--name", "x"], quiet_terminal(), world)
    assert "https://meta.example/custom" not in fetched


@pytest.mark.parametrize("status, wording", [(404, "Answered 404"), (500, "Answered 500"), (403, "403")])
def test_the_status_line_says_what_the_server_answered(home, status, wording):
    answers = {HOSTILE_SERVER: (status, {}, b"")}
    terminal = quiet_terminal()
    mcp_sign_in.run(["mcp", "add", HOSTILE_SERVER, "--name", "x"], terminal,
                    canned_world({}, {}, fetch=lambda url, *rest: answers.get(url, (404, {}, b"{}"))))
    shown = terminal.err.getvalue()
    assert wording in shown and "Needs a sign-in" not in shown.split("Find")[0]


def test_an_unreachable_server_closes_the_check_step_with_what_went_wrong(home):
    def unreachable(url, *rest):
        raise oauth.Unavailable(f"Couldn't reach {url}: refused")

    terminal = quiet_terminal()
    code = mcp_sign_in.run(["mcp", "add", HOSTILE_SERVER, "--name", "x"], terminal,
                           canned_world({}, {}, fetch=unreachable))
    shown = terminal.err.getvalue()
    assert code == mcp_sign_in.EXIT_FAILED and "Couldn't reach" in shown.split("Nothing was saved")[0]
    assert shown.count("Check whether") == 1 and "└ Couldn't reach" in shown


def test_the_anonymous_connection_goes_through_the_world(home):
    started = []

    class Stub:
        def __init__(self, config, **limits):
            started.append((config.url, limits))

        def start(self):
            return []

        def close(self):
            return ""

    answers = {HOSTILE_SERVER: (200, {}, b"{}")}
    world = canned_world({}, {}, answers, open_server=Stub,
                         fetch=lambda url, *rest: answers.get(url, (404, {}, b"{}")))
    code = mcp_sign_in.run(["mcp", "add", HOSTILE_SERVER, "--name", "stub"], quiet_terminal(), world)
    assert code == 0 and [url for url, _limits in started] == [HOSTILE_SERVER]
    assert settings_table()["mcpServers"]["stub"] == {"type": "http", "url": HOSTILE_SERVER}


def test_an_http_address_that_is_not_this_computer_is_turned_away_in_plain_words(home):
    code, shown = signed_in_through_cli("http://192.168.1.50:8080/mcp")
    assert code == mcp_sign_in.EXIT_FAILED and "isn't an https address, so ArtyCode won't add it" in shown


def hand_written_hub(home, holder, entry: dict, source: str = "yours") -> mcp.Hub:
    personal().mkdir(parents=True, exist_ok=True)
    written = {"mcpServers": {"w": {**entry, "url": f"{holder.address}/mcp"}}}
    (personal() / "settings.json").write_text(json.dumps(written))
    configs, problems = mcp.configs(personal(), tools.Workspace(home), False)
    assert problems == []
    hub = mcp.Hub(tools.Workspace(home))
    hub.start(configs)
    return hub


@pytest.mark.parametrize("said", ["Error assigning the issue to that user", "author not found",
                                  "prompt is over the tokenizer limit", "the blog index is rebuilding",
                                  "logging backend is down"])
def test_a_server_error_that_only_looks_like_auth_keeps_the_servers_own_words(home, probe_server, said):
    def failing_calls(handler, message):
        if message and message.get("method") == "tools/call":
            return fake_probe_mcp.rpc(handler, message, error={"code": -32000, "message": said})
        fake_probe_mcp.healthy(handler, message)

    holder = probe_server(failing_calls)
    hub = hand_written_hub(home, holder, {"headers": {"X-Api-Key": "k"}})
    try:
        assert hub.run("mcp__w__lookup", {}) == f"Error: w refused tools/call: {said}"
        assert hub.states()[0].status == mcp.CONNECTED
    finally:
        hub.close()


def test_a_hand_written_entry_with_headers_keeps_the_plain_401_wording(home, probe_server):
    holder = probe_server(lambda handler, message: fake_probe_mcp.send(handler, 401))
    hub = hand_written_hub(home, holder, {"headers": {"Authorization": "Bearer old"}})
    state = hub.states()[0]
    assert state.status == mcp.NOT_STARTED
    assert "w answered 401 Unauthorized" in state.problem and "arty mcp login" not in state.problem


def test_a_plain_entry_gets_the_sign_in_advice_for_a_real_auth_error_code_only(home, probe_server):
    def refusing(handler, message):
        if message and message.get("method") == "tools/call":
            return fake_probe_mcp.rpc(handler, message, error={"code": 401, "message": "token expired"})
        fake_probe_mcp.healthy(handler, message)

    holder = probe_server(refusing)
    hub = hand_written_hub(home, holder, {"type": "http"})
    try:
        assert "w now asks for a sign-in: run arty mcp login w" in hub.run("mcp__w__lookup", {})
        assert hub.states()[0].status == mcp.NEEDS_SIGN_IN
    finally:
        hub.close()


def test_a_project_entry_never_gets_the_sign_in_advice(home, probe_server):
    holder = probe_server(lambda handler, message: fake_probe_mcp.send(handler, 401))
    config = mcp.ServerConfig("w", "", [], {}, "this project", url=f"{holder.address}/mcp")
    hub = mcp.Hub(tools.Workspace(home))
    problems = hub.start([config])
    assert "arty mcp login" not in problems[0] and hub.states()[0].status == mcp.NOT_STARTED


def test_the_chat_connection_right_after_the_check_follows_no_redirect(home, probe_server, monkeypatch):
    other = probe_server(lambda handler, message: fake_probe_mcp.send(handler, 200, b"{}"))
    starts = []

    def answered(handler, message):
        if message is not None and message.get("method") == "initialize":
            starts.append(1)
            if len(starts) == 3:
                return fake_probe_mcp.send(handler, 302, headers={"Location": f"{other.address}/internal"})
        fake_probe_mcp.healthy(handler, message)

    holder = probe_server(answered)
    session_world(monkeypatch, desktop())
    session = session_for(home, ScriptedBrain([]))
    session.extras = cli.extras_module.Extras(hub=hub_for(home))
    try:
        session.command(f"/mcp add {holder.address}/mcp hop")
    finally:
        session.extras.close()
    assert other.requests == []


def trickling_body(handler, message):
    body = json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {}}).encode()
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    for index in range(len(body)):
        handler.wfile.write(body[index:index + 1])
        handler.wfile.flush()
        time.sleep(0.3)


def test_one_deadline_covers_the_first_answer_too(home, probe_server, monkeypatch):
    holder = probe_server(lambda handler, message: trickling_body(handler, message))
    monkeypatch.setattr(mcp_sign_in, "OPEN_DEADLINE_SECONDS", 1)
    started = time.monotonic()
    code, shown = signed_in_through_cli(f"{holder.address}/mcp", name="slow")
    assert code == mcp_sign_in.EXIT_FAILED and "took longer than" in shown and time.monotonic() - started < 4


def test_one_deadline_covers_every_stage_added_together(home, probe_server, monkeypatch):
    def pausing(handler, message):
        time.sleep(0.6)
        fake_probe_mcp.healthy(handler, message)

    holder = probe_server(pausing)
    monkeypatch.setattr(mcp_sign_in, "OPEN_DEADLINE_SECONDS", 2)
    started = time.monotonic()
    code, shown = signed_in_through_cli(f"{holder.address}/mcp", name="slow")
    assert code == mcp_sign_in.EXIT_FAILED and "took longer than" in shown and time.monotonic() - started < 5


def test_a_body_in_slow_pieces_is_stopped_at_the_deadline_while_it_is_read(home, probe_server, monkeypatch):
    def trickling_list(handler, message):
        if message and message.get("method") == "tools/list":
            return trickling_body(handler, message)
        fake_probe_mcp.healthy(handler, message)

    holder = probe_server(trickling_list)
    monkeypatch.setattr(mcp_sign_in, "OPEN_DEADLINE_SECONDS", 1)
    started = time.monotonic()
    code, shown = signed_in_through_cli(f"{holder.address}/mcp", name="slow")
    assert code == mcp_sign_in.EXIT_FAILED and "took longer than" in shown and time.monotonic() - started < 4


def test_one_endless_line_is_cut_off_at_the_size_cap_while_it_is_read(home, probe_server, monkeypatch):
    sent = []

    def endless_line(handler, message):
        if message and message.get("method") == "tools/list":
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.end_headers()
            try:
                for _piece in range(400):
                    handler.wfile.write(b"x" * 10_000)
                    handler.wfile.flush()
                    sent.append(10_000)
                    time.sleep(0.02)
            except OSError:
                return None
            return None
        fake_probe_mcp.healthy(handler, message)

    holder = probe_server(endless_line)
    monkeypatch.setattr(mcp_sign_in, "OPEN_MAX_ANSWER_BYTES", 100_000)
    started = time.monotonic()
    code, shown = signed_in_through_cli(f"{holder.address}/mcp", name="big")
    assert code == mcp_sign_in.EXIT_FAILED and "more than" in shown
    assert time.monotonic() - started < 3 and sum(sent) < 1_500_000


def test_sign_in_details_that_cannot_be_used_are_reported_and_nothing_is_saved(home, probe_server):
    def odd_details(handler, message):
        if message is None and "oauth-protected-resource" in handler.path:
            return fake_probe_mcp.send(handler, 200, json.dumps({"resource": "https://other.example/mcp"}).encode(),
                                       {"Content-Type": "application/json"})
        fake_probe_mcp.healthy(handler, message)

    holder = probe_server(odd_details)
    code, shown = signed_in_through_cli(f"{holder.address}/mcp", name="odd")
    assert code == mcp_sign_in.EXIT_FAILED and "couldn't tell whether" in shown
    assert not (personal() / "settings.json").exists()


def well_known_answers(answers: dict):
    def answer(handler, message):
        if message is None:
            wanted = answers.get(handler.path)
            if wanted is not None:
                status, body = wanted
                return fake_probe_mcp.send(handler, status, json.dumps(body).encode() if body else b"",
                                           {"Content-Type": "application/json"})
            return fake_probe_mcp.send(handler, 404)
        fake_probe_mcp.healthy(handler, message)

    return answer


def names_a_sign_in_server(address: str) -> tuple[int, dict]:
    return 200, {"resource": f"{address}/mcp", "authorization_servers": [address]}


@pytest.mark.parametrize("status", [404, 503])
def test_resource_metadata_naming_a_sign_in_server_with_no_details_stops_the_add(home, probe_server, status):
    holder = probe_server()
    answers = {"/.well-known/oauth-protected-resource/mcp": names_a_sign_in_server(holder.address)}
    answers.update({path: (status, {}) for path in ("/.well-known/oauth-authorization-server",
                                                   "/.well-known/openid-configuration")})
    holder.answer = well_known_answers(answers)
    code, shown = signed_in_through_cli(f"{holder.address}/mcp", name="odd")
    assert code == mcp_sign_in.EXIT_FAILED and "ArtyCode couldn't tell whether" in shown
    assert not (personal() / "settings.json").exists()


@pytest.mark.parametrize("status", [500, 503])
def test_a_server_error_on_every_well_known_address_stops_the_add(home, probe_server, status):
    holder = probe_server()

    def failing(handler, message):
        if message is None:
            return fake_probe_mcp.send(handler, status)
        fake_probe_mcp.healthy(handler, message)

    holder.answer = failing
    code, shown = signed_in_through_cli(f"{holder.address}/mcp", name="odd")
    assert code == mcp_sign_in.EXIT_FAILED and "ArtyCode couldn't tell whether" in shown and str(status) in shown
    assert not (personal() / "settings.json").exists()


def test_plain_not_found_answers_with_no_hint_still_mean_no_sign_in(home, probe_server):
    holder = probe_server()
    code, shown = signed_in_through_cli(f"{holder.address}/mcp", name="clean")
    assert code == 0, shown
    assert settings_table()["mcpServers"]["clean"] == {"type": "http", "url": f"{holder.address}/mcp"}


def test_a_dripping_401_body_is_stopped_at_the_deadline(home, probe_server, monkeypatch):
    def dripping(handler, message):
        time.sleep(1)
        handler.send_response(401)
        handler.send_header("Content-Length", "10")
        handler.end_headers()
        handler.wfile.write(b"x")
        handler.wfile.flush()
        time.sleep(4)

    holder = probe_server(dripping)
    monkeypatch.setattr(mcp_sign_in, "OPEN_DEADLINE_SECONDS", 2)
    started = time.monotonic()
    code, shown = signed_in_through_cli(f"{holder.address}/mcp", name="drip")
    assert code == mcp_sign_in.EXIT_FAILED and time.monotonic() - started < 2.7


def test_the_deadline_messages_end_with_one_full_stop(home, probe_server, monkeypatch):
    holder = probe_server(lambda handler, message: trickling_body(handler, message))
    monkeypatch.setattr(mcp_sign_in, "OPEN_DEADLINE_SECONDS", 1)
    _code, first = signed_in_through_cli(f"{holder.address}/mcp", name="first")
    assert "Not signed in: The check took longer than 1 s. Nothing was saved." in first

    def trickling_list(handler, message):
        if message and message.get("method") == "tools/list":
            return trickling_body(handler, message)
        fake_probe_mcp.healthy(handler, message)

    later = probe_server(trickling_list)
    _code, second = signed_in_through_cli(f"{later.address}/mcp", name="second")
    assert ("ArtyCode couldn't use it: the MCP server second: the check took longer than 1 s. Nothing was saved."
            in second)
    assert ".." not in first + second
