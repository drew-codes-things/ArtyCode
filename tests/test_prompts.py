from arty_cli import prompts, tools


def test_the_model_is_told_to_keep_changes_small_and_leave_tests_alone(tmp_path):
    prompt = prompts.system_prompt(tools.Workspace(tmp_path))
    assert "never edit or add tests unless asked" in prompt
    assert "smallest change" in prompt
    assert "list_files to find files by name" in prompt


def test_project_notes_come_after_the_rules(tmp_path):
    prompt = prompts.system_prompt(tools.Workspace(tmp_path), "Notes: use tabs")
    assert prompt.endswith("Notes: use tabs")


def test_the_model_is_told_to_search_for_a_name_instead_of_guessing_a_path(tmp_path):
    prompt = prompts.system_prompt(tools.Workspace(tmp_path))
    assert "search for it before guessing a path" in prompt
