import argparse
import getpass
import hashlib
import io
import json
import os
import socket
import sys
import webbrowser
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse

from arty_cli import extras as extras_module

from arty_cli import (__version__, agent, authorise, brain, commands, context, hooks, mcp, memory, mentions, models,
                       permissions, prompt_line, prompts, providers, sessions, settings, skills, tools, webfetch)

NOT_SET_UP = 2
FAILED = 1
PROMPT = "› "
CONTINUATION_PROMPT = "… "
LINE_CONTINUES = "\\"
NOTE_MARK = "#"
SHELL_MARK = "!"
SESSIONS_FOLDER = "sessions"
SUMMARY_LEAD = "Summary of our conversation so far:"
SUMMARY_ACK = "Got it, carrying on from there."
CARRY_ON = "Carry on with the work from there."
STOPPED = "That request stopped before it finished."
TRUST_QUESTION = ("This folder has its own Arty setup: notes Arty would follow (ARTY.md), /commands, skills, or hooks "
                  "and MCP servers that run programs on this computer. Only say yes if you trust whoever wrote them. "
                  "Trust this folder? [y/n]: ")
CHANGED_QUESTION = ("This folder's hooks, MCP servers or permission rules (.arty/settings.json, .arty/mcp.json) "
                    "changed since you trusted it, so they may run different programs now. Trust it again? [y/n]: ")
FINGERPRINTED_FILES = (hooks.PROJECT_FOLDER / hooks.SETTINGS_FILE, mcp.PROJECT_FILE)
HOOK_CONTEXT_LEAD = "(Added by a hook:)"
START_CONTEXT_LEAD = "(Added by a SessionStart hook:)"
MANUAL, AUTOMATIC = "manual", "auto"
STARTUP, RESUME, CLEAR = "startup", "resume", "clear"
PERMISSION_PROMPT, PLAN_APPROVAL = "permission_prompt", "plan_approval"
NOTIFICATION_LEADS = {PERMISSION_PROMPT: "Arty CLI needs your permission",
                      PLAN_APPROVAL: "Arty CLI has a plan for you to approve"}
NOTE_LEAD = "(I added this note to the project's ARTY.md; follow it from now on:)"
NOTE_ACK = "Noted."
UNDO_LEAD = "(I undid your last file change with /undo:)"
NO_SUMMARY = "The model didn't send a summary, so the conversation was kept as it was."
SUMMARY_REQUEST = ("Summarise this conversation for yourself so the work can carry on with only the summary: what "
                   "the user wants, decisions made, files changed and how, commands run and their results, and "
                   "what is left to do. Be specific with file paths. Plain text, no tools.")
COMMANDS = {
    "/help": "show this list",
    "/model [number|name]": "list Arty's models, or switch to one (remembered)",
    "/mode [name]": f"show or change how much is asked first: {', '.join(permissions.MODES)}",
    "/plan": "switch plan mode on or off (look and plan, change nothing)",
    "/status": "model, mode, project and conversation size",
    "/todos": "show the current checklist",
    "/memory": "show which ARTY.md notes are loaded",
    "/mcp": "MCP servers and the tools they add",
    "/skills": "skills the model can load",
    "/hooks": "hooks that run on events",
    "/compact": "replace the conversation with a summary to free up room",
    "/undo": "put back the last file change Arty made (again for the one before)",
    "/clear": "start a fresh conversation",
    "/resume": "list this project's earlier conversations",
    "/exit": "leave (Ctrl-D works too)",
}
SHORTCUTS = {
    "@path": "attach a file (pictures too)",
    "# note": "add a note to this project's ARTY.md",
    "!command": "run a shell command yourself; Arty sees the output",
    "line \\": "carry on typing on the next line",
}
TRUE_COLOUR_ACCENT = "\033[38;2;229;56;59m"
PALETTE_ACCENT = "\033[38;5;167m"
TRUE_COLOUR_NAMES = ("truecolor", "24bit")
ERROR_STYLE = "\033[1;33m"
COLOURS = {"dim": "\033[2m", "error": ERROR_STYLE, "removed": "\033[31m", "green": "\033[32m", "reset": "\033[0m"}
WORDMARK = "◆ Arty"
ANSWERS = {"y": permissions.YES, "yes": permissions.YES, "n": permissions.NO, "no": permissions.NO,
           "a": permissions.ALWAYS, "always": permissions.ALWAYS}
