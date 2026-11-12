import io
import subprocess
from pathlib import Path

import pytest

from arty_cli import __version__, update

OLD, NEW = "a" * 40, "b" * 40


class Git:
    def __init__(self, **answers):
        self.answers = {"status --porcelain": "", "symbolic-ref --short refs/remotes/origin/HEAD": "origin/main",
                        "rev-parse --abbrev-ref HEAD": "main", "rev-list --count HEAD..origin/main": "3",
                        "rev-parse HEAD": OLD, "show origin/main:arty_cli/__init__.py": '__version__ = "0.2.0"\n',
                        **answers}
        self.ran = []
        self.merged = False

    def __call__(self, command, **options):
        self.ran.append(list(command))
        said = " ".join(command[3:]) if command[0] == "git" else " ".join(command)
        if command[0] == "git" and command[3] == "merge":
            self.merged = self.answers.get("merge", 0) == 0
            return subprocess.CompletedProcess(command, self.answers.get("merge", 0), "", "not possible to fast-forward")
        if said == "rev-parse HEAD" and self.merged:
            return subprocess.CompletedProcess(command, 0, NEW + "\n", "")
        if said in self.answers:
            answer = self.answers[said]
            code = answer if isinstance(answer, int) else 0
            return subprocess.CompletedProcess(command, code, "" if isinstance(answer, int) else answer + "\n", "failed")
        return subprocess.CompletedProcess(command, 0, "", "")

    def said(self):
        return [" ".join(command) for command in self.ran]


@pytest.fixture
def clone(tmp_path):
    (tmp_path / "ArtyCode" / ".git").mkdir(parents=True)
    (tmp_path / "ArtyCode" / "pyproject.toml").write_text("[project]\nname = 'arty-cli'\n")
    (tmp_path / "ArtyCode" / "arty_cli").mkdir()
    return tmp_path / "ArtyCode"


def world_for(clone, git, **changes):
    fields = {"package_dir": clone / "arty_cli", "prefix": str(clone / ".venv"), "executable": "/venv/bin/python",
              "run": git, "read_url": lambda url: '__version__ = "0.2.0"\n', "platform": "linux", **changes}
    return update.World(**fields)


def updated(world, *arguments):
    out = io.StringIO()
    return update.main(list(arguments), out=out, world=world), out.getvalue()


def test_a_clean_clone_on_its_default_branch_is_fetched_fast_forwarded_and_reinstalled(clone):
    git = Git()
    code, shown = updated(world_for(clone, git))
    said = git.said()
    assert code == 0
    assert f"git -C {clone} fetch origin" in said
    assert f"git -C {clone} merge --ff-only origin/main" in said
    assert said[-1] == f"/venv/bin/python -m pip install --quiet -e {clone}"
    assert said.index(f"git -C {clone} fetch origin") < said.index(f"git -C {clone} merge --ff-only origin/main")
    assert __version__ in shown and "git checkout" in shown and str(clone) in shown
    assert "aaaaaaa" in shown and "bbbbbbb" in shown and "3 commits" in shown and "reinstalled" in shown
    assert not any("sudo" in command for command in said)


def test_a_clone_with_uncommitted_changes_is_left_alone(clone):
    git = Git(**{"status --porcelain": " M arty_cli/cli.py"})
    code, shown = updated(world_for(clone, git))
    assert code == 1
    assert "uncommitted" in shown and "nothing was changed" in shown
    assert not any("fetch" in command or "merge" in command or "pip" in command for command in git.said())


def test_a_clone_on_another_branch_is_left_alone(clone):
    git = Git(**{"rev-parse --abbrev-ref HEAD": "tui"})
    code, shown = updated(world_for(clone, git))
    assert code == 1
    assert "tui" in shown and "main" in shown
    assert not any("merge" in command or "pip" in command for command in git.said())


