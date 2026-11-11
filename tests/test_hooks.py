import json
import shlex
import sys

import pytest

from arty_cli import hooks, tools

PYTHON = shlex.quote(sys.executable)


def script(tmp_path, name, body):
    path = tmp_path / f"{name}.py"
    path.write_text(body)
    return f"{PYTHON} {shlex.quote(str(path))}"


def write_settings(folder, table):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / hooks.SETTINGS_FILE).write_text(json.dumps({"hooks": table}))


@pytest.fixture
def places(tmp_path):
    personal = tmp_path / "personal"
    project = tmp_path / "project"
    project.mkdir()
    return personal, tools.Workspace(project), tmp_path


def entry(command, matcher="", timeout=None):
    hook = {"type": "command", "command": command}
    if timeout:
        hook["timeout"] = timeout
    return {"matcher": matcher, "hooks": [hook]}


def test_a_pre_tool_hook_sees_the_call_as_json_and_exit_2_blocks_with_its_message(places):
    personal, workspace, tmp_path = places
    seen = tmp_path / "seen.json"
    command = script(tmp_path, "guard", "import json, sys\n"
                     f"data = json.load(sys.stdin)\nopen({str(seen)!r}, 'w').write(json.dumps(data))\n"
                     "sys.stderr.write('no edits to secrets')\nsys.exit(2)\n")
    write_settings(personal, {"PreToolUse": [entry(command, "edit_file|write_file")]})
    loaded, problems = hooks.load(personal, workspace, include_project=False)
    assert problems == []
    outcome = loaded.run("PreToolUse", {"tool_name": "edit_file", "tool_input": {"path": "a.py"}}, "edit_file")
    assert outcome.blocked and outcome.feedback == "no edits to secrets"
    sent = json.loads(seen.read_text())
    assert sent["hook_event_name"] == "PreToolUse" and sent["tool_input"] == {"path": "a.py"}
    assert sent["cwd"] == str(workspace.root) and sent["session_id"]


def test_the_matcher_picks_which_tools_a_hook_runs_for(places):
    personal, workspace, tmp_path = places
    command = script(tmp_path, "block", "import sys\nsys.exit(2)\n")
    write_settings(personal, {"PreToolUse": [entry(command, "edit_file")]})
    loaded, _ = hooks.load(personal, workspace, include_project=False)
    assert loaded.run("PreToolUse", {}, "edit_file").blocked
    assert not loaded.run("PreToolUse", {}, "edit_file_extra").blocked
    assert not loaded.run("PreToolUse", {}, "read_file").blocked


def test_an_empty_or_star_matcher_matches_every_tool(places):
    personal, workspace, tmp_path = places
    command = script(tmp_path, "block", "import sys\nsys.exit(2)\n")
    write_settings(personal, {"PreToolUse": [entry(command, "*")], "PostToolUse": [entry(command)]})
    loaded, _ = hooks.load(personal, workspace, include_project=False)
    assert loaded.run("PreToolUse", {}, "anything").blocked
    assert loaded.run("PostToolUse", {}, "read_file").blocked


def test_other_failures_and_timeouts_are_problems_not_blocks(places):
    personal, workspace, tmp_path = places
    failing = script(tmp_path, "fail", "import sys\nsys.stderr.write('oops')\nsys.exit(1)\n")
    slow = script(tmp_path, "slow", "import time\ntime.sleep(5)\n")
    write_settings(personal, {"PostToolUse": [entry(failing), entry(slow, timeout=1)]})
    loaded, _ = hooks.load(personal, workspace, include_project=False)
    outcome = loaded.run("PostToolUse", {}, "read_file")
    assert not outcome.blocked
    assert any("oops" in problem for problem in outcome.problems)
    assert any("1 s" in problem for problem in outcome.problems)


def test_a_prompt_hook_can_add_context_from_its_output(places):
    personal, workspace, tmp_path = places
    command = script(tmp_path, "context", "print('Today is a test day.')\n")
    write_settings(personal, {"UserPromptSubmit": [entry(command)]})
    loaded, _ = hooks.load(personal, workspace, include_project=False)
    assert loaded.run("UserPromptSubmit", {"prompt": "hi"}).context == "Today is a test day."


