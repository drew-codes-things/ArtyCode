import argparse
import contextlib
import hashlib
import importlib
import io
import signal
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

from arty_cli import extras as extras_module
from arty_cli import eyes
from arty_cli import finish
from arty_cli import rules

from arty_cli import (__version__, brain, commands, context, helpers, hooks, lint, mcp, mcp_sign_in, memory,
                      permissions, prompt_line, providers, remote, repomap, sandbox, screen, sessions, settings,
                      sign_ins, skills, tools, websearch, wrap)
from arty_cli import upkeep
from arty_cli import look, notify, view
from arty_cli.chat_session import SESSIONS_FOLDER, Session
from arty_cli.conversation import HELP_MARK, NOTE_MARK, RESUME, SHELL_MARK, STARTUP
from arty_cli.login_flow import FAILED, login
from arty_cli.screen import NOTIFICATION_LEADS, PERMISSION_PROMPT, PLAN_APPROVAL, JsonLines, Terminal
from arty_cli.welcome import say_how_to_resume, welcome, welcome_header

NOT_SET_UP = 2
SIGNAL_EXIT_BASE = 128
ENDING_SIGNALS = ("SIGTERM", "SIGHUP")
PROMPT = "› "
CONTINUATION_PROMPT = "… "
LINE_CONTINUES = "\\"
TRUST_QUESTION = ("This folder has its own Arty setup: notes Arty would follow (ARTY.md or AGENTS.md), /commands, skills, helpers, "
                  "or hooks and MCP servers that run programs on this computer. Only say yes if you trust whoever wrote them. "
                  "Trust this folder? [y/n]: ")
CHANGED_QUESTION = ("This folder's hooks, MCP servers or permission rules (.arty/settings.json, .arty/mcp.json) "
                    "changed since you trusted it, so they may run different programs now. Trust it again? [y/n]: ")
REPO_MAP_FOLDER = "repomap"
FINGERPRINTED_FILES = (hooks.PROJECT_FOLDER / hooks.SETTINGS_FILE, mcp.PROJECT_FILE)
SETUP_CHANGED_BEFORE_SAVE = ("This folder's settings or MCP servers changed since you trusted it, so you'll be "
                             "asked about them again next time.")
BRANCH_CHECK_SECONDS = 5.0
TEXT_OUTPUT, JSON_OUTPUT, STREAM_JSON_OUTPUT = "text", "json", "stream-json"
OUTPUT_FORMATS = (TEXT_OUTPUT, JSON_OUTPUT, STREAM_JSON_OUTPUT)
REMOTE_ONLY_IN_CHAT = "--remote works in the chat, not with a single question."


def parse_arguments(argv):
    parser = argparse.ArgumentParser(prog="arty-cli", description="ArtyCode: reads, edits and runs code in the "
                                     "current folder, asking before it changes anything. Runs on your Arty.")
    parser.add_argument("words", nargs="*", help="`login` to connect to Arty, `doctor` to check the setup, `update` to "
                        "get the newest version, or a question to ask once")
    parser.add_argument("-p", "--print", dest="once", help="ask once, print the answer and exit")
    parser.add_argument("--output-format", choices=OUTPUT_FORMATS, default=TEXT_OUTPUT,
                        help="with -p: text (streamed), json (one object with the answer at the end) or "
                             "stream-json (one JSON object per line as the work happens)")
    parser.add_argument("-c", "--continue", dest="carry_on", action="store_true",
                        help="carry on with the last conversation in this project")
    parser.add_argument("-r", "--resume", metavar="ID", help="carry on with an earlier conversation (see /resume)")
    parser.add_argument("--model", help="model for this session, e.g. arty/qwen3:14b")
    parser.add_argument("--project", type=Path, default=None, help="project folder (default: here)")
    parser.add_argument("--permission-mode", choices=permissions.MODES, default=permissions.DEFAULT,
                        help="how much to ask first (allow-all asks only before a command it can't read: only "
                             "in a throwaway folder)")
    parser.add_argument("--auto-edit", action="store_true", help="same as --permission-mode auto-edit")
    parser.add_argument("--remote", action="store_true",
                        help="share the chat with Arty on your phone or starfire.ai from the start (same as /remote)")
    parser.add_argument("--rules", choices=rules.LEVELS, default=None,
                        help="built-in working rules for this run: on (told and checked), tell-only or off")
    parser.add_argument("--sandbox", action="store_true",
                        help="run commands in a sandbox: only the project folder is writable, no network")
    parser.add_argument("--plain", action="store_true",
                        help="keep the plain line-at-a-time prompt instead of the full terminal interface")
    parser.add_argument("--version", action="version", version=f"ArtyCode {__version__}")
    return parser.parse_args(argv)


