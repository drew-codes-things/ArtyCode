import subprocess
import tomllib
from pathlib import Path

from arty_cli import cli

PROJECT = Path(__file__).resolve().parents[1]


def test_typing_arty_or_arty_cli_starts_the_same_program():
    scripts = tomllib.loads((PROJECT / "pyproject.toml").read_text())["project"]["scripts"]
    assert scripts["arty"] == scripts["arty-cli"] == "arty_cli.cli:main"
    module, function = scripts["arty"].split(":")
    assert module == cli.__name__ and callable(getattr(cli, function))


def test_the_debian_installer_is_valid_bash():
    checked = subprocess.run(["bash", "-n", str(PROJECT / "install.sh")], capture_output=True, text=True)
    assert checked.returncode == 0, checked.stderr


def test_the_debian_installer_refuses_unknown_options():
    ran = subprocess.run(["bash", str(PROJECT / "install.sh"), "--bogus"], capture_output=True, text=True)
    assert ran.returncode == 1
    assert "--uninstall" in ran.stderr


def test_the_windows_installer_makes_both_commands_and_can_uninstall():
    script = (PROJECT / "install.ps1").read_text()
    assert "$CommandNames = @('arty', 'arty-cli')" in script
    assert "[switch]$Uninstall" in script
    assert "DoNotExpandEnvironmentNames" in script


def uninstall_with_home(home, bin_folder):
    environment = {"PATH": "/usr/bin:/bin", "HOME": str(home.parent), "ARTY_CLI_HOME": str(home),
                   "ARTY_CLI_BIN": str(bin_folder)}
    return subprocess.run(["bash", str(PROJECT / "install.sh"), "--uninstall"], capture_output=True, text=True,
                          env=environment)


def test_uninstall_never_deletes_a_folder_this_installer_did_not_make(tmp_path):
    precious = tmp_path / "local"
    precious.mkdir()
    (precious / "keep.txt").write_text("mine")
    (precious / "venv" / "bin").mkdir(parents=True)
    (precious / "venv" / "bin" / "arty-cli").write_text("")
    ran = uninstall_with_home(precious, tmp_path / "bin")
    assert ran.returncode == 1 and "not an arty-cli install" in ran.stderr
    assert (precious / "keep.txt").read_text() == "mine"


def test_uninstall_removes_a_marked_install_or_an_older_one_with_only_its_venv(tmp_path):
    for name, marked in (("marked", True), ("older", False)):
        home = tmp_path / name
        (home / "venv" / "bin").mkdir(parents=True)
        (home / "venv" / "bin" / "arty-cli").write_text("")
        if marked:
            (home / ".arty-cli-install").write_text("")
        ran = uninstall_with_home(home, tmp_path / "bin")
        assert ran.returncode == 0, ran.stderr
        assert not home.exists()


def test_the_installers_refuse_plain_http_sources(tmp_path):
    environment = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "ARTY_CLI_HOME": str(tmp_path / "app"),
                   "ARTY_CLI_BIN": str(tmp_path / "bin"), "ARTY_CLI_SOURCE": "http://example.com/arty-cli.tar.gz"}
    ran = subprocess.run(["bash", str(PROJECT / "install.sh")], capture_output=True, text=True, env=environment)
    assert ran.returncode == 1 and "https" in ran.stderr
    assert not (tmp_path / "app").exists()
    script = (PROJECT / "install.ps1").read_text()
    assert "'^http://'" in script and "'^git\\+http://'" in script
