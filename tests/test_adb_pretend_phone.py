import os
import shlex
import sys
from pathlib import Path

import pytest

from arty_cli import permissions, screen
from tests.portable import ON_WINDOWS
from tests.scripted import ScriptedBrain, project, session_for

assert project

FAKE_ADB = Path(__file__).with_name("fake_adb.py")
COMMANDS = ("adb devices", "adb -s PRETEND0001 shell ls /sdcard/Download",
            "adb -s PRETEND0001 pull /sdcard/Download/notes.txt notes.txt",
            "adb -s PRETEND0001 shell getprop ro.product.model")


def pretend_adb_on_path(folder: Path, monkeypatch) -> Path:
    folder.mkdir()
    if ON_WINDOWS:
        (folder / "adb.cmd").write_text(f'@"{sys.executable}" "{FAKE_ADB}" %*\n')
    else:
        launcher = folder / "adb"
        launcher.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(FAKE_ADB))} "$@"\n')
        launcher.chmod(0o755)
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}{os.environ['PATH']}")
    return folder


@pytest.fixture
def phone(project, tmp_path, monkeypatch):
    storage = tmp_path / "phone"
    (storage / "Download").mkdir(parents=True)
    (storage / "Download" / "notes.txt").write_text("milk\neggs\n")
    (storage / "Download" / "photo.jpg").write_bytes(b"\xff\xd8")
    monkeypatch.setenv("FAKE_ADB_PHONE", str(storage))
    monkeypatch.setenv("FAKE_ADB_LOG", str(tmp_path / "adb.log"))
    pretend_adb_on_path(tmp_path / "bin", monkeypatch)
    return tmp_path / "adb.log"


def test_arty_drives_adb_on_a_pretend_phone_through_the_command_tool_and_its_prompts(project, phone, monkeypatch):
    brain = ScriptedBrain([[("run_command", {"command": command})] for command in COMMANDS] + ["The phone is a Pretend Pixel."])
    session = session_for(project, brain, mode=permissions.DEFAULT)
    asked = []
    monkeypatch.setattr(screen, "is_interactive", lambda: True)
    session.terminal.asked = lambda prompt: asked.append(prompt) or "y"
    assert session.ask("What phone is plugged in, and what is in its Download folder?")
    results = [brain.tool_results(number)[-1] for number in range(1, len(COMMANDS) + 1)]
    assert "PRETEND0001\tdevice" in results[0]
    assert "notes.txt" in results[1] and "photo.jpg" in results[1]
    assert "1 file pulled" in results[2] and (project / "notes.txt").read_text() == "milk\neggs\n"
    assert "Pretend Pixel" in results[3]
    questions = session.terminal.err.getvalue()
    assert all(command in questions for command in COMMANDS) and len(asked) == len(COMMANDS)
    assert phone.read_text().splitlines() == [command.removeprefix("adb ") for command in COMMANDS]


def test_saying_no_to_the_prompt_keeps_adb_from_running(project, phone, monkeypatch):
    brain = ScriptedBrain([[("run_command", {"command": "adb devices"})], "Fine."])
    session = session_for(project, brain, mode=permissions.DEFAULT)
    monkeypatch.setattr(screen, "is_interactive", lambda: True)
    session.terminal.asked = lambda prompt: "n" if prompt.startswith("Choose") else ""
    session.ask("list devices")
    assert not phone.exists()
