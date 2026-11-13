import json
import os
import sys

import pytest

from arty_cli import agent, cli, conversation, finish, permissions, prune, tools
from tests.portable import plain_python_call, short_python_call
from tests.scripted import ScriptedBrain, project, session_for

assert project

FIXTURE_CALC = '''class CalculatorError(ValueError):
    pass


def evaluate(source: str) -> int:
    tokens = source.split()
    if not tokens:
        raise CalculatorError("empty expression")
    return int(tokens[0])
'''


def shown_on_screen(session) -> str:
    return session.terminal.out.getvalue() + session.terminal.err.getvalue()


def test_the_stuck_stop_is_shown_to_the_user_not_only_kept_in_the_conversation(project):
    (project / "calc.py").write_text(FIXTURE_CALC)
    same = [("read_file", {"path": "calc.py"})]
    brain = ScriptedBrain([same] * (agent.STUCK_REPEATS + 1))
    session = session_for(project, brain)
    session.ask("fix calc.py")
    assert session.messages[-1]["content"] == agent.STUCK_STOPPED
    assert agent.STUCK_STOPPED in shown_on_screen(session)


STRIPPED_CALC = FIXTURE_CALC.replace("source.split()", "_tokenize(source.strip())")
FLAPPING_EDIT = ("edit_file", {"path": "calc.py",
                               "old_text": "    tokens = _tokenize(source)\n    if not tokens:",
                               "new_text": "    tokens = _tokenize(source.strip())\n    if not tokens:"})
REWRITE_WITHOUT_OVERWRITE = ("write_file", {"path": "calc.py", "content": STRIPPED_CALC})


def test_a_model_that_obeys_the_failing_call_note_is_not_stopped_on_the_next_step(project):
    (project / "calc.py").write_text(STRIPPED_CALC)
    brain = ScriptedBrain([[("read_file", {"path": "calc.py"})], [FLAPPING_EDIT], [FLAPPING_EDIT], [FLAPPING_EDIT],
                           [REWRITE_WITHOUT_OVERWRITE], [("run_command", {"command": "echo checked"})],
                           "The change was already there; calc.py is unchanged."])
    session = session_for(project, brain)
    session.ask("make evaluate strip the input")
    told = [m["content"] for m in brain.prompts[-1] if m["role"] == "user"]
    assert agent.STUCK_FAILING_NOTE in told
    assert agent.STUCK_STOPPED not in [m.get("content") for m in session.messages]
    assert session.messages[-1]["content"] == "The change was already there; calc.py is unchanged."


def test_the_same_failing_call_after_the_note_still_stops_the_request(project):
    (project / "calc.py").write_text(STRIPPED_CALC)
    brain = ScriptedBrain([[("read_file", {"path": "calc.py"})], [FLAPPING_EDIT], [FLAPPING_EDIT], [FLAPPING_EDIT],
                           [REWRITE_WITHOUT_OVERWRITE], [FLAPPING_EDIT], "never reached"])
    session = session_for(project, brain)
    session.ask("make evaluate strip the input")
    assert session.messages[-1]["content"] == agent.STUCK_STOPPED
    assert brain.log.count("edit_file") == 4


PARSE_ATOM = '''    def parse_atom(self):
        token = self.peek()
        if token is None:
            raise CalculatorError("expected a number")
        self.pos += 1
        return token
'''
COPIED_PLACEHOLDER = ("    def parse_atom(self):\n        token = self.peek()\n        if token is None:\n"
                      "            raise CalculatorError(\n                f\"expected a closing ')', got {f... "
                      "(37 characters dropped to save room)\"\n            )")


def test_an_edit_that_copies_the_dropped_text_placeholder_is_refused_and_the_file_kept(project):
    (project / "calc.py").write_text(PARSE_ATOM)
    copied = ("edit_file", {"path": "calc.py", "old_text": PARSE_ATOM.split("\n        self.pos")[0],
                            "new_text": COPIED_PLACEHOLDER})
    brain = ScriptedBrain([[("read_file", {"path": "calc.py"})], [copied], "Gave up."])
    session = session_for(project, brain)
    session.ask("make parse_atom report the closing bracket")
    refused = brain.tool_results()[-1]
    assert refused.startswith("Error") and "characters dropped to save room" in refused
    assert (project / "calc.py").read_text() == PARSE_ATOM


