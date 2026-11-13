import os
import shlex
import sys
from pathlib import Path, PureWindowsPath

from arty_cli import __version__, context, look, memory, permissions, providers, rules
from arty_cli.chat_session import Session, rules_level
from arty_cli.screen import Terminal

WORDMARK = "◆ ArtyCode"
PRODUCT_NAME = "ArtyCode"
COMMAND_NAMES = ("arty", "arty-cli")
OWN_COMMAND_NAME = "arty-cli"
WINDOWS_PROGRAM_ENDING = ".exe"
RESUME_LINE = "Resume this chat: {}"
HOME_MARK = "~"
WELCOME_LABEL_WIDTH = 8
MODE_MEANINGS = {permissions.DEFAULT: "asks before it changes anything",
                 permissions.AUTO_EDIT: "edits without asking, asks before commands",
                 permissions.PLAN: "looks and plans, changes nothing",
                 permissions.ALLOW_ALL: "asks only before a command it can't read"}
WELCOME_TIPS = ('Ask in plain words, e.g. "why does the login test fail?" or "add a --dry-run flag"',
                "@file attaches a file · !command runs one yourself · # note saves a project note",
                "? or /help for commands · Ctrl-C stops a request · Ctrl-D leaves")
HEADER_TIPS = ("Esc stops a request · Enter while Arty works queues your next message",
             "? for shortcuts · /help for every command · @ attaches a file")
NO_NOTES_TIP = "No ARTY.md here yet: /init looks through the project and writes notes Arty reads every time."


def shown_folder(folder: Path, home: Path) -> str:
    try:
        inside = folder.relative_to(home)
    except ValueError:
        return str(folder)
    return HOME_MARK if inside == Path() else f"{HOME_MARK}{os.sep}{inside}"


def welcome_row(terminal: Terminal, label: str, value: str, colour: str = "reset") -> None:
    terminal.note(f"  {terminal.paint(f'{label:{WELCOME_LABEL_WIDTH}}', 'dim')}{terminal.paint(value, colour)}", "reset")


def welcome(session: Session, tips: tuple = WELCOME_TIPS) -> None:
    terminal = session.terminal
    mode = session.gate.mode
    terminal.note(f"{terminal.paint(WORDMARK, 'accent')} {terminal.paint(__version__, 'dim')}", "reset")
    welcome_row(terminal, "Folder", shown_folder(session.workspace.root, Path.home()))
    welcome_row(terminal, "Model", session.client.model, "accent")
    welcome_row(terminal, "Mode", f"{mode}: {MODE_MEANINGS.get(mode, '')} (Shift+Tab to change)")
    level = rules_level(session.workspace)
    welcome_row(terminal, "Rules", f"{level}: {rules.MEANINGS[level]} (/rules to change)")
    if mode == permissions.ALLOW_ALL:
        terminal.note("allow-all: nothing is asked first apart from commands ArtyCode can't read. Use it only in a "
                      "folder you can throw away.", "error")
    terminal.note("")
    for tip in tips:
        terminal.note(f"  {tip}")
    if not memory.project_note_files(session.workspace.root):
        terminal.note(f"  {NO_NOTES_TIP}")
    terminal.note("")


def rules_summary(level: str) -> str:
    meaning = rules.MEANINGS[level]
    return level if meaning == level else f"{level}: {meaning}"


def context_window(session: Session) -> int | None:
    try:
        return context.window_tokens(session.client)
    except providers.ERRORS:
        return None


def header_details(session: Session, window: int | None) -> list[tuple[str, str]]:
    mode = session.gate.mode
    details = [("model", session.client.model), ("folder", shown_folder(session.workspace.root, Path.home()))]
    if window:
        details.append(("context", f"{window:,} tokens"))
    return details + [("mode", f"{mode}: {MODE_MEANINGS.get(mode, '')}"),
                      ("rules", rules_summary(rules_level(session.workspace)))]


def details_now(session: Session):
    windows = {}

    def current() -> list[tuple[str, str]]:
        model = session.client.model
        if model not in windows:
            windows[model] = context_window(session)
        return header_details(session, windows[model])

    return current


def welcome_header(session: Session, theme: look.Theme, show_block) -> None:
    terminal = session.terminal
    details = details_now(session)
    show_block(lambda width: look.header(PRODUCT_NAME, __version__, details(), width, theme))
    terminal.note("")
    if session.gate.mode == permissions.ALLOW_ALL:
        terminal.note("allow-all: nothing is asked first apart from commands ArtyCode can't read. Use it only in a "
                      "folder you can throw away.", "error")
    for tip in HEADER_TIPS:
        terminal.note(f"  {tip}")
    if not memory.project_note_files(session.workspace.root):
        terminal.note(f"  {NO_NOTES_TIP}")


def started_as() -> str:
    name = PureWindowsPath(sys.argv[0] if sys.argv else "").name.lower().removesuffix(WINDOWS_PROGRAM_ENDING)
    return name if name in COMMAND_NAMES else OWN_COMMAND_NAME


def resume_command(session_id: str, project: Path, here: Path) -> str:
    words = [started_as()]
    if Path(project).resolve() != Path(here).resolve():
        words += ["--project", str(project)]
    return shlex.join(words + ["--resume", session_id])


def say_how_to_resume(session: Session) -> None:
    if not session.saved.path.is_file():
        return
    command = resume_command(session.saved.id, session.workspace.root, Path.cwd())
    terminal = session.terminal
    terminal.end_line()
    terminal.err.write(RESUME_LINE.format(command) + "\n")
    terminal.err.flush()
