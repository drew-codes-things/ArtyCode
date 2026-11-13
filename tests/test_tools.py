import contextlib
import json
import os
import signal
import sys
import time

import pytest

from arty_cli import tools
from tests.portable import python_call


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def add(a, b):\n    return a - b\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[core]\n")
    return tools.Workspace(tmp_path)


def test_reading_a_file_numbers_its_lines(workspace):
    assert tools.run("read_file", {"path": "src/app.py"}, workspace) == "1\tdef add(a, b):\n2\t    return a - b"


def test_reading_part_of_a_file(workspace):
    assert tools.run("read_file", {"path": "src/app.py", "offset": 2, "limit": 1}, workspace) == (
        "[src/app.py: lines 2-2 of 2; 1 above, 0 below]\n2\t    return a - b")


def test_editing_replaces_the_one_matching_text(workspace):
    tools.run("read_file", {"path": "src/app.py"}, workspace)
    result = tools.run("edit_file", {"path": "src/app.py", "old_text": "a - b", "new_text": "a + b"}, workspace)
    assert "src/app.py" in result
    assert (workspace.root / "src" / "app.py").read_text() == "def add(a, b):\n    return a + b\n"


def test_editing_refuses_text_that_is_not_there(workspace):
    result = tools.run("edit_file", {"path": "src/app.py", "old_text": "a * b", "new_text": "x"}, workspace)
    assert result.startswith("Error")
    assert "not found" in result


def test_editing_refuses_text_that_matches_twice_unless_asked(workspace):
    (workspace.root / "twice.txt").write_text("x\nx\n")
    tools.run("read_file", {"path": "twice.txt"}, workspace)
    refused = tools.run("edit_file", {"path": "twice.txt", "old_text": "x", "new_text": "y"}, workspace)
    assert refused.startswith("Error") and "2 places" in refused
    tools.run("edit_file", {"path": "twice.txt", "old_text": "x", "new_text": "y", "replace_all": True}, workspace)
    assert (workspace.root / "twice.txt").read_text() == "y\ny\n"


def test_writing_creates_missing_folders(workspace):
    tools.run("write_file", {"path": "docs/notes/a.md", "content": "hello\n"}, workspace)
    assert (workspace.root / "docs" / "notes" / "a.md").read_text() == "hello\n"


def test_listing_skips_version_control_folders(workspace):
    listed = tools.run("list_files", {"pattern": "**/*"}, workspace).splitlines()
    assert "src/app.py" in listed
    assert not any(line.startswith(".git") for line in listed)


def test_searching_names_file_and_line(workspace):
    assert tools.run("search", {"pattern": r"return a"}, workspace) == "src/app.py:2:    return a - b"


def test_searching_one_file_looks_inside_it(workspace):
    (workspace.root / "src" / "other.py").write_text("return a\n")
    assert tools.run("search", {"pattern": r"return a", "path": "src/app.py"}, workspace) == "src/app.py:2:    return a - b"


def test_a_bad_search_pattern_is_reported(workspace):
    assert tools.run("search", {"pattern": "("}, workspace).startswith("Error")


def test_a_missing_file_is_an_error_not_a_crash(workspace):
    assert tools.run("read_file", {"path": "nope.py"}, workspace).startswith("Error")


def test_an_unknown_tool_is_an_error(workspace):
    assert tools.run("format_disk", {}, workspace).startswith("Error")


def test_running_a_command_reports_output_and_exit_code(workspace):
    command = python_call("-c", "print(6 * 7); raise SystemExit(3)")
    result = tools.run("run_command", {"command": command}, workspace)
    assert "42" in result
    assert "exit code 3" in result


def test_a_command_that_runs_too_long_is_stopped(workspace):
    command = python_call("-c", "import time; time.sleep(5)")
    result = tools.run("run_command", {"command": command, "timeout_seconds": 1}, workspace)
    assert "stopped after 1 s" in result


@pytest.mark.skipif(sys.platform == "win32", reason="setsid is POSIX")
def test_a_stopped_command_returns_even_when_a_child_it_left_behind_holds_the_output(workspace):
    command = "setsid sh -c 'echo $$ > stray.pid; exec sleep 30' & echo started; sleep 20"
    began = time.monotonic()
    try:
        result = tools.run("run_command", {"command": command, "timeout_seconds": 1}, workspace)
    finally:
        stray = workspace.root / "stray.pid"
        if stray.exists():
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(stray.read_text()), signal.SIGKILL)
    assert time.monotonic() - began < 10
    assert "started" in result and "stopped after 1 s" in result


def test_paths_outside_the_project_are_recognised(workspace, tmp_path):
    assert workspace.contains("src/app.py")
    assert not workspace.contains("../elsewhere.txt")
    assert not workspace.contains(str(tmp_path.parent / "x"))


def test_the_change_an_edit_would_make_can_be_previewed(workspace):
    path, before, after = tools.planned_change("edit_file", {"path": "src/app.py", "old_text": "a - b", "new_text": "a + b"}, workspace)
    assert path.name == "app.py"
    assert "a - b" in before and "a + b" in after
    assert (workspace.root / "src" / "app.py").read_text().endswith("a - b\n")


