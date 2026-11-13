import shlex
import sys

ON_WINDOWS = sys.platform == "win32"


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
