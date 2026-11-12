import re
import subprocess
import sys
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from arty_cli import __version__

PROJECT_URL = "https://github.com/drew-codes-things/ArtyCode"
ARCHIVE_URL = f"{PROJECT_URL}/archive/refs/heads/main.tar.gz"
VERSION_URL = "https://raw.githubusercontent.com/drew-codes-things/ArtyCode/main/arty_cli/__init__.py"
VERSION_FILE = "arty_cli/__init__.py"
INSTALL_MARKER = ".arty-cli-install"
CLONE, INSTALLER, PIPX, PIP = "git checkout", "installer", "pipx", "pip"
FALLBACK_BRANCH = "origin/main"
CHECK = "--check"
USAGE = "Use: arty update [--check]"
EXIT_GOOD, EXIT_FAILED, EXIT_USAGE = 0, 1, 2
VERSION_LINE = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']', re.M)
VERSION_NUMBER = re.compile(r"\d+(?:\.\d+)*")
READ_SECONDS = 15
GIT_SECONDS = 120
INSTALL_SECONDS = 600
SHORT_COMMIT = 7


def read_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": f"arty-cli/{__version__}"})
    with urllib.request.urlopen(request, timeout=READ_SECONDS) as response:
        return response.read().decode("utf-8", errors="replace")


@dataclass(frozen=True)
class World:
    package_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parent)
    prefix: str = sys.prefix
    executable: str = sys.executable
    run: Callable = subprocess.run
    read_url: Callable = read_text
    platform: str = sys.platform


class UpdateStopped(Exception):
    pass


def install_kind(world: World) -> str:
    project = world.package_dir.parent
    if (project / ".git").exists() and (project / "pyproject.toml").is_file():
        return CLONE
    prefix = Path(world.prefix)
    if (prefix.parent / INSTALL_MARKER).is_file():
        return INSTALLER
    return PIPX if PIPX in prefix.parts else PIP


def version_in(text: str) -> str:
    found = VERSION_LINE.search(text)
    return found.group(1) if found else ""


def numbers(version: str) -> tuple[int, ...]:
    found = VERSION_NUMBER.match(version.strip())
    return tuple(int(part) for part in found.group(0).split(".")) if found else ()


def is_newer(latest: str, current: str) -> bool:
    return bool(numbers(latest)) and numbers(latest) > numbers(current)


def upgrade_command(kind: str, world: World) -> str:
    if kind == PIPX:
        return "pipx upgrade arty-cli"
    return f'{world.executable} -m pip install --upgrade "arty-cli @ {ARCHIVE_URL}"'


def how_installed(kind: str, world: World) -> str:
    where = world.package_dir.parent if kind == CLONE else world.prefix
    names = {CLONE: f"from a {CLONE}", INSTALLER: f"by the {INSTALLER} (install.sh or install.ps1)",
             PIPX: f"with {PIPX}", PIP: f"with {PIP}"}
    return f"ArtyCode {__version__}, installed {names[kind]} in {where}."


