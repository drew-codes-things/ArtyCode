import json
from pathlib import Path

import pytest

from arty_cli import agent, checks, permissions, rules, tools
from tests.portable import python_call

EM_DASH = "\u2014"


def hit_lines(path, before, after):
    return [(hit.line, hit.kind) for hit in checks.added_hits(Path(path), before, after)]


@pytest.mark.parametrize("line", ["# add the numbers", "x = 1  # add the numbers", "    # TODO: later"])
def test_a_python_comment_on_an_added_line_is_found(line):
    assert hit_lines("app.py", "", f"y = 2\n{line}\n") == [(2, checks.COMMENT)]


@pytest.mark.parametrize("line", ["#!/usr/bin/env python3", "# -*- coding: utf-8 -*-", "import os  # noqa: F401",
                                  "x = y  # type: ignore[attr-defined]", "def f():  # pragma: no cover",
                                  "# pylint: disable=invalid-name", "# fmt: off", "# SPDX-License-Identifier: MIT"])
def test_python_tool_directives_are_allowed(line):
    assert hit_lines("app.py", "", f"{line}\n") == []


def test_a_python_docstring_on_an_added_line_is_found():
    after = 'def add(a, b):\n    """Add two numbers."""\n    return a + b\n'
    assert hit_lines("app.py", "", after) == [(2, checks.DOCSTRING)]


def test_a_hash_inside_a_python_string_is_not_a_comment():
    assert hit_lines("app.py", "", 'colour = "#ff0000"\nmark = "# not a comment"\n') == []


def test_only_lines_the_edit_added_are_checked():
    before = "# an old comment the user wrote\nx = 1\n"
    assert hit_lines("app.py", before, before + "y = 2\n") == []
    assert hit_lines("app.py", before, before + "y = 2  # new\n") == [(3, checks.COMMENT)]


def test_a_repeated_comment_line_that_was_already_there_is_not_counted_as_added():
    before = "".join(f"x{number} = {number}\n# ----\n" for number in range(150))
    after = before + "z = 0\n"
    assert hit_lines("app.py", before, after) == []


def test_python_that_does_not_parse_yet_is_still_checked_line_by_line():
    assert hit_lines("app.py", "", "def broken(:\n    # half done\n") == [(2, checks.COMMENT)]


@pytest.mark.parametrize("line, found", [
    ("# copy the files", True), ("cp a b  # copy", True), ("#!/bin/bash", False),
    ("# shellcheck disable=SC2086", False), ('echo "a # b"', False), ("echo ${#items[@]}", False),
    ("echo $#", False), ("echo 'x#y'", False),
])
def test_shell_comments_and_their_exceptions(line, found):
    assert bool(hit_lines("run.sh", "", f"{line}\n")) is found


@pytest.mark.parametrize("suffix", [".js", ".ts", ".go", ".rs", ".c", ".java", ".kt", ".swift", ".cs", ".css"])
def test_slash_comments_are_found_in_c_like_languages(suffix):
    assert hit_lines(f"app{suffix}", "", "let x = 1;\n// set x\n") == [(2, checks.COMMENT)]


@pytest.mark.parametrize("line, found", [
    ("// eslint-disable-next-line no-console", False), ("// @ts-check", False), ("//go:build linux", False),
    ("// SPDX-License-Identifier: MIT", False), ("/* istanbul ignore next */", False), ("#region setup", False),
    ('const url = "https://example.com/a";', False), ("const path = 'a//b';", False), ("x = 1; // trailing", True),
    ("/* block */", True), ("x = 1; /* inline */", True),
])
def test_c_like_comments_and_their_exceptions(line, found):
    assert bool(hit_lines("app.ts", "", f"{line}\n")) is found


def test_every_line_of_an_added_block_comment_is_found():
    after = "/*\n explain\n more\n*/\nconst x = 1;\n"
    assert [line for line, _kind in hit_lines("app.js", "", after)] == [1, 2, 3, 4]


def test_files_without_a_known_comment_style_get_no_comment_check():
    assert hit_lines("notes.txt", "", "# heading\n// not code\n") == []
    assert hit_lines("README.md", "", "# Title\n") == []


@pytest.mark.parametrize("name", ["app.py", "README.md", "notes.txt", ".arty/history.md"])
def test_an_em_dash_on_an_added_line_is_found_in_any_file(name):
    assert hit_lines(name, "", f"ok = 1\nwords {EM_DASH} more\n") == [(2, checks.EM_DASH_KIND)]


def test_an_em_dash_that_was_already_there_is_left_alone():
    before = f"old {EM_DASH} text\n"
    assert hit_lines("README.md", before, before + "new text\n") == []