def test_every_tool_has_a_definition_the_model_can_read():
    names = {definition["function"]["name"] for definition in tools.DEFINITIONS}
    assert names == set(tools.HANDLERS)
    assert all(definition["type"] == "function" for definition in tools.DEFINITIONS)


def test_the_todo_list_is_kept_and_shown_as_a_checklist(workspace):
    items = [{"text": "read the parser", "status": "done"}, {"text": "fix the bug", "status": "in_progress"},
             {"text": "run the tests", "status": "pending"}]
    result = tools.run("todo_write", {"items": items}, workspace)
    assert result.splitlines() == ["[x] read the parser", "[~] fix the bug", "[ ] run the tests"]
    assert workspace.todos == items


def test_a_todo_with_an_unknown_status_is_refused(workspace):
    assert tools.run("todo_write", {"items": [{"text": "x", "status": "maybe"}]}, workspace).startswith("Error")


def test_a_background_command_can_be_read_and_stopped(workspace):
    command = python_call("-u", "-c", "import time; print('ready'); time.sleep(30)")
    started = tools.run("run_command", {"command": command, "background": True}, workspace)
    job = started.split()[1]
    output = ""
    for _ in range(50):
        output += tools.run("command_output", {"job": job, "wait_seconds": 0}, workspace)
        if "ready" in output:
            break
        __import__("time").sleep(0.1)
    assert "ready" in output and "still running" in output
    assert "stopped" in tools.run("stop_command", {"job": job}, workspace)


def test_an_unknown_background_job_is_an_error(workspace):
    assert tools.run("command_output", {"job": "9"}, workspace).startswith("Error")


def test_editing_a_windows_file_keeps_its_line_endings(workspace):
    target = workspace.root / "run.bat"
    target.write_bytes(b"@echo off\r\necho one\r\necho two\r\n")
    tools.run("read_file", {"path": "run.bat"}, workspace)
    tools.run("edit_file", {"path": "run.bat", "old_text": "echo one\necho two", "new_text": "echo uno\necho two"},
              workspace)
    assert target.read_bytes() == b"@echo off\r\necho uno\r\necho two\r\n"


def test_editing_a_unix_file_keeps_its_line_endings(workspace):
    target = workspace.root / "run.sh"
    target.write_bytes(b"one\ntwo\n")
    tools.run("read_file", {"path": "run.sh"}, workspace)
    tools.run("edit_file", {"path": "run.sh", "old_text": "one", "new_text": "uno\nmore"}, workspace)
    assert target.read_bytes() == b"uno\nmore\ntwo\n"


def test_rewriting_a_windows_file_keeps_its_line_endings(workspace):
    target = workspace.root / "run.bat"
    target.write_bytes(b"a\r\nb\r\n")
    tools.run("read_file", {"path": "run.bat"}, workspace)
    tools.run("write_file", {"path": "run.bat", "content": "a\nc\n", "overwrite": True}, workspace)
    assert target.read_bytes() == b"a\r\nc\r\n"


def test_a_new_file_is_written_exactly_as_given(workspace):
    tools.run("write_file", {"path": "new.txt", "content": "a\nb\n"}, workspace)
    assert (workspace.root / "new.txt").read_bytes() == b"a\nb\n"


def test_reading_a_windows_file_shows_clean_lines(workspace):
    (workspace.root / "w.txt").write_bytes(b"one\r\ntwo\r\n")
    assert tools.run("read_file", {"path": "w.txt"}, workspace) == "1\tone\n2\ttwo"


def test_listing_with_dot_dot_reaches_the_folder_above_only_when_run(workspace):
    (workspace.root.parent / "beside.txt").write_text("x")
    listed = tools.run("list_files", {"pattern": "../*.txt"}, workspace)
    assert str(workspace.root.parent / "beside.txt") in listed


def test_dot_dot_after_a_wildcard_is_refused(workspace):
    assert tools.run("list_files", {"pattern": "*/../*"}, workspace).startswith("Error")


def test_listing_patterns_match_like_globs(workspace):
    (workspace.root / "top.py").write_text("")
    (workspace.root / "src" / "deep").mkdir()
    (workspace.root / "src" / "deep" / "x.py").write_text("")
    assert tools.run("list_files", {"pattern": "*.py"}, workspace).splitlines() == ["top.py"]
    assert tools.run("list_files", {"pattern": "src/*.py"}, workspace).splitlines() == ["src/app.py"]
    assert tools.run("list_files", {"pattern": "**/*.py"}, workspace).splitlines() == [
        "src/app.py", "src/deep/x.py", "top.py"]


def test_skipped_folders_are_never_walked(workspace, monkeypatch):
    (workspace.root / "node_modules" / "pkg").mkdir(parents=True)
    (workspace.root / "node_modules" / "pkg" / "a.js").write_text("")
    entered = []
    real_scandir = tools.os.scandir

    def watched(path="."):
        entered.append(str(path))
        return real_scandir(path)

    monkeypatch.setattr(tools.os, "scandir", watched)
    tools.run("search", {"pattern": "x"}, workspace)
    assert entered
    assert not any("node_modules" in path or ".git" in path for path in entered)


