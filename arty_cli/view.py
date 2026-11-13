import re
import threading
from dataclasses import dataclass

from arty_cli import agent, look, notify, permissions, tools, wrap

OUTPUT_TOOLS = ("run_command", "command_output", "search", "list_files")
SHOWN_OUTPUT_ROWS = 3
SHOWN_DIFF_ROWS = 12
EXPAND_HINT = "ctrl+o or /last to see all"
EXIT_LINE = re.compile(r"^\[exit code (-?\d+)\]$")
CONTROL_SEQUENCE = re.compile(r"(?:\033[P\]X^_]|[\x90\x98\x9d\x9e\x9f])[^\x07\x1b\x9c]*(?:\x07|\033\\|\x9c)?"
                              r"|(?:\033\[|\x9b)[0-?]*[ -/]*[@-~]|\033[@-_]|[\x00-\x08\x0b-\x1f\x7f-\x9f]")
TEXT_MARK = "● "
NO_STEP_YET = "No step has run yet, so there's nothing to show."
LONG_TURN_SECONDS = 30
FINISHED_TITLE = "ArtyCode finished"


@dataclass(frozen=True)
class Step:
    label: str
    name: str
    arguments: dict
    result: str


def readable(text: str) -> str:
    return CONTROL_SEQUENCE.sub("", text)


def output_of(name: str, result: str) -> tuple[str, bool]:
    lines = result.rstrip("\n").splitlines()
    ending = EXIT_LINE.match(lines[-1].strip()) if lines else None
    if name == "run_command" and ending:
        return "\n".join(lines[:-1]).rstrip("\n"), ending.group(1) != "0"
    return result, result.startswith("Error")


def call_details(call: dict) -> tuple[str, dict]:
    name, arguments, _problem = agent.call_parts(call)
    return name, arguments or {}


