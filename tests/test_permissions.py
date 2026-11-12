import json
from pathlib import Path

import pytest

from arty_cli import permissions, tools


class Asker:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.questions = []

    def __call__(self, question, preview=""):
        self.questions.append((question, preview))
        return self.answers.pop(0) if self.answers else "n"


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    return tools.Workspace(tmp_path)


@pytest.mark.parametrize("command", ["ls -la", "git status", "git diff HEAD~1", "cat a.py", "pwd", "grep -n x a.py"])
def test_looking_around_needs_no_ok(command):
    assert permissions.is_read_only_command(command)


@pytest.mark.parametrize("command", ["rm -rf build", "ls; rm a.py", "cat a.py > b.py", "git push", "git checkout .",
                                     "echo $(whoami)", "find . -delete", "find . -exec rm {} ;", "ls | sh", "cat `x`"])
def test_anything_that_changes_or_chains_needs_an_ok(command):
    assert not permissions.is_read_only_command(command)


def test_reading_inside_the_project_is_allowed_without_asking(workspace):
    ask = Asker()
    gate = permissions.Gate(workspace, ask)
    assert gate.allows("read_file", {"path": "a.py"})
    assert ask.questions == []


def test_reading_outside_the_project_asks(workspace):
    ask = Asker("y")
    gate = permissions.Gate(workspace, ask)
    assert gate.allows("read_file", {"path": "/etc/hostname"})
    assert "outside" in ask.questions[0][0]


