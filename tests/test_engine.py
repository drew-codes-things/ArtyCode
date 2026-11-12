import contextlib

from arty_cli import agent, cli, context, prune, tools
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


def test_argument_mistakes_small_models_make_are_forgiven(project):
    (project / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    edits = '[{"old_string": "return a - b", "new_string": "return a + b"}]'
    todos = '[{"content": "Fix add", "status": "completed"}, {"content": "Test it", "status": "in-progress"}]'
    brain = ScriptedBrain([
        [("read_file", {"file_path": "calc.py"})],
        [("multi_edit", {"file_path": "calc.py", "edits": edits})],
        [("todo_write", {"todos": todos})],
        [("run_command", {"cmd": "echo checked"})],
        "Fixed.",
    ])
    session = session_for(project, brain)
    session.ask("fix add in calc.py")
    results = [message["content"] for message in brain.prompts[-1] if message["role"] == "tool"]
    assert len(results) == 4 and not [result for result in results if result.startswith("Error")]
    assert (project / "calc.py").read_text() == "def add(a, b):\n    return a + b\n"
    assert session.workspace.todos == [{"text": "Fix add", "status": "done"},
                                       {"text": "Test it", "status": "in_progress"}]


def test_an_unknown_argument_name_is_answered_with_the_right_names(project):
    (project / "a.txt").write_text("hi\n")
    brain = ScriptedBrain([[("read_file", {"path": "a.txt", "lines": 5})], "Done."])
    session_for(project, brain).ask("read a.txt")
    result = brain.tool_results()[0]
    assert result.startswith("Error") and "lines" in result
    assert "path" in result and "offset" in result and "limit" in result


def test_a_file_attached_with_at_can_be_edited_without_reading_it_again(project):
    (project / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    brain = ScriptedBrain([[("edit_file", {"path": "calc.py", "old_text": "a - b", "new_text": "a + b"})], "Fixed."])
    session_for(project, brain).ask("fix @calc.py")
    assert brain.tool_results()[0].startswith("Edited calc.py")
    assert "1\tdef add(a, b):" in brain.prompts[0][-1]["content"]


def test_a_fail_read_fail_read_cycle_is_caught_and_the_model_is_told_to_rewrite(project):
    (project / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    wrong = ("edit_file", {"path": "calc.py", "old_text": "return a-b", "new_text": "return a + b"})
    looked = ("read_file", {"path": "calc.py"})
    brain = ScriptedBrain([[looked], [wrong], [looked], [wrong], [looked], [wrong], [looked], [wrong], [looked],
                           [wrong], [looked], [wrong]])
    session = session_for(project, brain)
    session.ask("fix add")
    misses = [result for prompt in brain.prompts for result in
              [m["content"] for m in prompt if m["role"] == "tool" and m["content"].startswith("Error")]]
    assert any("write_file" in result and "overwrite" in result for result in misses)
    told = [m["content"] for m in brain.prompts[-1] if m["role"] == "user"]
    assert any(text.startswith("You have made the same tool calls") for text in told)
    assert brain.log.count("edit_file") <= 4
    assert session.messages[-1]["content"] == agent.STUCK_STOPPED


def test_an_explore_helper_reading_a_big_file_page_after_page_stays_inside_the_window(project):
    copy_of_arty_cli_source(project)
    pages = [[("read_file", {"path": "big.py", "offset": offset})] for offset in (1, 401, 801, 1201, 1)]
    brain = ScriptedBrain([[("explore", {"task": "how does big.py compact?"})], *pages, "It summarises.", "Done."])
    session_for(project, brain).ask("how does big.py compact? use explore")
    helper_prompts = [prompt for prompt in brain.prompts if prompt[0]["content"] == agent.EXPLORER_PROMPT]
    assert len(helper_prompts) == 6
    limit = context.compact_limit(brain, agent.explorer_definitions())
    assert max(context.estimated_tokens(prompt, brain) for prompt in helper_prompts) <= limit
    assert brain.tool_results()[-1].startswith("It summarises.")


def test_a_long_job_is_pruned_without_a_summary_call_when_that_makes_room(project):
    copy_of_arty_cli_source(project)
    pages = [[("read_file", {"path": "big.py", "offset": offset})] for offset in (1, 401, 801, 1201, 1)]
    brain = ScriptedBrain([*pages, "Read it all."])
    session = session_for(project, brain)
    session.ask("read all of big.py")
    assert SUMMARY_MARK not in brain.log
    assert brain.largest_prompt() <= context.compact_limit(brain, session.offered())
    assert any(result.startswith(prune.STUB_LEAD) for result in brain.tool_results())


def test_commands_are_recorded_with_what_they_showed(project):
    brain = ScriptedBrain([[("run_command", {"command": "echo 3 passed"})], "Done."])
    session = session_for(project, brain)
    session.ask("run it")
    assert session.done == ["run_command echo 3 passed: 3 passed (exit code 0)"]


def forced_summary(project, monkeypatch):
    copy_of_arty_cli_source(project, "big.py")
    copy_of_arty_cli_source(project, "big2.py")
    monkeypatch.setattr(cli.Session, "compact_limit", lambda self: 6_000)
    monkeypatch.setattr(cli.Session, "refresh_budget", lambda self: setattr(self.workspace, "result_chars", 60_000))
    monkeypatch.setattr(cli.prune, "prune", lambda *given: None)
    brain = ScriptedBrain([
        [("write_file", {"path": "calc.py", "content": "def add(a, b):\n    return a + b\n"})],
        [("run_command", {"command": "echo 1 failed, 2 passed; exit 1"})],
        [("read_file", {"path": "big.py"})],
        [("read_file", {"path": "big2.py"})],
        "Done.",
    ], summary="Made calc.py; tests run.")
    session = session_for(project, brain)
    session.ask("make calc.py")
    carried = next(prompt[1]["content"] for prompt in brain.prompts
                   if len(prompt) > 1 and str(prompt[1]["content"]).startswith(cli.COMPACTED_LEAD))
    return session, brain, carried


def test_after_a_summary_the_last_check_output_and_the_last_edited_file_are_carried(project, monkeypatch):
    session, brain, carried = forced_summary(project, monkeypatch)
    assert SUMMARY_MARK in brain.log
    assert "Made calc.py; tests run." in carried
    assert "run_command echo 1 failed, 2 passed; exit 1: 1 failed, 2 passed (exit code 1)" in carried
    assert "1 failed, 2 passed\n\n[exit code 1]" in carried
    assert "calc.py" in carried and "1\tdef add(a, b):\n2\t    return a + b" in carried


def test_after_a_summary_only_the_carried_file_counts_as_read(project, monkeypatch):
    session, _brain, _carried = forced_summary(project, monkeypatch)
    assert set(session.workspace.seen) == {(project / "calc.py").resolve()}


class Interrupting:
    def __call__(self, messages):
        raise KeyboardInterrupt


def test_stopping_keeps_every_finished_round_and_drops_only_the_unfinished_one(project):
    (project / "a.txt").write_text("alpha\n")
    brain = ScriptedBrain([[("read_file", {"path": "a.txt"})], [("run_command", {"command": "echo hi"})],
                           Interrupting()])
    session = session_for(project, brain)
    assert session.ask("look around") is False
    roles = [message["role"] for message in session.messages[1:]]
    assert roles == ["user", "assistant", "tool", "assistant", "tool", "assistant"]
    assert session.messages[-1]["content"].startswith(cli.STOPPED)
    assert "alpha" in session.messages[3]["content"]


def test_stopping_in_the_middle_of_a_round_drops_its_calls_but_keeps_the_rounds_before(project, monkeypatch):
    (project / "a.txt").write_text("alpha\n")
    brain = ScriptedBrain([[("read_file", {"path": "a.txt"})], [("run_command", {"command": "echo hi"})], "x"])
    session = session_for(project, brain)
    real_run = tools.run

    def stopped_at_the_command(name, arguments, workspace):
        if name == "run_command":
            raise KeyboardInterrupt
        return real_run(name, arguments, workspace)

    monkeypatch.setattr(tools, "run", stopped_at_the_command)
    session.ask("look around")
    roles = [message["role"] for message in session.messages[1:]]
    assert roles == ["user", "assistant", "tool", "assistant"]
    assert "run_command echo hi (stopped part way)" in session.messages[-1]["content"]


class QueuingConsole:
    def __init__(self, *texts):
        self.texts = list(texts)

    @contextlib.contextmanager
    def showing(self, label, started_at, tokens):
        yield self

    def take_queued(self, wanted):
        taken = [text for text in self.texts if wanted(text)]
        self.texts = [text for text in self.texts if not wanted(text)]
        return taken


def test_a_message_queued_while_arty_works_reaches_the_model_at_the_next_step(project):
    (project / "a.txt").write_text("alpha\n")
    brain = ScriptedBrain([[("read_file", {"path": "a.txt"})], "Done."])
    session = session_for(project, brain)
    session.terminal.console = QueuingConsole("also check b.txt", "/status")
    session.ask("read a.txt")
    sent = [message["content"] for message in brain.prompts[1] if message["role"] == "user"]
    assert sent[0] == "read a.txt" and sent[-1].endswith("also check b.txt")
    assert brain.prompts[1][-2]["role"] == "tool"
    assert session.terminal.console.texts == ["/status"]
    assert "also check b.txt" in session.request
