import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def keys_from_typed_lines():
    waiting = []

    def next_key() -> str:
        if not waiting:
            waiting.extend(input() + "\r")
        return waiting.pop(0)

    return next_key


@pytest.fixture(autouse=True)
def console_keys_come_from_typed_lines(monkeypatch):
    from arty_cli import prompt_line
    monkeypatch.setattr(prompt_line, "console_keys", keys_from_typed_lines)
