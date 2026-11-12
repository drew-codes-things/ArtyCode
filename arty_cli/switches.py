import json
from pathlib import Path

SETTINGS_FILE = "settings.json"
PROJECT_SETTINGS = Path(".arty") / SETTINGS_FILE


def value_in(path: Path, name: str):
    try:
        table = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return table.get(name) if isinstance(table, dict) else None


def is_on(personal_folder: Path, project_root: Path, include_project: bool, name: str) -> bool:
    places = [Path(personal_folder) / SETTINGS_FILE]
    if include_project:
        places.append(Path(project_root) / PROJECT_SETTINGS)
    return all(value_in(path, name) is not False for path in places)
