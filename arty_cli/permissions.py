import difflib
import fnmatch
import glob
import itertools
import json
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from arty_cli import eyes, finish, rules, tools

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
GIT_RUNNING_OPTIONS = ("--textconv", "--upload-pack", "--receive-pack", "--exec=", "--open-files-in-pager")
GIT_CONFIG_OPTIONS = ("-c", "--config-env")
GIT_GREP_PAGER_OPTION = "-O"
BRANCH_LISTING_FLAGS = {"-a", "--all", "-r", "--remotes", "-v", "-vv", "--verbose", "-l", "--list", "--show-current",
                        "--color", "--no-color", "--column", "--no-column", "-i", "--ignore-case", "--abbrev",
                        "--no-abbrev", "--omit-empty", "-q", "--quiet"}
BRANCH_FILTERS = {"--contains", "--no-contains", "--merged", "--no-merged", "--points-at", "--sort", "--format"}
BRANCH_LIST_ONLY = {"-l", "--list"}
GREP_LINK_FOLLOWING = ("R", ("--dereference-recursive",))
LINK_FOLLOWING_FLAGS = {"grep": GREP_LINK_FOLLOWING, "egrep": GREP_LINK_FOLLOWING, "fgrep": GREP_LINK_FOLLOWING,
                        "rg": ("L", ("--follow",)),
                        "ls": ("L", ("--dereference",)), "du": ("L", ("--dereference",)),
                        "tree": ("loRH", ("--o",)), "file": ("C", ("--compile",)), "diff": ("r", ("--recursive",))}
FIND_LINK_FOLLOWING = {"-L", "-follow"}
SHELL_DIRECTORY_STACK = ("~+", "~-")
WINDOWS_ABSOLUTE = re.compile(r"^([A-Za-z]:|\\\\|//)")
WINDOWS_PROVIDER_DRIVE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]+:")
FLAG_VALUE_MARKS = ":="
PATH_MARKS = set("/\\~")
GLOB_CHARACTERS = set("*?[")
BASH_ONLY_BRACKETS = re.compile(r"\[(?:\^|!?\[[:=.])")
QUOTED_OR_ESCAPED = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"|\\.", re.DOTALL)
GIT_PROGRAM_SETTINGS = re.compile(r"^(core\.(fsmonitor|pager|sshcommand|askpass|editor)|diff\.external|"
                                  r"diff\..+\.(textconv|command)|filter\..+\.(clean|smudge|process)|"
                                  r"gpg(\..+)?\.program|log\.showsignature|include(if)?\..+)=", re.IGNORECASE)
GIT_HARMLESS_VALUES = {"", "false", "true", "0", "1", "no", "yes", "off", "on", "cat", "less"}
GIT_LOCAL_SCOPES = ("local", "worktree", "command")
GIT_CONFIG_SECONDS = 10
SHELL_CONTROL = re.compile(r"[;&|<>`$\n]")
GROUPING = set("(){}")
QUOTES = "'\""
COMMENT_MARK = "#"
LINE_CONTINUATION = "\\\n"
READING_TOOLS = ("read_file", "list_files", "search", eyes.VIEW_IMAGE)
FREE_TOOLS = ("command_output", "stop_command", "todo_write")
EDITS = "edits"
WEB_SEARCH = "web_search"
DEFAULT, AUTO_EDIT, PLAN, ALLOW_ALL = "default", "auto-edit", "plan", "allow-all"
MODES = (DEFAULT, AUTO_EDIT, PLAN, ALLOW_ALL)
REFUSED_LEAD = "The user didn't allow that, so it wasn't done."
REFUSED = REFUSED_LEAD + " Don't try another way round it; say so and ask what they'd like."
REFUSED_INSTEAD = (REFUSED_LEAD + " They said what to do instead: {said}. Do that, and don't try another way round "
                   "what they refused.")
RULE_DENIED = ("A permission rule in the settings forbids this, so it wasn't done. Don't try another way round it; "
               "tell the user.")