def has_project_setup(workspace) -> bool:
    root = workspace.root
    return (bool(memory.project_note_files(root)) or (root / commands.PROJECT_COMMANDS).is_dir()
            or (root / skills.PROJECT_SKILLS).is_dir() or (root / helpers.PROJECT_HELPERS).is_dir()
            or (root / hooks.PROJECT_FOLDER / hooks.SETTINGS_FILE).is_file()
            or (root / mcp.PROJECT_FILE).is_file())


def setup_fingerprint(workspace) -> str:
    digest = hashlib.sha256()
    for name in FINGERPRINTED_FILES:
        path = workspace.root / name
        digest.update(f"{name.as_posix()}\0".encode())
        digest.update(path.read_bytes() if path.is_file() else b"\0absent\0")
    return digest.hexdigest()


def project_notes_allowed(workspace, personal: Path, terminal: Terminal) -> bool:
    fingerprint = setup_fingerprint(workspace)
    state = memory.trust_state(personal, workspace.root, fingerprint)
    if state == memory.TRUSTED:
        memory.trust(personal, workspace.root, fingerprint)
        return True
    if not has_project_setup(workspace):
        return True
    if not screen.is_interactive():
        reason = "its hooks or MCP servers changed since it was trusted" if state == memory.CHANGED else "this folder isn't trusted yet"
        terminal.note(f"Not following this project's ARTY.md or AGENTS.md, /commands, skills, helpers, hooks or MCP servers: {reason}. "
                      "Start ArtyCode here in a terminal to trust it.", "error")
        return False
    if state == memory.CHANGED:
        terminal.note("This folder's hooks or MCP servers changed since you trusted it.", "error")
    try:
        answer = terminal.asked(CHANGED_QUESTION if state == memory.CHANGED else TRUST_QUESTION).strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer in ("y", "yes"):
        memory.trust(personal, workspace.root, fingerprint)
        return True
    terminal.note("Leaving this folder's notes, /commands, skills, helpers, hooks and MCP servers out.")
    return False


def line_from(session, reader, kept: str) -> tuple[str, bool]:
    terminal, mode, shared = session.terminal, session.gate.mode, bool(session.remote)
    if not terminal.shows_status():
        prompt, width = prompt_line.mode_prompt(terminal, mode, shared=shared)
        return reader.read(prompt, width, kept)
    prompt, width = prompt_line.box_prompt(terminal)
    return reader.read(prompt, width, kept, prompt_line.mode_box(terminal, mode, shared))


def first_line(session, reader) -> str:
    if reader is None:
        return input(PROMPT)
    kept = ""
    while True:
        kept, wants_next_mode = line_from(session, reader, kept)
        if not wants_next_mode:
            return kept
        session.gate.mode = prompt_line.next_mode(session.gate.mode)
        session.tell_mode()


def read_request(session, reader) -> str:
    lines = [first_line(session, reader)]
    while lines[-1].rstrip().endswith(LINE_CONTINUES):
        lines[-1] = lines[-1].rstrip()[:-1].rstrip()
        lines.append(input(CONTINUATION_PROMPT))
    return "\n".join(lines).strip()


def handle(session: Session, line: str) -> bool:
    if line == HELP_MARK:
        line = "/help"
    if line.startswith("/"):
        return session.command(line)
    if line.startswith(SHELL_MARK) and len(line) > 1:
        session.run_shell(line[1:].strip())
    elif line.startswith(NOTE_MARK) and len(line) > 1:
        session.add_note(line[1:].strip())
    else:
        session.tell_remote({"type": "user", "text": line})
        session.ask(line)
    return True


def context_room(session: Session) -> context.ContextRoom | None:
    limit = session.compact_limit()
    if limit <= 0:
        return None
    return context.ContextRoom(context.estimated_tokens(session.messages, session.client), limit)


def context_left(session: Session) -> int | None:
    room = context_room(session)
    return room.left_percent if room else None


