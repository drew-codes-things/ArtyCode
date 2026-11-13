import json

from arty_cli import agent, prune, tools
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
