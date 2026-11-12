import json
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

from arty_cli import sandbox, tools

needs_bwrap = pytest.mark.skipif(not sandbox.linux_sandbox_works(), reason="bubblewrap can't run here")


def settings_file(folder, table):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "settings.json").write_text(json.dumps(table))


def test_the_linux_wrapper_keeps_the_system_read_only_and_the_project_writable(tmp_path):
    arguments = sandbox.linux_arguments("/usr/bin/bwrap", tmp_path, "make test", network=False, hidden=[])
    assert arguments[:3] == ["/usr/bin/bwrap", "--ro-bind", "/"]
    assert ["--bind", str(tmp_path), str(tmp_path)] == arguments[arguments.index("--bind"):arguments.index("--bind") + 3]
    assert "--unshare-net" in arguments and "--die-with-parent" in arguments
    assert arguments[-3:] == ["/bin/bash", "-c", "make test"]


def test_network_is_left_alone_only_when_allowed(tmp_path):
    arguments = sandbox.linux_arguments("/usr/bin/bwrap", tmp_path, "true", network=True, hidden=[])
    assert "--unshare-net" not in arguments


def test_secret_folders_and_files_are_hidden(tmp_path):
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".netrc").write_text("machine x password y\n")
    hidden = sandbox.hidden_paths(tmp_path)
    arguments = sandbox.linux_arguments("/usr/bin/bwrap", tmp_path / "project", "true", network=False, hidden=hidden)
    assert ["--tmpfs", str(tmp_path / ".ssh")] == arguments[arguments.index(str(tmp_path / ".ssh")) - 1:][:2]
    assert ["--ro-bind", "/dev/null", str(tmp_path / ".netrc")] == arguments[arguments.index(str(tmp_path / ".netrc")) - 2:][:3]


def test_the_arty_cli_config_folder_is_among_the_hidden_paths(tmp_path):
    (tmp_path / ".config" / "arty-cli").mkdir(parents=True)
    assert tmp_path / ".config" / "arty-cli" in sandbox.hidden_paths(tmp_path)


def test_the_sandbox_is_off_unless_switched_on(tmp_path):
    choice = sandbox.chosen(tmp_path / "personal", tmp_path / "project", include_project=True, asked=False)
    assert choice.mode == sandbox.OFF


def test_your_settings_switch_it_on_and_allow_network(tmp_path):
    settings_file(tmp_path / "personal", {"sandbox": True, "sandboxNetwork": True})
    choice = sandbox.chosen(tmp_path / "personal", tmp_path / "project", include_project=True, asked=False)
    assert choice.mode == sandbox.ON and choice.network


def test_a_project_can_switch_it_on_but_never_off_or_open_the_network(tmp_path):
    settings_file(tmp_path / "project" / ".arty", {"sandbox": "required", "sandboxNetwork": True})
    choice = sandbox.chosen(tmp_path / "personal", tmp_path / "project", include_project=True, asked=False)
    assert choice.mode == sandbox.REQUIRED and not choice.network
    settings_file(tmp_path / "personal", {"sandbox": "required"})
    settings_file(tmp_path / "project" / ".arty", {"sandbox": False})
    assert sandbox.chosen(tmp_path / "personal", tmp_path / "project", include_project=True, asked=False).mode == sandbox.REQUIRED


def test_the_command_line_flag_switches_it_on(tmp_path):
    assert sandbox.chosen(tmp_path / "p", tmp_path / "q", include_project=False, asked=True).mode == sandbox.ON


def test_without_a_working_sandbox_on_falls_back_with_a_note_and_required_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "platform_problem", lambda root: "bwrap isn't installed")
    fallback = sandbox.Sandbox(tmp_path, sandbox.Choice(sandbox.ON, False))
    assert "without the sandbox" in fallback.problem and "bwrap isn't installed" in fallback.problem
    assert fallback.wrapped("true") is None
    refusing = sandbox.Sandbox(tmp_path, sandbox.Choice(sandbox.REQUIRED, False))
    assert "refused" in refusing.problem
    with pytest.raises(tools.ToolError, match="sandbox"):
        refusing.wrapped("true")
    workspace = tools.Workspace(tmp_path)
    workspace.sandbox = refusing
    with pytest.raises(tools.ToolError, match="required"):
        tools.run_command(workspace, "echo hi")


