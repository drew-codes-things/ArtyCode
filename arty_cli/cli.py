import argparse
import contextlib
import getpass
import hashlib
import importlib
import io
import json
import os
import socket
import sys
import time
import webbrowser
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlparse

from arty_cli import extras as extras_module
from arty_cli import eyes
from arty_cli import rules

from arty_cli import (__version__, agent, authorise, brain, commands, context, helpers, hooks, lint, mcp, memory,
                       mentions, models, permissions, prompt_line, prompts, providers, remote, repomap, sandbox, sessions,
                       settings, skills, stopkey, suggest, tools, waiting, webfetch,
                       websearch, wrap)
from arty_cli import upkeep

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
REQUEST_LEAD = "The request you are working on:"
CHECKLIST_LEAD = "Your checklist:"
DONE_LEAD = "Changes and commands that already ran:"
NO_SUMMARY_LEAD = "The conversation got too long and was cleared to make room."
MAX_LISTED_DONE = 40
MAX_SUMMARISED_RESULT_CHARS = 2_000
STOPPED = "That request stopped before it finished."
ASK_AGAIN = "Ask again when it's back."
TRUST_QUESTION = ("This folder has its own Arty setup: notes Arty would follow (ARTY.md or AGENTS.md), /commands, skills, helpers, "
                  "or hooks and MCP servers that run programs on this computer. Only say yes if you trust whoever wrote them. "
                  "Trust this folder? [y/n]: ")
CHANGED_QUESTION = ("This folder's hooks, MCP servers or permission rules (.arty/settings.json, .arty/mcp.json) "
                    "changed since you trusted it, so they may run different programs now. Trust it again? [y/n]: ")
REPO_MAP_FOLDER = "repomap"
FINGERPRINTED_FILES = (hooks.PROJECT_FOLDER / hooks.SETTINGS_FILE, mcp.PROJECT_FILE)
SETUP_CHANGED_BEFORE_SAVE = ("This folder's settings or MCP servers changed since you trusted it, so you'll be "
                             "asked about them again next time.")
HOOK_CONTEXT_LEAD = "(Added by a hook:)"
START_CONTEXT_LEAD = "(Added by a SessionStart hook:)"
MANUAL, AUTOMATIC = "manual", "auto"
STARTUP, RESUME, CLEAR = "startup", "resume", "clear"
MAX_TURN_CHARS = 80
PERMISSION_PROMPT, PLAN_APPROVAL = "permission_prompt", "plan_approval"
NOTIFICATION_LEADS = {PERMISSION_PROMPT: "ArtyCode needs your permission",
                      PLAN_APPROVAL: "ArtyCode has a plan for you to approve"}
NOTE_LEAD = "(I added this note to the project's ARTY.md; follow it from now on:)"
NOTE_ACK = "Noted."
UNDO_LEAD = "(I undid your last file change with /undo:)"
NO_SUMMARY = "The model didn't send a summary, so the conversation was kept as it was."
SUMMARY_REQUEST = ("Summarise this conversation for yourself so the work can carry on with only the summary: what "
                   "the user wants, decisions made, files changed and how, commands run and their results, and "
                   "what is left to do. Be specific with file paths. Plain text, no tools.")
HELP_GROUPS = (
    ("Asking", {
        "@path": "attach a file (pictures too)",
        "!command": "run a shell command yourself; Arty sees the output",
        "# note": "add a note to this project's ARTY.md",
        "line \\": "carry on typing on the next line",
        "/init": "look through the project and write ARTY.md",
        "/review": "review the uncommitted changes",
    }),
    ("Going back", {
        "/undo": "put back the last file change Arty made (again for the one before)",
        "/rewind [number]": "list your requests, or go back to before one and its file changes",
        "/clear": "start a fresh conversation",
        "/resume": "list this project's earlier conversations",
        "/compact": "swap the conversation for a summary to free up room",
    }),
    ("Settings", {
        "/model [number|name]": "list Arty's models, or switch to one (remembered)",
        "/mode [name]": f"how much is asked first: {', '.join(permissions.MODES)}",
        "/plan": "plan mode on or off (look and plan, change nothing)",
        "/status": "model, mode, project and conversation size",
        "/remote [off]": "share this chat with your phone or starfire.ai (off to stop)",
        "/rules [on|tell-only|off]": "built-in working rules: told and checked, told only, or off",
    }),
    ("Looking at the setup", {
        "/todos": "the current checklist",
        "/memory": "which ARTY.md notes are loaded",
        "/mcp, /skills": "MCP servers and skills",
        "/helpers, /hooks": "your own helpers and hooks",
    }),
    ("Keys", {
        "Shift+Tab": "next mode: default, auto-edit, plan",
        "Esc": "stop the current request",
        "Ctrl-C": "clear what you typed, twice to leave (at the plain prompt it stops the request)",
        "Ctrl-D, /exit": "leave",
        "Enter while Arty works": "queue your next message for when it finishes",
        "Alt+Enter": "start another line (so does a line ending in \\)",
        "Up, Down, Ctrl-R": "earlier requests in this project, and searching them",
        "Tab, Enter": "take the highlighted /command, model or @file from the menu",
        "?, /help": "this list",
    }),
)
CUSTOM_HELP_HEADING = "Your commands"
HELP_MARK = "?"
HELP_GAP = 2
TRUE_COLOUR_ACCENT = "\033[38;2;229;56;59m"
PALETTE_ACCENT = "\033[38;5;167m"
TRUE_COLOUR_NAMES = ("truecolor", "24bit")
ERROR_STYLE = "\033[1;33m"
COLOURS = {"dim": "\033[2m", "error": ERROR_STYLE, "removed": "\033[31m", "green": "\033[32m", "reset": "\033[0m"}
WORDMARK = "◆ ArtyCode"
STEP_MARK = "●"
THOUGHT_MARK = "✻"
ANSWER_ARROW = "→"
MIN_THOUGHT_SECONDS = 1
STEP_HANG = "  "
OUTCOME_HANG = "    "
CHECKBOX_INDENT = "    "
CHECKBOX_HANG = "      "
OUTCOME_MARK = "└"
HOME_MARK = "~"
WELCOME_LABEL_WIDTH = 8
MODE_MEANINGS = {permissions.DEFAULT: "asks before it changes anything",
                 permissions.AUTO_EDIT: "edits without asking, asks before commands",
                 permissions.PLAN: "looks and plans, changes nothing",
                 permissions.ALLOW_ALL: "asks nothing"}
