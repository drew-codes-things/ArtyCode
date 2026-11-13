import contextlib
import io
import json
import os
import sys
import time

from arty_cli import agent, context, permissions, prompt_line, remote, stopkey, view, waiting, wrap

PERMISSION_PROMPT, PLAN_APPROVAL = "permission_prompt", "plan_approval"
NOTIFICATION_LEADS = {PERMISSION_PROMPT: "ArtyCode needs your permission",
                      PLAN_APPROVAL: "ArtyCode has a plan for you to approve"}
TRUE_COLOUR_ACCENT = "\033[38;2;229;56;59m"
PALETTE_ACCENT = "\033[38;5;167m"
TRUE_COLOUR_NAMES = ("truecolor", "24bit")
ERROR_STYLE = "\033[1;33m"
COLOURS = {"dim": "\033[2m", "error": ERROR_STYLE, "removed": "\033[31m", "green": "\033[32m", "reset": "\033[0m"}
STEP_MARK = "●"
THOUGHT_MARK = "✻"
ANSWER_ARROW = "→"
MIN_THOUGHT_SECONDS = 1
STEP_HANG = "  "
OUTCOME_HANG = "    "
CHECKBOX_INDENT = "    "
CHECKBOX_HANG = "      "
OUTCOME_MARK = "└"
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
SCOPED_ALWAYS = "Yes, and don't ask again for {scope} commands until you quit"
SCOPED_SAVE = "Yes, and save an allow rule for {scope} commands in this project"
NO_CHOICE = ("n", "No, and tell Arty what to do instead")
INSTEAD_QUESTION = "What should Arty do instead? (Enter to just say no) "
CHECKBOXES = {"pending": "☐", "in_progress": "☐", "done": "☑"}
CHECKBOX_COLOURS = {"pending": "reset", "in_progress": "accent", "done": "dim"}
ERROR_MARK = "Error"
REMOTE_ANSWER_HINT = "  Or answer Allow or Deny on another device."


def is_interactive() -> bool:
    return sys.stdin.isatty()


def write_utf8_when_redirected(streams=None, platform: str = sys.platform) -> None:
    if platform != "win32":
        return
    for stream in streams if streams is not None else (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper) and not stream.isatty():
            stream.reconfigure(encoding="utf-8", errors="replace")


def scoped_choice(choice: tuple[str, str], scoped: str, scope: str) -> tuple[str, str]:
    return (choice[0], scoped.format(scope=scope)) if scope else choice


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
        self.view = None
        self.measure = None
        self.last_step = self.last_output_step = None
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
        seconds = time.monotonic() - self.turn_started
        if self.view:
            self.view.turn_finished(seconds, f"Worked for {waiting.elapsed_text(seconds)}")
        if not self.steps:
            return
        if self.view:
            self.view.settle()
            self.view.gap()
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
        if self.view:
            self.view.thought(thought)
            return
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
        if self.view:
            self.view.settle()
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
        if self.view:
            self.view.text(text)
        else:
            shown = view.readable(text)
            self.out.write(self.flow.feed(shown) if self.out.isatty() else shown)
            self.out.flush()
            self.mid_line = not text.endswith("\n")
        self.streamed_chars += len(text)
        if self.events and text:
            self.events.write({"type": "text", "text": text})

    def show_tool_call(self, call: dict) -> None:
        if self.view:
            self.view.tool_call(call)
        if not self.events:
            return
        name, arguments, _problem = agent.call_parts(call)
        given = (call.get("function") or {}).get("arguments")
        self.events.write({"type": "tool_use", "id": call.get("id") or "", "name": name,
                           "input": given if arguments is None else arguments})

    def show_tool_result(self, call: dict, result: str) -> None:
        name, arguments = view.call_details(call)
        self.last_step = view.Step(agent.step_line(name, arguments), name, arguments, result)
        if name in view.OUTPUT_TOOLS:
            self.last_output_step = self.last_step
        if self.view:
            self.view.tool_result(call, result)
        if self.measure:
            self.measure()
        if self.events:
            self.events.write({"type": "tool_result", "tool_use_id": call.get("id") or "",
                               "name": (call.get("function") or {}).get("name", ""), "content": result,
                               "is_error": result.startswith(ERROR_MARK)})

    def show_step(self, line: str) -> None:
        self.show_thought()
        self.end_line()
        self.steps += 1
        if self.view:
            self.view.step(line)
            return
        first, *rest = self.err_lines(f"{STEP_MARK} {view.readable(line)}", STEP_HANG)
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
        line = view.readable(line)
        if self.view:
            self.stop_thinking()
            self.view.outcome(line, is_problem)
            return
        self.end_line()
        for shown in self.err_lines(f"  {OUTCOME_MARK} {line}", OUTCOME_HANG):
            self.err.write(self.paint(shown, "error" if is_problem else "dim") + "\n")
        self.err.flush()

    def show_activity(self, line: str) -> None:
        self.end_line()
        for shown in self.err_lines(f"  · {view.readable(line)}", OUTCOME_HANG):
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
        if self.view:
            self.view.question_diff(preview)
            return
        for line in preview.splitlines():
            colour = "green" if line.startswith("+") else "removed" if line.startswith("-") else "dim"
            self.err.write(self.paint(line, colour) + "\n")

    def ask(self, question: str, preview: str = "", scope: str = "") -> str:
        with self.keys_paused():
            return self.asked_about(question, preview, scope)

    def asked_about(self, question: str, preview: str = "", scope: str = "") -> str:
        question = view.readable(question)
        if not is_interactive() and not self.remote:
            self.note(f"Refused without asking (no terminal to ask in): {question}", "error")
            return permissions.NO
        self.end_line()
        if preview:
            self.show_diff(preview)
        answers = {**ANSWERS, **SAVE_ANSWERS} if self.offers_rules else ANSWERS
        saving = (scoped_choice(SAVE_CHOICE, SCOPED_SAVE, scope),) if self.offers_rules else ()
        always = scoped_choice(ALWAYS_CHOICE, SCOPED_ALWAYS, scope)
        prompt = self.show_choices(question, (YES_CHOICE, always, *saving, NO_CHOICE))
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
            if self.view:
                self.view.alert(NOTIFICATION_LEADS[PERMISSION_PROMPT], question)
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
        self.note(f"{question} {ANSWER_ARROW} {meaning}", "dim" if self.view else "accent")

    def answer_lines(self):
        return self.console.answer_lines() if self.console else remote.answer_lines(is_interactive)

    def asked(self, question: str) -> str:
        if self.console:
            return self.console.line(question)
        prompt_line.discard_typed_ahead()
        return input(prompt_line.readline_safe(self.paint(question, "accent")))

    def review_plan(self, plan: str) -> tuple[str, str]:
        with self.keys_paused():
            return plan_review(self, plan)

    def show_last(self) -> None:
        step = self.last_output_step or self.last_step
        if step is None:
            self.note(view.NO_STEP_YET)
            return
        if self.view:
            self.view.full_step(step)
            return
        self.note(f"{STEP_MARK} {step.label}", "accent")
        output, _failed = view.output_of(step.name, step.result)
        for line in view.readable(output).splitlines() or ["(no output)"]:
            self.note(f"{OUTCOME_HANG}{line}", "reset")


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
