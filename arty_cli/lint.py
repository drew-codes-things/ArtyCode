import configparser
import json
import shutil
import subprocess
from pathlib import Path

from arty_cli import switches

SWITCH = "lintAfterEdits"
DEFAULT_TIMEOUT_SECONDS = 15
MAX_NOTE_CHARS = 1500
PYTHON_SUFFIXES = (".py",)
SCRIPT_SUFFIXES = (".js", ".mjs", ".cjs")
SHELL_SUFFIXES = (".sh", ".bash")
JSON_SUFFIXES = (".json",)
RUFF_FILES = ("ruff.toml", ".ruff.toml")
FLAKE8_FILES = (".flake8",)
FLAKE8_SECTION_FILES = ("setup.cfg", "tox.ini")
FLAKE8_LOCAL_PLUGINS = "flake8:local-plugins"


def wanted(personal_folder: Path, project_root: Path, include_project: bool) -> bool:
    return switches.is_on(personal_folder, project_root, include_project, SWITCH)


def clipped(text: str) -> str:
    text = text.strip()
    return text if len(text) <= MAX_NOTE_CHARS else text[:MAX_NOTE_CHARS] + "\n... more lint output cut ..."


class Checker:
    def __init__(self, project_root, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS):
        self.root = Path(project_root).resolve()
        self.timeout_seconds = timeout_seconds

    def problems(self, path: Path, text: str) -> str:
        path = Path(path)
        shown = self.shown(path)
        found = [note for note in self.notes_for(path, shown, text) if note]
        return clipped("\n".join(found)) if found else ""

    def notes_for(self, path: Path, shown: str, text: str) -> list[str]:
        suffix = path.suffix.lower()
        if suffix in PYTHON_SUFFIXES:
            return [python_syntax(shown, text), self.python_linter(path, shown)]
        if suffix in SCRIPT_SUFFIXES:
            return [self.outside_program("node", ["--check", str(path)], shown)]
        if suffix in SHELL_SUFFIXES:
            return [self.outside_program("bash", ["-n", str(path)], shown),
                    self.outside_program("shellcheck", [str(path)], shown)]
        if suffix in JSON_SUFFIXES:
            return [json_problem(shown, text)]
        return []

    def python_linter(self, path: Path, shown: str) -> str:
        if self.uses_ruff():
            return self.outside_program("ruff", ["check", "--quiet", "--no-cache", str(path)], shown)
        if self.uses_flake8() and not self.flake8_loads_project_plugins():
            return self.outside_program("flake8", [str(path)], shown)
        return ""

    def uses_ruff(self) -> bool:
        if any((self.root / name).is_file() for name in RUFF_FILES):
            return True
        pyproject = self.root / "pyproject.toml"
        return pyproject.is_file() and "[tool.ruff" in pyproject.read_text(encoding="utf-8", errors="replace")

    def uses_flake8(self) -> bool:
        if any((self.root / name).is_file() for name in FLAKE8_FILES):
            return True
        for name in FLAKE8_SECTION_FILES:
            parser = configparser.ConfigParser()
            try:
                parser.read(self.root / name, encoding="utf-8")
            except configparser.Error:
                continue
            if parser.has_section("flake8"):
                return True
        return False

    def flake8_loads_project_plugins(self) -> bool:
        for name in FLAKE8_FILES + FLAKE8_SECTION_FILES:
            parser = configparser.RawConfigParser()
            try:
                parser.read(self.root / name, encoding="utf-8")
            except configparser.Error:
                return True
            if any(section.lower() == FLAKE8_LOCAL_PLUGINS for section in parser.sections()):
                return True
        return False

    def program_outside_project(self, name: str) -> str:
        found = shutil.which(name)
        if not found:
            return ""
        resolved = Path(found).resolve()
        return "" if resolved.is_relative_to(self.root) else str(resolved)

    def outside_program(self, name: str, arguments: list[str], shown: str) -> str:
        program = self.program_outside_project(name)
        if not program:
            return ""
        try:
            finished = subprocess.run([program, *arguments], cwd=self.root, capture_output=True, text=True,
                                      timeout=self.timeout_seconds, errors="replace")
        except subprocess.TimeoutExpired:
            return f"Lint ({name}) on {shown} took longer than {self.timeout_seconds:g} s and was stopped."
        except OSError as error:
            return f"Lint ({name}) on {shown} couldn't run: {error}"
        if finished.returncode == 0:
            return ""
        output = (finished.stdout + finished.stderr).strip() or f"exit code {finished.returncode}"
        return f"Lint ({name}) found problems in {shown}:\n{output}"

    def shown(self, path: Path) -> str:
        resolved = path.resolve()
        return str(resolved.relative_to(self.root)) if resolved.is_relative_to(self.root) else str(path)


def python_syntax(shown: str, text: str) -> str:
    try:
        compile(text, shown, "exec", dont_inherit=True)
    except SyntaxError as error:
        return f"Lint (Python syntax) found a problem in {shown}: {error.msg} on line {error.lineno}"
    except ValueError as error:
        return f"Lint (Python syntax) found a problem in {shown}: {error}"
    return ""


def json_problem(shown: str, text: str) -> str:
    try:
        json.loads(text)
    except ValueError as error:
        return f"Lint (JSON) found a problem in {shown}: {error}"
    return ""
