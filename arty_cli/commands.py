from dataclasses import dataclass
from pathlib import Path

PLACEHOLDER = "$ARGUMENTS"
COMMAND_FOLDER = "commands"
PROJECT_COMMANDS = Path(".arty") / COMMAND_FOLDER
BUILT_IN = {
    "/init": (
        "Look through this project (its layout, build and test commands, code style, anything surprising) and write "
        "ARTY.md in the project folder: short notes a new developer, or you in a later session, needs to work here. "
        "If ARTY.md exists, improve it instead of starting again."
    ),
    "/review": (
        "Review the uncommitted changes in this project: run git status and git diff (and git diff --staged), read "
        "what you need around them, and list real problems (bugs, missed cases, risky changes) with file and line, "
        "most serious first. Don't change any files."
    ),
}


@dataclass(frozen=True)
class Command:
    name: str
    template: str
    source: str

    def prompt(self, arguments: str) -> str:
        text = self.template.strip()
        if PLACEHOLDER in text:
            return text.replace(PLACEHOLDER, arguments.strip())
        return f"{text}\n\n{arguments.strip()}" if arguments.strip() else text


def from_folder(folder: Path, source: str) -> dict[str, Command]:
    if not folder.is_dir():
        return {}
    return {f"/{path.stem}": Command(f"/{path.stem}", path.read_text(encoding="utf-8", errors="replace"), source)
            for path in sorted(folder.glob("*.md"))}


def find(workspace, personal_folder: Path, include_project: bool = True) -> dict[str, Command]:
    found = {name: Command(name, template, "built in") for name, template in BUILT_IN.items()}
    found.update(from_folder(Path(personal_folder) / COMMAND_FOLDER, "yours"))
    if include_project:
        found.update(from_folder(workspace.root / PROJECT_COMMANDS, "this project"))
    return found