RULE_SHAPE = re.compile(r"^([A-Za-z0-9_*?\[\]-]+)(?:\((.*)\))?$", re.DOTALL)
COMMAND_PARTS = re.compile(r"[;&|\n]+")
SETTINGS_FILE = "settings.json"
PROJECT_SETTINGS = Path(".arty") / SETTINGS_FILE
PATH_TOOLS = ("read_file", "write_file", "edit_file", "multi_edit", "notebook_edit", "search", eyes.VIEW_IMAGE)
LOOKING_TOOLS = (eyes.SCREENSHOT, eyes.HTTP_CHECK)
LOOK_QUESTIONS = {eyes.SCREENSHOT: "Open {url} in a headless browser?", eyes.HTTP_CHECK: "Check {url}?"}
OUTSIDE_PAGE_QUESTION = "Open a file outside the project in a headless browser: {path}?"
PROTECTED_FOLDERS = (".arty", ".git")
PROTECTED_NOTE = "it is part of the project's ArtyCode or git setup, which can run programs"
PLAN_REFUSAL = ("Plan mode is on: nothing may be changed or run except looking. Finish looking, then set out the plan "
                "step by step with present_plan so the user can approve it and switch plan mode off.")
PLAN_TOOL = "present_plan"
PLAN_APPROVED_LEAD = "The user approved the plan."
PLAN_APPROVED = (PLAN_APPROVED_LEAD + " Nothing has been changed yet: anything you tried while plan mode was on was "
                 "refused. Plan mode is off now (mode: {mode}), so carry the plan out with your tools, step by step, "
                 "and then say what you changed.")
PLAN_REJECTED = "The user rejected the plan and plan mode is still on: change nothing yet."
PLAN_FEEDBACK = " Their feedback: {feedback}. Look again if you need to, then present a new plan."
PLAN_NO_FEEDBACK = " They didn't say why; ask what they'd like changed."
NOT_PLANNING = "Error: plan mode isn't on, so there is nothing to approve; carry on with the work."
EMPTY_PLAN = "Error: the plan is empty; write it out step by step."
PREFIX_RULE_END = ":*"
SESSION_PREFIX_MARK = "run_command prefix:"
MAX_SUBCOMMAND_WORDS = 2
PROGRAM_WORD = re.compile(r"^[A-Za-z0-9_./+-]+$")
SUBCOMMAND_WORD = re.compile(r"^[a-z][a-z0-9_:-]*$")
SCRIPT_WORD = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9_./-]*$")
MODULE_WORD = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
MODULE_FLAG = "-m"
RUNNER = re.compile(r"^(python[0-9.]*|pypy[0-9.]*|py|node|nodejs|deno|bun|bunx|npx|pnpx|uvx|ruby|perl|php|lua|java|"
                    r"sh|bash|zsh|dash|ksh|fish|pwsh|powershell|cmd)$")
LAUNCHER_PROGRAMS = {"systemd-run", "systemd-inhibit", "stdbuf", "setsid", "flock", "strace", "ltrace", "chroot",
                     "unshare", "nsenter", "script", "gdb", "lldb", "valgrind", "xvfb-run", "busybox", "toybox",
                     "parallel", "chrt", "taskset", "runuser", "pkexec", "fakeroot", "unbuffer", "dbus-run-session",
                     "ssh-agent", "firejail", "bwrap", "proot", "torsocks", "proxychains", "proxychains4", "faketime",
                     "sg", "newgrp", "start-stop-daemon", "daemonize", "catchsegv", "nocache"}
WRAPPER_PROGRAMS = {"sudo", "su", "doas", "env", "xargs", "eval", "exec", "source", ".", "nohup", "time", "timeout",
                    "nice", "ionice", "watch", "command", "builtin"} | LAUNCHER_PROGRAMS
SHELL_KEYWORDS = {"if", "then", "else", "elif", "fi", "while", "until", "do", "done", "for", "case", "esac", "in",
                  "function", "coproc", "select", "!", "[[", "]]", "{", "}"}
LAUNCHING_SUBCOMMANDS = {"run", "x", "dlx"}
SCRIPT_RUNNING_PROGRAMS = {"make", "gmake", "bmake", "cmake", "ninja", "tar", "zip", "vim", "vi", "nvim", "view",
                           "vimdiff", "gvim", "emacs", "nano", "ex", "ed", "less", "more", "most", "pager", "man",
                           "tmux", "screen"}
