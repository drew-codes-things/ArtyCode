from arty_cli import context, tools
from tests.scripted import (DREWS_WINDOW, SUMMARY_MARK, ScriptedBrain, copy_of_arty_cli_source, project,
                            session_for)

assert project


def test_reading_its_own_cli_on_a_24k_window_pages_instead_of_summarising_it_away(project):
    copy_of_arty_cli_source(project)
    brain = ScriptedBrain([[("read_file", {"path": "big.py"})], "Read it."])
    session = session_for(project, brain)
    assert session.ask("summarise big.py")
    assert SUMMARY_MARK not in brain.log
    seen = brain.tool_results(1)
    assert len(seen) == 1 and "read on with offset=" in seen[0]
    assert brain.largest_prompt() < context.compact_limit(brain)


def test_the_result_budget_is_about_a_quarter_of_the_room_before_a_summary(project):
    session = session_for(project, ScriptedBrain([]))
    session.ask("hello")
    budget = session.workspace.result_chars
    room = context.compact_limit(session.client, session.offered()) * context.CHARS_PER_TOKEN
    assert 0.2 * room <= budget <= 0.3 * room
    assert 12_000 <= budget <= 20_000


def test_a_smaller_window_gives_smaller_results(project):
    big = session_for(project, ScriptedBrain([], window=DREWS_WINDOW))
    small = session_for(project, ScriptedBrain([], window=8_192))
    big.ask("hello")
    small.ask("hello")
    assert small.workspace.result_chars < big.workspace.result_chars


def test_command_output_and_search_follow_the_budget(project):
    workspace = tools.Workspace(project)
    workspace.result_chars = 3_000
    (project / "many.txt").write_text("needle in a haystack line\n" * 2_000)
    found = tools.search(workspace, "needle")
    assert len(found) <= 3_200 and "stopped" in found.splitlines()[-1]
    shown = tools.read_file(workspace, "many.txt")
    assert len(shown) <= 3_200 and "offset=" in shown.splitlines()[-1]


def test_a_newest_result_too_big_for_the_window_is_cut_not_summarised(project, monkeypatch):
    monkeypatch.setattr(context, "RESULT_SHARE", 3.0)
    brain = ScriptedBrain([[("read_file", {"path": "big.py"})], "Read it."])
    copy_of_arty_cli_source(project)
    session = session_for(project, brain)
    session.ask("read big.py")
    assert SUMMARY_MARK not in brain.log
    seen = brain.tool_results(1)
    assert seen and "characters cut" in seen[0]
    assert brain.largest_prompt() < context.compact_limit(brain)
