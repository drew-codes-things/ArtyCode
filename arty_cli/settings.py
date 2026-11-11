import json
import os
import sys
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

APP_FOLDER = "arty-cli"
CONFIG_NAME = "config.json"
DEFAULT_MODEL = "arty/qwen3:14b"
RETIRED_DEFAULT_MODELS = ("arty/qwen2.5-coder:14b",)
PRIVATE_FILE_MODE = 0o600
ENVIRONMENT_NAMES = {
    "base_url": "ARTY_URL",
    "remote_url": "ARTY_REMOTE_URL",
    "api_key": "ARTY_API_KEY",
    "access_client_id": "ARTY_ACCESS_CLIENT_ID",
    "access_client_secret": "ARTY_ACCESS_CLIENT_SECRET",
    "model": "ARTY_CLI_MODEL",
}


class SettingsError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    base_url: str = ""
    remote_url: str = ""
    api_key: str = ""
    access_client_id: str = ""
    access_client_secret: str = ""
    model: str = DEFAULT_MODEL

    def is_ready(self) -> bool:
        return bool(self.api_key and (self.base_url or self.remote_url))


def config_dir(environ=os.environ, platform=sys.platform) -> Path:
    if platform == "win32":
        return Path(environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / APP_FOLDER
    return Path(environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / APP_FOLDER


def config_path(environ=os.environ) -> Path:
    return config_dir(environ) / CONFIG_NAME


def read_file(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SettingsError(f"Can't read the settings in {path}: {error}") from None
    if not isinstance(stored, dict):
        raise SettingsError(f"The settings in {path} should be a JSON object.")
    known = {field.name for field in fields(Settings)}
    return {name: str(value) for name, value in stored.items() if name in known and value is not None}


def without_retired_default(stored: dict) -> dict:
    return {name: value for name, value in stored.items()
            if not (name == "model" and value in RETIRED_DEFAULT_MODELS)}


def load(path: Path | None = None, environ=os.environ) -> Settings:
    loaded = Settings(**without_retired_default(read_file(path or config_path(environ))))
    overrides = {name: environ[variable] for name, variable in ENVIRONMENT_NAMES.items() if environ.get(variable)}
    return replace(loaded, **overrides)


def save(settings: Settings, path: Path | None = None) -> Path:
    target = path or config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PRIVATE_FILE_MODE)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        if hasattr(os, "fchmod"):
            os.fchmod(handle.fileno(), PRIVATE_FILE_MODE)
        json.dump(asdict(settings), handle, indent=2)
    return target


def remember(path: Path | None = None, **changes) -> Path:
    target = path or config_path()
    stored = replace(Settings(**read_file(target)), **changes)
    return save(stored, target)
