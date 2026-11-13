import shlex
import sys
from pathlib import Path

ON_WINDOWS = sys.platform == "win32"
SHORT_PYTHON = "artypy"


def quoted(word: str) -> str:
    if ON_WINDOWS:
        return "'" + str(word).replace("'", "''") + "'"
    return shlex.quote(str(word))


def program_call(*words) -> str:
    line = " ".join(quoted(word) for word in words)
    return f"& {line}" if ON_WINDOWS else line


def python_call(*arguments) -> str:
    return program_call(sys.executable, *arguments)


def file_making_call(name: str) -> str:
    return f"New-Item -ItemType File {name}" if ON_WINDOWS else f"touch {name}"


def plain_python_call(*arguments) -> str:
    if ON_WINDOWS and not any(character.isspace() for character in sys.executable):
        return " ".join([sys.executable, *(str(argument) for argument in arguments)])
    return python_call(*arguments)


def short_python_call(folder: Path, *arguments) -> str:
    folder.mkdir(parents=True, exist_ok=True)
    if ON_WINDOWS:
        (folder / f"{SHORT_PYTHON}.cmd").write_text(f'@"{sys.executable}" %*\n')
    else:
        launcher = folder / SHORT_PYTHON
        launcher.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
        launcher.chmod(0o755)
    return " ".join([SHORT_PYTHON, *(str(argument) for argument in arguments)])