def test_a_write_that_copies_a_hidden_result_or_a_cut_marker_is_refused(project):
    workspace = tools.Workspace(project)
    for placeholder in (f"{prune.STUB_LEAD}: read_file calc.py (9 lines). Do it again if you need it.]",
                        "x = 1\n... 4000 characters cut ...\ny = 2"):
        found = tools.run("write_file", {"path": "new.py", "content": placeholder}, workspace)
        assert found.startswith("Error"), found
        assert not (project / "new.py").exists()


def test_a_file_that_already_holds_the_marker_text_can_still_be_edited(project):
    holding = "NOTE = '(12 characters dropped to save room)'\nvalue = 1\n"
    (project / "notes.py").write_text(holding)
    workspace = tools.Workspace(project)
    tools.read_file(workspace, "notes.py")
    found = tools.run("edit_file", {"path": "notes.py", "old_text": "value = 1", "new_text": "value = 2"}, workspace)
    assert found.startswith("Edited"), found


def test_past_arguments_are_shortened_once_with_the_real_count(project):
    content = "x" * 5_000
    call = {"id": "c1", "type": "function",
            "function": {"name": "write_file", "arguments": json.dumps({"path": "a.py", "content": content})}}
    once = prune.with_short_arguments(call)
    twice = prune.with_short_arguments(once)
    assert once == twice
    assert "4800 characters dropped" in json.loads(twice["function"]["arguments"])["content"]


def miss_results(brain) -> list[str]:
    return [result for result in brain.tool_results() if result.startswith("Error") and tools.NOT_FOUND in result]


def test_a_miss_whose_new_text_is_already_in_the_file_says_the_change_is_already_made(project):
    (project / "calc.py").write_text(STRIPPED_CALC)
    brain = ScriptedBrain([[("read_file", {"path": "calc.py"})], [FLAPPING_EDIT], "Already done."])
    session_for(project, brain).ask("make evaluate strip the input")
    missed = miss_results(brain)[-1]
    assert tools.ALREADY_MADE_LEAD in missed
    assert "lines 6-7" in missed and "6\t    tokens = _tokenize(source.strip())" in missed


def test_after_a_miss_the_file_counts_as_read_so_the_corrected_edit_applies(project):
    (project / "calc.py").write_text(FIXTURE_CALC)
    guessed = ("edit_file", {"path": "calc.py", "old_text": "    tokens = source.split(' ')",
                             "new_text": "    tokens = source.strip().split()"})
    copied = ("edit_file", {"path": "calc.py", "old_text": "    tokens = source.split()",
                            "new_text": "    tokens = source.strip().split()"})
    brain = ScriptedBrain([[guessed], [copied], "Done."])
    session_for(project, brain).ask("make evaluate strip the input")
    assert brain.tool_results()[-1].startswith("Edited calc.py"), brain.tool_results()[-1]
    assert "source.strip().split()" in (project / "calc.py").read_text()


def test_the_second_miss_on_a_file_carries_the_whole_file_and_a_direct_instruction(project):
    (project / "calc.py").write_text(FIXTURE_CALC)
    first = ("edit_file", {"path": "calc.py", "old_text": "    tokens = source.split(' ')", "new_text": "x"})
    second = ("edit_file", {"path": "calc.py", "old_text": "    return int(tokens[1])", "new_text": "y"})
    brain = ScriptedBrain([[("read_file", {"path": "calc.py"})], [first], [second], "Stuck."])
    session_for(project, brain).ask("fix evaluate")
    once, twice = miss_results(brain)
    whole = [f"{number}\t{line}" for number, line in enumerate(FIXTURE_CALC.splitlines(), start=1)]
    assert agent.WHOLE_FILE_LEAD.format(path="calc.py", lines=len(whole)) not in once
    assert agent.WHOLE_FILE_LEAD.format(path="calc.py", lines=len(whole)) in twice
    assert all(line in twice for line in whole)
    assert "miss 2 on calc.py" in twice and "copy old_text from" in twice


