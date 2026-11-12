import re

from arty_cli import look, wrap

PLAIN = look.Theme(colour=False)
COLOURED = look.Theme(colour=True, depth=look.TRUE)
COLOUR_CODE = re.compile(r"\033\[[0-9;]*m")
DIFF = "\n".join(["--- calc.py", "+++ calc.py", "@@ -1,3 +1,3 @@", " def add(a, b):", "-    return a - b",
                  "+    return a + b", " ", "@@ -40,2 +40,3 @@", " x = 1", "+y = 2", " z = 3"])


def bare(text):
    return COLOUR_CODE.sub("", text)


def test_the_welcome_card_has_a_border_a_title_and_labelled_rows_that_fit_the_width():
    lines = look.card("◆ ArtyCode 0.1.0", [("model", "arty/qwen3:14b"), ("folder", "~/projects/demo")], 80, PLAIN)
    assert lines[0].startswith("╭─ ◆ ArtyCode 0.1.0 ") and lines[0].endswith("╮")
    assert lines[1] == "│ model   arty/qwen3:14b  │" and lines[-1].startswith("╰") and lines[-1].endswith("╯")
    assert len({len(line) for line in lines}) == 1


def test_a_narrow_card_cuts_long_values_with_an_ellipsis_and_never_overflows():
    lines = look.card("◆ ArtyCode 0.1.0", [("folder", "~/" + "deep/" * 20)], 30, PLAIN)
    assert all(len(line) <= 30 for line in lines)
    assert lines[1].rstrip("│ ").endswith("…")


def test_the_card_title_is_painted_in_the_accent_and_the_border_dim():
    top = look.card("◆ ArtyCode 0.1.0", [("model", "m")], 80, COLOURED)[0]
    assert "\033[38;2;229;56;59m◆" in top and "\033[2m╭" in top and bare(top).startswith("╭─ ◆ ArtyCode")


def test_a_user_message_is_a_filled_block_and_plain_without_colour():
    assert look.user_block("first\nsecond", 40, PLAIN) == ["› first", "  second"]
    painted = look.user_block("fix it", 20, COLOURED)
    assert painted[0].startswith("\033[38;2;236;236;236;48;2;52;52;52m› fix it") and wrap.visible_length(painted[0]) == 20


def test_a_light_background_gets_a_light_block():
    light = look.theme_for(True, {"COLORFGBG": "0;15", "COLORTERM": "truecolor"})
    assert light.light and "48;2;228;228;228" in look.user_block("hi", 10, light)[0]
    assert not look.theme_for(True, {"COLORFGBG": "15;0"}).light


def test_colour_depth_follows_the_terminal():
    assert look.colour_depth({"COLORTERM": "truecolor"}) == look.TRUE
    assert look.colour_depth({"WT_SESSION": "abc"}) == look.TRUE
    assert look.colour_depth({"TERM": "linux"}) == look.BASIC
    assert look.colour_depth({"TERM": "xterm-256color"}) == look.PALETTE


def render(text, theme=PLAIN):
    markdown = look.Markdown(theme)
    shown = [markdown.line(line) for line in text.split("\n")]
    return [line.text for line in shown if line is not None]


def test_markdown_headings_lists_quotes_and_rules_are_drawn_without_their_marks():
    shown = render("## Plan\n- first\n  * nested\n1. one\n> careful\n---\n- [x] done")
    assert shown == ["Plan", "• first", "  • nested", "1. one", "│ careful", "─" * look.RULE_MOST, "☑ done"]


def test_inline_markdown_turns_into_styles():
    shown = render("Use **bold**, *slanted*, `code` and [docs](https://x.dev) but keep snake_case_names", COLOURED)[0]
    assert "\033[1mbold\033[0m" in shown and "\033[3mslanted\033[0m" in shown and "\033[36mcode\033[0m" in shown
    assert "\033[4mdocs\033[0m" in shown and "snake_case_names" in bare(shown)
    assert bare(shown) == "Use bold, slanted, code and docs (https://x.dev) but keep snake_case_names"