NEVER_PREFIXED = WRAPPER_PROGRAMS | SHELL_KEYWORDS | SCRIPT_RUNNING_PROGRAMS
EXACT_ONLY_PROGRAMS = NEVER_PREFIXED | {"rm", "rmdir", "mv", "cp", "ln", "touch", "mkdir", "chmod", "chown", "chgrp",
                                          "dd", "truncate", "shred", "tee", "install", "unlink", "rsync", "scp", "sftp",
                                          "ssh", "curl", "wget", "kill", "pkill", "killall", "find", "sed", "awk",
                                          "gawk", "trap", "alias", "export", "unset", "set", "crontab"}
SUBCOMMAND_PROGRAMS = {"git", "npm", "yarn", "pnpm", "cargo", "go", "docker", "podman", "kubectl", "pip", "pip3", "uv",
                       "poetry", "pipenv", "conda", "dotnet", "gradle", "gradlew", "mvn", "apt", "apt-get", "brew",
                       "systemctl", "gh", "helm", "terraform", "composer", "bundle", "gem", "rustup", "flutter",
                       "dart", "swift"}
ENVIRONMENT_SETTING = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
SHELL_PROGRAMS = {"sh", "bash", "zsh", "dash", "ksh", "fish", "pwsh", "powershell", "cmd"}
UNREADABLE_COMMAND_STARTS = re.compile(r"\$\(|[;&|()`{}\n]")
UNREADABLE_WORD_EDGES = "\"'\\$<>"
MAX_INNER_RUN_WORDS = 64
PIPES = ("|", "|&")
SEPARATORS = ("||", "&&", "|&", "|", "&", ";", "\n")
ENDING_SEPARATORS = (";", "&", "\n")
REDIRECTION = re.compile(r"&?>>?(?:&(?:\d+|-)(?=$|[\s;&|])|[ \t]*([^\s;&|<>()]+))")
DESCRIPTOR_AT_END = re.compile(r"(^|\s)\d+$")
DISCARDED_OUTPUT = "/dev/null"
SUBSTITUTIONS = ("<(", ">(")
EXPANSIONS = "`$"
FILTER_PROGRAMS = {"tail", "head", "grep", "egrep", "fgrep", "wc", "sort", "uniq", "cat"}
FLAGS_ONLY_FILTERS = {"sort", "uniq"}
SORT_WRITING_LETTERS = set("oT")
SORT_WRITING_OPTIONS = ("--o", "--compress", "--temporary")


def refuse(question: str, preview: str = "", scope: str = "") -> str:
    return NO


def program_name(word: str) -> str:
    return Path(word).name.lower().removesuffix(".exe")


def discards_output(command: str, index: int) -> int:
    found = REDIRECTION.match(command, index)
    if found is None or (found.group(1) is not None and found.group(1) != DISCARDED_OUTPUT):
        return -1
    return found.end()


def backslash_escapes() -> bool:
    return sys.platform != "win32"


def starts_a_word(text: str) -> bool:
    return not text or text[-1].isspace()


def separator_at(command: str, index: int) -> str:
    return next((separator for separator in SEPARATORS if command.startswith(separator, index)), "")


def command_pieces(command: str) -> list[tuple[str, str]]:
    pieces, text, joiner, quote, index = [], "", "", "", 0
    while index < len(command):
        character = command[index]
        if quote == "'":
            quote = "" if character == "'" else quote
            text, index = text + character, index + 1
            continue
        if character == "\\" and backslash_escapes():
            escaped = command[index:index + 2]
            text, index = text + ("" if escaped == LINE_CONTINUATION else escaped), index + 2
            continue
        if character in EXPANSIONS:
            return []
        if quote == '"' or character in QUOTES:
            quote = "" if character == quote else quote or character
            text, index = text + character, index + 1
            continue
        if character == COMMENT_MARK and starts_a_word(text):
            return []
        if character in GROUPING or character == "<" or command.startswith(SUBSTITUTIONS, index):
            return []
        if character == ">" or command.startswith("&>", index):
            index = discards_output(command, index)
            if index < 0:
                return []
            text = DESCRIPTOR_AT_END.sub(r"\1", text)
            continue
        separator = separator_at(command, index)
        if separator:
            pieces.append((joiner, text.strip()))
            joiner, text, index = separator, "", index + len(separator)
            continue
        text, index = text + character, index + 1
    if quote:
        return []
    if text.strip() or joiner not in ENDING_SEPARATORS:
        pieces.append((joiner, text.strip()))
    return pieces if all(text for _joiner, text in pieces) else []