def test_the_second_miss_on_a_big_file_carries_the_function_around_the_closest_match(project):
    filler = [f"def helper_{number}(value):\n    return value + {number}\n" for number in range(300)]
    target = "def evaluate(source):\n    tokens = source.split()\n    total = 0\n    for token in tokens:\n" \
             "        total += int(token)\n    return total\n"
    (project / "calc.py").write_text("\n".join(filler[:150] + [target] + filler[150:]))
    first = ("edit_file", {"path": "calc.py", "old_text": "    for token in token_list:", "new_text": "x"})
    second = ("edit_file", {"path": "calc.py", "old_text": "        total += int(tokens)", "new_text": "y"})
    brain = ScriptedBrain([[("read_file", {"path": "calc.py", "limit": 20})], [first], [second], "Stuck."])
    session = session_for(project, brain)
    session.ask("fix evaluate")
    twice = miss_results(brain)[-1]
    assert "def evaluate(source):" in twice and "        total += int(token)" in twice
    assert "def helper_0(value)" not in twice
    assert len(twice) <= session.workspace.result_chars


CALC_LINES = 150
PYTEST_LIKE = ("import sys\n"
               "for number in range(500):\n"
               "    print(f'E       calc.CalculatorError: expected a number, got token {number} in test_evaluate')\n"
               "print('FAILED test_calc.py::test_evaluate_complex_expression - calc.CalculatorError')\n"
               "print('======================== 15 failed, 15 passed in 0.09s =========================')\n"
               "sys.exit(1)\n")


