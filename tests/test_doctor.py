import io
import os
import urllib.error
from dataclasses import replace
from pathlib import Path

import pytest

from arty_cli import brain, doctor, settings

KEY = "arty_dev_s3cr3t-key-value"
ACCESS_SECRET = "access-s3cr3t-value"
HOME = "http://192.0.2.13:8081/v1"
AWAY = "https://arty.example/v1"
MODELS = [("arty/qwen3:14b", "the main model"), ("arty/coder:7b", "for code")]


class FakeClient:
    reachable = HOME
    marks_coding = True
    desktop_page = "results from the desktop"
    brain_search = "results from the brain"
    listing_fails = False
    window = 24_576

    def __init__(self, config):
        self.settings = config

    def pick_route(self):
        if not FakeClient.reachable:
            raise brain.BrainError(f"Can't reach Arty's brain (tried {HOME}). Is the server on, and is this computer online?")
        return brain.Route(FakeClient.reachable, {})

    def available_models(self):
        if FakeClient.listing_fails:
            raise brain.BrainError("Arty's brain couldn't list its models: 500")
        return list(MODELS)

    def model_entries(self, path):
        return [{"id": name, "coding": name.endswith("coder:7b")} if FakeClient.marks_coding else {"id": name}
                for name, _ in MODELS]

    def desktop_search(self, query):
        return FakeClient.desktop_page

    def context_tokens(self):
        return FakeClient.window

    def brain_tool(self, name, arguments):
        if FakeClient.brain_search is None:
            raise brain.ToolMissing("This Arty brain has no web search.")
        return FakeClient.brain_search


class Answered:
    def __enter__(self):
        return self

    def __exit__(self, *problem):
        return False

    def read(self):
        return b"{}"


def accepts(request, timeout=None):
    return Answered()


def refuses(request, timeout=None):
    raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, io.BytesIO(b"{}"))


@pytest.fixture
def world(tmp_path, monkeypatch):
    FakeClient.reachable, FakeClient.marks_coding, FakeClient.listing_fails = HOME, True, False
    FakeClient.desktop_page, FakeClient.brain_search = "results from the desktop", "results from the brain"
    FakeClient.window = 24_576
    config = tmp_path / "config" / "arty-cli" / "config.json"
    settings.save(settings.Settings(base_url=HOME, remote_url=AWAY, api_key=KEY, access_client_secret=ACCESS_SECRET,
                                    access_client_id="access-id"), config)
    return doctor.World(environ={"XDG_CONFIG_HOME": str(tmp_path / "config"), "APPDATA": str(tmp_path / "config")},
                        platform="linux", make_client=FakeClient, open_url=accepts,
                        which=lambda name: f"/usr/bin/{name}", find_browser=lambda: "/usr/bin/chromium",
                        sandbox_problem=lambda root: "", terminal_size=lambda: os.terminal_size((120, 40)),
                        is_terminal=lambda: True)


def report(world):
    out = io.StringIO()
    code = doctor.main([], out=out, world=world)
    return code, out.getvalue()


def line_about(shown: str, word: str) -> str:
    return next(line for line in shown.splitlines() if word in line and (word == "Settings" or "Settings" not in line))


def test_a_healthy_setup_gets_a_tick_on_every_line_and_exit_code_0(world):
    code, shown = report(world)
    lines = [line for line in shown.splitlines() if line.strip()]
    assert code == 0
    assert all(line.startswith(doctor.TICK) for line in lines[:-1]), shown
    assert len(lines) == 12
    assert "24576" in line_about(shown, "Context window")
    assert "private" in line_about(shown, "Settings")
    assert HOME in line_about(shown, "answered") and "home" in line_about(shown, "answered")
    assert "accepted" in line_about(shown, "key")
    assert "2 models" in shown and settings.DEFAULT_MODEL in shown
    assert "arty/coder:7b" in line_about(shown, "oding")
    assert "desktop" in line_about(shown, "search")
    assert "/usr/bin/chromium" in shown and "120x40" in shown and "colour on" in shown
    assert lines[-1] == doctor.ALL_GOOD


