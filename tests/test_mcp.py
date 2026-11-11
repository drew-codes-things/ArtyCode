import json
import sys
import time
from pathlib import Path

import pytest

from arty_cli import mcp, tools

FAKE_SERVER = str(Path(__file__).with_name("fake_mcp_server.py"))


def logged_soon(log, line, seconds=3.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if line in log.read_text():
            return True
        time.sleep(0.05)
    return False


def fake_config(name="fake", **env):
    return mcp.ServerConfig(name, sys.executable, [FAKE_SERVER], {key: str(value) for key, value in env.items()}, "yours")


@pytest.fixture
def hub(tmp_path):
    log = tmp_path / "server.log"
    started = mcp.Hub(tools.Workspace(tmp_path), call_seconds=2)
    problems = started.start([fake_config(FAKE_MCP_LOG=log, FAKE_MCP_SLOW=5)])
    yield started, problems, log
    started.close()


def test_a_server_is_started_and_its_tools_listed_across_pages(hub):
    started, problems, log = hub
    assert problems == []
    names = [definition["function"]["name"] for definition in started.definitions()]
    assert names == ["mcp__fake__echo", "mcp__fake__fail", "mcp__fake__slow", "mcp__fake__crash"]
    echo = started.definitions()[0]["function"]
    assert echo["description"] == "Say it back" and echo["parameters"]["properties"]["text"] == {"type": "string"}
    assert echo["parameters"]["type"] == "object"
    assert started.tool("mcp__fake__echo").read_only and not started.tool("mcp__fake__fail").read_only
    lines = log.read_text().splitlines()
    assert lines[:2] == ["request initialize", "notification notifications/initialized"]


def test_a_call_returns_its_text_and_answers_the_server_s_ping(hub):
    started, _problems, log = hub
    result = started.run("mcp__fake__echo", {"text": "hello"})
    assert result == "echo: hello\n[image/png content]"
    assert logged_soon(log, "client answered server-1 with {}")


def test_a_tool_error_is_reported_as_an_error(hub):
    started, _problems, _log = hub
    assert started.run("mcp__fake__fail", {}) == "Error: it broke"


def test_a_slow_call_times_out_with_a_plain_error(hub):
    started, _problems, _log = hub
    assert "didn't answer" in started.run("mcp__fake__slow", {})


def test_a_crashed_server_is_reported_and_its_tools_say_so(hub):
    started, _problems, _log = hub
    assert "stopped" in started.run("mcp__fake__crash", {})
    assert "stopped" in started.run("mcp__fake__echo", {"text": "x"})


def test_a_server_that_cannot_start_is_a_problem_not_a_crash(tmp_path):
    started = mcp.Hub(tools.Workspace(tmp_path), start_seconds=2)
    problems = started.start([mcp.ServerConfig("ghost", "definitely-not-a-program-arty", [], {}, "yours")])
    assert started.definitions() == [] and "ghost" in problems[0]
    started.close()


def test_names_are_made_safe_and_short():
    assert mcp.exposed_name("my server!", "do.thing") == "mcp__my_server___do_thing"
    assert len(mcp.exposed_name("s" * 50, "t" * 50)) == mcp.MAX_TOOL_NAME


def write(path, table):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(table))


def test_configs_come_from_personal_settings_and_trusted_project_mcp_json(tmp_path):
    personal = tmp_path / "personal"
    workspace = tools.Workspace(tmp_path / "project")
    (tmp_path / "project").mkdir()
    write(personal / "settings.json", {"mcpServers": {"notes": {"command": "notes-mcp", "args": ["--fast"]}}})
    write(tmp_path / "project" / ".arty" / "mcp.json", {"mcpServers": {"db": {"command": "db-mcp", "env": {"A": "1"}}}})
    untrusted, problems = mcp.configs(personal, workspace, include_project=False)
    trusted, _ = mcp.configs(personal, workspace, include_project=True)
    assert problems == []
    assert [(c.name, c.command, c.args, c.source) for c in untrusted] == [("notes", "notes-mcp", ["--fast"], "yours")]
    assert [(c.name, c.env, c.source) for c in trusted[1:]] == [("db", {"A": "1"}, "this project")]


def test_bad_server_entries_are_reported(tmp_path):
    personal = tmp_path / "personal"
    write(personal / "settings.json", {"mcpServers": {"broken": {"args": []}, "remote": {"url": "https://x"}}})
    found, problems = mcp.configs(personal, tools.Workspace(tmp_path), include_project=False)
    assert found == [] and len(problems) == 2
    assert any("only local" in problem for problem in problems)


def test_a_bare_program_name_is_never_found_in_the_project_or_the_current_folder(tmp_path, monkeypatch):
    project = tmp_path / "project"
    planted = project / "bin"
    planted.mkdir(parents=True)
    fake = planted / "helper-tool"
    fake.write_text("#!/bin/sh\necho planted\n")
    fake.chmod(0o755)
    real_folder = tmp_path / "real"
    real_folder.mkdir()
    real = real_folder / "helper-tool"
    real.write_text("#!/bin/sh\necho real\n")
    real.chmod(0o755)
    monkeypatch.chdir(planted)
    monkeypatch.setenv("PATH", f".:{planted}:{real_folder}")
    assert mcp.program_path("helper-tool", tools.Workspace(project)) == str(real)
    monkeypatch.setenv("PATH", f".:{planted}")
    assert mcp.program_path("helper-tool", tools.Workspace(project)) is None