def calc_of(lines: int = CALC_LINES) -> str:
    return "".join(f"def step_{number}(value):\n    return value + {number}\n\n" for number in range(lines // 3))


def edit_of(number: int) -> tuple:
    return ("edit_file", {"path": "calc.py", "old_text": f"    return value + {number}\n",
                          "new_text": f"    return value + {number} * 2\n"})


def calc_view_in(prompt: list[dict], text: str) -> bool:
    numbered = [f"{number}\t{line}" for number, line in enumerate(text.splitlines(), start=1) if line]
    return any(not str(message.get("content") or "").startswith(prune.STUB_LEAD)
               and all(line in str(message.get("content") or "") for line in numbered)
               for message in prompt if message["role"] in ("tool", "user"))


def recording_file(path, seen: list[str], step):
    def recorded(_prompt):
        seen.append(path.read_text())
        return step
    return recorded


def test_after_older_results_are_hidden_the_model_still_sees_the_file_it_is_editing(project):
    calc = project / "calc.py"
    calc.write_text(calc_of())
    (project / "fake_pytest.py").write_text(PYTEST_LIKE)
    tests = ("run_command", {"command": plain_python_call("fake_pytest.py")})
    steps = [[("read_file", {"path": "calc.py"})], [tests]]
    for number in range(1, 15):
        steps += [[edit_of(number)], [edit_of(number + 20)], [tests]]
    seen = []
    brain = ScriptedBrain([recording_file(calc, seen, step) for step in steps + ["Done."]])
    session = session_for(project, brain)
    session.ask("fix calc.py so the tests pass")
    assert conversation.PRUNED_NOTE in session.terminal.err.getvalue()
    stubs = [sum(str(m.get("content") or "").startswith(prune.STUB_LEAD) for m in prompt) for prompt in brain.prompts]
    pruned = [index for index in range(1, len(stubs)) if stubs[index] > stubs[index - 1]]
    assert pruned
    blind = [index for index in pruned if not calc_view_in(brain.prompts[index], seen[index])]
    assert not blind, f"{len(blind)} of {len(pruned)} prompts after a prune had no current view of calc.py"


def test_the_footer_shows_real_room_right_after_older_results_are_dropped(project, monkeypatch):
    calc = project / "calc.py"
    calc.write_text(calc_of())
    (project / "fake_pytest.py").write_text(PYTEST_LIKE)
    launchers = project.parent / "launchers"
    monkeypatch.setenv("PATH", str(launchers) + os.pathsep + os.environ.get("PATH", ""))
    tests = ("run_command", {"command": short_python_call(launchers, "fake_pytest.py")})
    steps = [[("read_file", {"path": "calc.py"})], [tests]]
    for number in range(1, 15):
        steps += [[edit_of(number)], [edit_of(number + 20)], [tests]]
    session = session_for(project, ScriptedBrain(steps + ["Done."]))
    shown = []
    session.terminal.measure = lambda: shown.append(
        (session.terminal.err.getvalue().count(conversation.PRUNED_NOTE), cli.context_left(session)))
    session.ask("fix calc.py so the tests pass")
    notes = session.terminal.err.getvalue().count(conversation.PRUNED_NOTE)
    assert notes
    right_after = [next(left for count, left in shown if count >= note) for note in range(1, notes + 1)]
    assert min(right_after) >= 40, right_after


SMALL_CALC = '''def add(a, b):
    return a + b


def multiply(a, b):
    return a + b


def subtract(a, b):
    return a - b
'''
SMALL_TESTS = '''from calc import add, multiply, subtract


def test_add():
    assert add(2, 3) == 5


def test_multiply():
    assert multiply(2, 3) == 6


def test_subtract():
    assert subtract(5, 3) == 2


def test_add_negative():
    assert add(-1, -1) == -2
'''
RUN_TESTS = ("run_command", {"command": f"{sys.executable} -B -m pytest -q -p no:cacheprovider"})
BREAKING_EDIT = ("edit_file", {"path": "calc.py", "old_text": "def add(a, b):\n    return a + b",
                               "new_text": "def add(a, b):\n    return a * b"})


class ToolRecordingBrain(ScriptedBrain):
    def __init__(self, steps):
        super().__init__(steps)
        self.offered = []

    def reply(self, messages, tools, on_text):
        self.offered.append([definition["function"]["name"] for definition in tools or []])
        return super().reply(messages, tools, on_text)


def session_with_finish_check(project, brain):
    (project / "calc.py").write_text(SMALL_CALC)
    (project / "test_calc.py").write_text(SMALL_TESTS)
    session = session_for(project, brain)
    session.workspace.finish_check = finish.FinishCheck()
    return session


def test_edits_that_make_the_tests_worse_are_named_plainly_with_the_newly_failing_tests(project):
    brain = ToolRecordingBrain([[("read_file", {"path": "calc.py"})], [RUN_TESTS], [BREAKING_EDIT], [RUN_TESTS],
                                "I made add worse."])
    session = session_with_finish_check(project, brain)
    session.ask("fix multiply")
    assert finish.WORSE_LEAD not in brain.tool_results()[1]
    worse = brain.tool_results()[-1]
    assert finish.WORSE_LEAD in worse
    assert "1 failed, 3 passed" in worse and "3 failed, 1 passed" in worse
    assert "test_calc.py::test_add" in worse and "test_calc.py::test_add_negative" in worse
    assert "test_calc.py::test_multiply" not in worse.split(finish.NEWLY_FAILING_LEAD)[1]
    assert finish.UNDO_TOOL in worse
    assert finish.UNDO_TOOL not in brain.offered[0] and finish.UNDO_TOOL in brain.offered[-1]
    assert "made the tests worse" in shown_on_screen(session)
    assert "return a * b" in (project / "calc.py").read_text()


def test_undo_edits_puts_the_files_back_to_the_better_run_only_when_the_model_calls_it(project):
    second_break = ("edit_file", {"path": "calc.py", "old_text": "    return a - b", "new_text": "    return a + a"})
    brain = ToolRecordingBrain([[("read_file", {"path": "calc.py"})], [RUN_TESTS], [BREAKING_EDIT], [second_break],
                                [RUN_TESTS], [(finish.UNDO_TOOL, {})], [RUN_TESTS], "Back where it was."])
    session = session_with_finish_check(project, brain)
    session.ask("fix multiply")
    undone, rerun = brain.tool_results()[-2:]
    assert undone.startswith("Undid") and "2 changes" in undone
    assert (project / "calc.py").read_text() == SMALL_CALC
    assert "1 failed, 3 passed" in rerun and finish.WORSE_LEAD not in rerun


def test_a_test_file_that_no_longer_imports_counts_as_worse_not_as_fewer_failures(project):
    broken = ("edit_file", {"path": "calc.py", "old_text": "def subtract(a, b):", "new_text": "def subtract(a, b)"})
    brain = ScriptedBrain([[("read_file", {"path": "calc.py"})], [RUN_TESTS], [broken], [RUN_TESTS], "Broke it."])
    session = session_with_finish_check(project, brain)
    session.ask("fix multiply")
    assert finish.WORSE_LEAD in brain.tool_results()[-1]


def test_test_runs_with_no_file_change_between_them_are_not_called_worse(project):
    brain = ScriptedBrain([[RUN_TESTS], [RUN_TESTS], "Same."])
    session = session_with_finish_check(project, brain)
    session.ask("run the tests twice")
    assert not [result for result in brain.tool_results() if finish.WORSE_LEAD in result]


def test_a_missed_deletion_is_not_reported_as_already_made(project):
    (project / "calc.py").write_text("x = 1\ny = 2\n")
    workspace = tools.Workspace(project)
    tools.read_file(workspace, "calc.py")
    found = tools.run("edit_file", {"path": "calc.py", "old_text": "x = 10\ny = 2", "new_text": "y = 2"}, workspace)
    assert found.startswith("Error") and tools.ALREADY_MADE_LEAD not in found


def test_an_edit_whose_new_text_equals_old_text_says_nothing_changed(project):
    (project / "calc.py").write_text(PARSE_ATOM)
    workspace = tools.Workspace(project)
    tools.read_file(workspace, "calc.py")
    same = PARSE_ATOM.split("\n        self.pos")[0]
    found = tools.run("edit_file", {"path": "calc.py", "old_text": same, "new_text": same}, workspace)
    assert found.startswith("Error") and tools.NOTHING_CHANGED in found
    many = tools.run("multi_edit", {"path": "calc.py", "edits": [{"old_text": same, "new_text": same}]}, workspace)
    assert many.startswith("Error") and tools.NOTHING_CHANGED in many
    assert not workspace.checkpoints


SECRET_LINE = "token = FAKE-TOKEN-0123456789-NOT-REAL"


class QuietScreen:
    def show_text(self, text):
        pass

    def show_activity(self, line):
        pass


class Asker:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.questions = []

    def __call__(self, question, preview="", scope=""):
        self.questions.append(question)
        return self.answers.pop(0) if self.answers else permissions.NO


def results_of_two_misses(gate, path: str) -> list[str]:
    brain = ScriptedBrain([[("edit_file", {"path": path, "old_text": "nope one", "new_text": "x"})],
                           [("edit_file", {"path": path, "old_text": "nope two", "new_text": "x"})], "Done."])
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "go"}]
    agent.run_turn(messages, brain, gate.workspace, gate, QuietScreen(), stop_hooks=False)
    return [message["content"] for message in messages if message.get("role") == "tool"]


