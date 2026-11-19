import difflib
import re

from arty_cli import permissions, rules

SLASH_NAME = re.compile(r"/[a-z][a-z-]*")
MENU_ROWS = 12
NAME_GAP = 2
CLOSE_ENOUGH = 0.6
MENU_INDENT = "  "
MODEL_COMMAND = "/model"
FIXED_CHOICES = {"/mode": permissions.MODE_MEANINGS, "/rules": rules.MEANINGS}
COMMAND_WITH_CHOICE = re.compile(r"(/[a-z][a-z-]*)( ([^ \t\n]*))?")


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


def choices_for(command: str, models) -> dict:
    if command == MODEL_COMMAND:
        return models() if models is not None else {}
    return FIXED_CHOICES.get(command, {})


def takes_a_choice(typed: str) -> bool:
    return typed == MODEL_COMMAND or typed in FIXED_CHOICES


def argument_lead(typed: str, partial: str) -> str:
    return " " if takes_a_choice(typed) and not partial else ""


def suggestions(typed: str, commands, models=None) -> tuple[list[str], dict, str]:
    found = COMMAND_WITH_CHOICE.fullmatch(typed)
    if found:
        command, spaced, wanted = found.group(1), found.group(2), found.group(3) or ""
        listed = choices_for(command, models) if spaced or takes_a_choice(typed) else {}
        if listed or spaced:
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