class SessionHost:
    def __init__(self, session: Session, clock=time.monotonic):
        self.session = session
        self.root = session.workspace.root
        self.commands = session.slash_choices
        self.models = session.model_choices
        self.paint = session.terminal.paint
        self.theme = look.theme_for(session.terminal.colour)
        self.clock = clock
        self.room = None
        self.branch, self.branch_checked = "", None

    def measure(self) -> None:
        try:
            self.room = context_room(self.session)
        except providers.ERRORS:
            self.room = None

    def current_branch(self) -> str:
        now = self.clock()
        if self.branch_checked is None or now - self.branch_checked >= BRANCH_CHECK_SECONDS:
            self.branch, self.branch_checked = look.git_branch(self.root) or "", now
        return self.branch

    def footer_details(self) -> tuple[str, str, context.ContextRoom | None]:
        return self.session.client.model, self.current_branch(), self.room

    def expand(self) -> None:
        self.session.terminal.show_last()

    def mode(self) -> str:
        return self.session.gate.mode

    def cycle_mode(self) -> None:
        self.session.gate.mode = prompt_line.next_mode(self.session.gate.mode)
        self.session.tell_mode()

    def shared(self) -> bool:
        return bool(self.session.remote)


def started_interface(session: Session):
    terminal = session.terminal
    if not (screen.is_interactive() and terminal.shows_status()):
        return None
    try:
        tui = importlib.import_module("arty_cli.tui")
    except ImportError as error:
        terminal.note(f"The full interface needs prompt_toolkit ({error}); using the plain prompt.", "error")
        return None
    interface = tui.Interface(SessionHost(session), tui.history_path(settings.config_dir(), session.workspace.root))
    try:
        interface.start()
    except tui.InterfaceError as error:
        terminal.note(f"The full interface couldn't start ({error}); using the plain prompt.", "error")
        return None
    return interface


def chat_in_interface(session: Session, interface, share: bool) -> None:
    terminal = session.terminal
    before = (terminal.out, terminal.err, sys.stdout, sys.stderr)
    terminal.out = terminal.err = interface.scroll
    sys.stdout = sys.stderr = interface.stray
    terminal.console = interface
    host = getattr(interface, "host", None) or SessionHost(session)
    try:
        terminal.view = view.View(interface.scroll, host.theme, session.workspace,
                                  interface_notifier(session, interface), room=getattr(interface, "room", wrap.room))
        terminal.view.partial = getattr(interface, "show_partial", None)
        terminal.measure = host.measure
        welcome_header(session, host.theme, interface.show_block)
        host.measure()
        if share:
            session.start_remote()
        talk_in_interface(session, interface)
    finally:
        if session.remote:
            session.stop_remote()
        terminal.end_line()
        terminal.console = None
        terminal.view = None
        terminal.measure = None
        interface.stop()
        terminal.out, terminal.err, sys.stdout, sys.stderr = before
    if interface.failure is not None:
        terminal.note(f"The interface stopped unexpectedly: {interface.failure}", "error")


def interface_notifier(session: Session, interface) -> notify.Notifier | None:
    ring = getattr(interface, "ring", None)
    if ring is None or not notify.wanted(getattr(session.client, "settings", None)):
        return None
    return notify.Notifier(ring)


def talk_in_interface(session: Session, interface) -> None:
    while True:
        try:
            with interface.working():
                session.run_remote_requests()
            with session.running(remote.AT_PROMPT):
                line = interface.next_message()
        except KeyboardInterrupt:
            continue
        if line is None:
            return
        interface.show_request(line)
        try:
            with interface.working():
                carries_on = handle(session, line)
        except KeyboardInterrupt:
            session.terminal.note("Stopped.", "error")
            continue
        finally:
            if session.terminal.measure:
                session.terminal.measure()
        if not carries_on:
            return


def chat(session: Session, share: bool = False, plain: bool = False) -> None:
    interface = None if plain else started_interface(session)
    if interface is not None:
        chat_in_interface(session, interface, share)
        return
    welcome(session)
    reader = prompt_line.line_reader(screen.is_interactive, session.slash_choices, session.model_choices)
    if share:
        session.start_remote()
    try:
        talk(session, reader)
    finally:
        if session.remote:
            session.stop_remote()


def talk(session: Session, reader) -> None:
    while True:
        try:
            session.run_remote_requests()
            with session.running(remote.AT_PROMPT):
                line = read_request(session, reader)
        except EOFError:
            session.terminal.end_line()
            return
        except KeyboardInterrupt:
            session.terminal.note("")
            continue
        if line and not handle(session, line):
            return


def ready_settings(arguments, terminal: Terminal) -> tuple[settings.Settings, bool]:
    config = settings.load()
    tried_login = not config.is_ready() and screen.is_interactive()
    if tried_login:
        terminal.note("First run: choose how ArtyCode connects.", "accent")
        login(terminal)
        config = settings.load()
    if arguments.model:
        config = replace(config, model=arguments.model)
    return config, tried_login


def saved_conversation(arguments, store: sessions.Store, project: Path, terminal: Terminal):
    if arguments.resume:
        found = store.load(project, arguments.resume)
        if found is None:
            terminal.note(f"No conversation {arguments.resume} in this project; starting a new one.", "error")
        return found or store.start(project)
    if arguments.carry_on:
        return store.latest(project) or store.start(project)
    return store.start(project)