@pytest.mark.parametrize("command, counts", [
    (python_call("-m", "pytest", "-q"), True), ("make test", True), ("npm run build", True), ("ruff check .", True),
    ("cd sub && pytest", True), ("FOO=1 cargo test", True), ("ls", False), ("cat utils.py", False),
    ("git diff", False), ("git status && echo done", False), ("mkdir -p out", False), ("", False),
])
def test_which_commands_count_as_checking_the_work(command, counts):
    assert checks.is_checking_command(command) is counts


def call(name, call_id="c1", **arguments):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


class ScriptedBrain:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.sent = []

    def reply(self, messages, tools, on_text):
        self.sent.append([dict(message) for message in messages])
        content, calls = self.replies.pop(0)
        return {"role": "assistant", "content": content, "tool_calls": calls}


class Screen:
    def __init__(self):
        self.text, self.activity = [], []

    def show_text(self, text):
        self.text.append(text)

    def show_activity(self, line):
        self.activity.append(line)


def ruled_workspace(tmp_path, level=rules.ON):
    workspace = tools.Workspace(tmp_path)
    workspace.rules = rules.Rules(level)
    return workspace


def turn(workspace, *replies):
    scripted = ScriptedBrain(*replies)
    messages = [{"role": "user", "content": "do the job"}]
    gate = permissions.Gate(workspace, lambda question, preview="": "y")
    answer = agent.run_turn(messages, scripted, workspace, gate, Screen())
    return answer, messages, scripted


def tool_results(messages):
    return [message["content"] for message in messages if message["role"] == "tool"]


def sent_back_texts(messages):
    return [message["content"] for message in messages
            if message["role"] == "user" and message["content"].startswith(checks.FINISH_LEAD)]


CHECK = python_call("-c", "print('checked')")
HISTORY_ENTRY = "## 2026-11-12 - add\nStatus: DONE\n"


def test_an_edit_that_adds_a_comment_is_told_exactly_which_line_to_fix(tmp_path):
    workspace = ruled_workspace(tmp_path)
    answer, messages, _scripted = turn(
        workspace,
        ("", [call("write_file", path="app.py", content="x = 1\n# set y\ny = 2\n")]),
        ("", [call("run_command", "c2", command=CHECK)]),
        ("", [call("write_file", "c3", path=".arty/history.md", content=HISTORY_ENTRY)]),
        ("Done.", []),
        ("Done.", []),
        ("Done.", []),
        ("Done.", []),
    )
    first = tool_results(messages)[0]
    assert first.startswith("Created app.py")
    assert checks.HITS_LEAD in first and "app.py:2: comment: # set y" in first


def test_a_clean_edit_gets_no_rule_note(tmp_path):
    workspace = ruled_workspace(tmp_path)
    answer, messages, _scripted = turn(
        workspace,
        ("", [call("write_file", path="app.py", content="x = 1\n")]),
        ("", [call("run_command", "c2", command=CHECK)]),
        ("", [call("write_file", "c3", path=".arty/history.md", content=HISTORY_ENTRY)]),
        ("Done.", []),
    )
    assert checks.HITS_LEAD not in "\n".join(tool_results(messages))
    assert answer == "Done." and sent_back_texts(messages) == []


def test_finishing_without_checking_or_history_is_sent_back_once_for_each_then_allowed(tmp_path):
    workspace = ruled_workspace(tmp_path)
    answer, messages, _scripted = turn(
        workspace,
        ("", [call("write_file", path="app.py", content="x = 1\n")]),
        ("All done.", []),
        ("Still done.", []),
        ("Really done.", []),
    )
    assert answer == "Really done."
    verify, history = sent_back_texts(messages)
    assert checks.VERIFY_REQUEST in verify and ".arty/history.md" not in verify
    assert ".arty/history.md" in history and "Verified by" in history and checks.VERIFY_REQUEST not in history
    assert all(checks.REPORT_REMINDER in text for text in (verify, history))


def test_a_command_that_was_not_found_is_not_a_check(tmp_path):
    workspace = ruled_workspace(tmp_path)
    checks.start_turn(workspace)
    changed_through_the_checks(workspace, "write_file", {"path": "app.py", "content": "x = 1\n"})
    turn_state = checks.current_turn(workspace)
    watched = checks.watch(workspace, "run_command", {"command": "python -m pytest"})
    checks.checked_result(workspace, watched, "bash: python: command not found\n[exit code 127]")
    assert turn_state.checked_at < turn_state.changed_at
    watched = checks.watch(workspace, "run_command", {"command": "python3 -m pytest"})
    checks.checked_result(workspace, watched, "1 failed\n[exit code 1]")
    assert turn_state.checked_at > turn_state.changed_at