def subcommand_words(rest: list[str]) -> list[str] | None:
    taken = []
    for index, word in enumerate(rest[:MAX_SUBCOMMAND_WORDS]):
        if not SUBCOMMAND_WORD.match(word):
            return taken
        if RUNNER.match(word) or word in WRAPPER_PROGRAMS:
            inner = prefix_words(rest[index:])
            return taken + inner if inner else None
        taken.append(word)
    return taken


def runner_target(rest: list[str]) -> list[str]:
    if rest[:1] == [MODULE_FLAG] and len(rest) > 1 and MODULE_WORD.match(rest[1]):
        return rest[:2]
    if rest and SCRIPT_WORD.match(rest[0]):
        return rest[:1]
    return []


def prefix_words(words: list[str]) -> list[str]:
    if not words or not PROGRAM_WORD.match(words[0]):
        return []
    program = program_name(words[0])
    if program in EXACT_ONLY_PROGRAMS:
        return []
    target = runner_target(words[1:]) if RUNNER.match(program) else []
    if RUNNER.match(program) and not target:
        return []
    following = subcommand_words(words[1 + len(target):])
    if following is None or (program in SUBCOMMAND_PROGRAMS and not following):
        return []
    if following and following[-1] in LAUNCHING_SUBCOMMANDS:
        return []
    return [words[0], *target, *following]


def launches_anything(prefix: str) -> bool:
    words = prefix.split()
    return bool(words) and program_name(words[0]) in NEVER_PREFIXED


def is_honoured_allow(rule: str) -> bool:
    parts = rule_parts(rule)
    return parts is None or not launches_anything(command_prefix_of(*parts))


def starts_with_words(words: list[str], prefix: str) -> bool:
    wanted = prefix.split()
    return words[:len(wanted)] == wanted


def without_covered(prefixes: list[str]) -> list[str]:
    unique = list(dict.fromkeys(prefixes))
    return [prefix for prefix in unique
            if not any(other != prefix and starts_with_words(prefix.split(), other) for other in unique)]


def prefix_rule(prefix: str) -> str:
    return f"run_command({prefix}{PREFIX_RULE_END})"


def scope_text(prefixes: list[str]) -> str:
    return ", ".join(f"`{prefix}`" for prefix in prefixes)


def has_flag_letters(arguments: list[str], letters: set[str]) -> bool:
    return any(letters & set(short_flags(word)) for word in arguments)


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
    if git_option_runs_or_writes(arguments):
        return False
    return arguments[0] != "branch" or is_branch_listing(arguments[1:])


def git_option_runs_or_writes(arguments: list[str]) -> bool:
    leading = list(itertools.takewhile(lambda word: word.startswith("-"), arguments))
    if any(word.startswith(GIT_CONFIG_OPTIONS) for word in leading):
        return True
    if any(word.startswith(GIT_WRITING_OPTIONS + GIT_RUNNING_OPTIONS) for word in arguments):
        return True
    subcommand = arguments[len(leading):len(leading) + 1]
    return subcommand == ["grep"] and any(word.startswith(GIT_GREP_PAGER_OPTION) for word in arguments)


def option_runs_or_writes(words: list[str]) -> bool:
    program = program_name(words[0])
    if program == "git":
        return git_option_runs_or_writes(words[1:])
    if program in READ_ONLY_PROGRAMS or program == "find":
        return not is_read_only_piece(shlex.join(words))
    return False


def writes_tree_output(arguments: list[str]) -> bool:
    return any(word.startswith("--o") or (word.startswith("-") and not word.startswith("--") and "o" in word[1:])
               for word in arguments)


def is_read_only_command(command: str) -> bool:
    pieces = command_pieces(command)
    return bool(pieces) and all(is_read_only_piece(text) for _joiner, text in pieces)


def is_read_only_piece(command: str) -> bool:
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


def flag_values(word: str) -> list[str]:
    values = [word[index + 1:] for index, character in enumerate(word) if character in FLAG_VALUE_MARKS]
    if not word.startswith("--") and PATH_MARKS & set(word):
        values += [word[index:] for index in range(2, len(word))]
    return values