def leave_on_signal(number, _frame) -> None:
    for name in ENDING_SIGNALS:
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), signal.SIG_IGN)
    raise SystemExit(SIGNAL_EXIT_BASE + number)


@contextlib.contextmanager
def signals_end_cleanly():
    previous = {}
    if threading.current_thread() is threading.main_thread():
        for name in ENDING_SIGNALS:
            number = getattr(signal, name, None)
            if number is not None:
                previous[number] = signal.signal(number, leave_on_signal)
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def main(argv=None) -> int:
    screen.write_utf8_when_redirected()
    argv = sys.argv[1:] if argv is None else argv
    if upkeep.handles(argv):
        return upkeep.run(argv)
    if mcp_sign_in.handles(argv):
        return mcp_sign_in.run(argv)
    with signals_end_cleanly():
        return main_session(parse_arguments(argv))


def main_session(arguments) -> int:
    terminal = Terminal()
    try:
        if arguments.words in (["login"], ["setup"]):
            return login(terminal)
        config, tried_login = ready_settings(arguments, terminal)
        if not config.is_ready():
            if not tried_login:
                terminal.note("ArtyCode isn't logged in to Arty yet. Run `arty-cli login` first.", "error")
            return NOT_SET_UP
        settings.positive_number(config, "context_tokens")
        workspace = tools.Workspace(arguments.project or Path.cwd())
        workspace.max_command_seconds = settings.positive_number(config, "max_command_seconds") or tools.MAX_COMMAND_SECONDS
        workspace.finish_check = finish.FinishCheck(enabled=settings.is_on(config, "finish_check"))
        try:
            return run_session(arguments, config, workspace, terminal)
        finally:
            workspace.stop_jobs()
    except (*providers.ERRORS, settings.SettingsError) as error:
        terminal.note(str(error), "error")
        return FAILED


def brains_coding_model(client) -> str | None:
    named = getattr(client, "coding_model", None)
    return named() if named is not None else None


def run_session(arguments, config, workspace, terminal: Terminal) -> int:
    mode = permissions.AUTO_EDIT if arguments.auto_edit else arguments.permission_mode
    gate = permissions.Gate(workspace, terminal.ask, mode=mode)
    store = sessions.Store(settings.config_dir() / SESSIONS_FOLDER)
    saved = saved_conversation(arguments, store, workspace.root, terminal)
    client = providers.make(config)
    if saved.model and not arguments.model and brain.is_arty_model(saved.model):
        client.model = saved.model
    elif not arguments.model and not settings.model_was_chosen():
        client.model = brains_coding_model(client) or client.model
    trusted = project_notes_allowed(workspace, settings.config_dir(), terminal)
    gate.rules, problems = permissions.load_rules(settings.config_dir(), workspace, trusted)
    if trusted:
        gate.rule_file = project_rule_file(workspace, terminal)
    for problem in problems:
        terminal.note(problem, "error")
    if lint.wanted(settings.config_dir(), workspace.root, trusted):
        workspace.lint = lint.Checker(workspace.root)
    if repomap.wanted(settings.config_dir(), workspace.root, trusted):
        workspace.repo_map = repomap.RepoMap(workspace.root, settings.config_dir() / REPO_MAP_FOLDER)
    workspace.rules = rules.Rules(rules.chosen(settings.config_dir(), workspace.root, trusted, arguments.rules))
    choice = sandbox.chosen(settings.config_dir(), workspace.root, trusted, arguments.sandbox)
    workspace.sandbox = sandbox.Sandbox(workspace.root, choice, secret_paths=(settings.config_dir(),))
    loaded = session_extras(workspace, trusted, terminal, chosen_search(client, config, terminal),
                            eyes.Eyes(workspace, client))
    gate.ask = notifying(terminal.ask, loaded, terminal)
    gate.instead = terminal.instead
    gate.review = notifying(terminal.review_plan, loaded, terminal, PLAN_APPROVAL)
    try:
        session = Session(client, workspace, gate, terminal, saved, project_trusted=trusted, extras=loaded)
        if workspace.repo_map and workspace.repo_map.problem:
            terminal.note(workspace.repo_map.problem, "error")
        if workspace.sandbox.problem:
            terminal.note(workspace.sandbox.problem, "error")
        elif workspace.sandbox.active:
            terminal.note(workspace.sandbox.describe())
        session.start_hook(RESUME if saved.messages else STARTUP)
        once = arguments.once or " ".join(arguments.words)
        if once and arguments.remote:
            terminal.note(REMOTE_ONLY_IN_CHAT, "error")
            return FAILED
        if once and arguments.output_format == JSON_OUTPUT:
            return answered_as_json(session, once)
        if once and arguments.output_format == STREAM_JSON_OUTPUT:
            return answered_as_stream(session, once)
        if once:
            return 0 if handle(session, once) is not False and session.messages[-1]["role"] == "assistant" else FAILED
        try:
            chat(session, share=arguments.remote, plain=arguments.plain)
        finally:
            say_how_to_resume(session)
        return 0
    finally:
        for problem in loaded.close():
            terminal.note(problem, "error")


