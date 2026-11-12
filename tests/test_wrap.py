import io

from arty_cli import cli, wrap


def on_screen(written):
    rows, column = [""], 0
    position = 0
    while position < len(written):
        character = written[position]
        if written.startswith(wrap.CLEAR_TO_LINE_END, position):
            rows[-1] = rows[-1][:column]
            position += len(wrap.CLEAR_TO_LINE_END)
            continue
        if character == "\n":
            rows.append("")
            column = 0
        elif character == "\b":
            column -= 1
        else:
            rows[-1] = rows[-1][:column] + character + rows[-1][column + 1:]
            column += 1
        position += 1
    return rows


def flowed(*pieces, width=20):
    flow = wrap.Flow(lambda: width)
    return on_screen("".join(flow.feed(piece) for piece in pieces))


def test_text_breaks_between_words_not_inside_them():
    rows = flowed("The calculator supports addition and subtraction.")
    assert rows == ["The calculator ", "supports addition ", "and subtraction."]


def test_a_word_arriving_in_pieces_is_kept_whole():
    assert flowed("Here is the calc", "ulator you as", "ked for") == ["Here is the ", "calculator you asked", "for"]


def test_every_piece_is_shown_as_soon_as_it_arrives():
    flow = wrap.Flow(lambda: 20)
    assert flow.feed("Hi") == "Hi" and flow.feed(" the") == " the"


def test_the_models_own_line_breaks_are_kept():
    assert flowed("one\n\ntwo three\n") == ["one", "", "two three", ""]


def test_code_blocks_are_left_exactly_as_written():
    code = "```python\nprint('a very long line of code that runs well past the width')\n```\n"
    flow = wrap.Flow(lambda: 20)
    assert flow.feed("Run this:\n") + flow.feed(code) == "Run this:\n" + code
    assert on_screen(flow.feed("then it is done and you can carry on")) == ["then it is done and ", "you can carry on"]


def test_a_word_longer_than_the_line_is_not_broken_up():
    assert flowed("see /home/drew/projects/very/long/path/file.py now") == [
        "see ", "/home/drew/projects/very/long/path/file.py", "now"]


def test_a_note_wraps_at_words_and_keeps_its_indent():
    lines = wrap.wrapped_line("  Ask in plain words and it looks through the project", 24)
    assert lines == ["  Ask in plain words and", "  it looks through the", "  project"]


def test_colour_codes_do_not_count_towards_the_width():
    painted = "\033[2mdim words here\033[0m and plain words after"
    lines = wrap.wrapped_line(painted, 20)
    assert lines == ["\033[2mdim words here\033[0m and", "plain words after"]


def test_a_short_or_empty_note_is_one_line():
    assert wrap.wrapped_line("", 10) == [""] and wrap.wrapped_line("short", 10) == ["short"]


class Tty(io.StringIO):
    def isatty(self):
        return True


def test_on_a_terminal_the_answer_and_notes_wrap_to_its_width(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(wrap, "columns", lambda: 21)
    out, err = Tty(), Tty()
    terminal = cli.Terminal(out, err)
    terminal.show_text("The calculator supports addi")
    terminal.show_text("tion and subtraction.")
    terminal.note("Worked for 12s and took three steps")
    assert on_screen(out.getvalue()) == ["The calculator ", "supports addition ", "and subtraction.", ""]
    assert err.getvalue() == "Worked for 12s and\ntook three steps\n"


def test_into_a_pipe_nothing_is_wrapped(monkeypatch):
    monkeypatch.setattr(wrap, "columns", lambda: 21)
    out, err = io.StringIO(), io.StringIO()
    terminal = cli.Terminal(out, err)
    terminal.show_text("The calculator supports addition and subtraction.")
    terminal.note("Worked for 12s and took three steps")
    assert out.getvalue() == "The calculator supports addition and subtraction.\n"
    assert err.getvalue() == "Worked for 12s and took three steps\n"


def test_steps_and_outcomes_wrap_under_their_mark(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(wrap, "columns", lambda: 25)
    err = Tty()
    terminal = cli.Terminal(Tty(), err)
    terminal.show_step("Run python calculator.py with input")
    terminal.show_outcome("Result: 42 printed (exit code 0)", False)
    assert err.getvalue() == ("● Run python\n  calculator.py with\n  input\n"
                              "  └ Result: 42 printed\n    (exit code 0)\n")