WELCOME_TIPS = ('Ask in plain words, e.g. "why does the login test fail?" or "add a --dry-run flag"',
                "@file attaches a file · !command runs one yourself · # note saves a project note",
                "? or /help for commands · Ctrl-C stops a request · Ctrl-D leaves")
INTERFACE_TIPS = (*WELCOME_TIPS[:2],
                  "? or /help for commands · Esc stops a request · Ctrl-C twice or Ctrl-D leaves",
                  "Enter while Arty works queues your next message · Alt+Enter starts another line")
NO_NOTES_TIP = "No ARTY.md here yet: /init looks through the project and writes notes Arty reads every time."
ANSWERS = {"y": permissions.YES, "yes": permissions.YES, "n": permissions.NO, "no": permissions.NO,
           "a": permissions.ALWAYS, "always": permissions.ALWAYS}
SAVE_ANSWERS = {"s": permissions.SAVE, "save": permissions.SAVE}
PLAN_ANSWERS = {"y": permissions.YES, "yes": permissions.YES, "a": permissions.ALWAYS, "auto": permissions.ALWAYS,
                "n": permissions.NO, "no": permissions.NO}
PLAN_QUESTION = "Approve the plan?"
PLAN_CHOICES = (("y", "Yes, carry it out, asking before each change"),
                ("a", "Yes, and make its edits without asking (auto-edit)"),
                ("n", "No, keep planning"))