SAVE_ANSWERS = {"s": permissions.SAVE, "save": permissions.SAVE}
PLAN_ANSWERS = {"y": permissions.YES, "yes": permissions.YES, "a": permissions.ALWAYS, "auto": permissions.ALWAYS,
                "n": permissions.NO, "no": permissions.NO}
PLAN_QUESTION = "Approve the plan? [y]es / [a]pprove and auto-accept edits / [n]o, keep planning: "
PLAN_FEEDBACK_QUESTION = "What should change? (Enter to skip) "
CHOICES = "[y]es / [n]o / [a]lways"
SAVE_CHOICE = "[s]ave as a project rule"
TODO_MARKS = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]"}
TEXT_OUTPUT, JSON_OUTPUT, STREAM_JSON_OUTPUT = "text", "json", "stream-json"
OUTPUT_FORMATS = (TEXT_OUTPUT, JSON_OUTPUT, STREAM_JSON_OUTPUT)
ERROR_MARK = "Error"


def is_interactive() -> bool:
    return sys.stdin.isatty()


class JsonLines:
    def __init__(self, out):
        self.out = out

    def write(self, event: dict) -> None:
        self.out.write(json.dumps(event, ensure_ascii=False) + "\n")
        self.out.flush()


class Terminal:
    def __init__(self, out=None, err=None):
        self.out = out or sys.stdout
        self.err = err or sys.stderr
        self.events = None
        self.mid_line = False
        self.offers_rules = False
        self.colour = self.err.isatty() and not os.environ.get("NO_COLOR")
        true_colour = os.environ.get("COLORTERM", "").lower() in TRUE_COLOUR_NAMES
        self.colours = {**COLOURS, "accent": TRUE_COLOUR_ACCENT if true_colour else PALETTE_ACCENT}

    def paint(self, text: str, colour: str) -> str:
        return f"{self.colours[colour]}{text}{self.colours['reset']}" if self.colour else text

    def end_line(self) -> None:
        if self.mid_line:
            self.out.write("\n")
            self.out.flush()
            self.mid_line = False

    def show_text(self, text: str) -> None:
        self.out.write(text)
        self.out.flush()
        self.mid_line = not text.endswith("\n")
        if self.events and text:
            self.events.write({"type": "text", "text": text})

    def show_tool_call(self, call: dict) -> None:
        if not self.events:
            return
        name, arguments, _problem = agent.call_parts(call)
        given = (call.get("function") or {}).get("arguments")
        self.events.write({"type": "tool_use", "id": call.get("id") or "", "name": name,
                           "input": given if arguments is None else arguments})

    def show_tool_result(self, call: dict, result: str) -> None:
        if self.events:
            self.events.write({"type": "tool_result", "tool_use_id": call.get("id") or "",
                               "name": (call.get("function") or {}).get("name", ""), "content": result,
                               "is_error": result.startswith(ERROR_MARK)})

    def show_activity(self, line: str) -> None:
        self.end_line()
        self.err.write(self.paint(f"  · {line}", "dim") + "\n")
        self.err.flush()

    def note(self, line: str, colour: str = "dim") -> None:
        self.end_line()
        self.err.write(self.paint(line, colour) + "\n")
        self.err.flush()

    def show_diff(self, preview: str) -> None:
        for line in preview.splitlines():
            colour = "green" if line.startswith("+") else "removed" if line.startswith("-") else "dim"
            self.err.write(self.paint(line, colour) + "\n")

    def ask(self, question: str, preview: str = "") -> str:
        if not is_interactive():
            self.note(f"Refused without asking (no terminal to ask in): {question}", "error")
            return permissions.NO
        self.end_line()
        if preview:
            self.show_diff(preview)
        answers = {**ANSWERS, **SAVE_ANSWERS} if self.offers_rules else ANSWERS
        choices = f"{CHOICES} / {SAVE_CHOICE}" if self.offers_rules else CHOICES
        while True:
            answer = input(self.paint(f"{question} {choices}: ", "accent")).strip().lower()
            if answer in answers:
                return answers[answer]

    def review_plan(self, plan: str) -> tuple[str, str]:
        return plan_review(self, plan)