def test_a_report_marked_unverified_with_a_history_entry_may_finish(tmp_path):
    workspace = ruled_workspace(tmp_path)
    answer, messages, _scripted = turn(
        workspace,
        ("", [call("write_file", path="app.py", content="x = 1\n")]),
        ("", [call("write_file", "c2", path=".arty/history.md", content=HISTORY_ENTRY)]),
        ("Outcome: UNVERIFIED, no tests here.", []),
    )
    assert answer.startswith("Outcome: UNVERIFIED") and sent_back_texts(messages) == []


def test_a_check_run_before_the_last_edit_does_not_count(tmp_path):
    workspace = ruled_workspace(tmp_path)
    answer, messages, _scripted = turn(
        workspace,
        ("", [call("write_file", path="app.py", content="x = 1\n")]),
        ("", [call("run_command", "c2", command=CHECK)]),
        ("", [call("write_file", "c3", path="more.py", content="y = 1\n")]),
        ("", [call("write_file", "c4", path=".arty/history.md", content=HISTORY_ENTRY)]),
        ("Done.", []),
        ("Done again.", []),
    )
    [sent] = sent_back_texts(messages)
    assert checks.VERIFY_REQUEST in sent and ".arty/history.md" not in sent
    assert answer == "Done again."


def test_a_look_only_command_after_the_edit_is_not_a_check(tmp_path):
    workspace = ruled_workspace(tmp_path)
    answer, messages, _scripted = turn(
        workspace,
        ("", [call("write_file", path="app.py", content="x = 1\n")]),
        ("", [call("run_command", "c2", command="git status")]),
        ("", [call("write_file", "c3", path=".arty/history.md", content=HISTORY_ENTRY)]),
        ("Done.", []),
        ("Done again.", []),
    )
    assert len(sent_back_texts(messages)) == 1
    assert answer == "Done again."


def test_comments_left_in_send_the_model_back_until_a_limit_then_it_may_stop(tmp_path):
    workspace = ruled_workspace(tmp_path)
    replies = [("", [call("write_file", path="app.py", content="x = 1  # one\n")]),
               ("", [call("run_command", "c2", command=CHECK)]),
               ("", [call("write_file", "c3", path=".arty/history.md", content=HISTORY_ENTRY)])]
    replies += [("Done.", [])] * (checks.MAX_COMMENT_SEND_BACKS + 1)
    answer, messages, scripted = turn(workspace, *replies)
    sent = sent_back_texts(messages)
    assert len(sent) == checks.MAX_COMMENT_SEND_BACKS
    assert all("app.py:1: comment: x = 1  # one" in text for text in sent)
    assert answer == "Done." and scripted.replies == []


def test_fixing_the_comment_lets_the_turn_finish(tmp_path):
    workspace = ruled_workspace(tmp_path)
    answer, messages, _scripted = turn(
        workspace,
        ("", [call("write_file", path="app.py", content="x = 1  # one\n")]),
        ("", [call("edit_file", "c2", path="app.py", old_text="x = 1  # one", new_text="x = 1")]),
        ("", [call("run_command", "c3", command=CHECK)]),
        ("", [call("write_file", "c4", path=".arty/history.md", content=HISTORY_ENTRY)]),
        ("Done.", []),
    )
    assert answer == "Done." and sent_back_texts(messages) == []
    assert checks.HITS_LEAD not in tool_results(messages)[1]


def test_an_em_dash_in_a_written_file_is_flagged(tmp_path):
    workspace = ruled_workspace(tmp_path)
    _answer, messages, _scripted = turn(
        workspace,
        ("", [call("write_file", path="README.md", content=f"Fast {EM_DASH} and small\n")]),
        ("", [call("run_command", "c2", command=CHECK)]),
        ("", [call("write_file", "c3", path=".arty/history.md", content=HISTORY_ENTRY)]),
        ("Done.", []),
        ("Done.", []),
        ("Done.", []),
        ("Done.", []),
    )
    assert "README.md:1: em dash" in tool_results(messages)[0]


def test_a_turn_that_only_wrote_the_scratchpad_is_not_held_back(tmp_path):
    workspace = ruled_workspace(tmp_path)
    answer, messages, _scripted = turn(
        workspace,
        ("", [call("write_file", path=".arty/scratchpad.md", content="# Task: look\nStatus: DONE\n")]),
        ("Here is what I found.", []),
    )
    assert answer == "Here is what I found." and sent_back_texts(messages) == []


def test_a_question_that_changes_nothing_is_not_held_back(tmp_path):
    workspace = ruled_workspace(tmp_path)
    answer, messages, _scripted = turn(workspace, ("It adds numbers.", []))
    assert answer == "It adds numbers." and sent_back_texts(messages) == []


def test_tell_only_runs_no_checks(tmp_path):
    workspace = ruled_workspace(tmp_path, rules.TELL_ONLY)
    answer, messages, _scripted = turn(
        workspace,
        ("", [call("write_file", path="app.py", content="x = 1  # one\n")]),
        ("Done.", []),
    )
    assert answer == "Done." and checks.HITS_LEAD not in tool_results(messages)[0]
    assert sent_back_texts(messages) == []