def test_fenced_code_hides_its_fences_keeps_its_spaces_and_is_coloured():
    markdown = look.Markdown(COLOURED)
    lines = [markdown.line(line) for line in ["```python", "def add(a, b):", "    return a + b  # sum", "```", "after"]]
    assert lines[0] is None and lines[3] is None and not markdown.in_code()
    assert bare(lines[1].text) == "│ def add(a, b):" and not lines[1].wraps
    assert bare(lines[2].text) == "│     return a + b  # sum"
    assert "\033[35mdef\033[0m" in lines[1].text and "\033[2;3m# sum\033[0m" in lines[2].text
    assert lines[4].text == "after"


def test_code_in_an_unknown_language_or_without_colour_stays_readable():
    assert bare(render("```nosuchlanguage\nx = 1\n```", COLOURED)[0]) == "│ x = 1"
    assert render("```\nx = 1\n```")[0] == "│ x = 1"


def test_a_list_item_wraps_under_its_text():
    markdown = look.Markdown(PLAIN)
    item = markdown.line("- " + "word " * 30)
    assert item.hang == "  "
    rows = wrap.wrapped_line(item.text, 40, item.hang)
    assert rows[0].startswith("• word") and all(row.startswith("  word") for row in rows[1:])


def test_a_diff_is_numbered_coloured_and_split_between_hunks():
    shown = [bare(line) for line in look.numbered_diff(DIFF, COLOURED)]
    assert shown == ["     1  def add(a, b):", "     2 -    return a - b", "     2 +    return a + b", "     3  ",
                     "     ⋮", "    40  x = 1", "    41 +y = 2", "    42  z = 3"]
    painted = look.numbered_diff(DIFF, COLOURED)
    assert "\033[31m-    return a - b" in painted[1] and "\033[32m+    return a + b" in painted[2]
    assert look.diff_counts(DIFF) == (2, 1)


def test_a_long_diff_is_cut_with_the_number_of_hidden_lines():
    long = "@@ -1,0 +1,30 @@\n" + "\n".join(f"+line {n}" for n in range(30))
    shown = look.numbered_diff(long, PLAIN, most=5)
    assert len(shown) == 6 and shown[-1] == "    … +25 lines"


def test_output_is_collapsed_after_a_few_lines_with_a_hint():
    lines = [f"line {n}" for n in range(40)]
    shown, hidden = look.collapsed(lines, 3, PLAIN, "ctrl+o to see all")
    assert hidden == 37 and shown[-1] == "    … +37 lines (ctrl+o to see all)" and shown[:3] == lines[:3]
    assert look.collapsed(lines[:4], 3, PLAIN) == (lines[:4], 0)


def test_a_step_is_a_coloured_bullet_with_a_bold_verb_and_its_result_hangs_under_it():
    assert look.step_line("Run pytest -q", look.DONE, 80, PLAIN) == ["● Run pytest -q"]
    painted = look.step_line("Run pytest -q", look.FAILED, 80, COLOURED)[0]
    assert painted.startswith("\033[31m●\033[0m \033[1mRun\033[0m pytest")
    assert look.step_line("Read a.py", look.WAITING, 80, COLOURED)[0].startswith("\033[2m●")
    assert look.result_lines("2 lines", 80, PLAIN) == ["  └ 2 lines"]
    assert look.result_lines("boom", 80, COLOURED, is_problem=True) == ["\033[31m  └ boom\033[0m"]


def test_output_lines_are_indented_and_cut_to_the_width():
    assert look.output_lines("a\tb\n" + "x" * 50 + "\n", 20) == ["    a       b", "    " + "x" * 15 + "…"]


def test_shortcuts_fill_more_columns_on_wider_terminals():
    assert len(look.shortcut_columns(46)) == 1
    wide = look.shortcut_columns(140)
    assert len(wide) == 3 and sum(len(column) for column in wide) == len(look.SHORTCUTS)


def test_the_branch_comes_from_the_repository_head_even_in_a_worktree(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/shell-look\n")
    (tmp_path / "src").mkdir()
    assert look.git_branch(tmp_path / "src") == "shell-look"
    worktree = tmp_path / "other"
    (tmp_path / "linked").mkdir()
    (tmp_path / "linked" / "HEAD").write_text("0123456789abcdef\n")
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {tmp_path / 'linked'}\n")
    assert look.git_branch(worktree) == "0123456"


def test_no_branch_outside_a_repository(tmp_path):
    (tmp_path / ".git").write_text("not a link\n")
    assert look.git_branch(tmp_path) is None