def paths_among(words: list[str]) -> list[str]:
    found = []
    for word in words[1:]:
        found += flag_values(word) if word.startswith("-") else [word]
    return [path for path in dict.fromkeys(found) if path]


def names_a_provider_drive(path: str) -> bool:
    return sys.platform == "win32" and bool(WINDOWS_PROVIDER_DRIVE.match(path))


def named_paths(command: str) -> list[str]:
    literal = [path.replace("\\", "/") for path in paths_among(literal_words_of(command))]
    return list(dict.fromkeys(paths_among(words_of(command)) + literal))


def is_directory_stack_form(path: str) -> bool:
    head = path.split("/", 1)[0]
    return head.startswith(SHELL_DIRECTORY_STACK) and head != "~+"


def has_bash_only_wildcard(text: str) -> bool:
    return bool(BASH_ONLY_BRACKETS.search(QUOTED_OR_ESCAPED.sub("", text)))


def shell_matches(path: str, folder: Path) -> list[str]:
    pattern = path if os.path.isabs(path) else str(Path(folder) / path)
    if not GLOB_CHARACTERS & set(path):
        return [pattern]
    return glob.glob(pattern) or [pattern]


def with_tilde_expanded(path: str, folder) -> str:
    if path == "~+" or path.startswith("~+/"):
        return str(folder) + path[2:]
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
    if name in LOOKING_TOOLS:
        return looked_at_address(workspace, arguments)
    if name == WEB_SEARCH:
        return str(arguments.get("query") or "")
    return ""


def looked_at_address(workspace, arguments: dict) -> str:
    try:
        return eyes.page_address(str(arguments.get("url") or ""), workspace.root)
    except tools.ToolError:
        return str(arguments.get("url") or "")


def command_prefix_of(name: str, pattern: str | None) -> str:
    if name != "run_command" or pattern is None or not pattern.strip().endswith(PREFIX_RULE_END):
        return ""
    return pattern.strip().removesuffix(PREFIX_RULE_END).strip()


def rule_matches(rule: str, name: str, targets: list[str]) -> bool:
    parts = rule_parts(rule)
    if parts is None or not fnmatch.fnmatchcase(name, parts[0]):
        return False
    prefix = command_prefix_of(name, parts[1])
    if prefix:
        return any(starts_with_words(words_of(target), prefix) for target in targets)
    return parts[1] is None or any(fnmatch.fnmatchcase(target, parts[1].strip()) for target in targets)


def without_settings_and_keywords(words: list[str]) -> list[str]:
    while words and (ENVIRONMENT_SETTING.match(words[0]) or words[0] in SHELL_KEYWORDS):
        words = words[1:]
    return words


def run_from(words: list[str]) -> str:
    return shlex.join([program_name(words[0].lstrip("\\")), *words[1:]])


def inner_runs(arguments: list[str]) -> list[str]:
    runs = []
    for index, word in enumerate(arguments):
        if any(character.isspace() for character in word):
            runs += program_runs(word)
        elif not word.startswith("-"):
            runs.append(run_from(arguments[index:index + MAX_INNER_RUN_WORDS]))
    return runs


def piece_runs(words: list[str]) -> list[str]:
    words = without_settings_and_keywords(words)
    if not words:
        return []
    program = program_name(words[0].lstrip("\\"))
    launches_others = program in WRAPPER_PROGRAMS or program in SHELL_PROGRAMS
    return [run_from(words)] + (inner_runs(words[1:]) if launches_others else [])


def program_runs(command: str) -> list[str]:
    pieces = command_pieces(command)
    if not pieces:
        return [run for part in UNREADABLE_COMMAND_STARTS.split(command) for run in piece_runs(rough_words(part))]
    return [run for _joiner, text in pieces for run in piece_runs(words_of(text) or text.split())]


def rough_words(part: str) -> list[str]:
    return [word.strip(UNREADABLE_WORD_EDGES) for word in part.split() if word.strip(UNREADABLE_WORD_EDGES)]


def prefix_covers_an_option(rule: str, name: str, target: str) -> bool:
    parts = rule_parts(rule)
    words = words_of(target)
    return bool(parts and command_prefix_of(name, parts[1]) and words and option_runs_or_writes(words))