def test_without_rules_nothing_is_checked(tmp_path):
    workspace = tools.Workspace(tmp_path)
    answer, messages, _scripted = turn(
        workspace,
        ("", [call("write_file", path="app.py", content="x = 1  # one\n")]),
        ("Done.", []),
    )
    assert answer == "Done." and checks.HITS_LEAD not in tool_results(messages)[0]


def test_a_refused_edit_is_not_counted_as_a_change(tmp_path):
    workspace = ruled_workspace(tmp_path)
    scripted = ScriptedBrain(("", [call("write_file", path="app.py", content="x = 1  # one\n")]), ("Okay.", []))
    messages = [{"role": "user", "content": "do the job"}]
    gate = permissions.Gate(workspace, lambda question, preview="": "n")
    assert agent.run_turn(messages, scripted, workspace, gate, Screen()) == "Okay."
    assert sent_back_texts(messages) == [] and checks.HITS_LEAD not in tool_results(messages)[0]


def test_each_request_starts_its_own_count(tmp_path):
    workspace = ruled_workspace(tmp_path)
    turn(workspace,
         ("", [call("write_file", path="app.py", content="x = 1\n")]),
         ("Done.", []), ("Done.", []), ("Done.", []))
    answer, messages, _scripted = turn(workspace, ("Nothing to change.", []))
    assert answer == "Nothing to change." and sent_back_texts(messages) == []


def test_being_sent_back_is_shown_on_the_screen(tmp_path):
    workspace = ruled_workspace(tmp_path)
    scripted = ScriptedBrain(("", [call("write_file", path="app.py", content="x = 1\n")]), ("Done.", []),
                             ("Done.", []), ("Done.", []))
    screen = Screen()
    gate = permissions.Gate(workspace, lambda question, preview="": "y")
    agent.run_turn([{"role": "user", "content": "go"}], scripted, workspace, gate, screen)
    assert any(line.startswith(checks.SENT_BACK_NOTE) for line in screen.activity)


def test_helpers_do_not_run_the_finish_check(tmp_path):
    workspace = ruled_workspace(tmp_path)
    checks.start_turn(workspace)
    scripted = ScriptedBrain(("", [call("write_file", path="app.py", content="x = 1\n")]), ("Done.", []))
    messages = [{"role": "user", "content": "go"}]
    gate = permissions.Gate(workspace, lambda question, preview="": "y")
    assert agent.run_turn(messages, scripted, workspace, gate, Screen(), stop_hooks=False) == "Done."
    assert sent_back_texts(messages) == []


def test_the_history_template_has_every_field():
    for field in ("Status", "Changed", "What", "Why", "Verified by", "Not verified", "Left open"):
        assert f"{field}:" in rules.history_template("2026-11-12")


def test_no_source_file_of_arty_cli_holds_an_em_dash():
    package = Path(checks.__file__).parent
    for path in list(package.glob("*.py")) + list(Path(__file__).parent.glob("*.py")):
        assert EM_DASH not in path.read_text(encoding="utf-8"), path


def test_the_checker_passes_its_own_source():
    for module in (checks, rules):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert checks.added_hits(Path(module.__file__), "", source) == []


def changed_through_the_checks(workspace, name, arguments):
    watched = checks.watch(workspace, name, arguments)
    return checks.checked_result(workspace, watched, tools.run(name, arguments, workspace))


def test_a_check_run_in_the_background_counts_once_its_output_shows_it_finished(tmp_path):
    workspace = ruled_workspace(tmp_path)
    checks.start_turn(workspace)
    changed_through_the_checks(workspace, "write_file", {"path": "app.py", "content": "x = 1\n"})
    changed_through_the_checks(workspace, "run_command", {"command": CHECK, "background": True})
    workspace.jobs["1"].process.wait()
    turn = checks.current_turn(workspace)
    assert turn.checked_at < turn.changed_at
    changed_through_the_checks(workspace, "command_output", {"job": "1"})
    assert turn.checked_at > turn.changed_at


@pytest.mark.parametrize("ending, counts", [("\n[exit code 1]", True), ("\n[stopped after 120 s]", True),
                                            ("\n[stopped after 120 s. Run it in the background]", True),
                                            ("\n[job 2 finished with exit code 0]", True),
                                            ("\n[job 2 still running]", False), ("Job 1 started", False),
                                            ("\n[exit code 127]", False), ("\n[exit code 126]", False),
                                            ("\n[job 3 finished with exit code 127]", False)])
def test_which_command_results_show_the_command_ran(ending, counts):
    assert checks.ran_to_the_end(f"output{ending}") is counts