class Clone:
    def __init__(self, folder: Path, world: World):
        self.folder = folder
        self.world = world

    def git(self, *arguments: str, seconds: int = GIT_SECONDS):
        command = ["git", "-C", str(self.folder), *arguments]
        return self.world.run(command, capture_output=True, text=True, timeout=seconds)

    def answer(self, *arguments: str) -> str:
        finished = self.git(*arguments)
        if finished.returncode != 0:
            raise UpdateStopped(f"git {' '.join(arguments)} failed in {self.folder}: "
                                f"{(finished.stderr or '').strip() or finished.returncode}. Nothing was changed.")
        return finished.stdout.strip()

    def default_branch(self) -> str:
        finished = self.git("symbolic-ref", "--short", "refs/remotes/origin/HEAD")
        return finished.stdout.strip() if finished.returncode == 0 and finished.stdout.strip() else FALLBACK_BRANCH

    def fetched_behind(self, upstream: str) -> int:
        self.answer("fetch", "origin")
        return int(self.answer("rev-list", "--count", f"HEAD..{upstream}") or 0)

    def ready_to_move(self, upstream: str) -> None:
        if self.answer("status", "--porcelain"):
            raise UpdateStopped(f"The clone at {self.folder} has uncommitted changes, so nothing was changed. "
                                "Commit or stash them, then run arty update again.")
        branch, default = self.answer("rev-parse", "--abbrev-ref", "HEAD"), upstream.split("/", 1)[-1]
        if branch != default:
            raise UpdateStopped(f"The clone at {self.folder} is on {branch}, not {default}, so nothing was changed. "
                                f"Switch to {default} (git switch {default}), then run arty update again.")

    def update(self) -> list[str]:
        upstream = self.default_branch()
        self.ready_to_move(upstream)
        before = self.answer("rev-parse", "HEAD")
        behind = self.fetched_behind(upstream)
        if not behind:
            return [f"Fetched origin: {self.folder} is already up to date at {before[:SHORT_COMMIT]}. "
                    "Nothing else was done."]
        if self.git("merge", "--ff-only", upstream).returncode != 0:
            raise UpdateStopped(f"Fetched origin, but {self.folder} has commits of its own, so git could not "
                                f"fast-forward to {upstream}. The files were left as they were.")
        after = self.answer("rev-parse", "HEAD")
        install = [self.world.executable, "-m", "pip", "install", "--quiet", "-e", str(self.folder)]
        moved = (f"Fetched origin and moved {upstream.split('/', 1)[-1]} from {before[:SHORT_COMMIT]} to "
                 f"{after[:SHORT_COMMIT]} ({behind} commit{'' if behind == 1 else 's'}) in {self.folder}")
        finished = self.world.run(install, capture_output=True, text=True, timeout=INSTALL_SECONDS)
        if finished.returncode != 0:
            raise UpdateStopped(f"{moved}, but reinstalling failed. Run it yourself: {' '.join(install)}")
        return [f"{moved}, then reinstalled it with: {' '.join(install)}"]

    def check(self) -> list[str]:
        upstream = self.default_branch()
        behind = self.fetched_behind(upstream)
        if not behind:
            return [f"You have the newest ArtyCode ({self.folder} is up to date with {upstream})."]
        newest = version_in(self.git("show", f"{upstream}:{VERSION_FILE}").stdout or "")
        named = f" (version {newest})" if newest else ""
        return [f"A newer ArtyCode is out{named}: {self.folder} is {behind} commit{'' if behind == 1 else 's'} behind "
                f"{upstream}. Run `arty update` to get it."]


def check_release(world: World, kind: str) -> list[str]:
    try:
        latest = version_in(world.read_url(VERSION_URL))
    except (OSError, ValueError) as error:
        raise UpdateStopped(f"Couldn't check for a newer version ({error}). Try again when this computer is online.")
    if not latest:
        raise UpdateStopped("Couldn't check for a newer version: the project's version could not be read.")
    if is_newer(latest, __version__):
        return [f"A newer ArtyCode is out: {latest} (you have {__version__}). To get it, run:",
                f"  {upgrade_command(kind, world)}"]
    return [f"You have the newest ArtyCode ({__version__})."]


def steps(world: World, kind: str, only_check: bool) -> list[str]:
    if kind == CLONE:
        clone = Clone(world.package_dir.parent, world)
        return clone.check() if only_check else clone.update()
    if only_check:
        return check_release(world, kind)
    return ["ArtyCode doesn't change this kind of install itself. To update, run:", f"  {upgrade_command(kind, world)}"]


def main(argv: list[str], out=None, world: World | None = None) -> int:
    out = out or sys.stdout
    if any(argument != CHECK for argument in argv):
        out.write(f"{USAGE}\n")
        return EXIT_USAGE
    world = world or World()
    kind = install_kind(world)
    out.write(f"{how_installed(kind, world)}\n")
    try:
        said = steps(world, kind, only_check=CHECK in argv)
    except UpdateStopped as stopped:
        out.write(f"{stopped}\n")
        return EXIT_FAILED
    except (OSError, subprocess.SubprocessError) as error:
        out.write(f"Couldn't run the update ({error}). Nothing more was done.\n")
        return EXIT_FAILED
    for line in said:
        out.write(f"{line}\n")
    return EXIT_GOOD
