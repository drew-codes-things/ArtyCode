import re
from dataclasses import dataclass
from pathlib import Path

from arty_cli import permissions, skills, tools

HELPERS_FOLDER = "helpers"
PROJECT_HELPERS = Path(".arty") / HELPERS_FOLDER
HELPER_FILE_SUFFIX = ".md"
LOOKING_TOOLS = ("read_file", "list_files", "search", "web_fetch", "run_command")
ALLOWED_TOOLS = tuple(tools.HANDLERS) + (permissions.WEB_SEARCH,)
SAFE_NAME = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
MAX_DESCRIPTION_CHARS = 300


@dataclass(frozen=True)
class Helper:
    name: str
    description: str
    prompt: str
    tools: tuple
    source: str

    def changes_things(self) -> bool:
        return any(name in tools.CHANGING_TOOLS for name in self.tools)


def listed_tools(text: str) -> tuple:
    return tuple(name.strip() for name in text.split(",") if name.strip())


def helper_in(path: Path, source: str) -> tuple[Helper | None, str]:
    fields, body = skills.front_matter(path.read_text(encoding="utf-8", errors="replace"))
    name = fields.get("name") or path.stem
    if not SAFE_NAME.match(name):
        return None, f"Skipped the helper {path}: its name should be letters, digits, - or _ (up to 40)"
    prompt = body.strip()
    if not prompt:
        return None, f"Skipped the helper {name} in {path}: it has no instructions after the front matter"
    wanted = listed_tools(fields["tools"]) if fields.get("tools") else LOOKING_TOOLS
    unknown = [tool for tool in wanted if tool not in ALLOWED_TOOLS]
    if unknown:
        return None, (f"Skipped the helper {name} in {path}: it can't use {', '.join(unknown)}; "
                      f"helpers can use {', '.join(ALLOWED_TOOLS)}")
    description = (fields.get("description") or skills.first_line(prompt))[:MAX_DESCRIPTION_CHARS]
    return Helper(name, description, prompt, wanted, source), ""


def from_folder(folder: Path, source: str) -> tuple[dict[str, Helper], list[str]]:
    if not folder.is_dir():
        return {}, []
    found, problems = {}, []
    for path in sorted(folder.glob(f"*{HELPER_FILE_SUFFIX}")):
        helper, problem = helper_in(path, source)
        if helper:
            found[helper.name] = helper
        else:
            problems.append(problem)
    return found, problems


def find(personal_folder: Path, workspace, include_project: bool) -> tuple[dict[str, Helper], list[str]]:
    found, problems = from_folder(Path(personal_folder) / HELPERS_FOLDER, "yours")
    if include_project:
        project, trouble = from_folder(workspace.root / PROJECT_HELPERS, "this project")
        found.update(project)
        problems += trouble
    return found, problems