def test_a_clone_that_is_already_up_to_date_says_so_and_does_not_reinstall(clone):
    git = Git(**{"rev-list --count HEAD..origin/main": "0"})
    code, shown = updated(world_for(clone, git))
    assert code == 0 and "up to date" in shown
    assert not any("merge" in command or "pip" in command for command in git.said())


def test_a_clone_that_cannot_fast_forward_is_reported_and_not_reinstalled(clone):
    git = Git(merge=1)
    code, shown = updated(world_for(clone, git))
    assert code == 1 and "fast-forward" in shown
    assert not any("pip" in command for command in git.said())


def test_a_fetch_that_fails_is_reported_plainly(clone):
    git = Git(**{"fetch origin": 1})
    code, shown = updated(world_for(clone, git))
    assert code == 1 and "fetch" in shown
    assert not any("merge" in command for command in git.said())


def test_check_on_a_clone_only_fetches_and_counts(clone):
    git = Git()
    code, shown = updated(world_for(clone, git), "--check")
    assert code == 0
    assert "3 commits" in shown and "arty update" in shown
    assert not any("merge" in command or "pip" in command for command in git.said())


@pytest.mark.parametrize("prefix, marker, word, command", [
    ("/home/sam/.local/share/arty-cli/venv", True, "installer", "/venv/bin/python -m pip install --upgrade"),
    ("/home/sam/.local/pipx/venvs/arty-cli", False, "pipx", "pipx upgrade arty-cli"),
    ("/home/sam/project/.venv", False, "pip", "/venv/bin/python -m pip install --upgrade"),
])
def test_other_installs_get_the_one_command_to_run(tmp_path, prefix, marker, word, command):
    home = tmp_path / prefix.lstrip("/")
    package = home / "lib" / "python3.13" / "site-packages" / "arty_cli"
    package.mkdir(parents=True)
    if marker:
        (home.parent / update.INSTALL_MARKER).write_text("")
    git = Git()
    code, shown = updated(update.World(package_dir=package, prefix=str(home), executable="/venv/bin/python", run=git,
                                       read_url=lambda url: '__version__ = "0.2.0"\n', platform="linux"))
    assert code == 0
    assert word in shown and command in shown and __version__ in shown
    assert git.ran == []
    assert "sudo" not in shown


def test_check_reads_the_newest_version_and_says_when_one_is_out(tmp_path):
    package = tmp_path / "site-packages" / "arty_cli"
    package.mkdir(parents=True)
    asked = []

    def read_url(url):
        asked.append(url)
        return '__version__ = "9.0.0"\n'
    world = update.World(package_dir=package, prefix=str(tmp_path), executable="python", run=Git(), read_url=read_url,
                         platform="linux")
    code, shown = updated(world, "--check")
    assert code == 0 and "9.0.0" in shown and __version__ in shown
    assert asked == [update.VERSION_URL] and asked[0].startswith("https://")


def test_check_says_when_this_is_the_newest_and_when_it_cannot_tell(tmp_path):
    package = tmp_path / "site-packages" / "arty_cli"
    package.mkdir(parents=True)
    same = update.World(package_dir=package, prefix=str(tmp_path), executable="python", run=Git(),
                        read_url=lambda url: f'__version__ = "{__version__}"\n', platform="linux")
    assert "newest" in updated(same, "--check")[1]

    def offline(url):
        raise OSError("no network")
    code, shown = updated(update.World(package_dir=package, prefix=str(tmp_path), executable="python", run=Git(),
                                       read_url=offline, platform="linux"), "--check")
    assert code == 1 and "Couldn't check" in shown


def test_versions_compare_by_number_not_by_text():
    assert update.is_newer("0.10.0", "0.9.9") and not update.is_newer("0.1.0", "0.1.0")
    assert not update.is_newer("not a version", "0.1.0")


def test_unknown_options_are_refused_with_the_usage():
    out = io.StringIO()
    assert update.main(["--force"], out=out, world=update.World(package_dir=Path("/x/arty_cli"))) == 2
    assert "arty update [--check]" in out.getvalue()
