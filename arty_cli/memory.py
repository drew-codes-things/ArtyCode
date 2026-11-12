import json
import os
from pathlib import Path

MEMORY_FILE = "ARTY.md"
SHARED_AGENT_FILE = "AGENTS.md"
PROJECT_NOTE_FILES = (MEMORY_FILE, SHARED_AGENT_FILE)
NOTE_PREFIX = "- "
TRUST_FILE = "trusted-folders.json"
PRIVATE_FILE_MODE = 0o600
TRUSTED, CHANGED, UNKNOWN = "trusted", "changed", "unknown"
NOT_RECORDED = ""


def trusted_folders(personal_folder: Path) -> dict[str, str]:
    try:
        stored = json.loads((Path(personal_folder) / TRUST_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if isinstance(stored, list):
        return {str(folder): NOT_RECORDED for folder in stored}
    if isinstance(stored, dict):
        return {str(folder): str(fingerprint) for folder, fingerprint in stored.items()}
    return {}


def trust_state(personal_folder: Path, project: Path, fingerprint: str = NOT_RECORDED) -> str:
    folders = trusted_folders(personal_folder)
    folder = str(Path(project).resolve())
    if folder not in folders:
        return UNKNOWN
    recorded = folders[folder]
    return TRUSTED if NOT_RECORDED in (recorded, fingerprint) or recorded == fingerprint else CHANGED


def is_trusted(personal_folder: Path, project: Path, fingerprint: str = NOT_RECORDED) -> bool:
    return trust_state(personal_folder, project, fingerprint) == TRUSTED


def trust(personal_folder: Path, project: Path, fingerprint: str = NOT_RECORDED) -> None:
    folders = trusted_folders(personal_folder)
    folder = str(Path(project).resolve())
    if folders.get(folder) == fingerprint:
        return
    target = Path(personal_folder) / TRUST_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PRIVATE_FILE_MODE)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump({**folders, folder: fingerprint}, handle, indent=2)


def project_note_files(root: Path) -> list[Path]:
    return [Path(root) / name for name in PROJECT_NOTE_FILES if (Path(root) / name).is_file()]


def memory_files(workspace, personal_folder: Path, include_project: bool = True) -> list[Path]:
    personal = [Path(personal_folder) / MEMORY_FILE]
    project = project_note_files(workspace.root) if include_project else []
    return [path for path in personal + project if path.is_file()]


def instructions(workspace, personal_folder: Path, include_project: bool = True) -> str:
    parts = []
    for path in memory_files(workspace, personal_folder, include_project):
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        if text:
            parts.append(f"Notes from {path} (follow them):\n{text}")
    return "\n\n".join(parts)


def add_note(workspace, note: str) -> Path:
    path = workspace.root / MEMORY_FILE
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    separator = "" if not existing or existing.endswith("\n") else "\n"
    path.write_text(f"{existing}{separator}{NOTE_PREFIX}{note.strip()}\n", encoding="utf-8")
    return path