class View:
    def __init__(self, out, theme: look.Theme, workspace=None, notifier: notify.Notifier | None = None,
                 room=wrap.room):
        self.out = out
        self.theme = theme
        self.workspace = workspace
        self.notifier = notifier
        self.room = room
        self.lock = threading.RLock()
        self.markdown = look.Markdown(theme)
        self.typed = ""
        self.in_text = False
        self.last_line = None
        self.joined = False
        self.pending_step = None
        self.latest_label = ""
        self.previews = {}
        self.asked_previews = set()
        self.partial = None

    def write(self, lines: list[str]) -> None:
        if not lines:
            return
        self.out.write("\n".join(lines) + "\n")
        self.out.flush()
        self.last_line = lines[-1]
        self.joined = False

    def gap(self) -> None:
        if self.joined:
            self.joined = False
            return
        committed = getattr(self.out, "last_line", self.last_line)
        if committed is not None and committed.strip():
            self.write([""])

    def thought(self, text: str) -> None:
        with self.lock:
            self.settle()
            self.gap()
            self.write([self.theme.paint(readable(text), "dim")])
            self.joined = True

    def text(self, chunk: str) -> None:
        with self.lock:
            self.flush_step()
            self.typed += readable(chunk)
            while "\n" in self.typed:
                line, self.typed = self.typed.split("\n", 1)
                self.text_line(line)
            self.show_partial()

    def show_partial(self) -> None:
        if self.partial is None:
            return
        lead = (look.HANG if self.in_text else TEXT_MARK) if self.typed else ""
        self.partial(lead + self.typed)

    def text_line(self, line: str) -> None:
        rendered = self.markdown.line(line)
        if rendered is None:
            return
        if not rendered.text.strip() and not self.markdown.in_code():
            if self.in_text and getattr(self.out, "last_line", self.last_line):
                self.write([""])
            return
        width = max(self.room() - len(look.HANG), 1)
        rows = wrap.wrapped_line(rendered.text, width, rendered.hang) if rendered.wraps else [rendered.text]
        if not self.in_text:
            self.gap()
            self.in_text = True
            first, *rest = rows
            self.write([TEXT_MARK + first, *(look.HANG + row for row in rest)])
            return
        self.write([look.HANG + row for row in rows])

    def settle(self) -> None:
        with self.lock:
            if self.typed:
                self.text_line(self.typed)
                self.typed = ""
                self.show_partial()
            self.in_text = False
            self.flush_step()

    def flush_step(self) -> None:
        if self.pending_step is None:
            return
        label, self.pending_step = self.pending_step, None
        self.gap()
        self.write(look.step_line(label, look.WAITING, self.room(), self.theme))

    def step(self, label: str) -> None:
        with self.lock:
            self.settle()
            self.pending_step = readable(label)
            self.latest_label = label

    def outcome(self, line: str, is_problem: bool) -> None:
        line = readable(line)
        with self.lock:
            label, self.pending_step = self.pending_step, None
            self.settle()
            preview = self.previews.pop(self.latest_label, "")
            changed = bool(preview) and not is_problem and line != agent.SAID_NO
            if label is not None:
                self.gap()
                state = look.FAILED if is_problem else look.DONE
                self.write(look.step_line(label, state, self.room(), self.theme))
            result = look.result_lines(line, self.room(), self.theme, is_problem)
            if changed:
                result[-1] += " " + look.counts_text(*look.diff_counts(preview), self.theme)
            self.write(result)
            if changed and preview not in self.asked_previews:
                self.write(look.numbered_diff(preview, self.theme, SHOWN_DIFF_ROWS))
            self.asked_previews.discard(preview)

    def tool_call(self, call: dict) -> None:
        name, arguments = call_details(call)
        if name not in tools.CHANGING_TOOLS or self.workspace is None:
            return
        preview = permissions.diff_preview(self.workspace, name, arguments)
        if preview:
            self.previews[agent.step_line(name, arguments)] = preview

    def tool_result(self, call: dict, result: str) -> None:
        name, arguments = call_details(call)
        if name not in OUTPUT_TOOLS or agent.step_line(name, arguments) != self.latest_label:
            return
        output, failed = output_of(name, result)
        lines = look.output_lines(readable(output), self.room())
        if not lines or result.startswith(permissions.REFUSED_LEAD):
            return
        with self.lock:
            self.settle()
            if failed and len(lines) > SHOWN_OUTPUT_ROWS + 1:
                hidden = len(lines) - SHOWN_OUTPUT_ROWS
                more = self.theme.paint(f"{look.RESULT_HANG}{look.ELLIPSIS} {hidden} lines above ({EXPAND_HINT})", "dim")
                self.write([more, *(self.theme.paint(row, "dim") for row in lines[-SHOWN_OUTPUT_ROWS:])])
                return
            shown, hidden = look.collapsed([self.theme.paint(row, "dim") for row in lines], SHOWN_OUTPUT_ROWS,
                                           self.theme, EXPAND_HINT)
            self.write(shown)

    def question_diff(self, preview: str) -> None:
        with self.lock:
            self.settle()
            self.asked_previews.add(preview)
            self.write(look.numbered_diff(preview, self.theme))

    def full_step(self, step: Step) -> None:
        with self.lock:
            self.settle()
            self.gap()
            self.write(look.step_line(step.label, look.WAITING, self.room(), self.theme))
            output, _failed = output_of(step.name, step.result)
            body = look.output_lines(readable(output), self.room()) or [f"{look.RESULT_HANG}(no output)"]
            self.write(body)

    def user_request(self, text: str) -> list[str]:
        return look.user_block(text, self.room(), self.theme)

    def alert(self, title: str, text: str) -> None:
        if self.notifier is not None:
            self.notifier.notice(title, text)

    def turn_finished(self, seconds: float, summary: str) -> None:
        if seconds >= LONG_TURN_SECONDS:
            self.alert(FINISHED_TITLE, summary)