@pytest.fixture
def outside_secret(project):
    secret = project.parent / "outside" / "secret.conf"
    secret.parent.mkdir()
    secret.write_text(f"[account]\nuser = demo\n{SECRET_LINE}\n")
    return secret


@pytest.mark.parametrize("mode", [permissions.DEFAULT, permissions.AUTO_EDIT])
@pytest.mark.parametrize("answer", [permissions.NO, permissions.YES])
def test_missed_edits_never_show_a_file_outside_the_project_that_was_not_read(project, outside_secret, mode, answer):
    ask = Asker(answer, answer)
    gate = permissions.Gate(tools.Workspace(project), ask, mode)
    results = results_of_two_misses(gate, str(outside_secret))
    assert ask.questions and all("outside the project" in question for question in ask.questions)
    assert not any("FAKE-TOKEN" in result or "user = demo" in result for result in results), results


def test_missed_edits_show_a_file_outside_the_project_once_reading_it_was_allowed(project, outside_secret):
    ask = Asker(permissions.YES, permissions.YES, permissions.YES)
    gate = permissions.Gate(tools.Workspace(project), ask, permissions.DEFAULT)
    assert gate.check("read_file", {"path": str(outside_secret)}) is None
    gate.workspace.note_seen(outside_secret, outside_secret.read_text())
    assert SECRET_LINE in results_of_two_misses(gate, str(outside_secret))[-1]


