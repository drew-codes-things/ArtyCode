from arty_cli import memory, tools


def test_project_and_personal_notes_both_reach_the_model(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "ARTY.md").write_text("Use tabs.\n")
    personal = tmp_path / "personal"
    personal.mkdir()
    (personal / "ARTY.md").write_text("Call me Sam.\n")
    text = memory.instructions(tools.Workspace(project), personal)
    assert "Use tabs." in text and "Call me Sam." in text
    assert str(project / "ARTY.md") in text


def test_no_notes_means_nothing_extra(tmp_path):
    assert memory.instructions(tools.Workspace(tmp_path), tmp_path / "none") == ""


def test_a_quick_note_is_added_to_the_project_file(tmp_path):
    workspace = tools.Workspace(tmp_path)
    path = memory.add_note(workspace, "tests live in tests/")
    memory.add_note(workspace, "run them with pytest")
    assert path == tmp_path / "ARTY.md"
    assert path.read_text().splitlines()[-2:] == ["- tests live in tests/", "- run them with pytest"]
