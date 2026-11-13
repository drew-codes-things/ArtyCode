from arty_cli import agent
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