def test_an_edit_asks_and_shows_the_diff(workspace):
    ask = Asker("y")
    gate = permissions.Gate(workspace, ask)
    assert gate.allows("edit_file", {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"})
    question, preview = ask.questions[0]
    assert "a.py" in question
    assert "-x = 1" in preview and "+x = 2" in preview


def test_no_means_no(workspace):
    gate = permissions.Gate(workspace, Asker("n"))
    assert not gate.allows("write_file", {"path": "b.py", "content": "y = 2\n"})


def test_always_covers_later_edits_in_the_session(workspace):
    ask = Asker("a")
    gate = permissions.Gate(workspace, ask)
    assert gate.allows("write_file", {"path": "b.py", "content": "1\n"})
    assert gate.allows("edit_file", {"path": "a.py", "old_text": "x = 1", "new_text": "x = 3"})
    assert len(ask.questions) == 1


def test_always_for_a_command_covers_only_that_command(workspace):
    ask = Asker("a", "n")
    gate = permissions.Gate(workspace, ask)
    assert gate.allows("run_command", {"command": "pytest -q"})
    assert gate.allows("run_command", {"command": "pytest -q"})
    assert not gate.allows("run_command", {"command": "rm -rf /"})
    assert len(ask.questions) == 2


def test_auto_edit_skips_asking_inside_the_project_only(workspace):
    ask = Asker("n")
    gate = permissions.Gate(workspace, ask, mode=permissions.AUTO_EDIT)
    assert gate.allows("write_file", {"path": "b.py", "content": "1\n"})
    assert not gate.allows("write_file", {"path": "../outside.py", "content": "1\n"})
    assert len(ask.questions) == 1


def test_read_only_commands_run_without_asking(workspace):
    ask = Asker()
    assert permissions.Gate(workspace, ask).allows("run_command", {"command": "git status"})
    assert ask.questions == []


def test_without_a_terminal_nothing_risky_is_allowed(workspace):
    gate = permissions.Gate(workspace, permissions.refuse)
    assert not gate.allows("run_command", {"command": "make"})
    assert not gate.allows("edit_file", {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"})
    assert gate.allows("read_file", {"path": "a.py"})


def test_plan_mode_looks_but_never_changes(workspace):
    ask = Asker("y")
    gate = permissions.Gate(workspace, ask, mode=permissions.PLAN)
    assert gate.allows("read_file", {"path": "a.py"})
    assert gate.allows("run_command", {"command": "git log"})
    assert gate.check("edit_file", {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"}) == permissions.PLAN_REFUSAL
    assert gate.check("run_command", {"command": "make"}) == permissions.PLAN_REFUSAL
    assert ask.questions == []


def test_allow_all_never_asks(workspace):
    ask = Asker()
    gate = permissions.Gate(workspace, ask, mode=permissions.ALLOW_ALL)
    assert gate.allows("run_command", {"command": "make install"})
    assert gate.allows("write_file", {"path": "../elsewhere.py", "content": ""})
    assert ask.questions == []


def test_fetching_a_site_asks_once_per_site_when_told_always(workspace):
    ask = Asker("a", "n")
    gate = permissions.Gate(workspace, ask)
    assert gate.allows("web_fetch", {"url": "https://docs.python.org/3/library/json.html"})
    assert gate.allows("web_fetch", {"url": "https://docs.python.org/3/library/re.html"})
    assert not gate.allows("web_fetch", {"url": "https://example.com/"})
    assert len(ask.questions) == 2


def test_reading_background_output_needs_no_ok(workspace):
    ask = Asker()
    gate = permissions.Gate(workspace, ask)
    assert gate.allows("command_output", {"job": "1"}) and gate.allows("todo_write", {"items": []})
    assert ask.questions == []


@pytest.mark.parametrize("command", [
    "rg --pre ./run.sh x", "rg --pre=./run.sh x", "rg --pre-glob '*.py' --pre sh x", "rg --hostname-bin ./x y",
    "find . -fprint0 out", "find . -fprint out", "tree -o out.txt", "tree -aoout.txt",
    "git log --output=a.py", "git diff --output a.py", "git show --output=x HEAD", "git diff --ext-diff",
    "git branch -D main", "git branch -d old", "git branch -m new", "git branch -M x y", "git branch -c a b",
    "git branch --delete x", "git branch --move a b", "git branch --set-upstream-to=origin/x",
    "git branch -u origin/x", "git branch --unset-upstream", "git branch --edit-description", "git branch newname",
    "git branch -f main HEAD~1",
    "Get-Content (Remove-Item a.py -Force)", "Get-Content $(Remove-Item x)", "Get-ChildItem {Remove-Item x}",
    "Get-Content @(Remove-Item x)",
])
def test_read_only_programs_with_writing_or_running_options_need_an_ok(command):
    assert not permissions.is_read_only_command(command)


@pytest.mark.parametrize("command", [
    "rg -n todo", "rg --glob '*.py' x", "find . -name '*.py' -print", "tree -L 2", "git log --oneline -5",
    "git diff --stat", "git branch", "git branch -a", "git branch -vv", "git branch --list 'feat*'",
    "git branch --contains HEAD", "git branch --show-current", "git branch --sort=-committerdate",
    "grep -n 'def f(x)' a.py", "Get-Content a.py",
])
def test_ordinary_looking_around_still_needs_no_ok(command):
    assert permissions.is_read_only_command(command)


@pytest.mark.parametrize("command", ["cat ~/.ssh/id_rsa", "cat /etc/passwd", "grep -r token ~/.config",
                                     "cat ../secret.txt", "head -n 5 /home/someone/.netrc", "ls ..",
                                     "rg --file=/etc/shadow x", "cat sub/../../x"])
def test_looking_outside_the_project_with_a_command_asks(workspace, command):
    ask = Asker("n")
    gate = permissions.Gate(workspace, ask)
    assert not gate.allows("run_command", {"command": command})
    assert "outside" in ask.questions[0][0]


def test_looking_outside_with_a_command_asks_even_in_plan_mode(workspace):
    ask = Asker("y")
    gate = permissions.Gate(workspace, ask, mode=permissions.PLAN)
    assert gate.allows("run_command", {"command": "cat /etc/hostname"})
    assert len(ask.questions) == 1


def test_a_symlink_out_of_the_project_counts_as_outside(workspace, tmp_path_factory):
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    (workspace.root / "link").symlink_to(elsewhere)
    ask = Asker("n")
    assert not permissions.Gate(workspace, ask).allows("run_command", {"command": "ls link"})


def test_listing_with_a_pattern_that_climbs_out_asks(workspace):
    ask = Asker("n", "n")
    gate = permissions.Gate(workspace, ask)
    assert not gate.allows("list_files", {"pattern": "../*"})
    assert not gate.allows("list_files", {"pattern": "/etc/*"})
    assert gate.allows("list_files", {"pattern": "**/*.py"})
    assert len(ask.questions) == 2


def notebook_workspace(workspace):
    (workspace.root / "n.ipynb").write_text('{"cells": [{"cell_type": "code", "source": "a = 1", "metadata": {}, '
                                            '"outputs": [], "execution_count": null}], "metadata": {}, '
                                            '"nbformat": 4, "nbformat_minor": 4}')
    return {"path": "n.ipynb", "cell": 1, "source": "a = 2"}


def test_a_notebook_edit_asks_with_a_preview_and_plan_mode_refuses_it(workspace):
    edit = notebook_workspace(workspace)
    ask = Asker("y")
    assert permissions.Gate(workspace, ask).allows("notebook_edit", edit)
    assert "n.ipynb" in ask.questions[0][0] and "a = 2" in ask.questions[0][1]
    assert permissions.Gate(workspace, Asker(), mode=permissions.PLAN).check("notebook_edit", edit) == permissions.PLAN_REFUSAL
    assert permissions.Gate(workspace, Asker(), mode=permissions.AUTO_EDIT).allows("notebook_edit", edit)


def test_a_tool_the_gate_does_not_know_asks_first(workspace):
    ask = Asker("n")
    assert not permissions.Gate(workspace, ask).allows("something_new", {"x": 1})
    assert "something_new" in ask.questions[0][0]
    assert permissions.Gate(workspace, Asker(), mode=permissions.PLAN).check("something_new", {}) == permissions.PLAN_REFUSAL


@pytest.mark.parametrize("command", ["cat ~-/.ssh/id_rsa", "cat ~+/../other/secret.env", "cat ~+1/x", "head ~-",
                                     "cat ~--/x", "ls ~-2", "Get-Content ..\\secret.txt", "cat C:\\Users\\x\\key",
                                     "type \\\\server\\share\\x"])
def test_every_tilde_and_backslash_form_outside_the_project_asks(workspace, command):
    ask = Asker("n")
    assert not permissions.Gate(workspace, ask).allows("run_command", {"command": command})
    assert "outside" in ask.questions[0][0]


def test_tilde_plus_means_the_project_folder_itself(workspace):
    ask = Asker()
    assert permissions.Gate(workspace, ask).allows("run_command", {"command": "cat ~+/a.py"})
    assert ask.questions == []


def test_a_wildcard_that_reaches_through_a_link_asks(workspace, tmp_path_factory):
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    (elsewhere / "passwd").write_text("secret\n")
    (workspace.root / "linked").symlink_to(elsewhere)
    ask = Asker("n", "n")
    gate = permissions.Gate(workspace, ask)
    assert not gate.allows("run_command", {"command": "cat l*/passwd"})
    assert not gate.allows("run_command", {"command": "cat ./link?d/passwd"})
    assert permissions.Gate(workspace, Asker()).allows("run_command", {"command": "cat *.py"})


def test_plan_mode_and_helpers_ask_about_tilde_forms_too(workspace):
    ask = Asker("n")
    gate = permissions.Gate(workspace, ask, mode=permissions.PLAN)
    assert not gate.allows("run_command", {"command": "cat ~-/.ssh/id_rsa"})
    assert len(ask.questions) == 1


@pytest.mark.parametrize("command", ["grep -R token .", "grep -rR x .", "grep --dereference-recursive x .",
                                     "find -L . -name x", "find . -follow -name x", "ls -LR", "ls -lRL .",
                                     "ls --dereference -R", "du -L .", "du -sL .", "du --dereference .", "tree -l",
                                     "tree -al", "rg -L x", "rg --follow x", "tree -R", "tree -H . x",
                                     "file -C", "file --compile", "diff -r a b", "diff --recursive a b"])
def test_options_that_follow_links_or_write_need_an_ok(command):
    assert not permissions.is_read_only_command(command)


@pytest.mark.parametrize("command", ["grep -r token .", "find . -name '*.py'", "ls -la", "du -sh .", "tree -a",
                                     "rg -n x", "file a.py", "diff a.py b.py", "ls -l"])
def test_options_that_stay_inside_still_need_no_ok(command):
    assert permissions.is_read_only_command(command)


def git_repo(workspace, *settings):
    import subprocess
    subprocess.run(["git", "init", "-q", str(workspace.root)], check=True)
    for key, value in settings:
        subprocess.run(["git", "-C", str(workspace.root), "config", key, value], check=True)


@pytest.mark.parametrize("key, value", [("core.fsmonitor", "./run.sh"), ("diff.external", "./run.sh"),
                                        ("diff.x.textconv", "./run.sh"), ("filter.x.clean", "./run.sh"),
                                        ("filter.x.process", "./run.sh"), ("gpg.program", "./run.sh"),
                                        ("diff.x.command", "./run.sh"), ("gpg.ssh.program", "./run.sh"),
                                        ("core.pager", "./run.sh")])
def test_look_only_git_asks_when_the_repository_names_a_program(workspace, key, value):
    git_repo(workspace, (key, value))
    ask = Asker("n")
    assert not permissions.Gate(workspace, ask).allows("run_command", {"command": "git status"})
    assert "program" in ask.questions[0][0]


def test_look_only_git_in_a_plain_repository_needs_no_ok(workspace):
    git_repo(workspace, ("user.name", "x"), ("core.fsmonitor", "false"))
    ask = Asker()
    assert permissions.Gate(workspace, ask).allows("run_command", {"command": "git log --oneline"})
    assert ask.questions == []


def rules(allow=(), deny=()):
    return permissions.Rules.from_lists(list(allow), list(deny))


def test_saved_allow_rules_skip_the_question_for_matching_calls_only(workspace):
    ask = Asker("n", "n")
    gate = permissions.Gate(workspace, ask)
    gate.rules = rules(allow=["run_command(npm test*)", "edit_file(src/*)", "web_fetch(https://docs.python.org/*)"])
    (workspace.root / "src").mkdir()
    (workspace.root / "src" / "m.py").write_text("a = 1\n")
    assert gate.allows("run_command", {"command": "npm test -- --watch=false"})
    assert gate.allows("edit_file", {"path": "src/m.py", "old_text": "a = 1", "new_text": "a = 2"})
    assert gate.allows("web_fetch", {"url": "https://docs.python.org/3/library/os.html"})
    assert ask.questions == []
    assert not gate.allows("run_command", {"command": "npm test; rm -rf /"})
    assert not gate.allows("edit_file", {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"})
    assert len(ask.questions) == 2


def test_deny_rules_win_over_allow_rules_modes_and_any_part_of_a_command(workspace):
    gate = permissions.Gate(workspace, Asker("y"), mode=permissions.ALLOW_ALL)
    gate.rules = rules(allow=["run_command(*)"], deny=["run_command(rm *)", "read_file(*.env)", "mcp__notes__*"])
    assert gate.check("run_command", {"command": "ls && rm -rf x"}) == permissions.RULE_DENIED
    assert gate.check("read_file", {"path": "prod.env"}) == permissions.RULE_DENIED
    assert gate.rule_refusal("mcp__notes__delete", {"id": "1"}) == permissions.RULE_DENIED
    assert gate.check("run_command", {"command": "ls"}) is None


def test_allow_rules_never_let_plan_mode_change_anything(workspace):
    gate = permissions.Gate(workspace, Asker(), mode=permissions.PLAN)
    gate.rules = rules(allow=["run_command(*)", "write_file(*)"])
    assert gate.check("run_command", {"command": "touch x"}) == permissions.PLAN_REFUSAL
    assert gate.check("write_file", {"path": "x", "content": "y"}) == permissions.PLAN_REFUSAL


def test_rules_load_from_personal_and_trusted_project_settings(tmp_path):
    import json
    personal = tmp_path / "personal"
    personal.mkdir()
    (personal / "settings.json").write_text(json.dumps({"permissions": {"allow": ["run_command(make *)"]}}))
    project = tmp_path / "project"
    (project / ".arty").mkdir(parents=True)
    (project / ".arty" / "settings.json").write_text(json.dumps({"permissions": {"deny": ["run_command(make deploy)"],
                                                                                 "allow": [7]}}))
    workspace = tools.Workspace(project)
    trusted, problems = permissions.load_rules(personal, workspace, include_project=True)
    assert trusted.allow == ["run_command(make *)"] and trusted.deny == ["run_command(make deploy)"]
    assert problems and "7" in problems[0]
    untrusted, _ = permissions.load_rules(personal, workspace, include_project=False)
    assert untrusted.deny == []


def saving_gate(workspace, *answers):
    told = []
    gate = permissions.Gate(workspace, Asker(*answers))
    gate.rule_file = permissions.RuleFile(workspace.root / permissions.PROJECT_SETTINGS, told.append)
    return gate, told


def saved_allow_rules(workspace):
    return json.loads((workspace.root / permissions.PROJECT_SETTINGS).read_text())["permissions"]["allow"]


def test_saving_a_command_writes_an_allow_rule_that_later_sessions_follow(workspace):
    gate, told = saving_gate(workspace, permissions.SAVE)
    assert gate.allows("run_command", {"command": "pytest -q"})
    assert saved_allow_rules(workspace) == ["run_command(pytest -q)"]
    assert "run_command(pytest -q)" in told[0]
    later = permissions.Gate(workspace, Asker("n", "n"))
    later.rules, problems = permissions.load_rules(workspace.root / "nobody", workspace, include_project=True)
    assert problems == []
    assert later.allows("run_command", {"command": "pytest -q"})
    assert not later.allows("run_command", {"command": "pytest -q -x"})


def test_a_saved_rule_keeps_the_rest_of_the_settings_file(workspace):
    settings_file = workspace.root / permissions.PROJECT_SETTINGS
    settings_file.parent.mkdir()
    settings_file.write_text(json.dumps({"hooks": {"Stop": []}, "permissions": {"allow": ["read_file"], "deny": ["x"]}}))
    gate, _told = saving_gate(workspace, permissions.SAVE)
    assert gate.allows("edit_file", {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"})
    table = json.loads(settings_file.read_text())
    assert table["hooks"] == {"Stop": []}
    assert table["permissions"] == {"allow": ["read_file", "edit_file(a.py)"], "deny": ["x"]}


def test_wildcards_in_a_saved_command_only_match_themselves(workspace):
    gate, _told = saving_gate(workspace, permissions.SAVE, "n")
    assert gate.allows("run_command", {"command": "touch build*"})
    assert saved_allow_rules(workspace) == ["run_command(touch build[*])"]
    assert gate.allows("run_command", {"command": "touch build*"})
    assert not gate.allows("run_command", {"command": "touch buildx"})


def test_saving_a_fetch_allows_that_site(workspace):
    gate, _told = saving_gate(workspace, permissions.SAVE)
    assert gate.allows("web_fetch", {"url": "https://docs.python.org/3/library/json.html"})
    assert saved_allow_rules(workspace) == ["web_fetch(https://docs.python.org/*)"]


def test_a_chained_command_cannot_become_a_rule_so_it_is_allowed_for_the_session(workspace):
    gate, told = saving_gate(workspace, permissions.SAVE)
    assert gate.allows("run_command", {"command": "make && make install"})
    assert gate.allows("run_command", {"command": "make && make install"})
    assert not (workspace.root / permissions.PROJECT_SETTINGS).exists()
    assert "session" in told[0]


def test_an_unreadable_settings_file_is_left_alone_and_the_answer_lasts_the_session(workspace):
    settings_file = workspace.root / permissions.PROJECT_SETTINGS
    settings_file.parent.mkdir()
    settings_file.write_text("{not json")
    gate, told = saving_gate(workspace, permissions.SAVE)
    assert gate.allows("run_command", {"command": "pytest -q"})
    assert gate.allows("run_command", {"command": "pytest -q"})
    assert settings_file.read_text() == "{not json"
    assert "Couldn't save" in told[0]


def test_without_a_rule_file_saving_is_the_same_as_always(workspace):
    ask = Asker(permissions.SAVE)
    gate = permissions.Gate(workspace, ask)
    assert gate.allows("run_command", {"command": "pytest -q"})
    assert gate.allows("run_command", {"command": "pytest -q"})
    assert len(ask.questions) == 1


def test_a_multi_edit_asks_once_and_shows_the_whole_diff(workspace):
    (workspace.root / "a.py").write_text("x = 1\ny = 1\n")
    ask = Asker("y")
    gate = permissions.Gate(workspace, ask)
    edits = [{"old_text": "x = 1", "new_text": "x = 2"}, {"old_text": "y = 1", "new_text": "y = 2"}]
    assert gate.allows("multi_edit", {"path": "a.py", "edits": edits})
    question, preview = ask.questions[0]
    assert question == "Edit a.py?"
    assert "+x = 2" in preview and "+y = 2" in preview
    assert not permissions.Gate(workspace, Asker(), mode=permissions.PLAN).allows("multi_edit", {"path": "a.py",
                                                                                                "edits": edits})


def test_a_web_search_asks_once_shows_the_query_and_is_fine_in_plan_mode(workspace):
    ask = Asker("a")
    gate = permissions.Gate(workspace, ask, mode=permissions.PLAN)
    assert gate.allows("web_search", {"query": "python json"})
    assert gate.allows("web_search", {"query": "something else"})
    assert ask.questions == [("Search the web for: python json?", "")]


def test_a_deny_rule_can_forbid_some_searches(workspace):
    gate = permissions.Gate(workspace, Asker("y"))
    gate.rules = permissions.Rules(deny=["web_search(*password*)"])
    assert not gate.allows("web_search", {"query": "my password"})
    assert gate.allows("web_search", {"query": "python json"})


PLAN = "1. Add a --dry-run flag to cli.py\n2. Test it"


def test_approving_a_plan_leaves_plan_mode():
    gate = permissions.Gate(tools.Workspace(Path.cwd()), Asker(), mode=permissions.PLAN)
    seen = []
    gate.review = lambda plan: seen.append(plan) or (permissions.YES, "")
    result = gate.review_plan(PLAN)
    assert seen == [PLAN]
    assert gate.mode == permissions.DEFAULT
    assert "approved" in result and "default" in result


def test_approving_with_auto_accept_switches_to_auto_edit():
    gate = permissions.Gate(tools.Workspace(Path.cwd()), Asker(), mode=permissions.PLAN)
    gate.review = lambda plan: (permissions.ALWAYS, "")
    gate.review_plan(PLAN)
    assert gate.mode == permissions.AUTO_EDIT


def test_rejecting_a_plan_keeps_plan_mode_and_passes_on_the_feedback():
    gate = permissions.Gate(tools.Workspace(Path.cwd()), Asker(), mode=permissions.PLAN)
    gate.review = lambda plan: (permissions.NO, "write the test first")
    result = gate.review_plan(PLAN)
    assert gate.mode == permissions.PLAN
    assert "rejected" in result and "write the test first" in result


def test_without_a_reviewer_the_plain_question_is_asked():
    ask = Asker("y")
    gate = permissions.Gate(tools.Workspace(Path.cwd()), ask, mode=permissions.PLAN)
    gate.review_plan(PLAN)
    assert PLAN in ask.questions[0][0]
    assert gate.mode == permissions.DEFAULT


@pytest.mark.parametrize("mode, plan, problem", [(permissions.DEFAULT, PLAN, "isn't on"),
                                                 (permissions.PLAN, "  ", "empty")])
def test_a_plan_outside_plan_mode_or_without_steps_is_an_error(mode, plan, problem):
    ask = Asker()
    gate = permissions.Gate(tools.Workspace(Path.cwd()), ask, mode=mode)
    result = gate.review_plan(plan)
    assert result.startswith("Error") and problem in result
    assert ask.questions == [] and gate.mode == mode


PROTECTED_PATHS = [".arty/settings.json", ".arty/mcp.json", ".arty/helpers/x.md", ".git/hooks/pre-commit", ".git/config",
                   "vendor/lib/.git/hooks/post-checkout", ".GIT/config"]


@pytest.mark.parametrize("path", PROTECTED_PATHS)
def test_setup_and_git_files_are_always_asked_about_even_in_auto_edit(workspace, path):
    ask = Asker("n")
    gate = permissions.Gate(workspace, ask, mode=permissions.AUTO_EDIT)
    assert gate.check("write_file", {"path": path, "content": "{}"}) == permissions.REFUSED
    assert len(ask.questions) == 1 and path in ask.questions[0][0]


def test_setup_files_are_asked_about_after_always_and_a_saved_rule(workspace):
    ask = Asker(permissions.ALWAYS, "n")
    gate = permissions.Gate(workspace, ask)
    gate.rules = permissions.Rules(allow=["write_file(*)", "write_file(.arty/*)"], deny=[])
    assert gate.allows("edit_file", {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"})
    assert gate.allows("write_file", {"path": "b.py", "content": ""})
    assert not gate.allows("write_file", {"path": ".arty/settings.json", "content": "{}"})
    assert len(ask.questions) == 2


def test_a_yes_to_a_setup_file_is_for_that_one_change_only(workspace):
    gate, told = saving_gate(workspace, permissions.SAVE, permissions.ALWAYS, "n")
    assert gate.allows("write_file", {"path": ".git/hooks/pre-commit", "content": ""})
    assert gate.allows("write_file", {"path": ".git/hooks/pre-push", "content": ""})
    assert not gate.allows("write_file", {"path": ".git/hooks/pre-commit", "content": ""})
    assert not (workspace.root / permissions.PROJECT_SETTINGS).exists() and told == []