@dataclass
class Rules:
    allow: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)

    @classmethod
    def from_lists(cls, allow: list[str], deny: list[str]) -> "Rules":
        return cls(list(allow), list(deny))

    def denies(self, name: str, target: str) -> bool:
        targets = [target] + ([part.strip() for part in COMMAND_PARTS.split(target) if part.strip()]
                              + program_runs(target) if name == "run_command" else [])
        return any(rule_matches(rule, name, targets) for rule in self.deny)

    def allows(self, name: str, target: str) -> bool:
        if name == "run_command" and (SHELL_CONTROL.search(target) or has_bare_grouping(target)
                                      or not command_pieces(target)):
            return False
        return any(rule_matches(rule, name, [target]) and not prefix_covers_an_option(rule, name, target)
                   for rule in self.allow if is_honoured_allow(rule))

    def command_prefixes(self) -> list[str]:
        found = [command_prefix_of(*parts) for parts in map(rule_parts, self.allow) if parts]
        return [prefix for prefix in found if prefix and not launches_anything(prefix)]


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


def site_rule(url: str, tool: str = "web_fetch") -> str:
    parts = urlparse(url)
    return f"{tool}({literal_pattern(f'{parts.scheme}://{parts.netloc}')}/*)" if parts.scheme and parts.netloc else ""


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
    root: Path | None = None

    def add_allow(self, rule: str) -> bool:
        try:
            if self.root is not None:
                tools.refuse_linked_place(self.root, self.path)
            table = saved_settings(self.path)
            rules = table.setdefault("permissions", {})
            allowed = rules.setdefault("allow", [])
            if not isinstance(allowed, list):
                raise ValueError("its allow rules aren't a list")
            if rule not in allowed:
                allowed.append(rule)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(table, indent=2) + "\n", encoding="utf-8")
        except (OSError, ValueError, tools.ToolError) as error:
            self.tell(f"Couldn't save the rule {rule} to {self.path} ({error}); allowed for this session only.")
            return False
        self.tell(f"Saved the rule {rule} to {self.path}.")
        return True


def change_verb(name: str, arguments: dict) -> str:
    if name != "write_file":
        return "Edit"
    return "Overwrite" if arguments.get("overwrite") else "Write"


