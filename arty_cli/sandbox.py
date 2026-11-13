import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from arty_cli import sandbox_windows, switches, tools

SWITCH = "sandbox"
NETWORK_SWITCH = "sandboxNetwork"
OFF, ON, REQUIRED = "off", "on", "required"
STRENGTH = {OFF: 0, ON: 1, REQUIRED: 2}
PROBE_SECONDS = 10
LINUX_SHELL = "/bin/bash"
SECRET_FOLDERS = (".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".config/arty-cli", ".config/Arty",
                  ".config/gh", ".password-store", ".local/share/keyrings")
SECRET_FILES = (".netrc", ".git-credentials", ".npmrc", ".pypirc", ".arty-android.properties")
GIT_FOLDER = ".git"
PROTECTED_GIT_PARTS = ("config", "config.worktree", "hooks", "info")
ARTY_FOLDER = ".arty"
LINUX_ISOLATION = ["--unshare-pid", "--unshare-ipc", "--unshare-uts", "--die-with-parent", "--new-session"]
SESSION_VARIABLES = ("SSH_AUTH_SOCK", "SSH_AGENT_PID", "DBUS_SESSION_BUS_ADDRESS", "GPG_AGENT_INFO")
SECRET_NAME = re.compile(r"(^|_)(KEYS?|TOKENS?|SECRETS?|PASSWORD|PASSWD|PASSPHRASE|CREDENTIALS?|AUTH|COOKIE)(_|$)"
                         r"|(KEY|TOKEN|SECRET|PASSWORD)$")
BUS_PATH_PREFIX = "unix:path="
FALLBACK_NOTE = ("The sandbox is switched on but can't run here ({reason}), so commands run without the sandbox, "
                 "behind the usual approvals.")
REFUSED_NOTE = ("The sandbox is set to required but can't run here ({reason}), so commands are refused. "
                "Set \"sandbox\": true to fall back, or fix the reason.")


@dataclass(frozen=True)
class Choice:
    mode: str
    network: bool


def mode_of(value) -> str:
    if value is True:
        return ON
    return REQUIRED if value == REQUIRED else OFF


def chosen(personal_folder: Path, project_root: Path, include_project: bool, asked: bool) -> Choice:
    personal_file = Path(personal_folder) / switches.SETTINGS_FILE
    modes = [mode_of(switches.value_in(personal_file, SWITCH)), ON if asked else OFF]
    if include_project:
        modes.append(mode_of(switches.value_in(Path(project_root) / switches.PROJECT_SETTINGS, SWITCH)))
    network = switches.value_in(personal_file, NETWORK_SWITCH) is True
    return Choice(max(modes, key=STRENGTH.get), network)


def hidden_paths(home: Path, extra: tuple = ()) -> list[Path]:
    home = Path(home)
    candidates = [home / name for name in SECRET_FOLDERS + SECRET_FILES] + [Path(path) for path in extra]
    return list(dict.fromkeys(path for path in candidates if path.exists()))


def is_secret_name(name: str) -> bool:
    return bool(SECRET_NAME.search(name.upper()))


def sandbox_environment(environ) -> dict:
    return {name: value for name, value in environ.items()
            if name not in SESSION_VARIABLES and not is_secret_name(name)}


def bus_paths(address: str) -> list[Path]:
    return [Path(part.split(",")[0][len(BUS_PATH_PREFIX):]) for part in address.split(";")
            if part.startswith(BUS_PATH_PREFIX)]


def session_paths(environ) -> list[Path]:
    runtime = Path(environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
    agent = [Path(environ["SSH_AUTH_SOCK"])] if environ.get("SSH_AUTH_SOCK") else []
    candidates = [runtime, *agent, *bus_paths(environ.get("DBUS_SESSION_BUS_ADDRESS", ""))]
    found = [path for path in dict.fromkeys(candidates) if path.exists()]
    return [path for path in found if not any(path != other and path.is_relative_to(other) for other in found)]


def protected_places(root: Path) -> list[Path]:
    git = Path(root) / GIT_FOLDER
    candidates = [git] if git.is_file() else [git / part for part in PROTECTED_GIT_PARTS]
    candidates.append(Path(root) / ARTY_FOLDER)
    return [path for path in candidates if path.exists() and not path.is_symlink()]


def linux_arguments(bwrap: str, root: Path, command: str, network: bool, hidden: list[Path]) -> list[str]:
    arguments = [bwrap, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
    arguments += ["--bind", str(root), str(root)]
    for place in protected_places(root):
        arguments += ["--ro-bind", str(place), str(place)]
    for path in hidden:
        arguments += ["--tmpfs", str(path)] if Path(path).is_dir() else ["--ro-bind", "/dev/null", str(path)]
    arguments += LINUX_ISOLATION + ([] if network else ["--unshare-net"])
    return arguments + ["--chdir", str(root), LINUX_SHELL, "-c", command]


def linux_problem(root: Path) -> str:
    bwrap = shutil.which("bwrap")
    if not bwrap:
        return "bubblewrap (bwrap) isn't installed"
    probe = linux_arguments(bwrap, root, "true", network=False, hidden=[])
    try:
        finished = subprocess.run(probe, capture_output=True, text=True, timeout=PROBE_SECONDS, errors="replace")
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"bwrap didn't start: {error}"
    if finished.returncode != 0:
        return f"bwrap failed: {(finished.stderr.strip() or f'exit code {finished.returncode}')[:200]}"
    return ""


def linux_sandbox_works(root: Path = Path.home()) -> bool:
    return sys.platform.startswith("linux") and not linux_problem(root)


def windows_problem() -> str:
    return sandbox_windows.problem()


def windows_sandbox_works() -> bool:
    return sys.platform == "win32" and not windows_problem()


def platform_problem(root: Path) -> str:
    if sys.platform == "win32":
        return windows_problem()
    if sys.platform.startswith("linux"):
        return linux_problem(root)
    return f"there is no sandbox for {sys.platform} yet"


class Sandbox:
    def __init__(self, project_root, choice: Choice, home=None, secret_paths: tuple = ()):
        self.root = Path(project_root).resolve()
        self.choice = choice
        self.home = Path(home) if home else Path.home()
        self.secret_paths = tuple(secret_paths)
        self.reason = platform_problem(self.root) if choice.mode != OFF else ""
        if choice.mode != OFF and not self.reason and sys.platform == "win32":
            self.reason = sandbox_windows.prepared(self.root, self.home)
        self.active = choice.mode != OFF and not self.reason
        self.problem = self.note()

    def note(self) -> str:
        if self.choice.mode == OFF or self.active:
            return ""
        template = REFUSED_NOTE if self.choice.mode == REQUIRED else FALLBACK_NOTE
        return template.format(reason=self.reason)

    def describe(self) -> str:
        if not self.active:
            return ""
        network = "network allowed" if self.choice.network else "no network"
        return f"Commands run in a sandbox: the project folder is writable, the rest is read-only, {network}."

    def wrapped(self, command: str) -> list[str] | None:
        if not self.active:
            if self.choice.mode == REQUIRED:
                raise tools.ToolError(self.problem)
            return None
        hidden = hidden_paths(self.home, self.secret_paths + tuple(session_paths(os.environ)))
        return linux_arguments(shutil.which("bwrap"), self.root, command, self.choice.network, hidden)

    def start(self, command: str, **options):
        if sys.platform == "win32" and self.active:
            return sandbox_windows.start(self.root, command, self.choice.network)
        arguments = self.wrapped(command)
        if arguments is None:
            return None
        return subprocess.Popen(arguments, start_new_session=True, env=sandbox_environment(os.environ), **options)
