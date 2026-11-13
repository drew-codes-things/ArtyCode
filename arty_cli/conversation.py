import contextlib
import json
from dataclasses import dataclass
from pathlib import Path

from arty_cli import extras as extras_module
from arty_cli import rules

from arty_cli import (agent, brain, context, memory, mentions, offering, prompts, providers, prune, remote, settings,
                      skills, tools)
from arty_cli.screen import ERROR_MARK

NOTE_MARK = "#"
SHELL_MARK = "!"
HELP_MARK = "?"
SUMMARY_LEAD = "Carry on from this summary of our conversation so far:"
SUMMARY_HEADING = "Summary of our conversation so far:"
SUMMARY_ACK = "Got it, carrying on from there."
COMPACTED_LEAD = "Carry on with the work from there: the conversation got too long, so this is where it stands."
LAST_OUTPUT_LEAD = "The end of the last command's output ({command}):"
LAST_FILE_LEAD = "The file you changed last, {path}, as it is now (it counts as read):"
CARRIED_OUTPUT_SHARE = 4
CARRIED_FILE_SHARE = 2
PRUNED_NOTE = "The conversation is long, so older results were dropped to make room."
COMMAND_STEP = "run_command "
STEER_LEAD = "(I sent this while you were working; take it into account from here:)"
NOT_STEERING_MARKS = ("/", SHELL_MARK, NOTE_MARK, HELP_MARK)
REQUEST_LEAD = "The request you are working on:"
CHECKLIST_LEAD = "Your checklist:"
DONE_LEAD = "Changes and commands that already ran:"
NO_SUMMARY_LEAD = "The conversation got too long and was cleared to make room."
MAX_LISTED_DONE = 40
MAX_SUMMARISED_RESULT_CHARS = 2_000
STOPPED = "That request stopped before it finished."
ASK_AGAIN = "Ask again when it's back."
HOOK_CONTEXT_LEAD = "(Added by a hook:)"
START_CONTEXT_LEAD = "(Added by a SessionStart hook:)"
MANUAL, AUTOMATIC = "manual", "auto"
STARTUP, RESUME, CLEAR = "startup", "resume", "clear"
MAX_TURN_CHARS = 80
NOTE_ACK = "Noted."
NO_SUMMARY = "The model didn't send a summary, so the conversation was kept as it was."
SUMMARY_REQUEST = ("Summarise this conversation for yourself so the work can carry on with only the summary: what "
                   "the user wants, decisions made, files changed and how, commands run and their results, and "
                   "what is left to do. Be specific with file paths. Plain text, no tools.")
TODO_MARKS = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]"}
SUMMARY_LEADS = (COMPACTED_LEAD, SUMMARY_LEAD, SUMMARY_HEADING, NO_SUMMARY_LEAD)


def is_lasting(error: Exception) -> bool:
    return isinstance(error, (brain.SetupProblem, brain.WindowOverflow))


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


def stopped_note(done: list[str]) -> str:
    return f"{STOPPED} Already done: {'; '.join(done)}." if done else STOPPED


@dataclass(frozen=True)
class Turn:
    index: int
    checkpoints: int
    text: str
    content: object


def changes_after(count: int) -> str:
    return f" ({count} file change{'' if count == 1 else 's'} after it)" if count else ""