def notifying(ask, extras, terminal: Terminal, kind: str = PERMISSION_PROMPT):
    def asked(question: str, *rest):
        if screen.is_interactive():
            outcome = extras.hook("Notification", {"message": f"{NOTIFICATION_LEADS[kind]}: {question}",
                                                   "notification_type": kind})
            for problem in outcome.problems:
                terminal.note(f"hook: {problem}", "error")
        return ask(question, *rest)

    return asked


class TrustKeepingRuleFile:
    def __init__(self, workspace, terminal: Terminal):
        self.workspace = workspace
        self.tell = terminal.note
        self.rules = permissions.RuleFile(workspace.root / permissions.PROJECT_SETTINGS, terminal.note, workspace.root)

    def setup_still_trusted(self) -> bool:
        state = memory.trust_state(settings.config_dir(), self.workspace.root, setup_fingerprint(self.workspace))
        return state == memory.TRUSTED

    def add_allow(self, rule: str) -> bool:
        trusted_before = self.setup_still_trusted()
        if not self.rules.add_allow(rule):
            return False
        if trusted_before:
            memory.trust(settings.config_dir(), self.workspace.root, setup_fingerprint(self.workspace))
        else:
            self.tell(SETUP_CHANGED_BEFORE_SAVE)
        return True


def project_rule_file(workspace, terminal: Terminal) -> TrustKeepingRuleFile:
    terminal.offers_rules = True
    return TrustKeepingRuleFile(workspace, terminal)


def answered_quietly(session: Session, request: str) -> bool:
    shown, session.terminal.out = session.terminal.out, io.StringIO()
    try:
        return handle(session, request) is not False and session.messages[-1]["role"] == "assistant"
    finally:
        session.terminal.out = shown


def result_event(session: Session, answered: bool) -> dict:
    result = str(session.messages[-1].get("content") or "") if answered else ""
    return {"type": "result", "is_error": not answered, "result": result, "session_id": session.saved.id,
            "model": session.client.model}


def answered_as_json(session: Session, request: str) -> int:
    answered = answered_quietly(session, request)
    JsonLines(session.terminal.out).write(result_event(session, answered))
    return 0 if answered else FAILED


def answered_as_stream(session: Session, request: str) -> int:
    events = JsonLines(session.terminal.out)
    offered = [definition["function"]["name"] for definition in session.offered()]
    events.write({"type": "system", "subtype": "init", "session_id": session.saved.id, "model": session.client.model,
                  "cwd": str(session.workspace.root), "permission_mode": session.gate.mode, "tools": offered})
    session.terminal.events = events
    try:
        answered = answered_quietly(session, request)
    finally:
        session.terminal.events = None
    events.write(result_event(session, answered))
    return 0 if answered else FAILED


def chosen_search(client, config, terminal: Terminal):
    try:
        return websearch.searcher(client, config.search_with or websearch.DESKTOP)
    except brain.BrainError as error:
        terminal.note(f"{error} Searching through Arty's desktop instead.", "error")
        return client.search_web


def session_extras(workspace, trusted: bool, terminal: Terminal, search, looks=None):
    personal = settings.config_dir()
    found_hooks, problems = hooks.load(personal, workspace, trusted)
    wanted, trouble = mcp.configs(personal, workspace, trusted)
    problems += trouble
    found_helpers, trouble = helpers.find(personal, workspace, trusted)
    problems += trouble
    hub = mcp.Hub(workspace, sign_ins_store=sign_ins.Store(personal))
    if wanted:
        terminal.note(f"Starting MCP servers: {', '.join(config.name for config in wanted)}")
        problems += hub.start(wanted)
    for problem in problems:
        terminal.note(view.readable(problem), "error")
    return extras_module.Extras(hooks=found_hooks, skills=skills.find(personal, workspace, trusted), hub=hub,
                                search=search, helpers=found_helpers, looks=looks)


if __name__ == "__main__":
    sys.exit(main())
