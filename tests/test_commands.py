from arty_cli import commands, tools


def test_a_project_command_fills_in_its_arguments(tmp_path):
    folder = tmp_path / ".arty" / "commands"
    folder.mkdir(parents=True)
    (folder / "fix-issue.md").write_text("Fix issue $ARGUMENTS and add a test.\n")
    found = commands.find(tools.Workspace(tmp_path), tmp_path / "personal")
    assert found["/fix-issue"].prompt("42") == "Fix issue 42 and add a test."


def test_the_project_command_wins_over_a_personal_one(tmp_path):
    (tmp_path / ".arty" / "commands").mkdir(parents=True)
    (tmp_path / ".arty" / "commands" / "deploy.md").write_text("project deploy")
    personal = tmp_path / "personal"
    (personal / "commands").mkdir(parents=True)
    (personal / "commands" / "deploy.md").write_text("personal deploy")
    (personal / "commands" / "standup.md").write_text("standup notes")
    found = commands.find(tools.Workspace(tmp_path), personal)
    assert found["/deploy"].prompt("") == "project deploy"
    assert found["/standup"].prompt("") == "standup notes"


def test_built_in_prompts_are_always_there(tmp_path):
    found = commands.find(tools.Workspace(tmp_path), tmp_path / "personal")
    assert "ARTY.md" in found["/init"].prompt("")
    assert "git diff" in found["/review"].prompt("")


def test_arguments_without_a_placeholder_go_on_the_end(tmp_path):
    found = commands.find(tools.Workspace(tmp_path), tmp_path / "personal")
    assert found["/review"].prompt("only the parser").endswith("only the parser")