@pytest.mark.parametrize("deny", [["read_file(*.env)"], ["read_file(*.env)", "edit_file(*.env)"]])
@pytest.mark.parametrize("mode", [permissions.DEFAULT, permissions.AUTO_EDIT, permissions.ALLOW_ALL])
def test_missed_edits_never_show_a_file_a_deny_rule_keeps_from_being_read(project, deny, mode):
    (project / ".env").write_text("DB_PASSWORD=FAKE-not-real-123\n")
    gate = permissions.Gate(tools.Workspace(project), Asker(permissions.YES, permissions.YES), mode)
    gate.rules = permissions.Rules(allow=[], deny=deny)
    results = results_of_two_misses(gate, ".env")
    assert not any("FAKE-not-real" in result for result in results), results


def test_missed_edits_inside_the_project_still_ask_nothing_and_show_the_file_on_the_second_miss(project):
    (project / "calc.py").write_text(FIXTURE_CALC)
    ask = Asker()
    gate = permissions.Gate(tools.Workspace(project), ask, permissions.DEFAULT)
    results = results_of_two_misses(gate, "calc.py")
    assert ask.questions == []
    assert agent.WHOLE_FILE_LEAD.format(path="calc.py", lines=len(FIXTURE_CALC.splitlines())) in results[-1]


def test_one_repeat_after_other_work_does_not_trip_the_stuck_stop(project):
    (project / "config.py").write_text("A = 1\n")
    (project / "other.py").write_text("B = 1\n")
    read = [("read_file", {"path": "config.py"})]
    brain = ScriptedBrain([read, read, read, [("read_file", {"path": "other.py"})],
                           [("edit_file", {"path": "other.py", "old_text": "B = 1", "new_text": "B = 2"})],
                           [("run_command", {"command": "echo checked"})], read, "All finished."])
    session = session_for(project, brain)
    session.ask("tidy the config")
    assert session.messages[-1]["content"] == "All finished."


def test_the_same_step_right_after_the_warning_still_stops(project):
    (project / "config.py").write_text("A = 1\n")
    read = [("read_file", {"path": "config.py"})]
    brain = ScriptedBrain([read] * (agent.STUCK_REPEATS + 1) + ["never reached"])
    session = session_for(project, brain)
    session.ask("tidy the config")
    assert session.messages[-1]["content"] == agent.STUCK_STOPPED


def big_file_and_its_first_window(project, line_ending="\n"):
    lines = [f"value_{number} = {number}" for number in range(1, 601)]
    (project / "big.py").write_bytes((line_ending.join(lines) + line_ending).encode())
    workspace = tools.Workspace(project)
    return workspace, tools.read_file(workspace, "big.py")


@pytest.mark.parametrize("line_ending", ["\n", "\r\n"])
def test_a_rewrite_that_copies_the_read_window_tail_line_is_refused_and_the_file_kept(project, line_ending):
    workspace, shown = big_file_and_its_first_window(project, line_ending)
    tail = shown.splitlines()[-1]
    assert tail.startswith("... ") and "read on with offset=" in tail
    body = "\n".join(line.split("\t", 1)[1] for line in shown.splitlines() if "\t" in line) + "\n" + tail + "\n"
    result = tools.run("write_file", {"path": "big.py", "content": body, "overwrite": True}, workspace)
    assert result.startswith("Error") and "placeholder" in result
    assert len((project / "big.py").read_text().splitlines()) == 600


@pytest.mark.parametrize("line_ending", ["\n", "\r\n"])
def test_a_rewrite_that_copies_the_read_window_header_is_refused(project, line_ending):
    workspace, shown = big_file_and_its_first_window(project, line_ending)
    header = shown.splitlines()[0]
    assert header.startswith("[big.py: lines 1-")
    result = tools.run("write_file", {"path": "big.py", "content": header + "\nvalue_1 = 1\n", "overwrite": True},
                       workspace)
    assert result.startswith("Error") and "placeholder" in result
