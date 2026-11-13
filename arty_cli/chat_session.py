import socket

from arty_cli import (commands, context, helpers, hooks, mcp, memory, models, permissions, providers, remote, rules,
                      screen, sessions, settings, skills, suggest, tools)
from arty_cli.conversation import NOTE_ACK, RESUME, SUMMARY_ACK, SUMMARY_LEADS, TODO_MARKS, Conversation

SESSIONS_FOLDER = "sessions"
MAX_LISTED_SESSIONS = 20
THIS_SESSION = "  (this one)"
RESUME_HINT = "Carry on with one here with /resume <id>, or start with arty-cli --resume <id>."
NOTE_LEAD = "(I added this note to the project's ARTY.md; follow it from now on:)"
UNDO_LEAD = "(I undid your last file change with /undo:)"
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
        "/resume [id]": "pick an earlier conversation in this project and carry on with it",
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
        "/last": "everything the last step gave back (only a few lines are shown)",
        "/memory": "which ARTY.md notes are loaded",
        "/mcp, /skills": "MCP servers and skills",
        "/helpers, /hooks": "your own helpers and hooks",
    }),
    ("Keys", {
        "Shift+Tab": "next mode: default, auto-edit, plan",
        "Esc": "stop the current request",
        "Ctrl-C": "clear what you typed, twice to leave (at the plain prompt it stops the request)",
        "Ctrl-D, /exit": "leave",
        "Enter while Arty works": "queue a message: Arty reads it at its next step (commands wait)",
        "Alt+Enter": "start another line (so does a line ending in \\)",
        "Up, Down, Ctrl-R": "earlier requests in this project, and searching them",
        "Tab, Enter": "take the highlighted /command, model or @file from the menu",
        "Ctrl-O": "everything the last step gave back, like /last",
        "PageUp, PageDown": "scroll the conversation, as the mouse wheel does (Shift+drag selects text)",
        "?, /help": "this list",
    }),
)
CUSTOM_HELP_HEADING = "Your commands"
HELP_GAP = 2
REMOTE_ON_LEAD = "Remote control is on"
REMOTE_OPEN_IN = "  Open it in the Arty app (More > Computers) or on starfire.ai's Arty CLI page."
REMOTE_RULES = "  A phone needs its Arty CLI switch on to see it. /remote off stops sharing."
REMOTE_OFF = "Remote control is off; this session is no longer shared."
REMOTE_ALREADY_ON = "Remote control is already on. /remote off stops it."
REMOTE_USAGE = "Use /remote to share this session, /remote name <new name> to rename it, or /remote off to stop."
REMOTE_NAME_USAGE = "Give the new name: /remote name <new name>."
REMOTE_NAME_NEEDS_SHARING = "Turn remote control on with /remote first, then rename it."
REMOTE_NAME = "name"


class Session(Conversation):
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

    def resumable(self) -> list:
        listed = sessions.Store(self.personal / SESSIONS_FOLDER).listed(self.workspace.root)
        return [entry for entry in listed if entry.messages][:MAX_LISTED_SESSIONS]

    def session_line(self, entry) -> str:
        return f"{entry.id}  {entry.first_request}{THIS_SESSION if entry.id == self.saved.id else ''}"

    def resume(self, wanted: str) -> None:
        if not wanted:
            self.show_resumable()
            return
        if wanted == self.saved.id:
            self.terminal.note("That is this conversation already.")
            return
        found = sessions.Store(self.personal / SESSIONS_FOLDER).load(self.workspace.root, wanted)
        if found is None:
            self.terminal.note(f"No conversation {wanted} in this project; /resume lists them.", "error")
            return
        self.saved = found
        self.messages = self.restored(found)
        self.turns = []
        self.start_hook(RESUME)
        self.terminal.note(f"Carrying on with {found.id}: {found.first_request}", "green")

    def show_resumable(self) -> None:
        listed = self.resumable()
        if not listed:
            self.terminal.note("No earlier conversations in this project.")
            return
        if self.terminal.console:
            picked = self.terminal.console.choose("Carry on with which conversation?",
                                                  [(entry.id, self.session_line(entry)) for entry in listed])
            if picked:
                self.resume(picked)
            return
        for entry in listed:
            self.terminal.note(self.session_line(entry))
        self.terminal.note(RESUME_HINT)

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
            "/resume": lambda: self.resume(rest.strip()),
            "/remote": lambda: self.switch_remote(rest.strip()),
            "/rules": lambda: self.set_rules(rest.strip()),
            "/mcp": self.show_mcp,
            "/skills": self.show_skills,
            "/helpers": self.show_helpers,
            "/hooks": self.show_hooks,
            "/last": self.terminal.show_last,
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
        if not screen.is_interactive():
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


def rules_level(workspace) -> str:
    found = rules.rules_of(workspace)
    return found.level if found is not None else rules.OFF


def nothing_written(_text: str) -> None:
    return None
