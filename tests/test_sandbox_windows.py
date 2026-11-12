import sys
import uuid
from pathlib import Path

import pytest

from arty_cli import sandbox, sandbox_windows, tools

on_windows = pytest.mark.skipif(sys.platform != "win32", reason="the AppContainer sandbox is Windows only")
REACH_THE_INTERNET = ("try { $c = New-Object Net.Sockets.TcpClient; $c.Connect('1.1.1.1', 443); 'connected' } "
                      "catch { 'blocked: ' + $_.Exception.Message }")


def sandboxed_workspace(root, network=False):
    workspace = tools.Workspace(root)
    workspace.sandbox = sandbox.Sandbox(root, sandbox.Choice(sandbox.ON, network))
    assert workspace.sandbox.active, workspace.sandbox.problem
    return workspace


def test_the_environment_points_temporary_folders_at_the_sandboxes_own_folder():
    block = sandbox_windows.environment_block({"PATH": "C:\\Windows", "TEMP": "C:\\Users\\me\\Temp"}, "C:\\box")
    assert block.endswith("\0\0")
    entries = block.rstrip("\0").split("\0")
    assert "TEMP=C:\\box" in entries and "TMP=C:\\box" in entries and "PATH=C:\\Windows" in entries
    assert not any(entry.startswith("TEMP=C:\\Users") for entry in entries)


def test_the_command_goes_through_powershell_as_one_argument():
    line = sandbox_windows.command_line(Path("C:/code/shop"), 'echo "hi there"')
    assert "-NonInteractive" in line and "-Command" in line and 'echo \\"hi there\\"' in line


def test_the_command_starts_in_a_project_drive_because_the_folders_above_it_are_hidden():
    script = sandbox_windows.in_project_drive(Path("C:/Users/o'neil/shop"), "Get-ChildItem")
    assert "-Root 'C:" in script and "o''neil" in script
    assert script.endswith("Set-Location Project:\\; Get-ChildItem")


def test_the_sandbox_is_never_given_your_whole_user_folder(tmp_path):
    home = tmp_path / "Users" / "me"
    assert "whole user folder" in sandbox_windows.too_broad(home, home)
    assert "whole user folder" in sandbox_windows.too_broad(tmp_path, home)
    assert sandbox_windows.too_broad(home / "code" / "shop", home) == ""


def test_away_from_windows_the_windows_sandbox_says_why_it_cannot_run():
    if sys.platform == "win32":
        pytest.skip("this checks the message on other systems")
    assert "only runs on Windows" in sandbox_windows.problem()


@on_windows
def test_a_sandboxed_command_can_write_in_the_project_but_not_in_your_home(tmp_path):
    escaped = Path.home() / f"arty-sandbox-escape-{uuid.uuid4().hex}.txt"
    workspace = sandboxed_workspace(tmp_path)
    result = tools.run_command(workspace, f"Set-Content made.txt inside; Set-Content '{escaped}' outside; 'done'")
    try:
        assert (tmp_path / "made.txt").read_text().strip() == "inside"
        assert not escaped.exists()
        assert "denied" in result.lower()
    finally:
        escaped.unlink(missing_ok=True)


@on_windows
def test_a_sandboxed_command_cannot_read_your_files(tmp_path):
    secret = Path.home() / f"arty-sandbox-secret-{uuid.uuid4().hex}.txt"
    secret.write_text("SECRET-VALUE")
    try:
        result = tools.run_command(sandboxed_workspace(tmp_path), f"Get-Content '{secret}'")
        assert "SECRET-VALUE" not in result
    finally:
        secret.unlink()


@on_windows
def test_a_sandboxed_command_has_no_network_unless_allowed(tmp_path):
    assert "blocked" in tools.run_command(sandboxed_workspace(tmp_path), REACH_THE_INTERNET)
    assert "connected" in tools.run_command(sandboxed_workspace(tmp_path, network=True), REACH_THE_INTERNET)


@on_windows
def test_a_sandboxed_command_reports_its_exit_code(tmp_path):
    assert "[exit code 3]" in tools.run_command(sandboxed_workspace(tmp_path), "exit 3")


@on_windows
def test_a_sandboxed_program_passes_its_own_exit_code_back(tmp_path):
    assert "[exit code 3]" in tools.run_command(sandboxed_workspace(tmp_path), "cmd /c exit 3")


@on_windows
def test_a_sandboxed_command_that_runs_too_long_is_stopped(tmp_path):
    result = tools.run_command(sandboxed_workspace(tmp_path), "'started'; Start-Sleep 30", timeout_seconds=2)
    assert "started" in result and "stopped after 2 s" in result


@on_windows
def test_stopping_a_sandboxed_background_job_ends_it(tmp_path):
    workspace = sandboxed_workspace(tmp_path)
    tools.run_command(workspace, "'up'; Start-Sleep 60", background=True)
    assert workspace.jobs["1"].process.poll() is None
    tools.stop_command(workspace, "1")
    assert workspace.jobs["1"].process.poll() is not None
