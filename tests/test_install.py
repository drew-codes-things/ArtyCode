import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from arty_cli import cli

PROJECT = Path(__file__).resolve().parents[1]
debian_installer = pytest.mark.skipif(sys.platform == "win32", reason="install.sh is the Debian installer")


def test_typing_arty_or_arty_cli_starts_the_same_program():
    scripts = tomllib.loads((PROJECT / "pyproject.toml").read_text())["project"]["scripts"]
    assert scripts["arty"] == scripts["arty-cli"] == "arty_cli.cli:main"
    module, function = scripts["arty"].split(":")
    assert module == cli.__name__ and callable(getattr(cli, function))


@debian_installer
def test_the_debian_installer_is_valid_bash():
    checked = subprocess.run(["bash", "-n", str(PROJECT / "install.sh")], capture_output=True, text=True)
    assert checked.returncode == 0, checked.stderr


@debian_installer
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


@debian_installer
def test_uninstall_never_deletes_a_folder_this_installer_did_not_make(tmp_path):
    precious = tmp_path / "local"
    precious.mkdir()
    (precious / "keep.txt").write_text("mine")
    (precious / "venv" / "bin").mkdir(parents=True)
    (precious / "venv" / "bin" / "arty-cli").write_text("")
    ran = uninstall_with_home(precious, tmp_path / "bin")
    assert ran.returncode == 1 and "not an arty-cli install" in ran.stderr
    assert (precious / "keep.txt").read_text() == "mine"


@debian_installer
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


@debian_installer
def test_the_debian_installer_refuses_plain_http_sources(tmp_path):
    environment = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "ARTY_CLI_HOME": str(tmp_path / "app"),
                   "ARTY_CLI_BIN": str(tmp_path / "bin"), "ARTY_CLI_SOURCE": "http://example.com/arty-cli.tar.gz"}
    ran = subprocess.run(["bash", str(PROJECT / "install.sh")], capture_output=True, text=True, env=environment)
    assert ran.returncode == 1 and "https" in ran.stderr
    assert not (tmp_path / "app").exists()


def test_the_windows_installer_refuses_plain_http_sources():
    script = (PROJECT / "install.ps1").read_text()
    assert "'^http://'" in script and "'^git\\+http://'" in script


FAKE_PYTHON = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_PYTHON_LOG"
if [ "${1:-}" = "-m" ] && [ "${2:-}" = "venv" ]; then
  mkdir -p "$3/bin"
  cp "$0" "$3/bin/python"
  printf '#!/usr/bin/env bash\\necho "ArtyCode 9.9.9"\\n' > "$3/bin/arty-cli"
  cp "$3/bin/arty-cli" "$3/bin/arty"
  chmod +x "$3/bin/python" "$3/bin/arty-cli" "$3/bin/arty"
fi
exit 0
"""


@debian_installer
def test_piped_from_the_web_the_installer_fetches_the_project_itself(tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    fake = tools / "python3.13"
    fake.write_text(FAKE_PYTHON)
    fake.chmod(0o755)
    log = tmp_path / "python.log"
    environment = {"PATH": f"{tools}:/usr/bin:/bin", "HOME": str(tmp_path / "home"), "FAKE_PYTHON_LOG": str(log),
                   "ARTY_CLI_HOME": str(tmp_path / "app"), "ARTY_CLI_BIN": str(tmp_path / "bin")}
    script = (PROJECT / "install.sh").read_text()
    ran = subprocess.run(["bash"], input=script, capture_output=True, text=True, env=environment, cwd=tmp_path)
    assert ran.returncode == 0, ran.stderr
    installed = [line for line in log.read_text().splitlines() if "pip install" in line and "arty-cli @" in line]
    assert installed and installed[0].endswith("arty-cli @ https://github.com/drew-codes-things/ArtyCode/archive/refs/heads/main.tar.gz")
    assert "Installed ArtyCode 9.9.9" in ran.stdout
    assert (tmp_path / "bin" / "arty").is_symlink()


def test_the_readme_gives_a_one_line_install_that_matches_the_installer():
    readme = (PROJECT / "README.md").read_text()
    script = (PROJECT / "install.sh").read_text()
    one_liner = "curl -fsSL https://raw.githubusercontent.com/drew-codes-things/ArtyCode/main/install.sh | bash"
    assert one_liner in readme
    assert "sudo" not in one_liner
    assert 'DEFAULT_SOURCE="https://github.com/drew-codes-things/ArtyCode/archive/refs/heads/main.tar.gz"' in script
    assert "arty doctor" in readme and "arty update" in readme


def test_the_interface_library_is_installed_with_it():
    project = tomllib.loads((PROJECT / "pyproject.toml").read_text())["project"]
    assert [name.split(">")[0] for name in project["dependencies"]] == ["prompt_toolkit"]