PLAN_FEEDBACK_QUESTION = "What should change? (Enter to skip) "
YES_CHOICE = ("y", "Yes")
ALWAYS_CHOICE = ("a", "Yes, and don't ask again for this until you quit")
SAVE_CHOICE = ("s", "Yes, and save it as an allow rule for this project")
NO_CHOICE = ("n", "No, and tell Arty what to do instead")
INSTEAD_QUESTION = "What should Arty do instead? (Enter to just say no) "
TODO_MARKS = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]"}
CHECKBOXES = {"pending": "☐", "in_progress": "☐", "done": "☑"}
CHECKBOX_COLOURS = {"pending": "reset", "in_progress": "accent", "done": "dim"}
TEXT_OUTPUT, JSON_OUTPUT, STREAM_JSON_OUTPUT = "text", "json", "stream-json"
OUTPUT_FORMATS = (TEXT_OUTPUT, JSON_OUTPUT, STREAM_JSON_OUTPUT)
ERROR_MARK = "Error"
REMOTE_ON_LEAD = "Remote control is on"
REMOTE_OPEN_IN = "  Open it in the Arty app (More > Computers) or on starfire.ai's Arty CLI page."
REMOTE_RULES = "  A phone needs its Arty CLI switch on to see it. /remote off stops sharing."
REMOTE_ANSWER_HINT = "  Or answer Allow or Deny on another device."
REMOTE_OFF = "Remote control is off; this session is no longer shared."
REMOTE_ALREADY_ON = "Remote control is already on. /remote off stops it."
REMOTE_USAGE = "Use /remote to share this session, /remote name <new name> to rename it, or /remote off to stop."
REMOTE_NAME_USAGE = "Give the new name: /remote name <new name>."
REMOTE_NAME_NEEDS_SHARING = "Turn remote control on with /remote first, then rename it."
REMOTE_NAME = "name"
SUMMARY_LEADS = (SUMMARY_LEAD, NO_SUMMARY_LEAD)
REMOTE_ONLY_IN_CHAT = "--remote works in the chat, not with a single question."


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
        self.remote = None
        self.waiting = None
        self.answered_here = False
        self.colour = self.err.isatty() and not os.environ.get("NO_COLOR")
        true_colour = os.environ.get("COLORTERM", "").lower() in TRUE_COLOUR_NAMES
        self.colours = {**COLOURS, "accent": TRUE_COLOUR_ACCENT if true_colour else PALETTE_ACCENT}
        self.stop_key = None
        self.console = None
        self.offered = {}
        self.flow = wrap.Flow()
        self.clock = time.monotonic
        self.thinking_since = None
        self.start_turn()

    def start_turn(self) -> None:
        self.turn_started = time.monotonic()
        self.turn_tokens = 0
        self.streamed_chars = 0
        self.steps = 0

    def tokens_so_far(self) -> int:
        return self.turn_tokens + int(self.streamed_chars / context.CHARS_PER_TOKEN)

    def count_reply(self, reply: dict) -> None:
        self.turn_tokens += reply_tokens(reply)
        self.streamed_chars = 0

    def finish_turn(self) -> None:
        if not self.steps:
            return
        seconds = time.monotonic() - self.turn_started
        counted = f" · {waiting.TOKEN_MARK} {waiting.tokens_text(self.turn_tokens)} tokens" if self.turn_tokens else ""
        self.note(f"Worked for {waiting.elapsed_text(seconds)} · {self.steps} step{'' if self.steps == 1 else 's'}"
                  f"{counted}")

    def shows_status(self) -> bool:
        return self.out.isatty() and self.err.isatty()

    def status_line(self, label: str):
        self.stop_thinking()
        self.waiting = waiting.ThinkingLine(self.shown_now, lambda text: self.paint(text, "dim"), label=label,
                                            started_at=self.turn_started, tokens=self.tokens_so_far)
        return self.waiting

    def working(self, label: str):
        if self.console:
            return self.console.showing(label, self.turn_started, self.tokens_so_far)
        return self.status_line(label) if self.shows_status() else contextlib.nullcontext()

    def interruptible(self):
        if self.console or not (is_interactive() and self.shows_status()):
            return contextlib.nullcontext()
        keys = stopkey.keys_here()
        if keys is None:
            return contextlib.nullcontext()
        self.stop_key = stopkey.StopKey(keys)
        return self.stop_key

    def stopped_listening(self) -> None:
        self.stop_key = None

    def keys_paused(self):
        return self.stop_key.paused() if self.stop_key else contextlib.nullcontext()

    def paint(self, text: str, colour: str) -> str:
        return f"{self.colours[colour]}{text}{self.colours['reset']}" if self.colour else text

    def thinking(self):
        self.thinking_since = self.clock() if self.shows_status() else None
        return self.working(waiting.LABEL)

    def count_thinking(self, text: str) -> None:
        self.streamed_chars += len(text)

    def show_thought(self) -> None:
        since, self.thinking_since = self.thinking_since, None
        if since is None or self.clock() - since < MIN_THOUGHT_SECONDS:
            return
        self.end_line()
        thought = f"{THOUGHT_MARK} Thought for {waiting.elapsed_text(self.clock() - since)}"
        self.err.write(self.paint(thought, "dim") + "\n")
        self.err.flush()

    def shown_now(self, text: str) -> None:
        self.err.write(text)
        self.err.flush()

    def stop_thinking(self) -> None:
        if self.waiting:
            self.waiting.stop()
            self.waiting = None

    def end_line(self) -> None:
        self.stop_thinking()
        if self.mid_line:
            self.out.write("\n")
            self.out.flush()
            self.mid_line = False
        self.flow.reset()

    def err_lines(self, text: str, hang: str | None = None) -> list[str]:
        return wrap.wrapped_line(text, wrap.room(), hang) if self.err.isatty() else [text]

    def show_output(self, text: str) -> None:
        self.end_line()
        self.out.write(text)
        self.out.flush()
        self.mid_line = not text.endswith("\n")

    def show_text(self, text: str) -> None:
        if text:
            self.show_thought()
            self.stop_thinking()
        self.out.write(self.flow.feed(text) if self.out.isatty() else text)
        self.out.flush()
        self.streamed_chars += len(text)
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

    def show_step(self, line: str) -> None:
        self.show_thought()
        self.end_line()
        self.steps += 1
        first, *rest = self.err_lines(f"{STEP_MARK} {line}", STEP_HANG)
        marked = self.paint(STEP_MARK, "accent") + first[len(STEP_MARK):]
        self.err.write("\n".join([marked, *rest]) + "\n")
        self.err.flush()

    def show_todos(self, items: list[dict]) -> None:
        self.end_line()
        for item in items:
            status = item.get("status", "pending")
            box = CHECKBOXES.get(status, CHECKBOXES["pending"])
            for line in self.err_lines(f"{CHECKBOX_INDENT}{box} {item.get('text', '')}", CHECKBOX_HANG):
                self.err.write(self.paint(line, CHECKBOX_COLOURS.get(status, "reset")) + "\n")
        self.err.flush()

    def show_outcome(self, line: str, is_problem: bool) -> None:
        self.end_line()
        for shown in self.err_lines(f"  {OUTCOME_MARK} {line}", OUTCOME_HANG):
            self.err.write(self.paint(shown, "error" if is_problem else "dim") + "\n")
        self.err.flush()

    def show_activity(self, line: str) -> None:
        self.end_line()
        for shown in self.err_lines(f"  · {line}", OUTCOME_HANG):
            self.err.write(self.paint(shown, "dim") + "\n")
        self.err.flush()

    def note(self, line: str, colour: str = "dim") -> None:
        self.end_line()
        for shown in self.err_lines(line):
            self.err.write(self.paint(shown, colour) + "\n")
        self.err.flush()

    def show_plan(self, plan: str) -> None:
        flow = wrap.Flow()
        self.err.write((flow.feed(plan.rstrip()) if self.err.isatty() else plan.rstrip()) + "\n")

    def show_diff(self, preview: str) -> None:
        self.stop_thinking()
        for line in preview.splitlines():
            colour = "green" if line.startswith("+") else "removed" if line.startswith("-") else "dim"
            self.err.write(self.paint(line, colour) + "\n")

    def ask(self, question: str, preview: str = "") -> str:
        with self.keys_paused():
            return self.asked_about(question, preview)

    def asked_about(self, question: str, preview: str = "") -> str:
        if not is_interactive() and not self.remote:
            self.note(f"Refused without asking (no terminal to ask in): {question}", "error")
            return permissions.NO
        self.end_line()
        if preview:
            self.show_diff(preview)
        answers = {**ANSWERS, **SAVE_ANSWERS} if self.offers_rules else ANSWERS
        saving = (SAVE_CHOICE,) if self.offers_rules else ()
        prompt = self.show_choices(question, (YES_CHOICE, ALWAYS_CHOICE, *saving, NO_CHOICE))
        choice, by = self.answer(prompt, answers, question, preview)
        self.answered_here = not by
        return choice

    def instead(self) -> str:
        if not is_interactive() or not self.answered_here:
            return ""
        try:
            with self.keys_paused():
                return self.asked(INSTEAD_QUESTION).strip()
        except EOFError:
            return ""

    def show_choices(self, question: str, choices: tuple) -> str:
        if self.console:
            self.offered = dict(choices)
            self.console.open_question(question, choices)
            return ""
        self.note(question, "accent")
        for key, meaning in choices:
            self.note(f"  {self.paint(key, 'accent')}  {meaning}", "reset")
        if self.remote:
            self.note(REMOTE_ANSWER_HINT)
        keys = [key for key, _meaning in choices]
        return f"Choose {', '.join(keys[:-1])} or {keys[-1]}: "

    def answer(self, prompt: str, answers: dict, question: str, details: str = "") -> tuple[str, str]:
        if not self.console:
            return self.answered(prompt, answers, question, details)
        try:
            return self.answered(prompt, answers, question, details)
        finally:
            self.console.close_question()

    def answered(self, prompt: str, answers: dict, question: str, details: str = "") -> tuple[str, str]:
        if self.remote:
            choice, by = self.remote.answer(self.paint(prompt, "accent"), answers, question, details)
            if by:
                self.note(f"{'Refused' if choice == permissions.NO else 'Allowed'} on {by} (remote control).")
            else:
                self.note_answer(question, choice, answers)
            return choice, by
        if self.console:
            choice = answers.get(self.console.wait_answer() or "", permissions.NO)
            self.note_answer(question, choice, answers)
            return choice, ""
        while True:
            answer = self.asked(prompt).strip().lower()
            if answer in answers:
                return answers[answer], ""

    def note_answer(self, question: str, choice: str, answers: dict) -> None:
        if not self.console:
            return
        meaning = next((self.offered[key] for key in self.offered if answers.get(key) == choice), choice)
        self.note(f"{question} {ANSWER_ARROW} {meaning}", "accent")

    def answer_lines(self):
        return self.console.answer_lines() if self.console else remote.answer_lines(is_interactive)

    def asked(self, question: str) -> str:
        if self.console:
            return self.console.line(question)
        return input(prompt_line.readline_safe(self.paint(question, "accent")))

    def review_plan(self, plan: str) -> tuple[str, str]:
        with self.keys_paused():
            return plan_review(self, plan)


