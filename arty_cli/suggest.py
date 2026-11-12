import difflib
import re

SLASH_NAME = re.compile(r"/[a-z][a-z-]*")
MENU_ROWS = 12
NAME_GAP = 2
CLOSE_ENOUGH = 0.6
MENU_INDENT = "  "
MODEL_COMMAND = "/model "


def slash_choices(groups, custom_meanings: dict) -> dict[str, str]:
    choices = {}
    for _heading, rows in groups:
        for usage, meaning in rows.items():
            for name in SLASH_NAME.findall(usage):
                choices.setdefault(name, meaning)
    for name, meaning in custom_meanings.items():
        choices.setdefault(name, meaning)
    return choices


def is_command_start(typed: str) -> bool:
    return typed.startswith("/") and not any(space in typed for space in " \t\n")


def matching(typed: str, choices: dict) -> list[str]:
    if not is_command_start(typed):
        return []
    return [name for name in choices if name.startswith(typed)]


def suggestions(typed: str, commands, models=None) -> tuple[list[str], dict, str]:
    if models is not None and typed.startswith(MODEL_COMMAND):
        wanted = typed[len(MODEL_COMMAND):]
        if any(space in wanted for space in " \t\n"):
            return [], {}, wanted
        listed = models()
        return [name for name in listed if name.startswith(wanted)], listed, wanted
    listed = commands()
    return matching(typed, listed), listed, typed


def closest(name: str, choices: dict) -> str | None:
    found = difflib.get_close_matches(name, list(choices), n=1, cutoff=CLOSE_ENOUGH)
    return found[0] if found else None


def menu_lines(names: list[str], choices: dict, columns: int) -> list[str]:
    if not names:
        return []
    shown = names[:MENU_ROWS]
    width = max(len(name) for name in shown) + NAME_GAP
    room = max(1, columns - 1)
    lines = [f"{MENU_INDENT}{name:{width}}{choices.get(name, '')}".rstrip()[:room] for name in shown]
    if len(names) > MENU_ROWS:
        lines.append(f"{MENU_INDENT}and {len(names) - MENU_ROWS} more: keep typing to narrow it down"[:room])
    return lines
