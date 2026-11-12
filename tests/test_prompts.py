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


def test_the_model_is_told_write_file_needs_overwrite_for_an_existing_file(tmp_path):
    prompt = prompts.system_prompt(tools.Workspace(tmp_path))
    assert "write_file only for new files" in prompt and "overwrite: true" in prompt


def test_the_model_is_told_to_finish_the_whole_job_without_asking_to_carry_on(tmp_path):
    prompt = prompts.system_prompt(tools.Workspace(tmp_path))
    assert "until it is done and checked" in prompt and "don't stop part way" in prompt
    assert "todo_write" in prompt and "background: true" in prompt


def test_the_model_is_told_ordinary_builds_like_a_yt_dlp_front_end_are_fine(tmp_path):
    prompt = prompts.system_prompt(tools.Workspace(tmp_path))
    assert "yt-dlp" in prompt and "just build them" in prompt


def test_the_model_is_told_to_end_with_what_it_built_and_how_to_run_it(tmp_path):
    prompt = prompts.system_prompt(tools.Workspace(tmp_path))
    assert "exact command to run or open it" in prompt


def test_the_model_is_told_to_check_its_own_work_and_say_what_it_could_not_check(tmp_path):
    prompt = prompts.system_prompt(tools.Workspace(tmp_path))
    assert "check it yourself" in prompt and "run the script with sample input" in prompt
    assert "what you ran to check it and what that showed" in prompt and "could not be checked" in prompt
