from dataclasses import dataclass
from pathlib import Path

from arty_cli import tools

SKILL_FILE = "SKILL.md"
SKILLS_FOLDER = "skills"
PROJECT_SKILLS = Path(".arty") / SKILLS_FOLDER
FENCE = "---"
MAX_DESCRIPTION_CHARS = 300
MAX_LISTED_FILES = 50
QUOTES = "\"'"
DEFINITION = tools.function(
    "skill",
    "Load a skill: saved instructions for a kind of job (listed in your instructions as Skills). Load one when the "
    "job matches its description, then follow it.",
    {"name": tools.TEXT}, ["name"])


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    folder: Path
    body: str
    source: str


def front_matter(text: str) -> tuple[dict, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != FENCE:
        return {}, text
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == FENCE:
            fields = {}
            for entry in lines[1:index]:
                key, separator, value = entry.partition(":")
                if separator:
                    fields[key.strip().lower()] = value.strip().strip(QUOTES)
            return fields, "\n".join(lines[index + 1:])
    return {}, text


def first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def skill_in(folder: Path, source: str) -> Skill | None:
    path = folder / SKILL_FILE
    if not path.is_file():
        return None
    fields, body = front_matter(path.read_text(encoding="utf-8", errors="replace"))
    description = (fields.get("description") or first_line(body))[:MAX_DESCRIPTION_CHARS]
    return Skill(fields.get("name") or folder.name, description, folder, body.strip(), source)


def from_folder(folder: Path, source: str) -> dict[str, Skill]:
    if not folder.is_dir():
        return {}
    found = [skill_in(child, source) for child in sorted(folder.iterdir()) if child.is_dir()]
    return {skill.name: skill for skill in found if skill}


def find(personal_folder: Path, workspace, include_project: bool) -> dict[str, Skill]:
    found = from_folder(Path(personal_folder) / SKILLS_FOLDER, "yours")
    if include_project:
        found.update(from_folder(workspace.root / PROJECT_SKILLS, "this project"))
    return found


def listing(found: dict[str, Skill]) -> str:
    if not found:
        return ""
    lines = [f"- {skill.name}: {skill.description}" for skill in found.values()]
    return "Skills you can load with the skill tool when a job matches:\n" + "\n".join(lines)


def files_of(skill: Skill) -> list[str]:
    found = sorted(path.relative_to(skill.folder).as_posix() for path in skill.folder.rglob("*")
                   if path.is_file() and path.name != SKILL_FILE)
    return found[:MAX_LISTED_FILES]


def loaded(found: dict[str, Skill], name: str) -> str:
    skill = found.get(str(name))
    if skill is None:
        return f"Error: there is no skill called {name}. Skills: {', '.join(found) or 'none'}"
    files = files_of(skill)
    extra = f"\nFiles in it: {', '.join(files)}" if files else ""
    return f"{skill.body}\n\nSkill folder: {skill.folder}{extra}"
