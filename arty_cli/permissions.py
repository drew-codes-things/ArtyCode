import difflib
import fnmatch
import glob
import json
import os
import re
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from arty_cli import tools

YES, NO, ALWAYS, SAVE = "y", "n", "a", "s"
AGREEING = (YES, ALWAYS, SAVE)
RULE_WILDCARDS = re.compile(r"([*?\[])")
MAX_PREVIEW_CHARS = 6_000
READ_ONLY_PROGRAMS = {"ls", "pwd", "cat", "head", "tail", "wc", "grep", "rg", "find", "tree", "which", "file", "stat",
                      "du", "diff", "dir", "type", "get-childitem", "get-content", "select-string", "get-location"}
READ_ONLY_GIT = {"status", "diff", "log", "show", "branch", "blame", "rev-parse", "ls-files"}
FIND_ACTIONS = {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"}
RG_RUNNING_OPTIONS = ("--pre", "--hostname-bin")
GIT_WRITING_OPTIONS = ("--output", "--ext-diff", "--exec-path")
BRANCH_LISTING_FLAGS = {"-a", "--all", "-r", "--remotes", "-v", "-vv", "--verbose", "-l", "--list", "--show-current",
                        "--color", "--no-color", "--column", "--no-column", "-i", "--ignore-case", "--abbrev",
                        "--no-abbrev", "--omit-empty", "-q", "--quiet"}
BRANCH_FILTERS = {"--contains", "--no-contains", "--merged", "--no-merged", "--points-at", "--sort", "--format"}
BRANCH_LIST_ONLY = {"-l", "--list"}
LINK_FOLLOWING_FLAGS = {"grep": ("R", ("--dereference-recursive",)), "rg": ("L", ("--follow",)),
                        "ls": ("L", ("--dereference",)), "du": ("L", ("--dereference",)),
                        "tree": ("loRH", ("--o",)), "file": ("C", ("--compile",)), "diff": ("r", ("--recursive",))}
FIND_LINK_FOLLOWING = {"-L", "-follow"}
SHELL_DIRECTORY_STACK = ("~+", "~-")
WINDOWS_ABSOLUTE = re.compile(r"^([A-Za-z]:|\\\\|//)")
GLOB_CHARACTERS = set("*?[")
GIT_PROGRAM_SETTINGS = re.compile(r"^(core\.(fsmonitor|pager|sshcommand|askpass|editor)|diff\.external|"
                                  r"diff\..+\.(textconv|command)|filter\..+\.(clean|smudge|process)|"
                                  r"gpg(\..+)?\.program|log\.showsignature|include(if)?\..+)=", re.IGNORECASE)
GIT_HARMLESS_VALUES = {"", "false", "true", "0", "1", "no", "yes", "off", "on", "cat", "less"}
GIT_LOCAL_SCOPES = ("local", "worktree", "command")
GIT_CONFIG_SECONDS = 10
SHELL_CONTROL = re.compile(r"[;&|<>`$\n]")
GROUPING = set("(){}")
QUOTES = "'\""
READING_TOOLS = ("read_file", "list_files", "search")
FREE_TOOLS = ("command_output", "stop_command", "todo_write")
EDITS = "edits"
DEFAULT, AUTO_EDIT, PLAN, ALLOW_ALL = "default", "auto-edit", "plan", "allow-all"
MODES = (DEFAULT, AUTO_EDIT, PLAN, ALLOW_ALL)
REFUSED = "The user didn't allow that, so it wasn't done. Don't try another way round it; say so and ask what they'd like."
RULE_DENIED = ("A permission rule in the settings forbids this, so it wasn't done. Don't try another way round it; "
               "tell the user.")
RULE_SHAPE = re.compile(r"^([A-Za-z0-9_*?\[\]-]+)(?:\((.*)\))?$", re.DOTALL)
COMMAND_PARTS = re.compile(r"[;&|\n]+")
SETTINGS_FILE = "settings.json"
PROJECT_SETTINGS = Path(".arty") / SETTINGS_FILE
PATH_TOOLS = ("read_file", "write_file", "edit_file", "multi_edit", "notebook_edit", "search")
PLAN_REFUSAL = ("Plan mode is on: nothing may be changed or run except looking. Finish looking, then set out the plan "
                "step by step and ask the user to switch plan mode off (/mode default) to carry it out.")


def refuse(question: str, preview: str = "") -> str:
    return NO


def words_of(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return []


def literal_words_of(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True)
    lexer.whitespace_split = True
    lexer.escape = ""
    try:
        return list(lexer)
    except ValueError:
        return []


def short_flags(word: str) -> str:
    return word[1:] if word.startswith("-") and not word.startswith("--") else ""


def follows_links_or_writes(program: str, arguments: list[str]) -> bool:
    if program == "find":
        return bool(FIND_LINK_FOLLOWING & set(arguments))
    letters, long_options = LINK_FOLLOWING_FLAGS.get(program, ("", ()))
    return any(set(letters) & set(short_flags(word)) or option_name(word).startswith(long_options)
               for word in arguments if word.startswith("-")) if letters else False


def has_bare_grouping(command: str) -> bool:
    quote = ""
    for character in command:
        if quote:
            quote = "" if character == quote else quote
        elif character in QUOTES:
            quote = character
        elif character in GROUPING:
            return True
    return False


def option_name(word: str) -> str:
    return word.split("=", 1)[0]


def is_branch_listing(arguments: list[str]) -> bool:
    listing_only = bool(BRANCH_LIST_ONLY & set(arguments))
    value_due = False
    for word in arguments:
        if value_due:
            value_due = False
        elif option_name(word) in BRANCH_FILTERS:
            value_due = "=" not in word
        elif word in BRANCH_LISTING_FLAGS or option_name(word) in BRANCH_LISTING_FLAGS:
            continue
        elif word.startswith("-") or not listing_only:
            return False
    return True


def is_read_only_git(arguments: list[str]) -> bool:
    if not arguments or arguments[0] not in READ_ONLY_GIT:
        return False
    if any(word.startswith(GIT_WRITING_OPTIONS) for word in arguments):
        return False
    return arguments[0] != "branch" or is_branch_listing(arguments[1:])


def writes_tree_output(arguments: list[str]) -> bool:
    return any(word.startswith("--o") or (word.startswith("-") and not word.startswith("--") and "o" in word[1:])
               for word in arguments)


def is_read_only_command(command: str) -> bool:
    if SHELL_CONTROL.search(command) or has_bare_grouping(command):
        return False
    words = words_of(command)
    if not words:
        return False
    program, arguments = words[0].lower(), words[1:]
    if program == "git":
        return is_read_only_git(arguments)
    if follows_links_or_writes(program, arguments):
        return False
    if program == "find":
        return not FIND_ACTIONS & set(arguments)
    if program == "rg" and any(word.startswith(RG_RUNNING_OPTIONS) for word in arguments):
        return False
    if program == "tree" and writes_tree_output(arguments):
        return False
    return program in READ_ONLY_PROGRAMS


def paths_among(words: list[str]) -> list[str]:
    found = []
    for word in words[1:]:
        if word.startswith("--"):
            found += [word.split("=", 1)[1]] if "=" in word else []
        elif word.startswith("-"):
            found += [word[2:]] if len(word) > 2 and ("/" in word or "\\" in word or "~" in word) else []
        else:
            found.append(word)
    return [path for path in found if path]


def named_paths(command: str) -> list[str]:
    literal = [path.replace("\\", "/") for path in paths_among(literal_words_of(command))]
    return list(dict.fromkeys(paths_among(words_of(command)) + literal))


def is_directory_stack_form(path: str) -> bool:
    head = path.split("/", 1)[0]
    return head.startswith(SHELL_DIRECTORY_STACK) and head != "~+"


def with_tilde_expanded(path: str, root) -> str:
    if path == "~+" or path.startswith("~+/"):
        return str(root) + path[2:]
    return os.path.expanduser(path)


def git_settings_run_programs(root) -> bool:
    try:
        listed = subprocess.run(["git", "config", "--list", "--show-scope", "--includes"], cwd=root, text=True,
                                capture_output=True, timeout=GIT_CONFIG_SECONDS, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return True
    for line in listed.stdout.splitlines():
        scope, _, setting = line.partition("\t")
        if scope not in GIT_LOCAL_SCOPES or not GIT_PROGRAM_SETTINGS.match(setting):
            continue
        if setting.partition("=")[2].strip().lower() not in GIT_HARMLESS_VALUES:
            return True
    return False


def rule_parts(rule: str) -> tuple[str, str | None] | None:
    found = RULE_SHAPE.match(rule.strip())
    return (found.group(1), found.group(2)) if found else None


def call_target(workspace, name: str, arguments: dict) -> str:
    if name in PATH_TOOLS:
        try:
            return workspace.shown(workspace.resolve(str(arguments.get("path") or ".")))
        except (tools.ToolError, OSError, ValueError):
            return str(arguments.get("path") or "")
    if name == "list_files":
        return str(arguments.get("pattern") or "")
    if name == "run_command":
        return str(arguments.get("command") or "").strip()
    if name == "web_fetch":
        return str(arguments.get("url") or "")
    return ""


def rule_matches(rule: str, name: str, targets: list[str]) -> bool:
    parts = rule_parts(rule)
    if parts is None or not fnmatch.fnmatchcase(name, parts[0]):
        return False
    return parts[1] is None or any(fnmatch.fnmatchcase(target, parts[1].strip()) for target in targets)


@dataclass
class Rules:
    allow: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)

    @classmethod
    def from_lists(cls, allow: list[str], deny: list[str]) -> "Rules":
        return cls(list(allow), list(deny))

    def denies(self, name: str, target: str) -> bool:
        targets = [target] + ([part.strip() for part in COMMAND_PARTS.split(target) if part.strip()]
                              if name == "run_command" else [])
        return any(rule_matches(rule, name, targets) for rule in self.deny)

    def allows(self, name: str, target: str) -> bool:
        if name == "run_command" and (SHELL_CONTROL.search(target) or has_bare_grouping(target)):
            return False
        return any(rule_matches(rule, name, [target]) for rule in self.allow)


def rules_in(path: Path) -> tuple[dict, list[str]]:
    if not path.is_file():
        return {"allow": [], "deny": []}, []
    try:
        table = json.loads(path.read_text(encoding="utf-8")).get("permissions") or {}
    except (OSError, ValueError, AttributeError) as error:
        return {"allow": [], "deny": []}, [f"Couldn't read the permission rules in {path}: {error}"]
    found, problems = {"allow": [], "deny": []}, []
    for kind in found:
        for rule in table.get(kind) or [] if isinstance(table, dict) else []:
            if isinstance(rule, str) and rule_parts(rule):
                found[kind].append(rule.strip())
            else:
                problems.append(f"Skipped the {kind} rule {rule!r} in {path}: write it as tool or tool(pattern)")
    return found, problems


def load_rules(personal_folder: Path, workspace, include_project: bool) -> tuple[Rules, list[str]]:
    places = [Path(personal_folder) / SETTINGS_FILE] + ([workspace.root / PROJECT_SETTINGS] if include_project else [])
    rules, problems = Rules(), []
    for path in places:
        found, trouble = rules_in(path)
        rules.allow += found["allow"]
        rules.deny += found["deny"]
        problems += trouble
    return rules, problems


def literal_pattern(text: str) -> str:
    return RULE_WILDCARDS.sub(r"[\1]", text)


def site_rule(url: str) -> str:
    parts = urlparse(url)
    return f"web_fetch({literal_pattern(f'{parts.scheme}://{parts.netloc}')}/*)" if parts.scheme and parts.netloc else ""


def saved_settings(path: Path) -> dict:
    if not path.is_file():
        return {}
    table = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(table, dict) or not isinstance(table.get("permissions", {}), dict):
        raise ValueError("it isn't a JSON object with a permissions object")
    return table


@dataclass
class RuleFile:
    path: Path
    tell: Callable[[str], None]

    def add_allow(self, rule: str) -> bool:
        try:
            table = saved_settings(self.path)
            rules = table.setdefault("permissions", {})
            allowed = rules.setdefault("allow", [])
            if not isinstance(allowed, list):
                raise ValueError("its allow rules aren't a list")
            if rule not in allowed:
                allowed.append(rule)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(table, indent=2) + "\n", encoding="utf-8")
        except (OSError, ValueError) as error:
            self.tell(f"Couldn't save the rule {rule} to {self.path} ({error}); allowed for this session only.")
            return False
        self.tell(f"Saved the rule {rule} to {self.path}.")
        return True


def diff_preview(workspace, name: str, arguments: dict) -> str:
    try:
        path, before, after = tools.planned_change(name, arguments, workspace)
    except (tools.ToolError, OSError):
        return ""
    shown = workspace.shown(path)
    lines = difflib.unified_diff(before.splitlines(), after.splitlines(), fromfile=shown, tofile=shown, lineterm="")
    preview = "\n".join(lines)
    return preview if len(preview) <= MAX_PREVIEW_CHARS else preview[:MAX_PREVIEW_CHARS] + "\n... (diff cut)"


class Gate:
    def __init__(self, workspace, ask, mode=DEFAULT):
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        self.workspace = workspace
        self.ask = ask
        self.mode = mode
        self.always = set()
        self.rules = Rules()
        self.approved_servers = set()
        self.rule_file = None

    def rule_refusal(self, name: str, arguments: dict) -> str | None:
        return RULE_DENIED if self.rules.denies(name, call_target(self.workspace, name, arguments)) else None

    def rule_allows(self, name: str, arguments: dict) -> bool:
        return self.rules.allows(name, call_target(self.workspace, name, arguments))

    def changes_things(self, name: str, arguments: dict) -> bool:
        if name in READING_TOOLS or name in FREE_TOOLS or name == "web_fetch":
            return False
        if name == "run_command":
            return not is_read_only_command(str(arguments.get("command", ""))) or bool(arguments.get("background"))
        return True

    def outside(self, arguments: dict) -> bool:
        path = arguments.get("path")
        if not path:
            return False
        try:
            return not self.workspace.contains(str(path))
        except tools.ToolError:
            return False

    def reaches_outside(self, path: str) -> bool:
        if is_directory_stack_form(path) or WINDOWS_ABSOLUTE.match(path):
            return True
        expanded = with_tilde_expanded(path, self.workspace.root)
        try:
            return not all(self.workspace.contains(found) for found in self.shell_matches(expanded))
        except (tools.ToolError, OSError, ValueError):
            return True

    def shell_matches(self, path: str) -> list[str]:
        if not GLOB_CHARACTERS & set(path):
            return [path]
        pattern = path if os.path.isabs(path) else str(self.workspace.root / path)
        return glob.glob(pattern) or [path]

    def lists_outside(self, arguments: dict) -> bool:
        try:
            folder, _rest = tools.listing_folder(self.workspace, str(arguments.get("pattern") or ""),
                                                 str(arguments.get("path") or "."))
        except (tools.ToolError, OSError, ValueError):
            return False
        return not folder.is_relative_to(self.workspace.root)

    def rule_for(self, name: str, arguments: dict) -> str:
        if name == "web_fetch":
            return site_rule(str(arguments.get("url") or ""))
        target = call_target(self.workspace, name, arguments)
        rule = f"{name}({literal_pattern(target)})" if target else name
        return rule if self.rules.from_lists([rule], []).allows(name, target) else ""

    def saved_as_rule(self, rule: str) -> bool:
        if self.rule_file is None:
            return False
        if not rule:
            self.rule_file.tell("No rule fits this one, so it's allowed for this session only.")
            return False
        if not self.rule_file.add_allow(rule):
            return False
        self.rules.allow.append(rule)
        return True

    def confirm(self, question: str, preview: str = "", remember: str = "", rule: str = "") -> str | None:
        answer = self.ask(question, preview)
        if answer == SAVE and self.saved_as_rule(rule):
            return None
        if answer in (ALWAYS, SAVE) and remember:
            self.always.add(remember)
        return None if answer in AGREEING else REFUSED

    def check(self, name: str, arguments: dict) -> str | None:
        denied = self.rule_refusal(name, arguments)
        if denied:
            return denied
        if self.mode == ALLOW_ALL or name in FREE_TOOLS:
            return None
        if self.rule_allows(name, arguments) and (self.mode != PLAN or not self.changes_things(name, arguments)):
            return None
        if name in READING_TOOLS:
            if name == "list_files" and self.lists_outside(arguments):
                return self.confirm(f"List files outside the project: {arguments.get('pattern')}?")
            if not self.outside(arguments):
                return None
            return self.confirm(f"Read outside the project: {arguments.get('path')}?")
        if name == "web_fetch":
            return self.check_fetch(str(arguments.get("url", "")))
        if name in tools.CHANGING_TOOLS:
            return PLAN_REFUSAL if self.mode == PLAN else self.check_change(name, arguments)
        if name == "run_command":
            return self.check_command(str(arguments.get("command", "")), bool(arguments.get("background")))
        return self.check_other(name, read_only=False, arguments=arguments)

    def check_other(self, name: str, read_only: bool, preview: str = "", arguments: dict | None = None) -> str | None:
        denied = self.rule_refusal(name, arguments or {})
        if denied:
            return denied
        if self.mode == ALLOW_ALL or name in self.always:
            return None
        if self.rule_allows(name, arguments or {}) and (self.mode != PLAN or read_only):
            return None
        if self.mode == PLAN and not read_only:
            return PLAN_REFUSAL
        return self.confirm(f"Use {name}?", preview, remember=name, rule=self.rule_for(name, arguments or {}))

    def check_server(self, server: str, name: str, read_only: bool, preview: str, arguments: dict) -> str | None:
        denied = self.rule_refusal(name, arguments)
        if denied or self.mode == ALLOW_ALL:
            return denied
        if self.mode == PLAN and not read_only:
            return PLAN_REFUSAL
        if server in self.approved_servers and read_only:
            return None
        if server in self.approved_servers or self.rule_allows(name, arguments) or name in self.always:
            return self.check_other(name, read_only, preview, arguments)
        refusal = self.confirm(f"First use of the MCP server {server} this session: let it run {name}?", preview,
                               remember=name, rule=self.rule_for(name, arguments))
        if refusal is None:
            self.approved_servers.add(server)
        return refusal

    def allows(self, name: str, arguments: dict) -> bool:
        return self.check(name, arguments) is None

    def command_rule(self, command: str) -> str:
        return self.rule_for("run_command", {"command": command})

    def check_fetch(self, url: str) -> str | None:
        site = urlparse(url).netloc
        if site and f"site:{site}" in self.always:
            return None
        return self.confirm(f"Fetch {url}?", remember=f"site:{site}" if site else "", rule=site_rule(url))

    def check_change(self, name: str, arguments: dict) -> str | None:
        preview = diff_preview(self.workspace, name, arguments)
        if self.outside(arguments):
            return self.confirm(f"Change a file outside the project: {arguments.get('path')}?", preview)
        if self.mode == AUTO_EDIT or EDITS in self.always:
            return None
        verb = "Write" if name == "write_file" else "Edit"
        return self.confirm(f"{verb} {arguments.get('path')}?", preview, remember=EDITS,
                            rule=self.rule_for(name, arguments))

    def check_command(self, command: str, background: bool) -> str | None:
        if is_read_only_command(command) and not background:
            return self.check_looking(command)
        if self.mode == PLAN:
            return PLAN_REFUSAL
        if command in self.always:
            return None
        label = "Start in the background" if background else "Run"
        return self.confirm(f"{label}: {command}", remember=command, rule=self.command_rule(command))

    def check_looking(self, command: str) -> str | None:
        if command in self.always:
            return None
        if any(self.reaches_outside(path) for path in named_paths(command)):
            return self.confirm(f"Look outside the project: {command}?", remember=command,
                                rule=self.command_rule(command))
        if words_of(command)[0].lower() == "git" and git_settings_run_programs(self.workspace.root):
            return self.confirm(f"This repository's git settings name a program that git would run. Run: {command}?",
                                remember=command, rule=self.command_rule(command))
        return None