def test_no_secret_is_ever_printed(world):
    FakeClient.reachable = AWAY
    _, shown = report(world)
    assert KEY not in shown and ACCESS_SECRET not in shown and "s3cr3t" not in shown
    assert "from anywhere" in line_about(shown, "answered")


def test_a_settings_file_others_can_read_is_a_cross_with_the_fix(world):
    path = settings.config_path(world.environ)
    path.chmod(0o644)
    code, shown = report(world)
    assert code == 1
    assert line_about(shown, "Settings").startswith(doctor.CROSS)
    assert "chmod 600" in shown and "644" in shown


def test_without_a_brain_it_reports_and_does_not_crash(world):
    FakeClient.reachable = ""
    code, shown = report(world)
    assert code == 1
    assert line_about(shown, "Can't reach").startswith(doctor.CROSS)
    assert line_about(shown, "key").startswith(doctor.CROSS) and "not checked" in line_about(shown, "key")
    assert line_about(shown, "git").startswith(doctor.TICK)
    assert "needs" in shown.splitlines()[-1]


def test_not_logged_in_says_so_and_still_checks_this_computer(world):
    settings.config_path(world.environ).unlink()
    code, shown = report(world)
    assert code == 1
    assert "arty login" in shown
    assert "/usr/bin/chromium" in shown


def test_a_refused_key_is_a_cross_and_the_models_are_not_asked_for(world):
    world = replace(world, open_url=refuses)
    code, shown = report(world)
    assert code == 1
    assert line_about(shown, "key").startswith(doctor.CROSS) and "arty login" in line_about(shown, "key")
    assert "not checked" in line_about(shown, "odels")


def test_optional_things_missing_are_crosses_that_do_not_fail_the_run(world):
    FakeClient.marks_coding, FakeClient.desktop_page, FakeClient.brain_search = False, None, None
    world = replace(world, which=lambda name: None, find_browser=lambda: None,
                    sandbox_problem=lambda root: "bubblewrap (bwrap) isn't installed", is_terminal=lambda: False)
    code, shown = report(world)
    assert code == 0
    for word in ("oding", "search", "bubblewrap", "rowser", "git"):
        assert line_about(shown, word).startswith(doctor.CROSS), word
        assert doctor.OPTIONAL in line_about(shown, word)
    assert "colour off" in shown
    assert shown.splitlines()[-1] == doctor.ALL_GOOD


def test_search_through_the_brain_is_named_when_the_desktop_does_not_answer(world):
    FakeClient.desktop_page = None
    _, shown = report(world)
    assert "the brain" in line_about(shown, "search") and line_about(shown, "search").startswith(doctor.TICK)


def test_a_model_list_that_cannot_be_read_is_a_required_cross(world):
    FakeClient.listing_fails = True
    code, shown = report(world)
    assert code == 1 and line_about(shown, "odels").startswith(doctor.CROSS)


def test_settings_from_environment_variables_count_without_a_file(world):
    settings.config_path(world.environ).unlink()
    world = replace(world, environ={**world.environ, "ARTY_URL": HOME, "ARTY_API_KEY": KEY})
    code, shown = report(world)
    assert code == 0 and "environment" in line_about(shown, "Settings")
    assert KEY not in shown


def test_doctor_takes_no_arguments(world):
    out = io.StringIO()
    assert doctor.main(["--fix"], out=out, world=world) == 2
    assert "arty doctor" in out.getvalue()


def test_the_default_world_uses_the_real_computer():
    made = doctor.World()
    assert made.make_client is brain.Brain and made.platform and callable(made.which)
    assert isinstance(settings.config_path(made.environ), Path)


def test_a_context_window_below_16k_is_warned_about_without_failing_the_check(world):
    FakeClient.window = 12_288
    code, shown = report(world)
    line = line_about(shown, "Context window")
    assert code == 0 and line.startswith(doctor.CROSS) and "12288" in line and doctor.OPTIONAL in line


def test_a_brain_that_does_not_report_its_window_is_said_so(world):
    FakeClient.window = None
    _code, shown = report(world)
    assert "doesn't report" in line_about(shown, "Context window")
