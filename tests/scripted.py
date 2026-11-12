import io
import json
from pathlib import Path

import pytest

from arty_cli import cli, context, permissions, sessions, tools

DREWS_WINDOW = 24_576
SUMMARY_MARK = "SUMMARY"
ARTY_CLI_SOURCE = Path(cli.__file__)


def call_of(number: int, name: str, arguments) -> dict:
    given = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return {"id": f"c{number}", "type": "function", "function": {"name": name, "arguments": given}}


def is_summary_request(messages: list[dict]) -> bool:
    last = messages[-1] if messages else {}
    return last.get("role") == "user" and str(last.get("content") or "").startswith(cli.SUMMARY_REQUEST)


class ScriptedBrain:
    def __init__(self, steps, window: int = DREWS_WINDOW, summary: str = "The summary.", usage=None):
        self.model = "arty/scripted"
        self.steps = list(steps)
        self.window = window
        self.summary = summary
        self.usage = usage
        self.on_thinking = None
        self.prompts = []
        self.log = []
        self.calls_made = 0

    def address(self):
        return "http://brain.test/v1"

    def context_tokens(self):
        return self.window

    def search_web(self, query, results):
        return f"Results for {query}"

    def reply(self, messages, tools, on_text):
        copied = [dict(message) for message in messages]
        self.prompts.append(copied)
        if is_summary_request(messages):
            self.log.append(SUMMARY_MARK)
            return {"role": "assistant", "content": self.summary, "tool_calls": []}
        step = self.steps.pop(0) if self.steps else "Done."
        if callable(step):
            step = step(copied)
        reply = self.reply_for(step)
        if self.usage is not None:
            reply["usage"] = self.usage(copied)
        return reply

    def reply_for(self, step) -> dict:
        if isinstance(step, str):
            self.log.append("answer")
            return {"role": "assistant", "content": step, "tool_calls": []}
        calls = []
        for name, arguments in step:
            self.calls_made += 1
            self.log.append(name)
            calls.append(call_of(self.calls_made, name, arguments))
        return {"role": "assistant", "content": "", "tool_calls": calls}

    def largest_prompt(self) -> int:
        return max(context.estimated_tokens(prompt, self) for prompt in self.prompts)

    def tool_results(self, prompt_number: int = -1) -> list[str]:
        return [str(message.get("content") or "") for message in self.prompts[prompt_number]
                if message.get("role") == "tool"]


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "config"))
    monkeypatch.setenv("NO_COLOR", "1")
    folder = tmp_path / "project"
    folder.mkdir()
    return folder


def session_for(folder: Path, brain, mode: str = permissions.ALLOW_ALL, saved=None) -> cli.Session:
    workspace = tools.Workspace(folder)
    terminal = cli.Terminal(io.StringIO(), io.StringIO())
    gate = permissions.Gate(workspace, terminal.ask, mode=mode)
    saved = saved or sessions.Store(folder.parent / "sessions").start(folder)
    return cli.Session(brain, workspace, gate, terminal, saved)


def copy_of_arty_cli_source(folder: Path, name: str = "big.py") -> Path:
    target = folder / name
    target.write_text(ARTY_CLI_SOURCE.read_text(encoding="utf-8"), encoding="utf-8")
    return target