def shortened_value(value):
    if isinstance(value, str):
        return tools.clipped(value, MAX_SUMMARISED_RESULT_CHARS)
    if isinstance(value, dict):
        return {key: shortened_value(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [shortened_value(inner) for inner in value]
    return value


def shortened_call(call: dict) -> dict:
    function = call.get("function") or {}
    try:
        arguments = json.dumps(shortened_value(agent.parsed_arguments(function.get("arguments"))))
    except ValueError:
        return call
    return {**call, "function": {**function, "arguments": arguments}}


def shortened(message: dict) -> dict:
    if message.get("role") == "tool":
        return {**message, "content": tools.clipped(str(message.get("content") or ""), MAX_SUMMARISED_RESULT_CHARS)}
    if message.get("tool_calls"):
        return {**message, "tool_calls": [shortened_call(call) for call in message["tool_calls"]]}
    return message


def reply_tokens(reply: dict) -> int:
    usage = reply.get("usage") or {}
    reported = usage.get("completion_tokens") if isinstance(usage, dict) else None
    if isinstance(reported, int) and reported >= 0:
        return reported
    calls = json.dumps(reply.get("tool_calls") or [], ensure_ascii=False, default=str) if reply.get("tool_calls") else ""
    return context.text_tokens(str(reply.get("content") or "") + calls)


def plan_review(terminal: "Terminal", plan: str) -> tuple[str, str]:
    if not is_interactive() and not terminal.remote:
        terminal.note("Can't approve a plan without a terminal to ask in; staying in plan mode.", "error")
        return permissions.NO, ""
    terminal.end_line()
    terminal.note("Arty's plan:", "accent")
    terminal.show_plan(plan)
    prompt = terminal.show_choices(PLAN_QUESTION, PLAN_CHOICES)
    choice, by = terminal.answer(prompt, PLAN_ANSWERS, PLAN_QUESTION, plan)
    if choice != permissions.NO or by or not is_interactive():
        return choice, ""
    return permissions.NO, terminal.asked(PLAN_FEEDBACK_QUESTION).strip()


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
                        help="how much to ask first (allow-all asks nothing: only in a throwaway folder)")
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


NO_ADDRESS = ("ArtyCode doesn't know where your Arty brain is yet, so nothing was saved. Set its address, then run "
              "arty-cli login again:")
HOME_ADDRESS = "http://<brain address>:8081/v1"
ANYWHERE_ADDRESS = "https://<your address>/v1"
ACCESS_NOTE = "with ARTY_ACCESS_CLIENT_ID and ARTY_ACCESS_CLIENT_SECRET too if Cloudflare Access guards it"


def address_help(windows: bool) -> str:
    if windows:
        home, anywhere = f'$env:ARTY_URL = "{HOME_ADDRESS}"', f'$env:ARTY_REMOTE_URL = "{ANYWHERE_ADDRESS}"'
        keep = f'To keep it for new terminals: setx ARTY_URL "{HOME_ADDRESS}"'
    else:
        home, anywhere = f"export ARTY_URL={HOME_ADDRESS}", f"export ARTY_REMOTE_URL={ANYWHERE_ADDRESS}"
        keep = "To keep it, add the line to ~/.bashrc."
    return "\n".join((NO_ADDRESS, f"  At home:        {home}", f"  From anywhere:  {anywhere}",
                      f"                  {ACCESS_NOTE}", f"  {keep}"))


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
    terminal.note("How should ArtyCode log in to Arty?", "accent")
    for number, (label, _handler) in enumerate(LOGIN_CHOICES, 1):
        terminal.note(f"  {number}. {label}")
    while True:
        answer = input(f"Choose 1-{len(LOGIN_CHOICES)}: ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(LOGIN_CHOICES):
            return LOGIN_CHOICES[int(answer) - 1][1]


def login(terminal: Terminal) -> int:
    current = settings.load()
    if not knows_where_arty_is(current):
        first, *rest = address_help(os.name == "nt").splitlines()
        terminal.note(first, "error")
        for line in rest:
            terminal.note(line, "reset")
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
    if not is_interactive():
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


@dataclass(frozen=True)
class Turn:
    index: int
    checkpoints: int
    text: str
    content: object


def changes_after(count: int) -> str:
    return f" ({count} file change{'' if count == 1 else 's'} after it)" if count else ""


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
        self.turns = []
        self.request, self.done = "", []
        client.on_thinking = terminal.count_thinking
        self.remote = None
        self.remote_name = ""
        self.messages = saved.messages or self.fresh()

    def fresh(self) -> list[dict]:
        notes = memory.instructions(self.workspace, self.personal, self.project_trusted)
        listed = skills.listing(self.extras.skills)
        mapped = self.workspace.repo_map.render() if self.workspace.repo_map else ""
        built_in = rules.instructions(self.workspace)
        notes = "\n\n".join(part for part in (built_in, notes, listed, mapped) if part)
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
        return context.compact_limit(self.client, agent.offered_definitions(self.extras))

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
        content = mentions.expand(allowed, self.workspace)
        self.messages.append({"role": "user", "content": content})
        self.turns.append(Turn(self.turn_start, len(self.workspace.checkpoints), text, content))
        self.request, self.done = text, []
        self.terminal.start_turn()
        try:
            with self.running(remote.IN_TURN), self.terminal.interruptible():
                agent.run_turn(self.messages, self.client, self.workspace, self.gate, self.terminal, done=self.done,
                               squeeze=self.squeeze, extras=self.extras)
        except KeyboardInterrupt:
            self.abandon_turn(self.done, (self.remote.stop_reason() if self.remote else "") or "Stopped.")
            return False
        except providers.ERRORS as error:
            self.abandon_turn(self.done, str(error) if isinstance(error, brain.SetupProblem) else f"{error} {ASK_AGAIN}")
            return False
        finally:
            self.terminal.stopped_listening()
            self.tell_mode()
        self.terminal.end_line()
        self.terminal.finish_turn()
        self.remember()
        return True

    def abandon_turn(self, done: list[str], reason: str) -> None:
        self.tell_remote({"type": "error", "text": reason})
        if not done and not self.compacted_in_turn:
            del self.messages[self.turn_start:]
            self.terminal.note(f"{reason} That request was dropped from the conversation.", "error")
            return
        del self.messages[self.turn_start + 1:]
        self.messages.append({"role": "assistant", "content": stopped_note(done)})
        self.remember()
        self.terminal.note(f"{reason} What already ran is noted in the conversation.", "error")

    def kept_turns(self) -> list[Turn]:
        return [turn for turn in self.turns if turn.index < len(self.messages)
                and self.messages[turn.index].get("role") == "user"
                and self.messages[turn.index].get("content") == turn.content]

    def rewind(self, choice: str) -> None:
        turns = self.kept_turns()
        if not choice:
            self.list_turns(turns)
            return
        if not choice.isdigit() or not 1 <= int(choice) <= len(turns):
            self.terminal.note(f"No request {choice}; /rewind lists them.", "error")
            return
        number = int(choice)
        turn = turns[number - 1]
        self.files_back_to(turn.checkpoints)
        del self.messages[turn.index:]
        self.turns = turns[:number - 1]
        self.remember()
        self.terminal.note(f"Went back to before request {number}. You asked: {turn.text}", "green")

    def list_turns(self, turns: list[Turn]) -> None:
        if not turns:
            self.terminal.note("No requests to go back to since ArtyCode started (or since /clear or a summary).")
            return
        for number, turn in enumerate(turns, 1):
            first_line = turn.text.strip().splitlines()[0] if turn.text.strip() else ""
            undone = len(self.workspace.checkpoints) - turn.checkpoints
            self.terminal.note(f"{number}. {first_line[:MAX_TURN_CHARS]}{changes_after(undone)}")
        self.terminal.note("Go back to before one with /rewind <number>.")

    def files_back_to(self, checkpoints: int) -> None:
        while len(self.workspace.checkpoints) > checkpoints:
            outcome = self.workspace.undo_last()
            undone = outcome.startswith("Undid")
            self.terminal.note(outcome if undone else f"{outcome} Stopped putting files back there.",
                               "green" if undone else "error")
            if not undone:
                return

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
        self.turns = []
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
        request = self.fitted_for_summary(messages) + [{"role": "user", "content": asked}]
        offered = agent.offered_definitions(self.extras)
        summary = (self.client.reply(request, tools=offered, on_text=lambda text: None).get("content") or "").strip()
        if not summary:
            raise providers.ProviderError(NO_SUMMARY)
        return summary

    def fitted_for_summary(self, messages: list[dict]) -> list[dict]:
        fitted = list(messages)
        for index, message in enumerate(messages):
            if not self.is_too_long(fitted):
                break
            fitted[index] = shortened(message)
        return fitted

    def work_so_far(self) -> str:
        parts = [f"{REQUEST_LEAD}\n{self.request}"] if self.request else []
        if self.workspace.todos:
            listed = "\n".join(f"{TODO_MARKS.get(item.get('status'), '[ ]')} {item.get('text', '')}"
                                for item in self.workspace.todos)
            parts.append(f"{CHECKLIST_LEAD}\n{listed}")
        if self.done:
            parts.append(f"{DONE_LEAD}\n" + "\n".join(f"- {step}" for step in self.done[-MAX_LISTED_DONE:]))
        return "\n\n".join(parts)

    def squeeze(self, messages: list[dict]) -> None:
        if not self.is_too_long(messages):
            return
        instructions = self.compact_instructions(AUTOMATIC)
        if instructions is None:
            return
        self.terminal.note("The conversation is long, so it's being summarised before the next step.")
        try:
            summary = f"{SUMMARY_LEAD}\n{self.summary_of(messages, instructions)}"
        except providers.ERRORS as error:
            self.terminal.note(f"{error} Carrying on from the request, the checklist and what was done instead.",
                               "error")
            summary = NO_SUMMARY_LEAD
        carry_on = "\n\n".join(part for part in (summary, self.work_so_far(), CARRY_ON) if part)
        messages[:] = self.fresh() + [{"role": "user", "content": carry_on}]
        self.turns = []
        self.turn_start, self.compacted_in_turn = len(messages) - 1, True
        self.remember()
        self.tell_remote(remote.compacted_event(carry_on, SUMMARY_LEADS))

    def compact(self, trigger: str = MANUAL) -> bool:
        instructions = self.compact_instructions(trigger)
        if instructions is None:
            return False
        summary = self.summary_of(self.messages, instructions)
        self.messages = self.fresh() + [{"role": "user", "content": f"{SUMMARY_LEAD}\n{summary}"},
                                        {"role": "assistant", "content": SUMMARY_ACK}]
        self.turns = []
        self.remember()
        self.tell_remote(remote.compacted_event(summary, SUMMARY_LEADS))
        return True

    def run_shell(self, command: str) -> None:
        output = tools.run("run_command", {"command": command}, self.workspace)
        self.terminal.show_output(output + "\n")
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
        self.terminal.note(f'Remote control: on as "{self.remote_name}"' if self.remote else "Remote control: off")
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

    def show_helpers(self) -> None:
        if not self.extras.helpers:
            self.terminal.note(f"No helpers. Add a <name>.md under {self.personal / helpers.HELPERS_FOLDER} "
                               f"or {helpers.PROJECT_HELPERS} in a trusted project.")
        for helper in self.extras.helpers.values():
            self.terminal.note(f"{helper.name} ({helper.source}): {helper.description} [{', '.join(helper.tools)}]")

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
        self.tell_mode()

    def running(self, state: str):
        return self.remote.waker.during(state) if self.remote else contextlib.nullcontext()

    def tell_remote(self, event: dict) -> None:
        if self.remote:
            self.remote.send(event)

    def tell_mode(self) -> None:
        if self.remote:
            self.remote.tell_mode(self.gate.mode)

    def switch_remote(self, choice: str) -> None:
        if not choice:
            self.start_remote()
        elif choice == "off":
            self.stop_remote()
        elif choice.split()[0] == REMOTE_NAME:
            self.rename_remote(choice[len(REMOTE_NAME):].strip())
        else:
            self.terminal.note(REMOTE_USAGE, "error")

    def start_remote(self) -> None:
        if self.remote:
            self.terminal.note(REMOTE_ALREADY_ON)
            return
        details = {"name": socket.gethostname(), "folder": self.workspace.root.name, "model": self.client.model,
                   "mode": self.gate.mode}
        try:
            bridge = remote.connect(self.client, details, self.terminal.answer_lines())
        except remote.RelayError as error:
            self.terminal.note(f"{error}. Remote control stays off. {remote.next_step(error.status, starting=True)}",
                               "error")
            return
        except providers.ERRORS as error:
            self.terminal.note(f"{error} Remote control stays off.", "error")
            return
        if self.terminal.console:
            bridge.write_prompt = nothing_written
        self.remote = self.terminal.remote = self.terminal.events = bridge
        self.remote_name = bridge.title or details["name"]
        bridge.send_history(remote.history_events(self.messages, SUMMARY_LEADS, SUMMARY_ACK))
        computer = "" if self.remote_name == details["name"] else f" from {details['name']}"
        self.terminal.note(f'{REMOTE_ON_LEAD}: this chat is shared as "{self.remote_name}"{computer}.', "accent")
        self.terminal.note(REMOTE_OPEN_IN)
        self.terminal.note(REMOTE_RULES)

    def rename_remote(self, wanted: str) -> None:
        if not self.remote:
            self.terminal.note(REMOTE_NAME_NEEDS_SHARING)
            return
        if not wanted:
            self.terminal.note(REMOTE_NAME_USAGE, "error")
            return
        try:
            self.remote_name = self.remote.rename(wanted)
        except remote.RelayError as error:
            self.terminal.note(f"{error}. The name stays \"{self.remote_name}\".", "error")
            return
        self.terminal.note(f'{REMOTE_ON_LEAD}: this chat is now shared as "{self.remote_name}".', "accent")

    def stop_remote(self) -> None:
        if not self.remote:
            self.terminal.note(REMOTE_OFF)
            return
        bridge, self.remote = self.remote, None
        self.terminal.remote = self.terminal.events = None
        bridge.stop()
        self.show_remote_notices(bridge)
        self.terminal.note(REMOTE_OFF)

    def show_remote_notices(self, bridge) -> None:
        for notice in bridge.take_notices():
            self.terminal.note(notice, "error")

    def run_remote_requests(self) -> None:
        if not self.remote:
            return
        self.show_remote_notices(self.remote)
        if self.remote.ended:
            self.stop_remote()
            return
        message = self.remote.next_message()
        while message:
            self.terminal.note(f"From {message.by} (remote control): {message.text}", "accent")
            self.ask(message.text)
            if not self.remote:
                return
            message = self.remote.next_message()

    def custom_meanings(self, custom: dict) -> dict[str, str]:
        return {name: f"{command.template.strip().splitlines()[0][:60]} ({command.source})"
                for name, command in custom.items() if name not in commands.BUILT_IN}

    def slash_choices(self) -> dict[str, str]:
        custom = commands.find(self.workspace, self.personal, self.project_trusted)
        return suggest.slash_choices(HELP_GROUPS, self.custom_meanings(custom))

    def show_help(self, custom: dict) -> None:
        mine = self.custom_meanings(custom)
        groups = HELP_GROUPS + (((CUSTOM_HELP_HEADING, mine),) if mine else ())
        width = max(len(usage) for _heading, rows in groups for usage in rows) + HELP_GAP
        for number, (heading, rows) in enumerate(groups):
            if number:
                self.terminal.note("")
            self.terminal.note(heading, "accent")
            for usage, meaning in rows.items():
                self.terminal.note(f"  {usage:{width}}{self.terminal.paint(meaning, 'dim')}", "reset")

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
            "/rewind": lambda: self.rewind(rest.strip()),
            "/resume": self.show_resumable,
            "/remote": lambda: self.switch_remote(rest.strip()),
            "/rules": lambda: self.set_rules(rest.strip()),
            "/mcp": self.show_mcp,
            "/skills": self.show_skills,
            "/helpers": self.show_helpers,
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
            self.terminal.note(unknown_command_note(name, self.slash_choices()), "error")
        return True

    def set_rules(self, wanted: str) -> None:
        if wanted and wanted not in rules.LEVELS:
            self.terminal.note(f"Unknown rules setting {wanted}: use {', '.join(rules.LEVELS[:-1])} or "
                               f"{rules.LEVELS[-1]}.", "error")
            return
        if wanted:
            self.workspace.rules = rules.Rules(wanted)
            if self.messages and self.messages[0].get("role") == "system":
                self.messages[0] = self.fresh()[0]
        level = rules_level(self.workspace)
        self.terminal.note(f"Rules: {level}: {rules.MEANINGS[level]}")

    def listed_models(self) -> models.Choices:
        self.choices = models.Choices(models.listed_for(self.client, self.client.model))
        return self.choices

    def model_choices(self) -> dict[str, str]:
        if self.choices is None:
            try:
                self.listed_models()
            except providers.ERRORS:
                return {}
        return {choice.model: choice.about for choice in self.choices.listed}

    def pick_model(self, listed: models.Choices) -> None:
        offered = [(str(number), f"{choice.model}  {choice.about}".rstrip())
                   for number, choice in enumerate(listed.listed, 1)]
        picked = self.terminal.console.choose(f"Model: {self.client.model}. Switch to which?", offered)
        if picked:
            self.change_model(picked)

    def show_models(self) -> None:
        listed = self.listed_models()
        if self.terminal.console:
            self.pick_model(listed)
            return
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


def unknown_command_note(name: str, choices: dict) -> str:
    guess = suggest.closest(name, choices)
    hint = f" Did you mean {guess}?" if guess else ""
    return f"Unknown command {name}.{hint} Type /help for the list."


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


def shown_folder(folder: Path, home: Path) -> str:
    try:
        inside = folder.relative_to(home)
    except ValueError:
        return str(folder)
    return HOME_MARK if inside == Path() else f"{HOME_MARK}{os.sep}{inside}"


def welcome_row(terminal: Terminal, label: str, value: str, colour: str = "reset") -> None:
    terminal.note(f"  {terminal.paint(f'{label:{WELCOME_LABEL_WIDTH}}', 'dim')}{terminal.paint(value, colour)}", "reset")


def rules_level(workspace) -> str:
    found = rules.rules_of(workspace)
    return found.level if found is not None else rules.OFF


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
        terminal.note("allow-all: nothing is asked first. Use it only in a folder you can throw away.", "error")
    terminal.note("")
    for tip in tips:
        terminal.note(f"  {tip}")
    if not memory.project_note_files(session.workspace.root):
        terminal.note(f"  {NO_NOTES_TIP}")
    terminal.note("")


def nothing_written(_text: str) -> None:
    return None


class SessionHost:
    def __init__(self, session: Session):
        self.session = session
        self.root = session.workspace.root
        self.commands = session.slash_choices
        self.models = session.model_choices
        self.paint = session.terminal.paint

    def mode(self) -> str:
        return self.session.gate.mode

    def cycle_mode(self) -> None:
        self.session.gate.mode = prompt_line.next_mode(self.session.gate.mode)
        self.session.tell_mode()

    def shared(self) -> bool:
        return bool(self.session.remote)


def started_interface(session: Session):
    terminal = session.terminal
    if not (is_interactive() and terminal.shows_status()):
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
    try:
        welcome(session, INTERFACE_TIPS)
        if share:
            session.start_remote()
        talk_in_interface(session, interface)
    finally:
        if session.remote:
            session.stop_remote()
        terminal.end_line()
        terminal.console = None
        interface.stop()
        terminal.out, terminal.err, sys.stdout, sys.stderr = before
    if interface.failure is not None:
        terminal.note(f"The interface stopped unexpectedly: {interface.failure}", "error")


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
        if not carries_on:
            return


def chat(session: Session, share: bool = False, plain: bool = False) -> None:
    interface = None if plain else started_interface(session)
    if interface is not None:
        chat_in_interface(session, interface, share)
        return
    welcome(session)
    reader = prompt_line.line_reader(is_interactive, session.slash_choices, session.model_choices)
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
    tried_login = not config.is_ready() and is_interactive()
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


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if upkeep.handles(argv):
        return upkeep.run(argv)
    arguments = parse_arguments(argv)
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
        chat(session, share=arguments.remote, plain=arguments.plain)
        return 0
    finally:
        for problem in loaded.close():
            terminal.note(problem, "error")


def notifying(ask, extras, terminal: Terminal, kind: str = PERMISSION_PROMPT):
    def asked(question: str, *rest):
        if is_interactive():
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
        self.rules = permissions.RuleFile(workspace.root / permissions.PROJECT_SETTINGS, terminal.note)

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
    hub = mcp.Hub(workspace)
    if wanted:
        terminal.note(f"Starting MCP servers: {', '.join(config.name for config in wanted)}")
        problems += hub.start(wanted)
    for problem in problems:
        terminal.note(problem, "error")
    return extras_module.Extras(hooks=found_hooks, skills=skills.find(personal, workspace, trusted), hub=hub,
                                search=search, helpers=found_helpers, looks=looks)


if __name__ == "__main__":
    sys.exit(main())
