import re

import pytest

from arty_cli import agent, checks, cli
from tests.scripted import ScriptedBrain, project, session_for
from tests.test_engine import forced_summary

assert project

BRAINS_FOLLOW_UP = re.compile(r"^\W*(?:yes, )?(?:carry on\b|do (?:that|it) now\b|you said you haven['’]t\b|"
                              r"your last reply was cut off\b|your checklist still has unfinished items\b|"
                              r"an? (?:stop|subagentstop) hook asked you\b|you haven['’]t changed anything\b|"
                              r"you have made the same tool calls\b|before you finish\b)", re.I)

AUTOMATIC_MESSAGES = [
    agent.DO_IT_NOW, agent.NOT_DONE_ANSWER, agent.CARRY_ON_ANSWER, agent.CUT_SHORT_ANSWER,
    agent.unfinished_note([{"text": "Write tests", "status": "pending"}]), f"{agent.STOP_HOOK_LEAD} keep going",
    f"{agent.SUBAGENT_STOP_LEAD} keep going", agent.NOTHING_CHANGED_YET, agent.STUCK_NOTE, agent.STUCK_FAILING_NOTE,
    checks.FINISH_LEAD, cli.COMPACTED_LEAD,
]


@pytest.mark.parametrize("message", AUTOMATIC_MESSAGES)
def test_every_automatic_follow_up_is_recognised_by_the_brain_as_one(message):
    assert BRAINS_FOLLOW_UP.search(message), message


def automatic_messages_in(brain, request: str) -> list[str]:
    seen = []
    for prompt in brain.prompts:
        for message in prompt[1:]:
            text = str(message.get("content") or "")
            if message["role"] == "user" and text != request and text not in seen:
                seen.append(text)
    return seen


def test_the_follow_ups_sent_in_a_real_turn_all_start_the_way_the_brain_expects(project):
    (project / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    wrong = ("edit_file", {"path": "calc.py", "old_text": "return a-b", "new_text": "return a + b"})
    looked = ("read_file", {"path": "calc.py"})
    brain = ScriptedBrain(["I'll read calc.py next.", [looked], [wrong], [looked], [wrong], [looked], [wrong],
                           [looked], [wrong]])
    session_for(project, brain).ask("fix add")
    sent = automatic_messages_in(brain, "fix add")
    assert len(sent) >= 2
    assert all(BRAINS_FOLLOW_UP.search(text) for text in sent), sent


def test_the_message_after_a_summary_starts_the_way_the_brain_expects(project, monkeypatch):
    _session, brain, carried = forced_summary(project, monkeypatch)
    assert BRAINS_FOLLOW_UP.search(carried)
