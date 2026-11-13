import io

import pytest

from arty_cli import agent, authorise, cli, login_flow, permissions, prompts, tools
from arty_cli import __version__


def test_help_and_version_call_it_artycode(capsys):
    with pytest.raises(SystemExit):
        cli.parse_arguments(["--help"])
    shown = capsys.readouterr().out
    assert "ArtyCode: reads, edits and runs code" in shown and "Arty CLI" not in shown
    assert "usage: arty-cli" in shown
    with pytest.raises(SystemExit):
        cli.parse_arguments(["--version"])
    assert capsys.readouterr().out.strip() == f"ArtyCode {__version__}"


class Named:
    def __init__(self, root):
        self.workspace = tools.Workspace(root)
        self.gate = permissions.Gate(self.workspace, permissions.refuse)
        self.terminal = cli.Terminal(io.StringIO(), io.StringIO())
        self.client = type("Client", (), {"model": "arty/qwen3:14b"})()


def test_the_welcome_says_artycode_and_its_version(tmp_path):
    session = Named(tmp_path)
    cli.welcome(session)
    first = session.terminal.err.getvalue().splitlines()[0]
    assert first == f"◆ ArtyCode {__version__}"


def test_the_model_and_its_helpers_are_told_they_work_inside_artycode(tmp_path):
    told = (prompts.system_prompt(tools.Workspace(tmp_path)), agent.HELPER_ROLE, agent.EXPLORER_PROMPT)
    assert all("inside ArtyCode" in text and "arty-cli" not in text for text in told)


def test_what_the_user_reads_names_artycode_and_keeps_the_commands():
    said = (login_flow.NO_ADDRESS, cli.NOTIFICATION_LEADS[cli.PERMISSION_PROMPT], cli.NOTIFICATION_LEADS[cli.PLAN_APPROVAL],
            permissions.PROTECTED_NOTE, authorise.device_name())
    assert all("ArtyCode" in text and "Arty CLI" not in text for text in said)
    assert "arty-cli login" in login_flow.NO_ADDRESS
