import io
import json
import sys
from pathlib import Path

import pytest

from arty_cli import cli, context, permissions, rules, settings, tools
from tests.test_cli import FakeBrain, chat_with, started_workspaces

MAX_INSTRUCTION_TOKENS = 600


class Asker:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.questions = []

    def __call__(self, question, preview="", scope=""):
        self.questions.append(question)
        return self.answers.pop(0) if self.answers else "n"


@pytest.fixture
def workspace(tmp_path):
    found = tools.Workspace(tmp_path)
    found.rules = rules.Rules(rules.ON)
    return found


def test_the_instructions_cover_every_rule_and_stay_small(workspace):
    text = rules.instructions(workspace, "2026-11-12")
    for needed in ("callers", "smallest change", "make anything up", "no comments", "shebangs",
                   "suppressions", "license headers", "em dash", "destructive", "all questions at once",
                   "UNVERIFIED", ".arty/scratchpad.md", ".arty/history.md", "## 2026-11-12", "Outcome", "Changed",
                   "Verification", "Assumptions", "Open items", "project's notes"):
        assert needed in text, needed
    assert "\u2014" not in text
    tokens = context.estimated_tokens([{"role": "system", "content": text}], None)
    assert tokens <= MAX_INSTRUCTION_TOKENS


def test_tell_only_leaves_out_the_line_about_checks(workspace):
    assert rules.CHECKED_NOTE in rules.instructions(workspace, "2026-11-12")
    workspace.rules.level = rules.TELL_ONLY
    told = rules.instructions(workspace, "2026-11-12")
    assert told and rules.CHECKED_NOTE not in told


def test_no_instructions_when_off_or_never_set(tmp_path, workspace):
    workspace.rules.level = rules.OFF
    assert rules.instructions(workspace) == ""
    assert rules.instructions(tools.Workspace(tmp_path)) == ""


def write_settings(folder: Path, table: dict) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "settings.json").write_text(json.dumps(table))


@pytest.mark.parametrize("value, level", [(None, rules.ON), (True, rules.ON), ("on", rules.ON), (False, rules.OFF),
                                          ("off", rules.OFF), ("tell-only", rules.TELL_ONLY), ("bogus", rules.ON)])
def test_the_setting_values(tmp_path, value, level):
    personal = tmp_path / "personal"
    write_settings(personal, {} if value is None else {rules.SWITCH: value})
    assert rules.chosen(personal, tmp_path, include_project=False, asked=None) == level


def test_a_trusted_project_can_turn_the_rules_down_but_not_up(tmp_path):
    personal, project = tmp_path / "personal", tmp_path / "project"
    write_settings(project / ".arty", {rules.SWITCH: "tell-only"})
    assert rules.chosen(personal, project, include_project=True, asked=None) == rules.TELL_ONLY
    assert rules.chosen(personal, project, include_project=False, asked=None) == rules.ON
    write_settings(personal, {rules.SWITCH: False})
    assert rules.chosen(personal, project, include_project=True, asked=None) == rules.OFF


def test_the_command_line_choice_wins_for_that_run(tmp_path):
    personal = tmp_path / "personal"
    write_settings(personal, {rules.SWITCH: False})
    assert rules.chosen(personal, tmp_path, include_project=False, asked=rules.ON) == rules.ON


def test_the_scratchpad_and_history_are_written_without_asking(workspace):
    ask = Asker()
    gate = permissions.Gate(workspace, ask)
    assert gate.check("write_file", {"path": ".arty/scratchpad.md", "content": "# Task\n"}) is None
    assert gate.check("write_file", {"path": ".arty/history.md", "content": "## entry\n"}) is None
    assert gate.check("write_file", {"path": "./.arty/../.arty/history.md", "content": "## entry\n"}) is None
    assert ask.questions == []


def test_history_may_only_grow_at_the_end(workspace):
    history = workspace.root / ".arty" / "history.md"
    history.parent.mkdir()
    history.write_text("## one\nStatus: DONE\n")
    workspace.note_seen(history.resolve(), history.read_text())
    gate = permissions.Gate(workspace, Asker())
    appended = {"path": ".arty/history.md", "old_text": "Status: DONE\n", "new_text": "Status: DONE\n\n## two\n"}
    assert gate.check("edit_file", appended) is None
    rewritten = {"path": ".arty/history.md", "old_text": "## one", "new_text": "## first"}
    assert gate.check("edit_file", rewritten) == rules.APPEND_ONLY
    replaced = {"path": ".arty/history.md", "content": "## two\n", "overwrite": True}
    assert gate.check("write_file", replaced) == rules.APPEND_ONLY


def test_history_stays_append_only_even_when_nothing_is_asked(workspace):
    history = workspace.root / ".arty" / "history.md"
    history.parent.mkdir()
    history.write_text("## one\n")
    gate = permissions.Gate(workspace, Asker(), mode=permissions.ALLOW_ALL)
    assert gate.check("write_file", {"path": ".arty/history.md", "content": "", "overwrite": True}) == rules.APPEND_ONLY


@pytest.mark.parametrize("path", [".arty/settings.json", ".arty/mcp.json", ".arty/notes.md", ".arty/commands/x.md",
                                  "sub/.arty/history.md", ".arty/history.md.bak", ".ARTY/history.md"])
def test_everything_else_under_arty_is_still_asked_about(workspace, path):
    ask = Asker("n")
    gate = permissions.Gate(workspace, ask, mode=permissions.AUTO_EDIT)
    assert gate.check("write_file", {"path": path, "content": "{}"}) == permissions.REFUSED
    assert len(ask.questions) == 1


