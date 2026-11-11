from arty_cli import prompts, tools


def test_the_model_is_told_to_keep_changes_small_and_leave_tests_alone(tmp_path):
    prompt = prompts.system_prompt(tools.Workspace(tmp_path))
    assert "never edit or add tests unless asked" in prompt
    assert "smallest change" in prompt
    assert "list_files to find files by name" in prompt


def test_project_notes_come_after_the_rules(tmp_path):
    prompt = prompts.system_prompt(tools.Workspace(tmp_path), "Notes: use tabs")
    assert prompt.endswith("Notes: use tabs")