def plan_review(terminal: "Terminal", plan: str) -> tuple[str, str]:
    if not is_interactive():
        terminal.note("Can't approve a plan without a terminal to ask in; staying in plan mode.", "error")
        return permissions.NO, ""
    terminal.end_line()
    terminal.note("Arty's plan:", "accent")
    terminal.err.write(plan.rstrip() + "\n")
    while True:
        answer = input(terminal.paint(PLAN_QUESTION, "accent")).strip().lower()
        if answer in PLAN_ANSWERS:
            break
    if PLAN_ANSWERS[answer] != permissions.NO:
        return PLAN_ANSWERS[answer], ""
    return permissions.NO, input(terminal.paint(PLAN_FEEDBACK_QUESTION, "accent")).strip()


def parse_arguments(argv):
    parser = argparse.ArgumentParser(prog="arty-cli", description="Arty CLI: reads, edits and runs code in the "
                                     "current folder, asking before it changes anything. Runs on your Arty.")
    parser.add_argument("words", nargs="*", help="`login` to connect to Arty, or a question to ask once")
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
                        help="how much to ask first (allow-all asks nothing: only in a throwaway folder)")
    parser.add_argument("--auto-edit", action="store_true", help="same as --permission-mode auto-edit")
    parser.add_argument("--version", action="version", version=f"arty-cli {__version__}")
    return parser.parse_args(argv)


NO_ADDRESS = ("arty-cli doesn't know where your Arty is yet. Set ARTY_URL (on your home network) or ARTY_REMOTE_URL "
              "(from anywhere, with ARTY_ACCESS_CLIENT_ID and ARTY_ACCESS_CLIENT_SECRET if Cloudflare Access guards it), "
              "then run arty login again. Nothing was saved.")


def knows_where_arty_is(current: settings.Settings) -> bool:
    return bool(current.base_url or current.remote_url)


def arty_with_key(terminal: Terminal, current: settings.Settings) -> str:
    return getpass.getpass("Arty API key: ").strip()


def reaches_the_internet(url: str) -> bool:
    host = urlparse(url).hostname or ""
    try:
        found = {entry[4][0] for entry in socket.getaddrinfo(host, None)}
    except (socket.gaierror, UnicodeError):
        return False
    return any(webfetch.is_public(address) for address in found)


def arty_by_authorising(terminal: Terminal, current: settings.Settings) -> str:
    route = (brain.routes(replace(current, base_url="")) or brain.routes(current) or [None])[0]
    if route is None:
        raise authorise.AuthoriseError("Give at least one Arty brain address.")
    if route.url.startswith("http://"):
        if reaches_the_internet(route.url):
            raise authorise.AuthoriseError(f"{route.url} is a public address over plain http, so the approval and "
                                           "key could be read or swapped on the way. Use the https address instead.")
        terminal.note("Authorising over plain http: only do this on your own home network.", "error")
    request = authorise.start(route.url, route.headers)
    terminal.note(f"Approve this computer in Arty. The code is {request.user_code}", "accent")
    terminal.note(f"Opening {request.verify_url} (open it yourself if no browser appears).")
    webbrowser.open(request.verify_url)
    terminal.note("Waiting for you to approve it...")
    return authorise.wait_for_key(route.url, route.headers, request)


LOGIN_CHOICES = (
    ("API key (paste a key made in Arty)", arty_with_key),
    ("Browser login (opens Arty in your browser to approve this computer)", arty_by_authorising),
)