@pytest.mark.parametrize("level", [rules.OFF, rules.TELL_ONLY])
def test_with_the_rules_off_the_records_are_asked_about_too(workspace, level):
    workspace.rules.level = level
    ask = Asker("n")
    gate = permissions.Gate(workspace, ask, mode=permissions.AUTO_EDIT)
    refusal = gate.check("write_file", {"path": ".arty/scratchpad.md", "content": "x"})
    assert (refusal == permissions.REFUSED) is (level == rules.OFF)


def test_plan_mode_still_writes_nothing(workspace):
    gate = permissions.Gate(workspace, Asker(), mode=permissions.PLAN)
    assert gate.check("write_file", {"path": ".arty/scratchpad.md", "content": "x"}) == permissions.PLAN_REFUSAL


def test_a_deny_rule_still_wins(workspace):
    gate = permissions.Gate(workspace, Asker())
    gate.rules = permissions.Rules(allow=[], deny=["write_file(.arty/*)"])
    assert gate.check("write_file", {"path": ".arty/history.md", "content": "x"}) == permissions.RULE_DENIED


@pytest.mark.skipif(sys.platform == "win32", reason="symbolic links need extra rights on Windows")
@pytest.mark.parametrize("link", ["history.md", "scratchpad.md"])
def test_a_record_file_that_is_a_link_is_asked_about(workspace, link):
    folder = workspace.root / ".arty"
    folder.mkdir()
    (folder / "settings.json").write_text("{}")
    (folder / link).symlink_to(folder / "settings.json")
    ask = Asker("n")
    gate = permissions.Gate(workspace, ask, mode=permissions.AUTO_EDIT)
    assert gate.check("write_file", {"path": f".arty/{link}", "content": "x", "overwrite": True}) == permissions.REFUSED
    assert len(ask.questions) == 1


@pytest.mark.skipif(sys.platform == "win32", reason="symbolic links need extra rights on Windows")
def test_a_linked_arty_folder_is_asked_about(workspace, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (workspace.root / ".arty").symlink_to(elsewhere, target_is_directory=True)
    ask = Asker("n")
    gate = permissions.Gate(workspace, ask, mode=permissions.AUTO_EDIT)
    assert gate.check("write_file", {"path": ".arty/history.md", "content": "x"}) is not None


@pytest.fixture
def ready(monkeypatch, tmp_path):
    FakeBrain.made = []
    monkeypatch.setattr(cli.brain, "Brain", FakeBrain)
    monkeypatch.setattr(cli.settings, "load", lambda *a, **k: settings.Settings(base_url="http://b/v1", api_key="k"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "config"))
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    return project


def system_prompt_sent():
    return FakeBrain.made[0].calls[0][0]["content"]


def test_the_rules_reach_the_model_by_default(ready, monkeypatch):
    made = started_workspaces(monkeypatch)
    assert cli.main(["-p", "hello"]) == 0
    assert rules.HEADING in system_prompt_sent() and rules.CHECKED_NOTE in system_prompt_sent()
    assert made[0].rules.level == rules.ON


def test_the_project_notes_come_after_the_rules(ready, monkeypatch):
    (ready / "ARTY.md").write_text("Comments are welcome here.\n")
    monkeypatch.setattr(cli, "project_notes_allowed", lambda *a: True)
    cli.main(["-p", "hello"])
    prompt = system_prompt_sent()
    assert prompt.index(rules.HEADING) < prompt.index("Comments are welcome here.")


def test_the_rules_can_be_switched_off_in_your_settings(ready, monkeypatch):
    made = started_workspaces(monkeypatch)
    write_settings(cli.settings.config_dir(), {rules.SWITCH: False})
    cli.main(["-p", "hello"])
    assert rules.HEADING not in system_prompt_sent() and made[0].rules.level == rules.OFF


FLAG_LEVELS = [("off", False, False), ("tell-only", True, False), ("on", True, True)]


@pytest.mark.parametrize("level, told, checked", FLAG_LEVELS)
def test_the_rules_flag_sets_them_for_one_run(ready, monkeypatch, level, told, checked):
    made = started_workspaces(monkeypatch)
    cli.main(["--rules", level, "-p", "hello"])
    assert (rules.HEADING in system_prompt_sent()) is told
    assert made[0].rules.is_checked() is checked


def test_the_rules_command_shows_and_changes_them_for_the_session(ready, monkeypatch, capsys):
    chat_with(monkeypatch, "/rules", "/rules off", "hello", "/rules tell-only", "/rules sideways", "/exit")
    shown = capsys.readouterr().err
    assert "Rules: on" in shown and "Rules: off" in shown and "Rules: tell-only" in shown
    assert "sideways" in shown and "on, tell-only or off" in shown
    first_request = FakeBrain.made[0].calls[0][0]["content"]
    assert rules.HEADING not in first_request


def test_the_welcome_shows_the_rules_state(ready, monkeypatch, capsys):
    chat_with(monkeypatch, "/exit")
    shown = capsys.readouterr().err
    assert "Rules" in shown and rules.MEANINGS[rules.ON] in shown


def test_help_lists_the_rules_command(ready, monkeypatch, capsys):
    chat_with(monkeypatch, "/help", "/exit")
    assert "/rules" in capsys.readouterr().err


def test_a_once_request_is_not_held_up_when_nothing_changed(ready, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert cli.main(["-p", "what does this do?"]) == 0
    assert len(FakeBrain.made[0].calls) == 1
