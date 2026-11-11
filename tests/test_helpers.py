from arty_cli import helpers, tools


def write_helper(folder, name, text):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.md").write_text(text)


REVIEWER = ("---\nname: reviewer\ndescription: Reviews a diff for bugs\ntools: read_file, search, run_command\n---\n"
            "You review changes. Run git diff, read the changed files and list real bugs only.\n")


def test_a_helper_is_read_from_its_file(tmp_path):
    personal = tmp_path / "personal"
    write_helper(personal / helpers.HELPERS_FOLDER, "reviewer", REVIEWER)
    found, problems = helpers.find(personal, tools.Workspace(tmp_path), include_project=False)
    assert problems == []
    reviewer = found["reviewer"]
    assert reviewer.description == "Reviews a diff for bugs"
    assert reviewer.tools == ("read_file", "search", "run_command")
    assert reviewer.prompt.startswith("You review changes.") and reviewer.source == "yours"


def test_without_a_tools_line_a_helper_can_only_look(tmp_path):
    personal = tmp_path / "personal"
    write_helper(personal / helpers.HELPERS_FOLDER, "notes", "Summarise the docs folder.\n")
    found, _problems = helpers.find(personal, tools.Workspace(tmp_path), include_project=False)
    assert found["notes"].tools == helpers.LOOKING_TOOLS
    assert found["notes"].description == "Summarise the docs folder."
    assert not found["notes"].changes_things()


def test_project_helpers_need_trust_and_win_over_yours_by_name(tmp_path):
    personal, project = tmp_path / "personal", tmp_path / "project"
    write_helper(personal / helpers.HELPERS_FOLDER, "reviewer", REVIEWER)
    write_helper(project / helpers.PROJECT_HELPERS, "reviewer", "---\ndescription: Project reviewer\n---\nBe strict.\n")
    workspace = tools.Workspace(project)
    untrusted, _problems = helpers.find(personal, workspace, include_project=False)
    trusted, _problems = helpers.find(personal, workspace, include_project=True)
    assert untrusted["reviewer"].source == "yours"
    assert (trusted["reviewer"].description, trusted["reviewer"].source) == ("Project reviewer", "this project")


def test_unknown_tools_names_and_empty_prompts_are_reported(tmp_path):
    personal = tmp_path / "personal"
    folder = personal / helpers.HELPERS_FOLDER
    write_helper(folder, "odd", "---\ntools: read_file, launch_rockets, explore\n---\nGo.\n")
    write_helper(folder, "empty", "---\ndescription: nothing\n---\n")
    write_helper(folder, "bad name", "Look around.\n")
    found, problems = helpers.find(personal, tools.Workspace(tmp_path), include_project=False)
    assert found == {}
    assert len(problems) == 3
    assert any("launch_rockets" in problem and "explore" in problem for problem in problems)