def chosen_login(terminal: Terminal):
    terminal.note("How should arty-cli log in to Arty?", "accent")
    for number, (label, _handler) in enumerate(LOGIN_CHOICES, 1):
        terminal.note(f"  {number}. {label}")
    while True:
        answer = input(f"Choose 1-{len(LOGIN_CHOICES)}: ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(LOGIN_CHOICES):
            return LOGIN_CHOICES[int(answer) - 1][1]


def login(terminal: Terminal) -> int:
    current = settings.load()
    if not knows_where_arty_is(current):
        terminal.note(NO_ADDRESS, "error")
        return FAILED
    handler = chosen_login(terminal)
    try:
        key = handler(terminal, current)
    except authorise.AuthoriseError as error:
        terminal.note(f"{error} Nothing was saved.", "error")
        return FAILED
    if not key:
        terminal.note("No key was given. Nothing was saved.", "error")
        return FAILED
    terminal.note(f"Saved to {settings.remember(api_key=key)}. Arty answers from now on.", "green")
    return 0


def stopped_note(done: list[str]) -> str:
    return f"{STOPPED} Already done: {'; '.join(done)}." if done else STOPPED


def has_project_setup(workspace) -> bool:
    root = workspace.root
    return ((root / memory.MEMORY_FILE).is_file() or (root / commands.PROJECT_COMMANDS).is_dir()
            or (root / skills.PROJECT_SKILLS).is_dir() or (root / hooks.PROJECT_FOLDER / hooks.SETTINGS_FILE).is_file()
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
    if not is_interactive():
        reason = "its hooks or MCP servers changed since it was trusted" if state == memory.CHANGED else "this folder isn't trusted yet"
        terminal.note(f"Not following this project's ARTY.md, /commands, skills, hooks or MCP servers: {reason}. "
                      "Start arty-cli here in a terminal to trust it.", "error")
        return False
    if state == memory.CHANGED:
        terminal.note("This folder's hooks or MCP servers changed since you trusted it.", "error")
    try:
        answer = input(terminal.paint(CHANGED_QUESTION if state == memory.CHANGED else TRUST_QUESTION, "accent")).strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer in ("y", "yes"):
        memory.trust(personal, workspace.root, fingerprint)
        return True
    terminal.note("Leaving this folder's notes, /commands, skills, hooks and MCP servers out.")
    return False


class Session:
    def __init__(self, client, workspace, gate, terminal, saved, project_trusted=True, extras=None):
        self.client = client
        self.extras = extras or extras_module.NONE
        self.workspace = workspace
        self.gate = gate
        self.terminal = terminal
        self.saved = saved
        self.personal = settings.config_dir()
        self.project_trusted = project_trusted
        self.turn_start = 0
        self.compacted_in_turn = False
        self.choices = None
        self.messages = saved.messages or self.fresh()

    def fresh(self) -> list[dict]:
        notes = memory.instructions(self.workspace, self.personal, self.project_trusted)
        listed = skills.listing(self.extras.skills)
        notes = "\n\n".join(part for part in (notes, listed) if part)
        return [{"role": "system", "content": prompts.system_prompt(self.workspace, notes)}]

    def prompt_hook(self, text: str) -> str | None:
        outcome = self.extras.hook("UserPromptSubmit", {"prompt": text})
        for problem in outcome.problems:
            self.terminal.note(f"hook: {problem}", "error")
        if outcome.blocked:
            self.terminal.note(f"A hook stopped this request: {outcome.feedback or 'no reason given'}", "error")
            return None
        return f"{text}\n\n{HOOK_CONTEXT_LEAD}\n{outcome.context}" if outcome.context else text

    def remember(self) -> None:
        self.saved.save(self.messages, self.client.model)

    def compact_limit(self) -> int:
        return context.compact_limit(self.client)

    def is_too_long(self, messages: list[dict]) -> bool:
        return context.estimated_tokens(messages, self.client) > self.compact_limit()

    def compact_before_asking(self) -> None:
        if not self.is_too_long(self.messages):
            return
        self.terminal.note("The conversation is long, so it's being summarised first.")
        try:
            self.compact(AUTOMATIC)
        except providers.ERRORS as error:
            self.terminal.note(f"{error} The conversation wasn't summarised; carrying on as it is.", "error")

    def ask(self, text: str) -> bool:
        self.compact_before_asking()
        allowed = self.prompt_hook(text)
        if allowed is None:
            return False
        self.turn_start, self.compacted_in_turn = len(self.messages), False
        self.messages.append({"role": "user", "content": mentions.expand(allowed, self.workspace)})
        done = []
        try:
            agent.run_turn(self.messages, self.client, self.workspace, self.gate, self.terminal, done=done,
                           squeeze=self.squeeze, extras=self.extras)
        except KeyboardInterrupt:
            self.abandon_turn(done, "Stopped.")
            return False
        except providers.ERRORS as error:
            self.abandon_turn(done, f"{error} Ask again when it's back.")
            return False
        self.terminal.end_line()
        self.remember()
        return True

    def abandon_turn(self, done: list[str], reason: str) -> None:
        if not done and not self.compacted_in_turn:
            del self.messages[self.turn_start:]
            self.terminal.note(f"{reason} That request was dropped from the conversation.", "error")
            return
        del self.messages[self.turn_start + 1:]
        self.messages.append({"role": "assistant", "content": stopped_note(done)})
        self.remember()
        self.terminal.note(f"{reason} What already ran is noted in the conversation.", "error")

    def start_hook(self, source: str) -> None:
        outcome = self.extras.hook("SessionStart", {"source": source}, source)
        for problem in outcome.problems:
            self.terminal.note(f"hook: {problem}", "error")
        if outcome.blocked and outcome.feedback:
            self.terminal.note(f"SessionStart hook: {outcome.feedback}", "error")
        if outcome.context:
            self.messages.append({"role": "user", "content": f"{START_CONTEXT_LEAD}\n{outcome.context}"})
            self.messages.append({"role": "assistant", "content": NOTE_ACK})

    def clear(self) -> None:
        self.messages = self.fresh()
        self.start_hook(CLEAR)
        self.terminal.note("Fresh conversation.")

    def compact_instructions(self, trigger: str) -> str | None:
        outcome = self.extras.hook("PreCompact", {"trigger": trigger, "custom_instructions": ""}, trigger)
        for problem in outcome.problems:
            self.terminal.note(f"hook: {problem}", "error")
        if outcome.blocked:
            self.terminal.note(f"A PreCompact hook stopped the summary: {outcome.feedback or 'no reason given'}",
                               "error")
            return None
        return outcome.context

    def summary_of(self, messages: list[dict], instructions: str = "") -> str:
        asked = f"{SUMMARY_REQUEST}\n\n{instructions}" if instructions else SUMMARY_REQUEST
        request = messages + [{"role": "user", "content": asked}]
        offered = agent.offered_definitions(self.extras)
        summary = (self.client.reply(request, tools=offered, on_text=lambda text: None).get("content") or "").strip()
        if not summary:
            raise providers.ProviderError(NO_SUMMARY)
        return summary

    def squeeze(self, messages: list[dict]) -> None:
        if not self.is_too_long(messages):
            return
        instructions = self.compact_instructions(AUTOMATIC)
        if instructions is None:
            return
        self.terminal.note("The conversation is long, so it's being summarised before the next step.")
        summary = self.summary_of(messages, instructions)
        messages[:] = self.fresh() + [{"role": "user", "content": f"{SUMMARY_LEAD}\n{summary}\n\n{CARRY_ON}"}]
        self.turn_start, self.compacted_in_turn = len(messages) - 1, True
        self.remember()

    def compact(self, trigger: str = MANUAL) -> bool:
        instructions = self.compact_instructions(trigger)
        if instructions is None:
            return False
        summary = self.summary_of(self.messages, instructions)
        self.messages = self.fresh() + [{"role": "user", "content": f"{SUMMARY_LEAD}\n{summary}"},
                                        {"role": "assistant", "content": SUMMARY_ACK}]
        self.remember()
        return True

    def run_shell(self, command: str) -> None:
        output = tools.run("run_command", {"command": command}, self.workspace)
        self.terminal.show_text(output + "\n")
        self.messages.append({"role": "user", "content": f"(I ran `{command}` myself. Output:)\n{output}"})
        self.messages.append({"role": "assistant", "content": "Noted."})
        self.remember()

    def add_note(self, note: str) -> None:
        path = memory.add_note(self.workspace, note)
        self.messages.append({"role": "user", "content": f"{NOTE_LEAD}\n{note.strip()}"})
        self.messages.append({"role": "assistant", "content": NOTE_ACK})
        self.remember()
        self.terminal.note(f"Added to {path}", "green")

    def undo(self) -> None:
        outcome = self.workspace.undo_last()
        self.terminal.note(outcome, "green" if outcome.startswith("Undid") else "error")
        if outcome.startswith("Undid"):
            self.messages.append({"role": "user", "content": f"{UNDO_LEAD} {outcome}"})
            self.messages.append({"role": "assistant", "content": NOTE_ACK})
            self.remember()

    def show_status(self) -> None:
        self.terminal.note(f"Model: {self.client.model}")
        self.terminal.note(f"Address: {self.client.address() or 'not contacted yet'}")
        self.terminal.note(f"Mode: {self.gate.mode}")
        self.terminal.note(f"Project: {self.workspace.root}")
        self.terminal.note(f"Conversation: {len(self.messages)} messages, about "
                           f"{context.estimated_tokens(self.messages, self.client)} tokens (summarised past "
                           f"{self.compact_limit()} of {context.window_tokens(self.client)}), saved as {self.saved.id}")

    def show_todos(self) -> None:
        if not self.workspace.todos:
            self.terminal.note("No checklist yet.")
        for item in self.workspace.todos:
            self.terminal.note(f"{TODO_MARKS.get(item.get('status'), '[ ]')} {item.get('text', '')}")

    def show_memory(self) -> None:
        files = memory.memory_files(self.workspace, self.personal, self.project_trusted)
        for path in files:
            self.terminal.note(f"{path} ({len(path.read_text(encoding='utf-8', errors='replace'))} characters)")
        if not files:
            self.terminal.note(f"No notes yet. /init writes {self.workspace.root / memory.MEMORY_FILE}; "
                               f"'# note' adds one line; {self.personal / memory.MEMORY_FILE} holds notes for every project.")

    def show_resumable(self) -> None:
        listed = sessions.Store(self.personal / SESSIONS_FOLDER).listed(self.workspace.root)
        if not listed:
            self.terminal.note("No earlier conversations in this project.")
        for entry in listed[:20]:
            self.terminal.note(f"{entry.id}  {entry.first_request}")
        if listed:
            self.terminal.note("Carry on with: arty-cli --resume <id>")

    def show_mcp(self) -> None:
        hub = self.extras.hub
        if hub is None or not hub.servers:
            self.terminal.note(f"No MCP servers. Add them under \"mcpServers\" in {self.personal / mcp.SETTINGS_FILE} "
                               f"or {mcp.PROJECT_FILE} in a trusted project.")
            return
        for name, server in hub.servers.items():
            listed = [tool.exposed for tool in hub.tools.values() if tool.server == name]
            self.terminal.note(f"{name} ({server.config.source}): {len(listed)} tools")
            for exposed in listed:
                self.terminal.note(f"  {exposed}")

    def show_skills(self) -> None:
        if not self.extras.skills:
            self.terminal.note(f"No skills. Add a folder with a SKILL.md under {self.personal / skills.SKILLS_FOLDER} "
                               f"or {skills.PROJECT_SKILLS} in a trusted project.")
        for skill in self.extras.skills.values():
            self.terminal.note(f"{skill.name} ({skill.source}): {skill.description}")

    def show_hooks(self) -> None:
        listed = self.extras.hooks.listed() if self.extras.hooks else []
        if not listed:
            self.terminal.note(f"No hooks. Add them under \"hooks\" in {self.personal / hooks.SETTINGS_FILE} "
                               f"or {hooks.PROJECT_FOLDER / hooks.SETTINGS_FILE} in a trusted project.")
        for hook in listed:
            matcher = f" {hook.matcher}" if hook.matcher else ""
            self.terminal.note(f"{hook.event}{matcher}: {hook.command} ({hook.source})")

    def set_mode(self, mode: str) -> None:
        if mode not in permissions.MODES:
            self.terminal.note(f"Modes: {', '.join(permissions.MODES)}", "error")
            return
        self.gate.mode = mode
        self.terminal.note(f"Mode: {mode}")

    def show_help(self, custom: dict) -> None:
        for usage, meaning in {**COMMANDS, **SHORTCUTS}.items():
            self.terminal.note(f"{usage:16} {meaning}")
        for name, command in custom.items():
            self.terminal.note(f"{name:16} ({command.source}) {command.template.strip().splitlines()[0][:60]}")

    def command(self, line: str) -> bool:
        name, _, rest = line.partition(" ")
        custom = commands.find(self.workspace, self.personal, self.project_trusted)
        actions = {
            "/clear": self.clear,
            "/model": lambda: self.change_model(rest.strip()),
            "/mode": lambda: self.set_mode(rest.strip()) if rest.strip() else self.terminal.note(f"Mode: {self.gate.mode}"),
            "/plan": lambda: self.set_mode(permissions.DEFAULT if self.gate.mode == permissions.PLAN else permissions.PLAN),
            "/status": self.show_status,
            "/todos": self.show_todos,
            "/memory": self.show_memory,
            "/compact": lambda: self.compact() and self.terminal.note("Summarised."),
            "/undo": self.undo,
            "/resume": self.show_resumable,
            "/mcp": self.show_mcp,
            "/skills": self.show_skills,
            "/hooks": self.show_hooks,
            "/help": lambda: self.show_help({k: v for k, v in custom.items() if k not in commands.BUILT_IN}),
        }
        if name in ("/exit", "/quit"):
            return False
        if name in actions:
            try:
                actions[name]()
            except providers.ERRORS as error:
                self.terminal.note(str(error), "error")
        elif name in custom:
            self.ask(custom[name].prompt(rest))
        else:
            self.terminal.note(f"Unknown command {name}. Type /help for the list.", "error")
        return True

    def listed_models(self) -> models.Choices:
        self.choices = models.Choices(models.listed_for(self.client, self.client.model))
        return self.choices

    def show_models(self) -> None:
        listed = self.listed_models()
        for line in listed.lines(self.client.model):
            self.terminal.note(line, "accent" if line.startswith("●") else "dim")
        if not is_interactive():
            self.terminal.note("Switch with /model <number or name>.")
            return
        answer = input("Number or name (Enter keeps the current model): ").strip()
        if answer:
            self.change_model(answer)

    def chosen(self, wanted: str) -> models.Choice | None:
        if not wanted.isdigit():
            return models.Choice(wanted, "")
        listed = self.choices or self.listed_models()
        picked = listed.pick(wanted)
        if picked is None:
            self.terminal.note(f"Pick a number from 1-{len(listed.listed)}; /model shows the list.", "error")
        return picked

    def change_model(self, wanted: str) -> None:
        if not wanted:
            self.show_models()
            return
        choice = self.chosen(wanted)
        if choice is None:
            return
        self.client.model = choice.model
        settings.remember(model=choice.model)
        self.terminal.note(f"Model: {choice.model}, kept for next time.")


def first_line(session, reader) -> str:
    if reader is None:
        return input(PROMPT)
    kept = ""
    while True:
        prompt, width = prompt_line.mode_prompt(session.terminal, session.gate.mode)
        kept, wants_next_mode = reader.read(prompt, width, kept)
        if not wants_next_mode:
            return kept
        session.gate.mode = prompt_line.next_mode(session.gate.mode)


def read_request(session, reader) -> str:
    lines = [first_line(session, reader)]
    while lines[-1].rstrip().endswith(LINE_CONTINUES):
        lines[-1] = lines[-1].rstrip()[:-1].rstrip()
        lines.append(input(CONTINUATION_PROMPT))
    return "\n".join(lines).strip()


def handle(session: Session, line: str) -> bool:
    if line.startswith("/"):
        return session.command(line)
    if line.startswith(SHELL_MARK) and len(line) > 1:
        session.run_shell(line[1:].strip())
    elif line.startswith(NOTE_MARK) and len(line) > 1:
        session.add_note(line[1:].strip())
    else:
        session.ask(line)
    return True


def chat(session: Session) -> None:
    terminal = session.terminal
    terminal.note(f"{terminal.paint(WORDMARK, 'accent')} {terminal.paint(f'CLI {__version__}', 'dim')}", "reset")
    terminal.note(f"{session.client.model} · {session.gate.mode} · "
                  f"{session.workspace.root}", "accent")
    if session.gate.mode == permissions.ALLOW_ALL:
        terminal.note("allow-all: nothing is asked first. Use it only in a folder you can throw away.", "error")
    terminal.note("Ask for anything in this project. /help for commands and shortcuts, Shift+Tab to change mode, "
                  "Ctrl-D to leave.")
    reader = prompt_line.line_reader(is_interactive)
    while True:
        try:
            line = read_request(session, reader)
        except EOFError:
            terminal.end_line()
            return
        except KeyboardInterrupt:
            terminal.note("")
            continue
        if line and not handle(session, line):
            return


def ready_settings(arguments, terminal: Terminal):
    config = settings.load()
    if not config.is_ready() and is_interactive():
        terminal.note("First run: choose how arty-cli connects.", "accent")
        login(terminal)
        config = settings.load()
    if arguments.model:
        config = replace(config, model=arguments.model)
    return config


def saved_conversation(arguments, store: sessions.Store, project: Path, terminal: Terminal):
    if arguments.resume:
        found = store.load(project, arguments.resume)
        if found is None:
            terminal.note(f"No conversation {arguments.resume} in this project; starting a new one.", "error")
        return found or store.start(project)
    if arguments.carry_on:
        return store.latest(project) or store.start(project)
    return store.start(project)


def main(argv=None) -> int:
    arguments = parse_arguments(sys.argv[1:] if argv is None else argv)
    terminal = Terminal()
    try:
        if arguments.words in (["login"], ["setup"]):
            return login(terminal)
        config = ready_settings(arguments, terminal)
        if not config.is_ready():
            terminal.note("arty-cli isn't logged in to Arty yet. Run `arty-cli login` first.", "error")
            return NOT_SET_UP
        workspace = tools.Workspace(arguments.project or Path.cwd())
        try:
            return run_session(arguments, config, workspace, terminal)
        finally:
            workspace.stop_jobs()
    except (*providers.ERRORS, settings.SettingsError) as error:
        terminal.note(str(error), "error")
        return FAILED


def run_session(arguments, config, workspace, terminal: Terminal) -> int:
    mode = permissions.AUTO_EDIT if arguments.auto_edit else arguments.permission_mode
    gate = permissions.Gate(workspace, terminal.ask, mode=mode)
    store = sessions.Store(settings.config_dir() / SESSIONS_FOLDER)
    saved = saved_conversation(arguments, store, workspace.root, terminal)
    client = providers.make(config)
    if saved.model and not arguments.model and brain.is_arty_model(saved.model):
        client.model = saved.model
    trusted = project_notes_allowed(workspace, settings.config_dir(), terminal)
    gate.rules, problems = permissions.load_rules(settings.config_dir(), workspace, trusted)
    if trusted:
        gate.rule_file = project_rule_file(workspace, terminal)
    for problem in problems:
        terminal.note(problem, "error")
    loaded = session_extras(workspace, trusted, terminal, client.search_web)
    gate.ask = notifying(terminal.ask, loaded, terminal)
    gate.review = notifying(terminal.review_plan, loaded, terminal, PLAN_APPROVAL)
    try:
        session = Session(client, workspace, gate, terminal, saved, project_trusted=trusted, extras=loaded)
        session.start_hook(RESUME if saved.messages else STARTUP)
        once = arguments.once or " ".join(arguments.words)
        if once and arguments.output_format == JSON_OUTPUT:
            return answered_as_json(session, once)
        if once and arguments.output_format == STREAM_JSON_OUTPUT:
            return answered_as_stream(session, once)
        if once:
            return 0 if handle(session, once) is not False and session.messages[-1]["role"] == "assistant" else FAILED
        chat(session)
        return 0
    finally:
        loaded.close()


def notifying(ask, extras, terminal: Terminal, kind: str = PERMISSION_PROMPT):
    def asked(question: str, *rest):
        if is_interactive():
            outcome = extras.hook("Notification", {"message": f"{NOTIFICATION_LEADS[kind]}: {question}",
                                                   "notification_type": kind})
            for problem in outcome.problems:
                terminal.note(f"hook: {problem}", "error")
        return ask(question, *rest)

    return asked


def project_rule_file(workspace, terminal: Terminal) -> permissions.RuleFile:
    def tell(line: str) -> None:
        terminal.note(line)
        memory.trust(settings.config_dir(), workspace.root, setup_fingerprint(workspace))

    terminal.offers_rules = True
    return permissions.RuleFile(workspace.root / permissions.PROJECT_SETTINGS, tell)


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
    offered = [definition["function"]["name"] for definition in agent.offered_definitions(session.extras)]
    events.write({"type": "system", "subtype": "init", "session_id": session.saved.id, "model": session.client.model,
                  "cwd": str(session.workspace.root), "permission_mode": session.gate.mode, "tools": offered})
    session.terminal.events = events
    try:
        answered = answered_quietly(session, request)
    finally:
        session.terminal.events = None
    events.write(result_event(session, answered))
    return 0 if answered else FAILED


def session_extras(workspace, trusted: bool, terminal: Terminal, search):
    personal = settings.config_dir()
    found_hooks, problems = hooks.load(personal, workspace, trusted)
    wanted, trouble = mcp.configs(personal, workspace, trusted)
    problems += trouble
    hub = mcp.Hub(workspace)
    if wanted:
        terminal.note(f"Starting MCP servers: {', '.join(config.name for config in wanted)}")
        problems += hub.start(wanted)
    for problem in problems:
        terminal.note(problem, "error")
    return extras_module.Extras(hooks=found_hooks, skills=skills.find(personal, workspace, trusted), hub=hub,
                                search=search)


if __name__ == "__main__":
    sys.exit(main())
