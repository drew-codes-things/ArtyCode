import sys

import pytest

from arty_cli import agent, eyes, prompts, tools
from arty_cli import extras as extras_module
from tests.scripted import ScriptedBrain, project, session_for
from tests.test_follow_ups import BRAINS_FOLLOW_UP

assert project

SECTIONS = ("Looking:", "Knowing:", "Changing files:", "Tests:", "Commands:", "Working through a job:", "Finishing:")
PROMPT_BUDGET_CHARS = 3300
LONGEST_PROMPT_LINE = 240


class Looks:
    def definitions(self):
        return [eyes.VIEW_IMAGE_DEFINITION, eyes.SCREENSHOT_DEFINITION, eyes.HTTP_CHECK_DEFINITION]


def names_offered(session) -> list[str]:
    return [definition["function"]["name"] for definition in session.offered()]


def looking_session(folder, brain=None):
    session = session_for(folder, brain or ScriptedBrain([]))
    session.extras = extras_module.Extras(looks=Looks())
    session.situational = set()
    session.notice_situational_tools("")
    return session


def test_the_system_prompt_comes_in_short_labelled_sections(tmp_path):
    prompt = prompts.system_prompt(tools.Workspace(tmp_path))
    lines = prompt.splitlines()
    assert all(section in lines for section in SECTIONS)
    assert max(len(line) for line in lines) <= LONGEST_PROMPT_LINE
    assert len(prompt) < PROMPT_BUDGET_CHARS


def test_the_system_prompt_does_not_name_tools_that_are_only_offered_sometimes(tmp_path):
    prompt = prompts.system_prompt(tools.Workspace(tmp_path))
    assert all(name not in prompt for name in (eyes.VIEW_IMAGE, eyes.SCREENSHOT, "notebook_edit"))


def test_notebook_edit_is_offered_only_when_the_project_has_a_notebook(project):
    assert "notebook_edit" not in names_offered(looking_session(project))
    (project / "lab.ipynb").write_text("{}")
    assert "notebook_edit" in names_offered(looking_session(project))


def test_a_request_about_a_notebook_offers_notebook_edit_from_then_on(project):
    session = looking_session(project, ScriptedBrain(["Made it.", "Fine."]))
    session.ask("make a jupyter notebook that plots sales")
    session.ask("thanks")
    assert "notebook_edit" in names_offered(session)


def test_picture_tools_wait_for_a_web_or_picture_project_but_http_check_is_always_there(project):
    (project / "calc.py").write_text("print(1)\n")
    offered = names_offered(looking_session(project))
    assert eyes.VIEW_IMAGE not in offered and eyes.SCREENSHOT not in offered and eyes.HTTP_CHECK in offered
    (project / "web").mkdir()
    (project / "web" / "index.html").write_text("<h1>hi</h1>")
    offered = names_offered(looking_session(project))
    assert eyes.VIEW_IMAGE in offered and eyes.SCREENSHOT in offered


def test_a_request_to_look_at_a_page_offers_the_picture_tools(project):
    session = looking_session(project, ScriptedBrain(["Looked."]))
    session.ask("take a screenshot of the page and check the layout")
    assert eyes.SCREENSHOT in names_offered(session)


def test_the_tools_a_turn_is_sent_match_what_the_session_offers(project):
    seen = []

    class Recording(ScriptedBrain):
        def reply(self, messages, tools, on_text):
            seen.append([definition["function"]["name"] for definition in tools])
            return super().reply(messages, tools, on_text)

    session = looking_session(project, Recording(["Done."]))
    session.ask("hello")
    assert seen and seen[0] == names_offered(session)


def test_helpers_and_other_callers_still_get_every_tool_by_default():
    names = [definition["function"]["name"] for definition in agent.offered_definitions(extras_module.NONE)]
    assert "notebook_edit" in names


def different_listing(number: int) -> list:
    return [("list_files", {"pattern": f"*.{number}"})]


def automatic_user_messages(brain, request: str) -> list[str]:
    last = brain.prompts[-1]
    return [str(message.get("content")) for message in last[1:]
            if message["role"] == "user" and message.get("content") != request]


