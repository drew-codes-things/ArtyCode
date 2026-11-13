import io
import json
import re

import pytest

from arty_cli import look, notify, settings, tools, view

PLAIN = look.Theme(colour=False)
COLOUR_CODE = re.compile(r"\033\[[0-9;]*m")


def bare(text):
    return COLOUR_CODE.sub("", text)


def call(name, **arguments):
    return {"id": "c1", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def shown_view(workspace=None, theme=PLAIN, width=80):
    out = io.StringIO()
    return view.View(out, theme, workspace, room=lambda: width), out


def test_a_step_is_drawn_once_its_outcome_is_known_with_a_bullet_for_how_it_went():
    shown, out = shown_view()
    shown.step("Run pytest -q")
    assert out.getvalue() == ""
    shown.outcome("3 failed (exit code 1)", True)
    shown.step("Read a.py")
    shown.outcome("2 lines", False)
    assert out.getvalue() == "● Run pytest -q\n  └ 3 failed (exit code 1)\n\n● Read a.py\n  └ 2 lines\n"


def test_failed_steps_get_a_red_bullet_and_good_ones_a_green_bullet():
    shown, out = shown_view(theme=look.Theme(colour=True))
    shown.step("Run pytest -q")
    shown.outcome("boom", True)
    shown.step("Read a.py")
    shown.outcome("2 lines", False)
    assert "\033[31m●" in out.getvalue() and "\033[32m●" in out.getvalue()


def test_anything_else_written_first_shows_the_step_still_waiting():
    shown, out = shown_view()
    shown.step("Edit a.py")
    shown.settle()
    assert out.getvalue() == "● Edit a.py\n"
    shown.outcome("Edited a.py", False)
    assert out.getvalue().endswith("● Edit a.py\n  └ Edited a.py\n")


def test_streamed_answers_are_rendered_as_markdown_under_one_mark():
    shown, out = shown_view()
    partial = []
    shown.partial = partial.append
    shown.text("## Done\n\nThe **fix** is in `calc.py`:\n- add ")
    assert partial[-1] == "  - add "
    shown.text("now adds\n```python\nx = 1\n```\n")
    shown.settle()
    assert out.getvalue() == "● Done\n\n  The fix is in calc.py:\n  • add now adds\n  │ x = 1\n"


def test_a_long_answer_line_wraps_inside_its_indent():
    shown, out = shown_view(width=30)
    shown.text("word " * 12 + "\n")
    lines = out.getvalue().splitlines()
    assert lines[0].startswith("● word") and all(line.startswith("  word") for line in lines[1:])
    assert all(len(line) <= 30 for line in lines)


def long_command_result(count=40, code=0):
    return "\n".join(f"line {number}" for number in range(1, count + 1)) + f"\n[exit code {code}]"


def test_long_command_output_shows_its_first_lines_and_how_many_more_there_are():
    shown, out = shown_view()
    command = call("run_command", command="python3 make_lines.py")
    shown.step("Run python3 make_lines.py")
    shown.outcome("line 40 (exit code 0)", False)
    shown.tool_result(command, long_command_result())
    lines = out.getvalue().splitlines()
    assert lines[2:] == ["    line 1", "    line 2", "    line 3", f"    … +37 lines ({view.EXPAND_HINT})"]


def test_failing_command_output_shows_its_last_lines_where_the_error_is():
    shown, out = shown_view()
    command = call("run_command", command="pytest -q")
    shown.step("Run pytest -q")
    shown.outcome("1 failed (exit code 1)", True)
    shown.tool_result(command, long_command_result(code=1))
    lines = out.getvalue().splitlines()
    assert lines[2:] == [f"    … 37 lines above ({view.EXPAND_HINT})", "    line 38", "    line 39", "    line 40"]


def test_output_of_an_earlier_step_in_the_same_reply_is_not_drawn_under_a_later_one():
    shown, out = shown_view()
    shown.step("Read a.py")
    shown.outcome("2 lines", False)
    shown.tool_result(call("run_command", command="ls"), long_command_result())
    assert "line 1" not in out.getvalue()


def test_control_characters_in_output_are_dropped():
    shown, out = shown_view()
    shown.step("Run ls")
    shown.outcome("a.py (exit code 0)", False)
    shown.tool_result(call("run_command", command="ls"), "\033[31mred\033[0m\x07 a.py\n[exit code 0]")
    assert out.getvalue().splitlines()[-1] == "    red a.py"


def test_an_edit_made_without_asking_shows_its_counts_and_numbered_diff(tmp_path):
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    workspace = tools.Workspace(tmp_path)
    shown, out = shown_view(workspace)
    edit = call("edit_file", path="calc.py", old_text="    return a - b\n", new_text="    return a + b\n")
    shown.tool_call(edit)
    shown.step("Edit calc.py")
    shown.outcome("Edited calc.py", False)
    assert out.getvalue().splitlines() == ["● Edit calc.py", "  └ Edited calc.py +1 -1", "    1  def add(a, b):",
                                           "    2 -    return a - b", "    2 +    return a + b"]


def test_an_edit_whose_diff_was_shown_with_its_question_only_gets_the_counts(tmp_path):
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    workspace = tools.Workspace(tmp_path)
    shown, out = shown_view(workspace)
    edit = call("edit_file", path="calc.py", old_text="    return a - b\n", new_text="    return a + b\n")
    shown.tool_call(edit)
    shown.step("Edit calc.py")
    shown.question_diff(view.permissions.diff_preview(workspace, "edit_file", json.loads(edit["function"]["arguments"])))
    shown.outcome("Edited calc.py", False)
    lines = out.getvalue().splitlines()
    assert lines.count("    2 +    return a + b") == 1 and lines[-1] == "  └ Edited calc.py +1 -1"


def test_a_refused_edit_gets_no_counts(tmp_path):
    (tmp_path / "calc.py").write_text("x = 1\n")
    shown, out = shown_view(tools.Workspace(tmp_path))
    shown.tool_call(call("edit_file", path="calc.py", old_text="x = 1", new_text="x = 2"))
    shown.step("Edit calc.py")
    shown.outcome(view.agent.SAID_NO, False)
    assert out.getvalue().splitlines()[-1] == f"  └ {view.agent.SAID_NO}"


def test_the_full_last_step_shows_everything():
    shown, out = shown_view()
    shown.full_step(view.Step("Run seq 5", "run_command", {"command": "seq 5"}, "1\n2\n3\n4\n5\n[exit code 0]"))
    assert out.getvalue().splitlines() == ["● Run seq 5", "    1", "    2", "    3", "    4", "    5"]


def test_a_long_turn_rings_and_a_short_one_does_not():
    rung = []
    shown, _out = shown_view()
    shown.notifier = notify.Notifier(lambda: rung.append("bell"), command=lambda title, text: None)
    shown.turn_finished(12, "short")
    shown.turn_finished(31, "Worked for 31s")
    assert rung == ["bell"]


def test_notices_can_be_turned_off_and_are_on_by_default(tmp_path):
    assert notify.wanted(settings.Settings())
    assert not notify.wanted(settings.Settings(notify="off"))
    assert not notify.wanted(settings.load(tmp_path / "config.json", environ={"ARTY_CLI_NOTIFY": "no"}))


def test_a_desktop_notice_is_sent_only_where_the_program_exists():
    assert notify.desktop_command("t", "x", "linux", lambda name: None) is None
    assert notify.desktop_command("t", "x", "win32", lambda name: "/usr/bin/notify-send") is None
    assert notify.desktop_command("Done", "x" * 500, "linux", lambda name: "/usr/bin/notify-send") == [
        "/usr/bin/notify-send", "--app-name=ArtyCode", "Done", "x" * notify.MOST_NOTICE_CHARS]


def test_a_notice_that_cannot_start_still_rings_the_bell():
    rung = []

    def broken(*args, **kwargs):
        raise OSError("no session bus")

    notifier = notify.Notifier(lambda: rung.append(1), start=broken, command=lambda title, text: ["notify-send"])
    assert notifier.notice("t", "x") is False and rung == [1]


def test_a_notify_setting_that_is_neither_on_nor_off_is_refused():
    with pytest.raises(settings.SettingsError, match="notify should be on or off"):
        notify.wanted(settings.Settings(notify="sometimes"))
    assert notify.wanted(settings.Settings(notify="yes")) and notify.wanted(None)


CLIPBOARD = "\x1b]52;c;cHduZWQ=\x07"
TERMINAL_TRICKS = [CLIPBOARD, "\x01" + CLIPBOARD + "\x02", "\x1b]8;;http://x.example\x1b\\link\x1b]8;;\x1b\\",
                   "\x1bP1$qm\x1b\\", "\x9d0;title\x07", "\x9b31m", "\x1b]0;new title\x07", "\x1b[2J"]
RAW_ESCAPES = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


@pytest.mark.parametrize("trick", TERMINAL_TRICKS)
def test_readable_text_has_no_escape_or_control_sequences_left_and_keeps_the_words_around_them(trick):
    cleaned = view.readable(f"before {trick}after")
    assert not RAW_ESCAPES.search(cleaned) and cleaned.startswith("before ") and cleaned.endswith("after")
    assert "52;c" not in cleaned and "title" not in cleaned


@pytest.mark.parametrize("trick", TERMINAL_TRICKS)
def test_a_step_label_and_its_outcome_line_never_carry_terminal_sequences(trick):
    shown, out = shown_view()
    shown.step(f"Run cat notes{trick}.txt")
    shown.outcome(f"{trick}all good (exit code 0)", False)
    shown.text(f"The file says {trick}hello\n")
    assert not RAW_ESCAPES.search(bare(out.getvalue()).replace("\n", ""))
    assert "all good (exit code 0)" in out.getvalue()