def test_when_switched_off_nothing_is_probed(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "platform_problem", lambda root: pytest.fail("probed"))
    off = sandbox.Sandbox(tmp_path, sandbox.Choice(sandbox.OFF, False))
    assert off.problem == "" and off.wrapped("true") is None


@needs_bwrap
def test_a_sandboxed_command_can_write_in_the_project_but_not_in_your_home(tmp_path):
    cache = Path.home() / ".cache"
    cache.mkdir(exist_ok=True)
    outside = Path(tempfile.mkdtemp(prefix="arty-sandbox-test-", dir=cache))
    try:
        workspace = tools.Workspace(tmp_path)
        workspace.sandbox = sandbox.Sandbox(tmp_path, sandbox.Choice(sandbox.ON, False))
        result = tools.run_command(workspace, f"echo inside > made.txt; echo outside > {outside}/escaped.txt")
        assert (tmp_path / "made.txt").read_text() == "inside\n"
        assert not (outside / "escaped.txt").exists()
        assert "Read-only file system" in result
    finally:
        shutil.rmtree(outside)


@needs_bwrap
def test_a_sandboxed_command_cannot_read_hidden_secrets(tmp_path):
    cache = Path.home() / ".cache"
    cache.mkdir(exist_ok=True)
    home = Path(tempfile.mkdtemp(prefix="arty-sandbox-test-", dir=cache))
    try:
        (home / ".ssh").mkdir()
        (home / ".ssh" / "id_ed25519").write_text("SECRET-KEY\n")
        (home / ".netrc").write_text("SECRET-NETRC\n")
        workspace = tools.Workspace(tmp_path)
        workspace.sandbox = sandbox.Sandbox(tmp_path, sandbox.Choice(sandbox.ON, False), home=home)
        result = tools.run_command(workspace, f"cat {home}/.ssh/id_ed25519 {home}/.netrc; ls -a {home}")
        assert "SECRET" not in result
        assert ".ssh" in result
    finally:
        shutil.rmtree(home)


@needs_bwrap
def test_a_sandboxed_command_has_no_network(tmp_path):
    workspace = tools.Workspace(tmp_path)
    workspace.sandbox = sandbox.Sandbox(tmp_path, sandbox.Choice(sandbox.ON, False), home=tmp_path / "home")
    probe = f"{sys.executable} -c \"import socket; socket.create_connection(('192.0.2.1', 9), timeout=2)\""
    result = tools.run_command(workspace, probe)
    assert "unreachable" in result.lower()


@needs_bwrap
def test_stopping_a_sandboxed_background_job_ends_it(tmp_path):
    workspace = tools.Workspace(tmp_path)
    workspace.sandbox = sandbox.Sandbox(tmp_path, sandbox.Choice(sandbox.ON, False), home=tmp_path / "home")
    tools.run_command(workspace, "sleep 60", background=True)
    assert workspace.jobs["1"].process.poll() is None
    tools.stop_command(workspace, "1")
    assert workspace.jobs["1"].process.poll() is not None


def test_without_a_sandbox_commands_run_as_before(tmp_path):
    workspace = tools.Workspace(tmp_path)
    assert "[exit code 0]" in tools.run_command(workspace, "echo hi")


@needs_bwrap
def test_secrets_stay_hidden_when_the_project_folder_holds_them(tmp_path):
    cache = Path.home() / ".cache"
    cache.mkdir(exist_ok=True)
    home = Path(tempfile.mkdtemp(prefix="arty-sandbox-test-", dir=cache))
    try:
        (home / ".ssh").mkdir()
        (home / ".ssh" / "id_ed25519").write_text("SECRET-KEY\n")
        workspace = tools.Workspace(home)
        workspace.sandbox = sandbox.Sandbox(home, sandbox.Choice(sandbox.ON, False), home=home)
        result = tools.run_command(workspace, "cat .ssh/id_ed25519; echo replaced > .ssh/id_ed25519")
        assert "SECRET" not in result
        assert (home / ".ssh" / "id_ed25519").read_text() == "SECRET-KEY\n"
    finally:
        shutil.rmtree(home)