def test_a_long_turn_is_reminded_of_the_request_every_few_rounds(project):
    request = "make calc.py evaluate 2+3*4 without eval"
    rounds = [different_listing(number) for number in range(agent.ANCHOR_ROUNDS + 1)]
    brain = ScriptedBrain(rounds + ["Done."])
    session_for(project, brain).ask(request)
    anchors = automatic_user_messages(brain, request)
    assert len(anchors) == 1
    assert anchors[0].startswith(agent.ANCHOR_LEAD) and request in anchors[0]
    assert BRAINS_FOLLOW_UP.search(anchors[0])


def test_a_short_turn_is_not_reminded(project):
    rounds = [different_listing(number) for number in range(agent.ANCHOR_ROUNDS - 1)]
    brain = ScriptedBrain(rounds + ["Done."])
    session_for(project, brain).ask("list things")
    assert automatic_user_messages(brain, "list things") == []


def test_the_reminder_carries_the_checklist_and_waits_after_a_fresh_todo_write(project):
    checklist = ("todo_write", {"items": [{"text": "Write calc.py", "status": "done"},
                                          {"text": "Write test_calc.py", "status": "in_progress"}]})
    rounds = [different_listing(number) for number in range(3)] + [[checklist]]
    rounds += [different_listing(number) for number in range(3, 3 + agent.ANCHOR_ROUNDS)]
    brain = ScriptedBrain(rounds + ["Done."])
    session_for(project, brain).ask("build the calculator")
    anchors = [text for text in automatic_user_messages(brain, "build the calculator")
               if text.startswith(agent.ANCHOR_LEAD)]
    assert len(anchors) == 1
    assert "Write test_calc.py" in anchors[0] and "Write calc.py" in anchors[0]
    reminded_at = next(index for index, prompt in enumerate(brain.prompts)
                       if any(message.get("content") == anchors[0] for message in prompt))
    assert reminded_at == 4 + agent.ANCHOR_ROUNDS


def test_a_long_request_is_shortened_in_the_reminder(project):
    request = "build it " + "with care " * 400
    rounds = [different_listing(number) for number in range(agent.ANCHOR_ROUNDS)]
    brain = ScriptedBrain(rounds + ["Done."])
    session_for(project, brain).ask(request)
    anchors = automatic_user_messages(brain, request)
    assert len(anchors) == 1 and len(anchors[0]) < agent.ANCHOR_REQUEST_CHARS + 200


def test_run_command_says_stderr_is_included_long_output_is_trimmed_and_takes_a_folder():
    definition = next(d for d in tools.DEFINITIONS if d["function"]["name"] == "run_command")
    description = definition["function"]["description"]
    assert "stderr" in description and "trimmed" in description
    assert "cwd" in definition["function"]["parameters"]["properties"]


@pytest.mark.skipif(sys.platform == "win32", reason="pwd is a bash command")
def test_run_command_runs_in_a_folder_inside_the_project(tmp_path):
    (tmp_path / "app" / "src").mkdir(parents=True)
    workspace = tools.Workspace(tmp_path)
    output = tools.run("run_command", {"command": "pwd", "cwd": "app/src"}, workspace)
    assert output.splitlines()[0].endswith("app/src") and "[exit code 0]" in output


@pytest.mark.skipif(sys.platform == "win32", reason="pwd is a bash command")
def test_a_background_command_runs_in_the_folder_too(tmp_path):
    (tmp_path / "app").mkdir()
    workspace = tools.Workspace(tmp_path)
    tools.run("run_command", {"command": "pwd", "cwd": "app", "background": True}, workspace)
    output = tools.run("command_output", {"job": "1", "wait_seconds": 5}, workspace)
    assert output.splitlines()[0].endswith("/app")


@pytest.mark.parametrize("folder", ["..", "missing", "notes.txt"])
def test_run_command_refuses_a_folder_outside_the_project_or_that_is_not_a_folder(tmp_path, folder):
    (tmp_path / "project").mkdir()
    (tmp_path / "project" / "notes.txt").write_text("x")
    workspace = tools.Workspace(tmp_path / "project")
    output = tools.run("run_command", {"command": "echo hi", "cwd": folder}, workspace)
    assert output.startswith("Error:") and "hi" not in output


def test_a_folder_given_under_another_name_is_understood(tmp_path):
    (tmp_path / "app").mkdir()
    named = tools.typed_arguments("run_command", {"command": "pwd", "working_directory": "app"})
    assert named == {"command": "pwd", "cwd": "app"}
