import json
import os
import stat
import sys

import pytest

from arty_cli import lint, tools

stand_in_linters = pytest.mark.skipif(sys.platform == "win32", reason="the stand-in linters are shell scripts")


def fake_program(folder, name, output, exit_code=1):
    folder.mkdir(parents=True, exist_ok=True)
    script = folder / name
    script.write_text(f"#!/bin/sh\necho '{output}' \"$@\"\nexit {exit_code}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setenv("PATH", str(tmp_path / "bin") + os.pathsep + "/usr/bin" + os.pathsep + "/bin")
    return root


def edited(workspace, name, text):
    path = workspace.root / name
    path.write_bytes(text.encode("utf-8"))
    workspace.note_seen(path, text)
    return path


def test_a_python_syntax_error_is_reported_after_an_edit(project):
    workspace = tools.Workspace(project)
    workspace.lint = lint.Checker(project)
    path = edited(workspace, "app.py", "def ok():\n    return 1\n")
    result = tools.edit_file(workspace, str(path), "return 1", "return (1")
    assert result.startswith("Edited app.py")
    assert "Lint" in result and "app.py" in result and "line 2" in result


def test_a_clean_file_adds_nothing_to_the_result(project):
    workspace = tools.Workspace(project)
    workspace.lint = lint.Checker(project)
    path = edited(workspace, "app.py", "x = 1\n")
    assert tools.edit_file(workspace, str(path), "x = 1", "x = 2") == "Edited app.py"


@stand_in_linters
def test_ruff_runs_when_the_project_configures_it(project, tmp_path):
    fake_program(tmp_path / "bin", "ruff", "app.py:1:1: F401 unused import")
    (project / "ruff.toml").write_text("line-length = 100\n")
    note = lint.Checker(project).problems(project / "app.py", "import os\n")
    assert "ruff" in note and "F401 unused import" in note


def test_ruff_is_not_run_without_its_config(project, tmp_path):
    fake_program(tmp_path / "bin", "ruff", "app.py:1:1: F401 unused import")
    assert lint.Checker(project).problems(project / "app.py", "import os\n") == ""


@stand_in_linters
def test_flake8_config_in_setup_cfg_picks_flake8(project, tmp_path):
    fake_program(tmp_path / "bin", "flake8", "app.py:1:1: E999")
    (project / "setup.cfg").write_text("[flake8]\nmax-line-length = 100\n")
    assert "E999" in lint.Checker(project).problems(project / "app.py", "x = 1\n")


def test_a_linter_inside_the_project_folder_is_never_run(project, monkeypatch):
    fake_program(project / ".venv" / "bin", "ruff", "ran project code")
    monkeypatch.setenv("PATH", str(project / ".venv" / "bin") + os.pathsep + "/usr/bin" + os.pathsep + "/bin")
    (project / "ruff.toml").write_text("")
    assert "ran project code" not in lint.Checker(project).problems(project / "app.py", "x = 1\n")


def test_broken_json_is_reported(project):
    note = lint.Checker(project).problems(project / "data.json", '{"a": 1,}\n')
    assert "data.json" in note and "JSON" in note


@stand_in_linters
def test_a_slow_linter_is_stopped_and_said_so(project, tmp_path):
    folder = tmp_path / "bin"
    folder.mkdir()
    script = folder / "ruff"
    script.write_text("#!/bin/sh\nsleep 5\n")
    script.chmod(0o755)
    (project / "ruff.toml").write_text("")
    note = lint.Checker(project, timeout_seconds=0.5).problems(project / "app.py", "x = 1\n")
    assert "took longer than" in note


@stand_in_linters
def test_long_linter_output_is_cut(project, tmp_path):
    fake_program(tmp_path / "bin", "ruff", "E" * 5000)
    (project / "ruff.toml").write_text("")
    assert len(lint.Checker(project).problems(project / "app.py", "x = 1\n")) < lint.MAX_NOTE_CHARS + 200


@pytest.mark.skipif(sys.platform == "win32", reason="bash is not on every Windows machine")
def test_a_shell_syntax_error_is_reported(project):
    note = lint.Checker(project).problems(project / "run.sh", "if true; then\n  echo hi\n")
    assert "run.sh" in note


def test_lint_can_be_turned_off_in_settings(tmp_path):
    personal = tmp_path / "personal"
    personal.mkdir()
    project = tmp_path / "project"
    (project / ".arty").mkdir(parents=True)
    assert lint.wanted(personal, project, include_project=True)
    (personal / "settings.json").write_text(json.dumps({"lintAfterEdits": False}))
    assert not lint.wanted(personal, project, include_project=True)


def test_an_untrusted_project_cannot_change_the_lint_setting(tmp_path):
    personal = tmp_path / "personal"
    personal.mkdir()
    project = tmp_path / "project"
    (project / ".arty").mkdir(parents=True)
    (project / ".arty" / "settings.json").write_text(json.dumps({"lintAfterEdits": False}))
    assert lint.wanted(personal, project, include_project=False)
    assert not lint.wanted(personal, project, include_project=True)


def test_without_a_checker_edits_behave_as_before(project):
    workspace = tools.Workspace(project)
    path = edited(workspace, "app.py", "x = 1\n")
    assert tools.edit_file(workspace, str(path), "x = 1", "x = (") == "Edited app.py"