def is_protected_place(path: Path, root: Path) -> bool:
    return path.is_relative_to(root) and any(part.lower() in PROTECTED_FOLDERS for part in path.relative_to(root).parts)


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
        self.review = None
        self.instead = None

    def refused(self) -> str:
        said = self.instead().strip().rstrip(".") if self.instead else ""
        return REFUSED_INSTEAD.format(said=said) if said else REFUSED

    def rule_refusal(self, name: str, arguments: dict) -> str | None:
        return RULE_DENIED if self.rules.denies(name, call_target(self.workspace, name, arguments)) else None

    def rule_allows(self, name: str, arguments: dict) -> bool:
        return self.rules.allows(name, call_target(self.workspace, name, arguments))

    def changes_things(self, name: str, arguments: dict) -> bool:
        if name in READING_TOOLS or name in FREE_TOOLS or name in ("web_fetch", WEB_SEARCH, *LOOKING_TOOLS):
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

    def has_read(self, path: str) -> bool:
        try:
            return self.workspace.resolve(path) in self.workspace.seen
        except (tools.ToolError, OSError, ValueError):
            return False

    def shows_quietly(self, path: str) -> bool:
        arguments = {"path": path}
        if self.rule_refusal("read_file", arguments):
            return False
        if self.mode == ALLOW_ALL or not self.outside(arguments) or self.rule_allows("read_file", arguments):
            return True
        return self.has_read(path)

    def protects(self, arguments: dict) -> bool:
        path = str(arguments.get("path") or "")
        if not path:
            return False
        root = self.workspace.root
        as_written = Path(os.path.normpath(root / os.path.expanduser(path)))
        return any(is_protected_place(candidate, root) for candidate in (as_written, self.workspace.resolve(path)))

    def confirm_protected_change(self, name: str, arguments: dict) -> str | None:
        verb = change_verb(name, arguments)
        question = f"{verb} {arguments.get('path')}? ({PROTECTED_NOTE})"
        return None if self.ask(question, diff_preview(self.workspace, name, arguments)) in AGREEING else self.refused()

    def reaches_outside(self, path: str, folder: Path) -> bool:
        if is_directory_stack_form(path) or WINDOWS_ABSOLUTE.match(path) or names_a_provider_drive(path):
            return True
        expanded = with_tilde_expanded(path, folder)
        try:
            return not all(self.workspace.contains(found) for found in shell_matches(expanded, folder))
        except (tools.ToolError, OSError, ValueError):
            return True

    def command_folder(self, arguments: dict) -> Path:
        cwd = str(arguments.get("cwd") or "")
        if not cwd:
            return self.workspace.root
        try:
            return self.workspace.resolve(cwd)
        except (tools.ToolError, OSError, ValueError):
            return self.workspace.root

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

    def saved_as_rules(self, rules_to_save: list[str]) -> bool:
        return all([self.saved_as_rule(rule) for rule in rules_to_save or [""]])

    def confirm_once(self, question: str) -> str | None:
        return None if self.ask(question, "") in AGREEING else self.refused()

    def confirm(self, question: str, preview: str = "", remember: str = "", rule: str = "") -> str | None:
        return self.confirm_scoped(question, preview, [remember] if remember else [], [rule])

    def confirm_scoped(self, question: str, preview: str, remembered: list[str], rules_to_save: list[str],
                       scope: str = "") -> str | None:
        answer = self.ask(question, preview, scope)
        if answer == SAVE and self.saved_as_rules(rules_to_save):
            return None
        if answer in (ALWAYS, SAVE):
            self.always.update(remembered)
        return None if answer in AGREEING else self.refused()

    def session_prefixes(self) -> list[str]:
        found = [mark.removeprefix(SESSION_PREFIX_MARK) for mark in self.always if mark.startswith(SESSION_PREFIX_MARK)]
        return [prefix for prefix in found if not launches_anything(prefix)]

    def is_quiet_filter(self, words: list[str], folder: Path) -> bool:
        program, arguments = program_name(words[0]), words[1:]
        if program not in FILTER_PROGRAMS or follows_links_or_writes(program, arguments):
            return False
        if program == "cat":
            return not arguments
        if program in FLAGS_ONLY_FILTERS:
            writes = has_flag_letters(arguments, SORT_WRITING_LETTERS) or any(
                word.startswith(SORT_WRITING_OPTIONS) for word in arguments)
            return all(word.startswith("-") and word != "-" for word in arguments) and not writes
        return not any(self.reaches_outside(path, folder) for path in paths_among(words))

    def piece_allowed(self, joiner: str, text: str, prefixes: list[str], folder: Path) -> bool:
        words = words_of(text)
        if not words:
            return False
        if text in self.always or self.rules.allows("run_command", text):
            return True
        if any(starts_with_words(words, prefix) for prefix in prefixes) and not option_runs_or_writes(words):
            return True
        return joiner in PIPES and not has_bash_only_wildcard(text) and self.is_quiet_filter(words, folder)

    def pieces_allowed(self, command: str, folder: Path) -> bool:
        pieces = command_pieces(command)
        prefixes = self.session_prefixes() + self.rules.command_prefixes()
        return bool(pieces) and all(self.piece_allowed(joiner, text, prefixes, folder) for joiner, text in pieces)

    def prefixes_for(self, command: str, folder: Path | None = None) -> list[str]:
        found = []
        for joiner, text in command_pieces(command):
            words = words_of(text)
            if joiner in PIPES and words and self.is_quiet_filter(words, folder or self.workspace.root):
                continue
            prefix = prefix_words(words)
            if not prefix:
                return []
            found.append(" ".join(prefix))
        return without_covered(found)

    def confirm_command(self, question: str, command: str, folder: Path) -> str | None:
        prefixes = self.prefixes_for(command, folder)
        if not prefixes:
            return self.confirm(question, remember=command, rule=self.command_rule(command))
        return self.confirm_scoped(question, "", [SESSION_PREFIX_MARK + prefix for prefix in prefixes],
                                   [prefix_rule(prefix) for prefix in prefixes], scope_text(prefixes))

    def check(self, name: str, arguments: dict) -> str | None:
        denied = self.rule_refusal(name, arguments)
        if denied:
            return denied
        if self.mode != PLAN and rules.is_record_change(self.workspace, name, arguments):
            return rules.append_only_refusal(self.workspace, name, arguments)
        if self.mode == ALLOW_ALL or name in FREE_TOOLS:
            return None
        if name in tools.CHANGING_TOOLS and self.mode != PLAN and self.protects(arguments):
            return self.confirm_protected_change(name, arguments)
        if name == finish.UNDO_TOOL:
            return PLAN_REFUSAL if self.mode == PLAN else self.confirm_once(finish.undo_question(self.workspace))
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
        if name in LOOKING_TOOLS:
            return self.check_look(name, str(arguments.get("url") or ""))
        if name == WEB_SEARCH:
            return self.check_search(str(arguments.get("query", "")))
        if name in tools.CHANGING_TOOLS:
            return PLAN_REFUSAL if self.mode == PLAN else self.check_change(name, arguments)
        if name == "run_command":
            return self.check_command(str(arguments.get("command", "")), bool(arguments.get("background")),
                                      self.command_folder(arguments))
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

    def check_look(self, name: str, url: str) -> str | None:
        try:
            address = eyes.page_address(url, self.workspace.root)
        except tools.ToolError as error:
            return f"Error: {error}"
        parts = urlparse(address)
        if parts.scheme == eyes.FILE_SCHEME:
            place = eyes.file_of(address)
            inside = self.workspace.contains(str(place))
            return None if inside else self.confirm(OUTSIDE_PAGE_QUESTION.format(path=place))
        if eyes.is_this_computer(parts.hostname or ""):
            return None
        remembered = f"{name}:{parts.netloc}"
        if remembered in self.always:
            return None
        return self.confirm(LOOK_QUESTIONS[name].format(url=address), remember=remembered,
                            rule=site_rule(address, name))

    def review_plan(self, plan: str) -> str:
        if self.mode != PLAN:
            return NOT_PLANNING
        if not plan.strip():
            return EMPTY_PLAN
        if self.review is None:
            decision, feedback = self.ask(f"Approve this plan and leave plan mode?\n{plan}", ""), ""
        else:
            decision, feedback = self.review(plan)
        if decision not in AGREEING:
            return PLAN_REJECTED + (PLAN_FEEDBACK.format(feedback=feedback) if feedback else PLAN_NO_FEEDBACK)
        self.mode = AUTO_EDIT if decision == ALWAYS else DEFAULT
        return PLAN_APPROVED.format(mode=self.mode)

    def check_search(self, query: str) -> str | None:
        if WEB_SEARCH in self.always:
            return None
        return self.confirm(f"Search the web for: {query}?", remember=WEB_SEARCH, rule=WEB_SEARCH)

    def check_change(self, name: str, arguments: dict) -> str | None:
        preview = diff_preview(self.workspace, name, arguments)
        if self.outside(arguments):
            return self.confirm(f"Change a file outside the project: {arguments.get('path')}?", preview)
        if self.mode == AUTO_EDIT or EDITS in self.always:
            return None
        verb = change_verb(name, arguments)
        return self.confirm(f"{verb} {arguments.get('path')}?", preview, remember=EDITS,
                            rule=self.rule_for(name, arguments))

    def check_command(self, command: str, background: bool, folder: Path) -> str | None:
        if is_read_only_command(command) and not background:
            return self.check_looking(command, folder)
        if self.mode == PLAN:
            return PLAN_REFUSAL
        if command in self.always or self.pieces_allowed(command, folder):
            return None
        label = "Start in the background" if background else "Run"
        return self.confirm_command(f"{label}: {command}", command, folder)

    def check_looking(self, command: str, folder: Path) -> str | None:
        if command in self.always:
            return None
        pieces = [text for _joiner, text in command_pieces(command)]
        if any(has_bash_only_wildcard(text) or any(self.reaches_outside(path, folder) for path in named_paths(text))
               for text in pieces):
            return self.confirm(f"Look outside the project: {command}?", remember=command,
                                rule=self.command_rule(command))
        runs_git = any(words_of(text)[0].lower() == "git" for text in pieces)
        if runs_git and git_settings_run_programs(self.workspace.root):
            return self.confirm(f"This repository's git settings name a program that git would run. Run: {command}?",
                                remember=command, rule=self.command_rule(command))
        return None