def test_project_hooks_load_only_when_the_folder_is_trusted(places):
    personal, workspace, tmp_path = places
    command = script(tmp_path, "block", "import sys\nsys.exit(2)\n")
    write_settings(workspace.root / hooks.PROJECT_FOLDER, {"PreToolUse": [entry(command)]})
    untrusted, _ = hooks.load(personal, workspace, include_project=False)
    trusted, _ = hooks.load(personal, workspace, include_project=True)
    assert not untrusted.run("PreToolUse", {}, "read_file").blocked
    assert trusted.run("PreToolUse", {}, "read_file").blocked
    assert [hook.source for hook in trusted.listed()] == ["this project"]


def test_broken_settings_are_reported_and_skipped(places):
    personal, workspace, _ = places
    personal.mkdir()
    (personal / hooks.SETTINGS_FILE).write_text("{not json")
    loaded, problems = hooks.load(personal, workspace, include_project=False)
    assert loaded.listed() == [] and "settings.json" in problems[0]
    write_settings(personal, {"PreToolUse": [{"matcher": "", "hooks": [{"type": "command"}]}], "Nonsense": []})
    loaded, problems = hooks.load(personal, workspace, include_project=False)
    assert loaded.listed() == [] and len(problems) == 2


def test_no_settings_means_no_hooks(places):
    personal, workspace, _ = places
    loaded, problems = hooks.load(personal, workspace, include_project=True)
    assert loaded.listed() == [] and problems == []
    assert not loaded.run("Stop", {}).blocked


def test_a_hook_that_times_out_is_killed_with_everything_it_started(places):
    import time
    personal, workspace, tmp_path = places
    marker = tmp_path / "still-running"
    command = f"(sleep 2; touch {shlex.quote(str(marker))}) & sleep 20"
    write_settings(personal, {"PostToolUse": [entry(command, timeout=1)]})
    loaded, _ = hooks.load(personal, workspace, include_project=False)
    began = time.monotonic()
    outcome = loaded.run("PostToolUse", {}, "read_file")
    assert time.monotonic() - began < 8
    assert any("1 s" in problem for problem in outcome.problems)
    time.sleep(3)
    assert not marker.exists()


def json_hook(places, event, output):
    personal, workspace, tmp_path = places
    command = script(tmp_path, "decide", f"import json\nprint(json.dumps({output!r}))\n")
    write_settings(personal, {event: [entry(command)]})
    loaded, _ = hooks.load(personal, workspace, include_project=False)
    return loaded


@pytest.mark.parametrize("decision", ["allow", "deny", "ask"])
def test_a_pre_tool_hook_can_decide_with_json(places, decision):
    loaded = json_hook(places, "PreToolUse", {"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": decision, "permissionDecisionReason": "policy says so",
        "additionalContext": "the repo is frozen on Fridays"}})
    outcome = loaded.run("PreToolUse", {}, "run_command")
    assert outcome.decision == decision
    assert outcome.blocked is (decision == "deny")
    assert outcome.feedback == "policy says so"
    assert outcome.context == "the repo is frozen on Fridays"


def test_a_json_block_decision_and_continue_false_both_block(places):
    blocked = json_hook(places, "PostToolUse", {"decision": "block", "reason": "lint failed"})
    outcome = blocked.run("PostToolUse", {}, "edit_file")
    assert outcome.blocked and outcome.feedback == "lint failed"
    stopped = json_hook(places, "UserPromptSubmit", {"continue": False, "stopReason": "out of hours"})
    outcome = stopped.run("UserPromptSubmit", {"prompt": "hi"})
    assert outcome.blocked and outcome.feedback == "out of hours"


def test_json_additional_context_for_a_prompt_is_used_and_plain_text_still_works(places):
    loaded = json_hook(places, "UserPromptSubmit", {"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit", "additionalContext": "branch is main"}})
    assert loaded.run("UserPromptSubmit", {"prompt": "hi"}).context == "branch is main"


@pytest.mark.parametrize("event", ["SessionStart", "PreCompact", "Notification", "SubagentStop"])
def test_the_newer_events_can_be_set_up(places, event):
    personal, workspace, _tmp_path = places
    write_settings(personal, {event: [entry("true")]})
    loaded, problems = hooks.load(personal, workspace, include_project=False)
    assert problems == []
    assert [hook.event for hook in loaded.listed()] == [event]


def test_a_session_start_matcher_picks_how_the_session_began(places):
    personal, workspace, tmp_path = places
    write_settings(personal, {"SessionStart": [entry(script(tmp_path, "hi", "print('resumed')"), "resume")]})
    loaded, _problems = hooks.load(personal, workspace, include_project=False)
    assert loaded.run("SessionStart", {"source": "resume"}, "resume").context == "resumed"
    assert loaded.run("SessionStart", {"source": "startup"}, "startup").context == ""