def test_search_skips_files_linked_from_outside_the_project(workspace, tmp_path_factory):
    elsewhere = tmp_path_factory.mktemp("secret")
    (elsewhere / "token.txt").write_text("password = hunter2\n")
    (workspace.root / "leak.txt").symlink_to(elsewhere / "token.txt")
    assert "hunter2" not in tools.run("search", {"pattern": "password"}, workspace)


def test_windows_runs_powershell_by_its_full_path(monkeypatch):
    monkeypatch.setattr(tools.sys, "platform", "win32")
    monkeypatch.setenv("SystemRoot", "C:\\Windows")
    arguments, _options = tools.shell_command("dir")
    assert arguments[0].lower().endswith("windowspowershell\\v1.0\\powershell.exe")
    assert arguments[0].lower().startswith("c:\\windows\\system32")


def test_background_output_keeps_only_the_tail(workspace, monkeypatch):
    monkeypatch.setattr(tools, "MAX_JOB_OUTPUT_CHARS", 100)
    command = python_call("-u", "-c", "print('\\n'.join(str(n) for n in range(200)))")
    job = tools.run("run_command", {"command": command, "background": True}, workspace).split()[1]
    running = workspace.jobs[job]
    running.process.wait()
    for _ in range(50):
        if "199" in "".join(running.output):
            break
        __import__("time").sleep(0.05)
    assert sum(len(line) for line in running.output) <= 100
    output = tools.run("command_output", {"job": job}, workspace)
    assert "199" in output and "earlier lines dropped" in output


def test_stopping_every_job_ends_background_commands(workspace):
    command = python_call("-c", "import time; time.sleep(30)")
    job = tools.run("run_command", {"command": command, "background": True}, workspace).split()[1]
    workspace.stop_jobs()
    assert workspace.jobs[job].process.poll() is not None


def test_ctrl_c_during_a_command_stops_it(workspace, monkeypatch):
    command = python_call("-c", "import time; time.sleep(30)")
    started = []
    real_start = tools.start_process

    def interrupted(workspace, command):
        process = real_start(workspace, command)
        started.append(process)
        monkeypatch.setattr(process, "communicate", lambda timeout=None: (_ for _ in ()).throw(KeyboardInterrupt))
        return process

    monkeypatch.setattr(tools, "start_process", interrupted)
    with pytest.raises(KeyboardInterrupt):
        tools.run("run_command", {"command": command}, workspace)
    assert started[0].wait(timeout=5) is not None


def notebook(cells, minor=5):
    return {"cells": cells, "metadata": {"kernelspec": {"name": "python3"}}, "nbformat": 4, "nbformat_minor": minor}


def code_cell(source, outputs=None, cell_id="c1"):
    return {"cell_type": "code", "id": cell_id, "metadata": {}, "execution_count": 3, "source": source,
            "outputs": outputs or []}


@pytest.fixture
def lab(tmp_path):
    cells = [{"cell_type": "markdown", "id": "m1", "metadata": {}, "source": ["# Title\n", "Notes"]},
             code_cell(["x = 1\n", "print(x)"], [{"output_type": "stream", "name": "stdout", "text": ["1\n"]}]),
             code_cell("y = 2", [{"output_type": "execute_result", "data": {"text/plain": ["2"], "image/png": "AAAA"},
                                   "metadata": {}, "execution_count": 4}], "c2")]
    (tmp_path / "lab.ipynb").write_text(json.dumps(notebook(cells)))
    return tools.Workspace(tmp_path)


def saved_notebook(workspace):
    return json.loads((workspace.root / "lab.ipynb").read_text())


def test_a_notebook_reads_as_numbered_cells_with_their_text_output(lab):
    shown = tools.run("read_file", {"path": "lab.ipynb"}, lab)
    assert "Cell 1 (markdown)" in shown and "# Title\nNotes" in shown
    assert "Cell 2 (code)" in shown and "x = 1\nprint(x)" in shown and "Output:\n1" in shown
    assert "Cell 3 (code)" in shown and "[image/png output]" in shown


def test_replacing_a_code_cell_clears_its_old_output(lab):
    tools.run("read_file", {"path": "lab.ipynb"}, lab)
    result = tools.run("notebook_edit", {"path": "lab.ipynb", "cell": 2, "source": "x = 10\nprint(x)"}, lab)
    assert "cell 2" in result.lower()
    cell = saved_notebook(lab)["cells"][1]
    assert "".join(cell["source"]) == "x = 10\nprint(x)"
    assert cell["outputs"] == [] and cell["execution_count"] is None and cell["id"] == "c1"


def test_inserting_and_deleting_cells(lab):
    tools.run("read_file", {"path": "lab.ipynb"}, lab)
    tools.run("notebook_edit", {"path": "lab.ipynb", "cell": 1, "source": "Intro", "cell_type": "markdown",
                                "edit_mode": "insert"}, lab)
    saved = saved_notebook(lab)
    assert [cell["cell_type"] for cell in saved["cells"]] == ["markdown", "markdown", "code", "code"]
    assert "".join(saved["cells"][0]["source"]) == "Intro" and saved["cells"][0]["id"]
    tools.run("notebook_edit", {"path": "lab.ipynb", "cell": 4, "edit_mode": "delete"}, lab)
    assert len(saved_notebook(lab)["cells"]) == 3
    tools.run("notebook_edit", {"path": "lab.ipynb", "cell": 4, "source": "z = 3", "cell_type": "code",
                                "edit_mode": "insert"}, lab)
    assert saved_notebook(lab)["cells"][-1]["outputs"] == []