class Conversation:
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
        self.last_checkpoint = None
        client.on_thinking = terminal.count_thinking
        self.remote = None
        self.remote_name = ""
        self.situational = set()
        self.notice_situational_tools("")
        self.messages = self.restored(saved)

    def restored(self, saved) -> list[dict]:
        self.workspace.todos = list(getattr(saved, "todos", None) or [])
        self.workspace.checkpoints = list(getattr(saved, "checkpoints", None) or [])
        kept = list(saved.messages or [])
        if not kept:
            return self.fresh()
        return self.fresh() + kept[1:] if kept[0].get("role") == "system" else self.fresh() + kept

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
        self.saved.save(self.messages, self.client.model, todos=self.workspace.todos,
                        checkpoints=self.workspace.checkpoints)

    def offered(self) -> list[dict]:
        return agent.offered_definitions(self.extras, self.situational)

    def notice_situational_tools(self, request: str) -> None:
        if set(offering.SITUATIONAL_TOOLS) <= self.situational:
            return
        self.situational |= offering.wanted_tools(self.workspace.root, request)

    def compact_limit(self) -> int:
        return context.compact_limit(self.client, self.offered())

    def refresh_budget(self) -> None:
        self.workspace.result_chars = context.result_chars(self.compact_limit(), self.client)

    def is_too_long(self, messages: list[dict]) -> bool:
        return context.estimated_tokens(messages, self.client) > self.compact_limit()

    def pruned_enough(self, messages: list[dict], limit: int | None = None) -> bool:
        limit = self.compact_limit() if limit is None else limit
        prune.prune(messages, limit, self.client)
        self.forget_unseen(messages)
        return context.estimated_tokens(messages, self.client) <= limit

    def forget_unseen(self, messages: list[dict]) -> None:
        for path in prune.paths_out_of_view(messages):
            try:
                self.workspace.seen.pop(self.workspace.resolve(path), None)
            except (tools.ToolError, OSError, ValueError):
                continue

    def compact_before_asking(self) -> None:
        if not self.is_too_long(self.messages) or self.pruned_enough(self.messages):
            return
        self.terminal.note("The conversation is long, so it's being summarised first.")
        try:
            self.compact(AUTOMATIC)
        except providers.ERRORS as error:
            self.terminal.note(f"{error} The conversation wasn't summarised; carrying on as it is.", "error")

    def ask(self, text: str) -> bool:
        self.notice_situational_tools(text)
        self.compact_before_asking()
        allowed = self.prompt_hook(text)
        if allowed is None:
            return False
        self.turn_start, self.compacted_in_turn = len(self.messages), False
        self.refresh_budget()
        content = mentions.expand(allowed, self.workspace)
        self.messages.append({"role": "user", "content": content})
        self.turns.append(Turn(self.turn_start, len(self.workspace.checkpoints), text, content))
        self.request, self.done = text, []
        self.last_checkpoint = self.workspace.checkpoints[-1] if self.workspace.checkpoints else None
        self.terminal.start_turn()
        try:
            with self.running(remote.IN_TURN), self.terminal.interruptible():
                agent.run_turn(self.messages, self.client, self.workspace, self.gate, self.terminal, done=self.done,
                               squeeze=self.squeeze, extras=self.extras, steer=self.steering_messages,
                               on_round=self.remember,
                               offered=self.offered() + agent.planning_definitions(self.gate))
        except KeyboardInterrupt:
            self.abandon_turn(self.done, (self.remote.stop_reason() if self.remote else "") or "Stopped.")
            return False
        except providers.ERRORS as error:
            self.abandon_turn(self.done, str(error) if is_lasting(error) else f"{error} {ASK_AGAIN}")
            return False
        finally:
            self.terminal.stopped_listening()
            self.tell_mode()
        self.terminal.end_line()
        self.terminal.finish_turn()
        self.keep_history(str(self.messages[-1].get("content") or "") if self.messages else "")
        self.remember()
        return True

    def changes_this_request(self) -> list:
        checkpoints = self.workspace.checkpoints
        start = checkpoints.index(self.last_checkpoint) + 1 if self.last_checkpoint in checkpoints else 0
        return checkpoints[start:]

    def changed_in_turn(self) -> list[str]:
        shown = [self.workspace.shown(kept.path) for kept in self.changes_this_request()
                 if not rules.is_record_path(self.workspace, kept.path)]
        return list(dict.fromkeys(shown))

    def checked_after_last_change(self) -> bool:
        changes = [index for index, step in enumerate(self.done) if not step.startswith(COMMAND_STEP)]
        last_change = changes[-1] if changes else -1
        return any(step.startswith(COMMAND_STEP) for step in self.done[last_change + 1:])

    def keep_history(self, report: str, stopped: str = "") -> None:
        changed = self.changed_in_turn()
        if not rules.told(self.workspace) or not changed:
            return
        commands = [step.removeprefix(COMMAND_STEP).strip() for step in self.done if step.startswith(COMMAND_STEP)]
        record = rules.Record(self.request, changed, [f"run_command {command}" for command in commands],
                              self.checked_after_last_change(), report, stopped)
        try:
            rules.append_history(self.workspace, record)
        except (tools.ToolError, OSError) as error:
            self.terminal.note(f"Couldn't add to {rules.HISTORY.as_posix()}: {error}", "error")

    def steering_messages(self) -> list[dict]:
        taking = getattr(self.terminal.console, "take_queued", None)
        if taking is None:
            return []
        steered = []
        for text in taking(is_steering_text):
            allowed = self.prompt_hook(text)
            if allowed is None:
                continue
            self.tell_remote({"type": "user", "text": text})
            self.request = f"{self.request}\n{text}"
            steered.append({"role": "user", "content": mentions.expand(f"{STEER_LEAD}\n{allowed}", self.workspace)})
        return steered

    def finished_upto(self) -> int:
        index = kept = self.turn_start + 1
        while index < len(self.messages):
            calls = self.messages[index].get("tool_calls") or []
            answered = self.messages[index + 1:index + 1 + len(calls)]
            if len(answered) < len(calls) or any(message.get("role") != "tool" for message in answered):
                break
            index = kept = index + 1 + len(calls)
        return kept

    def abandon_turn(self, done: list[str], reason: str) -> None:
        self.tell_remote({"type": "error", "text": reason})
        kept = self.finished_upto()
        if not done and not self.compacted_in_turn and kept == self.turn_start + 1:
            del self.messages[self.turn_start:]
            self.terminal.note(f"{reason} That request was dropped from the conversation.", "error")
            return
        del self.messages[kept:]
        self.messages.append({"role": "assistant", "content": stopped_note(done)})
        self.keep_history("", f"stopped before it finished: {reason}")
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
        offered = self.offered()
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

    def last_command_output(self, messages: list[dict]) -> str:
        for step in reversed(prune.steps_of(messages)):
            content = str(messages[step.index].get("content") or "")
            if step.name == "run_command" and not prune.is_stub(content) and agent.EXIT_CODE.search(content):
                tail = content[-(self.workspace.result_chars // CARRIED_OUTPUT_SHARE):]
                return f"{LAST_OUTPUT_LEAD.format(command=step.command())}\n{tail}"
        return ""

    def last_changed_file(self) -> str:
        lines = max(self.workspace.result_chars // CARRIED_FILE_SHARE // tools.AVERAGE_LINE_CHARS, 1)
        for checkpoint in reversed(self.workspace.checkpoints):
            if not checkpoint.path.is_file():
                continue
            try:
                numbered = tools.read_file(self.workspace, str(checkpoint.path), 1, lines)
            except (tools.ToolError, OSError, ValueError):
                continue
            return f"{LAST_FILE_LEAD.format(path=self.workspace.shown(checkpoint.path))}\n{numbered}"
        return ""

    def is_step_on(self, step: prune.Step, path: Path) -> bool:
        try:
            return bool(step.path()) and self.workspace.resolve(step.path()) == path
        except (tools.ToolError, OSError, ValueError):
            return False

    def has_current_view(self, messages: list[dict], path: Path) -> bool:
        lead = LAST_FILE_LEAD.format(path=self.workspace.shown(path))
        steps = prune.steps_of(messages)
        changes = [step.index for step in steps if step.name in tools.CHANGING_TOOLS and self.is_step_on(step, path)
                   and not str(messages[step.index].get("content") or "").startswith(ERROR_MARK)]
        last_change = changes[-1] if changes else -1
        for step in steps:
            content = str(messages[step.index].get("content") or "")
            if step.index <= last_change or prune.is_stub(content):
                continue
            if lead in content or (step.name == prune.READ_TOOL and self.is_step_on(step, path)):
                return True
        return False

    def keep_edited_file_in_view(self, messages: list[dict]) -> None:
        changes = self.changes_this_request()
        if not changes or not messages or messages[-1].get("role") != "tool":
            return
        if self.has_current_view(messages, changes[-1].path):
            return
        view = self.last_changed_file()
        if view:
            messages[-1] = {**messages[-1], "content": f"{messages[-1].get('content') or ''}\n\n{view}"}

    def carried_state(self, messages: list[dict]) -> str:
        output = self.last_command_output(messages)
        self.workspace.seen.clear()
        return "\n\n".join(part for part in (output, self.last_changed_file()) if part)

    def squeeze(self, messages: list[dict], overflowed: bool = False) -> None:
        limit = self.compact_limit()
        limit = prune.overflow_limit(messages, self.client, limit) if overflowed else limit
        if not prune.needs_pruning(messages, limit, self.client):
            return
        pruned = self.pruned_enough(messages, limit)
        if not pruned:
            prune.cut_newest(messages, limit, self.client)
        if pruned or context.estimated_tokens(messages, self.client) <= limit:
            self.keep_edited_file_in_view(messages)
            self.terminal.note(PRUNED_NOTE)
            self.measure_room()
            return
        instructions = self.compact_instructions(AUTOMATIC)
        if instructions is None:
            return
        self.terminal.note("The conversation is long, so it's being summarised before the next step.")
        try:
            summary = f"{SUMMARY_HEADING}\n{self.summary_of(messages, instructions)}"
        except providers.ERRORS as error:
            self.terminal.note(f"{error} Carrying on from the request, the checklist and what was done instead.",
                               "error")
            summary = NO_SUMMARY_LEAD
        parts = (COMPACTED_LEAD, summary, self.work_so_far(), self.carried_state(messages))
        carry_on = "\n\n".join(part for part in parts if part)
        messages[:] = self.fresh() + [{"role": "user", "content": carry_on}]
        self.turns = []
        self.turn_start, self.compacted_in_turn = len(messages) - 1, True
        self.remember()
        self.tell_remote(remote.compacted_event(carry_on, SUMMARY_LEADS))

    def measure_room(self) -> None:
        if self.terminal.measure:
            self.terminal.measure()

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

    def running(self, state: str):
        return self.remote.waker.during(state) if self.remote else contextlib.nullcontext()

    def tell_remote(self, event: dict) -> None:
        if self.remote:
            self.remote.send(event)

    def tell_mode(self) -> None:
        if self.remote:
            self.remote.tell_mode(self.gate.mode)


def is_steering_text(text: str) -> bool:
    return bool(text.strip()) and not text.startswith(NOT_STEERING_MARKS)
