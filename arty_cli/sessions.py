import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

PROJECT_KEY_CHARS = 16
PRIVATE_FILE_MODE = 0o600
FIRST_REQUEST_CHARS = 70


def first_request_of(messages: list[dict]) -> str:
    for message in messages:
        if message.get("role") == "user":
            content = message.get("content")
            text = content if isinstance(content, str) else " ".join(
                part.get("text", "") for part in content or [] if isinstance(part, dict))
            return text.strip().splitlines()[0][:FIRST_REQUEST_CHARS] if text.strip() else ""
    return ""


@dataclass
class Saved:
    id: str
    path: Path
    messages: list = field(default_factory=list)
    model: str = ""

    @property
    def first_request(self) -> str:
        return first_request_of(self.messages)

    def save(self, messages: list[dict], model: str) -> None:
        self.messages, self.model = messages, model
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PRIVATE_FILE_MODE)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"model": model, "messages": messages}, handle)


class Store:
    def __init__(self, folder: Path):
        self.root = Path(folder)

    def folder(self, project: Path) -> Path:
        key = hashlib.sha256(str(Path(project).resolve()).encode()).hexdigest()[:PROJECT_KEY_CHARS]
        return self.root / key

    def start(self, project: Path) -> Saved:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        return Saved(stamp, self.folder(project) / f"{stamp}.json")

    def read(self, path: Path) -> Saved | None:
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(stored, dict) or not isinstance(stored.get("messages"), list):
            return None
        return Saved(path.stem, path, stored["messages"], str(stored.get("model") or ""))

    def listed(self, project: Path) -> list[Saved]:
        folder = self.folder(project)
        if not folder.is_dir():
            return []
        found = (self.read(path) for path in sorted(folder.glob("*.json"), reverse=True))
        return [saved for saved in found if saved]

    def latest(self, project: Path) -> Saved | None:
        listed = self.listed(project)
        return listed[0] if listed else None

    def load(self, project: Path, session_id: str) -> Saved | None:
        return self.read(self.folder(project) / f"{Path(session_id).name}.json")