def test_bad_notebook_edits_are_explained_and_change_nothing(lab):
    before = (lab.root / "lab.ipynb").read_text()
    assert tools.run("notebook_edit", {"path": "lab.ipynb", "cell": 9, "source": "x"}, lab).startswith("Error")
    assert tools.run("notebook_edit", {"path": "lab.ipynb", "cell": 1, "source": "x", "edit_mode": "insert",
                                       "cell_type": "sql"}, lab).startswith("Error")
    assert tools.run("notebook_edit", {"path": "lab.ipynb", "cell": 1, "edit_mode": "move"}, lab).startswith("Error")
    (lab.root / "plain.txt").write_text("hi")
    assert tools.run("notebook_edit", {"path": "plain.txt", "cell": 1, "source": "x"}, lab).startswith("Error")
    assert (lab.root / "lab.ipynb").read_text() == before


def test_a_broken_notebook_file_is_reported_not_crashed_on(tmp_path):
    (tmp_path / "bad.ipynb").write_text("{not json")
    workspace = tools.Workspace(tmp_path)
    assert tools.run("read_file", {"path": "bad.ipynb"}, workspace).startswith("Error")


def test_reading_a_huge_file_stops_at_a_size_limit_and_says_where_to_go_on(tmp_path):
    (tmp_path / "big.txt").write_text(("y" * 1000 + "\n") * 1500)
    shown = tools.read_file(tools.Workspace(tmp_path), "big.txt")
    assert len(shown) <= tools.DEFAULT_RESULT_CHARS + 200
    last = int(shown.splitlines()[-2].split("\t")[0])
    assert f"offset={last + 1}" in shown.splitlines()[-1]


def test_a_zero_or_negative_limit_reads_the_default_amount(tmp_path):
    (tmp_path / "three.txt").write_text("one\ntwo\nthree\n")
    workspace = tools.Workspace(tmp_path)
    assert tools.read_file(workspace, "three.txt", limit=0).splitlines() == ["1\tone", "2\ttwo", "3\tthree"]
    assert len(tools.read_file(workspace, "three.txt", limit=-4).splitlines()) == 3


def test_a_partial_read_says_how_many_lines_are_left(tmp_path):
    (tmp_path / "three.txt").write_text("one\ntwo\nthree\n")
    shown = tools.read_file(tools.Workspace(tmp_path), "three.txt", limit=1).splitlines()
    assert shown[0] == "[three.txt: lines 1-1 of 3; 0 above, 2 below]" and shown[1] == "1\tone"
    assert shown[-1] == "... 2 more lines; read on with offset=2"


def test_a_whole_read_has_no_more_lines_note(tmp_path):
    (tmp_path / "three.txt").write_text("one\ntwo\nthree\n")
    assert "more lines" not in tools.read_file(tools.Workspace(tmp_path), "three.txt", offset=2, limit=5)


def test_search_output_has_a_size_limit(tmp_path):
    for number in range(50):
        (tmp_path / f"f{number}.txt").write_text(("match " + "z" * 1990 + "\n") * 3)
    found = tools.search(tools.Workspace(tmp_path), "match")
    assert len(found) <= tools.DEFAULT_RESULT_CHARS + 200 and "stopped" in found.splitlines()[-1]


def test_windows_stops_jobs_with_the_system_taskkill_not_one_found_nearby(monkeypatch):
    ran = []
    monkeypatch.setattr(tools.sys, "platform", "win32")
    monkeypatch.setenv("SystemRoot", "C:\\Windows")
    monkeypatch.setattr(tools.subprocess, "run", lambda arguments, **options: ran.append(arguments))
    tools.stop(type("Process", (), {"pid": 42})())
    assert ran[0][0] == "C:\\Windows\\System32\\taskkill.exe"


