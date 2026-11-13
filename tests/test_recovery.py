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