def test_an_existing_file_must_be_read_before_it_is_changed(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    workspace = tools.Workspace(tmp_path)
    refused = tools.run("edit_file", {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"}, workspace)
    assert refused.startswith("Error") and "read_file" in refused
    assert tools.run("write_file", {"path": "a.py", "content": "y"}, workspace).startswith("Error")
    tools.run("read_file", {"path": "a.py"}, workspace)
    assert tools.run("edit_file", {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"}, workspace).startswith("Edited")
    assert tools.run("edit_file", {"path": "a.py", "old_text": "x = 2", "new_text": "x = 3"}, workspace).startswith("Edited")
    assert tools.run("write_file", {"path": "new.py", "content": "z = 1\n"}, workspace).startswith("Created")


def test_a_file_changed_on_disk_since_it_was_read_must_be_read_again(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    workspace = tools.Workspace(tmp_path)
    tools.run("read_file", {"path": "a.py"}, workspace)
    (tmp_path / "a.py").write_text("x = 1\nchanged = True\n")
    refused = tools.run("edit_file", {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"}, workspace)
    assert refused.startswith("Error") and "changed" in refused
    assert "changed = True" in (tmp_path / "a.py").read_text()


def test_writes_replace_the_file_in_one_step_and_leave_nothing_beside_it(tmp_path, monkeypatch):
    target = tmp_path / "run.sh"
    target.write_text("echo one\n")
    workspace = tools.Workspace(tmp_path)
    tools.run("read_file", {"path": "run.sh"}, workspace)
    replaced = []
    real_replace = tools.os.replace
    monkeypatch.setattr(tools.os, "replace", lambda source, destination: (replaced.append(destination),
                                                                           real_replace(source, destination)))
    tools.run("write_file", {"path": "run.sh", "content": "echo two\n", "overwrite": True}, workspace)
    assert replaced == [target] and target.read_text() == "echo two\n"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["run.sh"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_writes_keep_the_file_mode(tmp_path):
    target = tmp_path / "run.sh"
    target.write_text("echo one\n")
    target.chmod(0o755)
    workspace = tools.Workspace(tmp_path)
    tools.run("read_file", {"path": "run.sh"}, workspace)
    tools.run("write_file", {"path": "run.sh", "content": "echo two\n", "overwrite": True}, workspace)
    assert target.stat().st_mode & 0o777 == 0o755


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
@pytest.mark.parametrize("umask, expected", [(0o022, 0o644), (0o002, 0o664), (0o077, 0o600)])
def test_new_files_get_the_mode_the_umask_allows(tmp_path, umask, expected):
    workspace = tools.Workspace(tmp_path)
    previous = os.umask(umask)
    try:
        tools.run("write_file", {"path": "made.html", "content": "<p>hi</p>\n"}, workspace)
    finally:
        os.umask(previous)
    assert (tmp_path / "made.html").stat().st_mode & 0o777 == expected
    assert sorted(path.name for path in tmp_path.iterdir()) == ["made.html"]


def test_undo_puts_back_each_change_in_turn_and_removes_created_files(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    workspace = tools.Workspace(tmp_path)
    tools.run("read_file", {"path": "a.py"}, workspace)
    tools.run("edit_file", {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"}, workspace)
    tools.run("write_file", {"path": "made.py", "content": "new\n"}, workspace)
    assert "made.py" in workspace.undo_last()
    assert not (tmp_path / "made.py").exists()
    assert "a.py" in workspace.undo_last()
    assert (tmp_path / "a.py").read_text() == "x = 1\n"
    assert workspace.undo_last() == tools.NOTHING_TO_UNDO


def test_undo_refuses_when_the_file_changed_after_arty_changed_it(tmp_path):
    workspace = tools.Workspace(tmp_path)
    tools.run("write_file", {"path": "made.py", "content": "new\n"}, workspace)
    (tmp_path / "made.py").write_text("edited by hand\n")
    assert "changed" in workspace.undo_last()
    assert (tmp_path / "made.py").read_text() == "edited by hand\n"


@pytest.mark.parametrize("glob", ["*.py", "**/*.py", "src/*.py", "src/**/*.py", "app.py"])
def test_search_globs_match_by_name_or_by_path_in_the_project(workspace, glob):
    assert tools.run("search", {"pattern": r"return a", "glob": glob}, workspace) == "src/app.py:2:    return a - b"


@pytest.mark.parametrize("glob", ["*.js", "docs/*.py", "**/*.txt"])
def test_search_globs_that_match_nothing_find_nothing(workspace, glob):
    assert tools.run("search", {"pattern": r"return a", "glob": glob}, workspace).startswith("No matches")


def test_multi_edit_makes_every_change_in_order_as_one_undoable_step(workspace):
    tools.run("read_file", {"path": "src/app.py"}, workspace)
    edits = [{"old_text": "def add", "new_text": "def plus"}, {"old_text": "a - b", "new_text": "a + b"},
             {"old_text": "plus(a, b)", "new_text": "plus(a, b=0)"}]
    result = tools.run("multi_edit", {"path": "src/app.py", "edits": edits}, workspace)
    assert result.splitlines()[0] == "Made 3 edits to src/app.py"
    assert (workspace.root / "src" / "app.py").read_text() == "def plus(a, b=0):\n    return a + b\n"
    assert len(workspace.checkpoints) == 1
    workspace.undo_last()
    assert (workspace.root / "src" / "app.py").read_text() == "def add(a, b):\n    return a - b\n"


@pytest.mark.parametrize("edits, problem", [
    ([{"old_text": "a - b", "new_text": "a + b"}, {"old_text": "a * b", "new_text": "x"}], "edit 2: old_text was not found"),
    ([{"old_text": "a", "new_text": "z"}], "edit 1: old_text is in"),
    ([{"old_text": "", "new_text": "z"}], "edit 1: old_text is empty"),
    ([], "edits needs at least one"),
    (["a - b"], "edit 1 should be an object"),
])
def test_multi_edit_changes_nothing_when_any_edit_fails(workspace, edits, problem):
    tools.run("read_file", {"path": "src/app.py"}, workspace)
    result = tools.run("multi_edit", {"path": "src/app.py", "edits": edits}, workspace)
    assert result.startswith("Error") and problem in result
    assert (workspace.root / "src" / "app.py").read_text() == "def add(a, b):\n    return a - b\n"
    assert workspace.checkpoints == []


def test_multi_edit_can_replace_every_match_when_asked(workspace):
    (workspace.root / "twice.txt").write_text("x\nx\ny\n")
    tools.run("read_file", {"path": "twice.txt"}, workspace)
    edits = [{"old_text": "x", "new_text": "z", "replace_all": True}, {"old_text": "y", "new_text": "w"}]
    tools.run("multi_edit", {"path": "twice.txt", "edits": edits}, workspace)
    assert (workspace.root / "twice.txt").read_text() == "z\nz\nw\n"


def test_multi_edit_needs_the_file_read_first(workspace):
    result = tools.run("multi_edit", {"path": "src/app.py", "edits": [{"old_text": "a - b", "new_text": "a + b"}]},
                       workspace)
    assert result.startswith("Error")
    assert (workspace.root / "src" / "app.py").read_text() == "def add(a, b):\n    return a - b\n"


def test_an_edit_whose_newlines_were_written_as_backslash_n_still_applies():
    before = "def greet():\n    return 'hi'\n"
    after = tools.edited_text(before, "def greet():\\n    return 'hi'", "def greet():\\n    return 'hello'", False)
    assert after == "def greet():\n    return 'hello'\n"


def test_a_real_backslash_n_in_the_file_is_matched_as_written():
    before = 'print("a\\nb")\n'
    assert tools.edited_text(before, 'print("a\\nb")', 'print("a\\nc")', False) == 'print("a\\nc")\n'


def test_a_real_escape_the_file_already_has_stays_an_escape_in_unescaped_new_text():
    before = 'def report():\n    pass\n\nprint("done\\n")\n'
    after = tools.edited_text(before, "def report():\\n    pass", 'def report():\\n    print("done\\n")', False)
    assert after == 'def report():\n    print("done\\n")\n\nprint("done\\n")\n'
    compile(after, "report.py", "exec")


def test_new_text_is_left_as_written_when_old_text_matched_as_written():
    before = "a = 1\n"
    assert tools.edited_text(before, "a = 1", "a = 1\\nb = 2", False) == "a = 1\\nb = 2\n"


def test_escaped_text_that_still_does_not_match_gives_the_usual_error():
    with pytest.raises(tools.ToolError, match="was not found"):
        tools.edited_text("one\ntwo\n", "three\\nfour", "x", False)


def test_escaped_text_found_in_several_places_needs_more_context():
    with pytest.raises(tools.ToolError, match="2 places"):
        tools.edited_text("a\nb\na\nb\n", "a\\nb", "c", False)


INDENTED = "def f(x):\n    if x:  \n        return 1\n    return 0\n"


def test_old_text_that_differs_only_in_spaces_at_line_ends_still_applies():
    after = tools.edited_text(INDENTED, "    if x:\n        return 1", "    if x:\n        return 2", False)
    assert after == "def f(x):\n    if x:\n        return 2\n    return 0\n"


def test_old_text_with_other_indentation_applies_with_the_files_own_indentation():
    after = tools.edited_text(INDENTED, "if x:\n    return 1\n", "if x:\n    log(x)\n    return 2\n", False)
    assert after == "def f(x):\n    if x:  \n        log(x)\n        return 2\n    return 0\n"


def test_old_text_indented_less_at_every_level_keeps_each_level_of_the_file():
    before = "def greet(name):\n    message = \"Hi\"\n    return message\n"
    old = "def greet(name):\n  message = \"Hi\"\n  return message"
    new = "def greet(name):\n  message = \"Hey\"\n  return message"
    after = tools.edited_text(before, old, new, False)
    assert after == "def greet(name):\n    message = \"Hey\"\n    return message\n"


def test_typographic_quotes_dashes_and_spaces_in_old_text_still_match():
    before = 'msg = "it\'s" - 1\n'
    old = "msg = \u201cit\u2019s\u201d\u00a0\u2013 1"
    assert tools.edited_text(before, old, 'msg = "ok"', False) == 'msg = "ok"\n'


def test_a_loose_match_in_several_places_is_refused():
    before = "a:\n    x = 1  \nb:\n    x = 1  \n"
    with pytest.raises(tools.ToolError, match="2 places"):
        tools.edited_text(before, "x = 1\n", "x = 2\n", True)


def test_the_tool_result_says_how_old_text_matched(workspace):
    (workspace.root / "f.py").write_text(INDENTED)
    tools.run("read_file", {"path": "f.py"}, workspace)
    exact = tools.run("edit_file", {"path": "f.py", "old_text": "    return 0", "new_text": "    return -1"}, workspace)
    assert exact.splitlines()[0] == "Edited f.py"
    loose = tools.run("edit_file", {"path": "f.py", "old_text": "if x:\n    return 1", "new_text": "if x:\n    return 3"},
                      workspace)
    assert loose.startswith("Edited f.py") and "ignoring indentation" in loose
    made = tools.run("multi_edit", {"path": "f.py", "edits": [
        {"old_text": "def f(x):", "new_text": "def g(x):"},
        {"old_text": "    if x:\n        return 3", "new_text": "    if x:\n        return 4"}]}, workspace)
    assert made.startswith("Made 2 edits to f.py") and "edit 2" in made and "spaces at line ends" in made
    assert (workspace.root / "f.py").read_text() == "def g(x):\n    if x:\n        return 4\n    return -1\n"


def test_writing_over_an_existing_file_is_refused_and_points_to_edit_file(workspace):
    tools.run("read_file", {"path": "src/app.py"}, workspace)
    refused = tools.run("write_file", {"path": "src/app.py", "content": "x = 1\n"}, workspace)
    assert refused.startswith("Error") and "edit_file" in refused and "overwrite" in refused
    assert (workspace.root / "src" / "app.py").read_text() == "def add(a, b):\n    return a - b\n"


def test_overwrite_true_replaces_an_existing_file_and_new_files_need_nothing(workspace):
    tools.run("read_file", {"path": "src/app.py"}, workspace)
    assert tools.run("write_file", {"path": "src/app.py", "content": "x = 1\n", "overwrite": True},
                     workspace).startswith("Updated src/app.py")
    assert (workspace.root / "src" / "app.py").read_text() == "x = 1\n"
    assert tools.run("write_file", {"path": "new.py", "content": "y = 2\n"}, workspace).startswith("Created new.py")


def test_the_write_file_definition_offers_overwrite():
    write = next(item["function"] for item in tools.DEFINITIONS if item["function"]["name"] == "write_file")
    assert write["parameters"]["properties"]["overwrite"]["type"] == "boolean"
    assert "edit_file" in write["description"]


class EndlessProcess:
    def __init__(self):
        self.timeouts = []
        self.ended = False

    def communicate(self, timeout=None):
        self.timeouts.append(timeout)
        if not self.ended:
            raise tools.subprocess.TimeoutExpired("server", timeout)
        return "", None

    def end_tree(self):
        self.ended = True

    def wait(self, timeout=None):
        return 0


def test_a_command_that_runs_too_long_says_how_to_run_it_for_longer_or_in_the_background(workspace, monkeypatch):
    process = EndlessProcess()
    monkeypatch.setattr(tools, "start_process", lambda workspace, command: process)
    result = tools.run("run_command", {"command": "npm run dev", "timeout_seconds": 30}, workspace)
    assert process.timeouts[0] == 30
    assert "stopped after 30 s" in result and "background: true" in result
    assert f"up to {tools.MAX_COMMAND_SECONDS}" in result


def test_the_longest_a_command_may_run_follows_the_setting(workspace, monkeypatch):
    process = EndlessProcess()
    monkeypatch.setattr(tools, "start_process", lambda workspace, command: process)
    workspace.max_command_seconds = 7200
    result = tools.run("run_command", {"command": "make", "timeout_seconds": 100_000}, workspace)
    assert process.timeouts[0] == 7200 and "up to 7200" in result


def test_the_longest_a_command_may_run_is_generous_for_builds_by_default(workspace):
    assert workspace.max_command_seconds == tools.MAX_COMMAND_SECONDS >= 1800


def test_reading_a_background_job_can_wait_for_it_to_finish(workspace):
    command = python_call("-u", "-c", "import time; time.sleep(0.5); print('built')")
    job = tools.run("run_command", {"command": command, "background": True}, workspace).split()[1]
    output = tools.run("command_output", {"job": job, "wait_seconds": 30}, workspace)
    assert "built" in output and "finished with exit code 0" in output


def test_reading_a_background_server_waits_only_as_long_as_asked(workspace):
    command = python_call("-u", "-c", "import time; print('listening'); time.sleep(30)")
    job = tools.run("run_command", {"command": command, "background": True}, workspace).split()[1]
    started = time.monotonic()
    output = tools.run("command_output", {"job": job, "wait_seconds": 1}, workspace)
    waited = time.monotonic() - started
    workspace.stop_jobs()
    assert "listening" in output and tools.STILL_RUNNING in output
    assert 0.9 <= waited < 10


def test_a_stopped_command_says_so_before_its_output_so_a_server_is_not_taken_for_running(workspace, monkeypatch):
    process = EndlessProcess()
    monkeypatch.setattr(tools, "start_process", lambda workspace, command: process)
    monkeypatch.setattr(tools, "output_after_stop", lambda process: " * Serving Flask app 'app'\n")
    result = tools.run("run_command", {"command": "python3 app.py"}, workspace)
    assert result.startswith("[stopped after 120 s while still running, so it is not running now.")
    assert result.endswith(" * Serving Flask app 'app'\n")


def test_a_list_sent_as_a_json_string_is_read_as_a_list():
    typed = tools.typed_arguments("todo_write", {"items": '[{"text": "a", "status": "pending"}]'})
    assert typed == {"items": [{"text": "a", "status": "pending"}]}


def test_a_string_that_is_not_json_is_left_for_the_tool_to_refuse():
    assert tools.typed_arguments("todo_write", {"items": "not a list"}) == {"items": "not a list"}


def test_common_other_names_for_arguments_become_the_right_ones():
    assert tools.typed_arguments("edit_file", {"file_path": "a", "old_string": "x", "new_string": "y"}) == {
        "path": "a", "old_text": "x", "new_text": "y"}
    assert tools.typed_arguments("run_command", {"cmd": "ls"}) == {"command": "ls"}


def test_an_alias_never_overrides_the_right_name_or_a_real_parameter():
    assert tools.typed_arguments("edit_file", {"path": "a", "file_path": "b", "old_text": "x", "new_text": "y"})[
        "path"] == "a"
    assert tools.typed_arguments("write_file", {"path": "a", "content": "x"}) == {"path": "a", "content": "x"}


def test_checklist_statuses_and_item_names_are_normalised():
    typed = tools.typed_arguments("todo_write", {"todos": [{"content": "a", "status": "Completed"},
                                                           {"task": "b", "status": "in-progress"},
                                                           {"text": "c", "status": "todo"}]})
    assert typed == {"items": [{"text": "a", "status": "done"}, {"text": "b", "status": "in_progress"},
                               {"text": "c", "status": "pending"}]}


def test_wrong_argument_names_get_the_list_of_right_ones(tmp_path):
    result = tools.run("read_file", {"where": "a.txt"}, tools.Workspace(tmp_path))
    assert result.startswith("Error") and "where" in result and "path, offset, limit" in result


FUNCTIONS = "".join(f"def step_{number}(value):\n    return value + {number}\n\n\n" for number in range(1, 30))


def test_a_missed_edit_shows_the_closest_lines_with_their_numbers(tmp_path):
    (tmp_path / "steps.py").write_text(FUNCTIONS)
    workspace = tools.Workspace(tmp_path)
    tools.read_file(workspace, "steps.py")
    result = tools.run("edit_file", {"path": "steps.py", "old_text": "def step_12(valu):\n    return value + 12",
                                     "new_text": "x"}, workspace)
    assert result.startswith("Error: old_text was not found")
    assert "45\tdef step_12(value):\n46\t    return value + 12" in result


def test_a_missed_edit_in_a_multi_edit_also_shows_the_closest_lines(tmp_path):
    (tmp_path / "steps.py").write_text(FUNCTIONS)
    workspace = tools.Workspace(tmp_path)
    tools.read_file(workspace, "steps.py")
    result = tools.run("multi_edit", {"path": "steps.py", "edits": [
        {"old_text": "return value + 1\n", "new_text": "return value + 100\n"},
        {"old_text": "def step_3(valu):", "new_text": "x"}]}, workspace)
    assert result.startswith("Error: edit 2: old_text was not found") and "9\tdef step_3(value):" in result


def test_a_made_edit_shows_the_changed_lines_with_a_few_around_them(tmp_path):
    (tmp_path / "steps.py").write_text(FUNCTIONS)
    workspace = tools.Workspace(tmp_path)
    tools.read_file(workspace, "steps.py")
    result = tools.edit_file(workspace, "steps.py", "return value + 12\n", "doubled = value * 2\n    return doubled\n")
    first, *shown = result.splitlines()
    assert first.startswith("Edited steps.py")
    assert "46\t    doubled = value * 2" in shown and "47\t    return doubled" in shown
    assert "45\tdef step_12(value):" in shown and len(shown) <= 12


def test_a_default_read_of_a_long_file_is_a_window_with_a_header_and_an_outline(tmp_path):
    (tmp_path / "steps.py").write_text(FUNCTIONS * 4)
    shown = tools.read_file(tools.Workspace(tmp_path), "steps.py").splitlines()
    assert shown[0] == f"[steps.py: lines 1-{tools.DEFAULT_READ_LINES} of 464; 0 above, 214 below]"
    assert "Outline" in shown[1] and "1: def step_1(value):" in shown and "45: def step_12(value):" in shown
    numbered = [line for line in shown if line[:1].isdigit() and "\t" in line]
    assert len(numbered) == tools.DEFAULT_READ_LINES
    assert shown[-1] == f"... 214 more lines; read on with offset={tools.DEFAULT_READ_LINES + 1}"


def test_a_later_window_has_a_header_but_no_outline(tmp_path):
    (tmp_path / "steps.py").write_text(FUNCTIONS * 4)
    shown = tools.read_file(tools.Workspace(tmp_path), "steps.py", offset=301, limit=50).splitlines()
    assert shown[0] == "[steps.py: lines 301-350 of 464; 300 above, 114 below]"
    assert shown[1].startswith("301\t") and not any("Outline" in line for line in shown)


BEYOND_ASCII = "café ✓ → ü"


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell's output encoding is a Windows matter")
def test_powershell_output_beyond_ascii_comes_back_unchanged(workspace):
    assert BEYOND_ASCII in tools.run_command(workspace, f"Write-Output '{BEYOND_ASCII}'")


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell's output encoding is a Windows matter")
def test_a_program_writing_utf8_through_powershell_comes_back_unchanged(workspace):
    script = f"import sys; sys.stdout.buffer.write({BEYOND_ASCII!r}.encode('utf-8'))"
    assert BEYOND_ASCII in tools.run_command(workspace, f"{python_call('-c', script)} | Write-Output")
