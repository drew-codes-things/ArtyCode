import _thread
import json
import threading

import pytest

from arty_cli import agent, brain, extras, helpers, hooks, permissions, skills, tools
from tests.portable import file_making_call, python_call

MAKE_A_FILE = file_making_call("made")


def call(name, call_id="c1", **arguments):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


class ScriptedBrain:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.sent = []

    def reply(self, messages, tools, on_text):
        self.sent.append({"messages": [dict(m) for m in messages], "tools": tools})
        content, calls = self.replies.pop(0)
        if content:
            on_text(content)
        return {"role": "assistant", "content": content, "tool_calls": calls}


class Screen:
    def __init__(self):
        self.text, self.activity = [], []

    def show_text(self, text):
        self.text.append(text)

    def show_activity(self, line):
        self.activity.append(line)


def test_a_fix_is_read_made_and_reported(tmp_path):
    (tmp_path / "app.py").write_text("def add(a, b):\n    return a - b\n")
    workspace = tools.Workspace(tmp_path)
    scripted = ScriptedBrain(
        ("", [call("read_file", path="app.py")]),
        ("", [call("edit_file", "c2", path="app.py", old_text="a - b", new_text="a + b")]),
        ("Fixed: add now adds.", []),
    )
    screen = Screen()
    messages = [{"role": "user", "content": "fix add"}]
    answer = agent.run_turn(messages, scripted, workspace, permissions.Gate(workspace, lambda q, p="": "y"), screen)
    assert answer == "Fixed: add now adds."
    assert (tmp_path / "app.py").read_text().endswith("a + b\n")
    assert messages[-1] == {"role": "assistant", "content": "Fixed: add now adds."}
    tool_results = [m for m in scripted.sent[2]["messages"] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_results] == ["c1", "c2"]
    assert "1\tdef add" in tool_results[0]["content"]
    assert any("app.py" in line for line in screen.activity)


def test_a_refused_step_tells_the_model_not_to_work_round_it(tmp_path):
    workspace = tools.Workspace(tmp_path)
    scripted = ScriptedBrain(("", [call("run_command", command="rm -rf build")]), ("OK, I left it.", []))
    agent.run_turn([{"role": "user", "content": "clean"}], scripted, workspace,
                   permissions.Gate(workspace, permissions.refuse), Screen())
    result = [m for m in scripted.sent[1]["messages"] if m["role"] == "tool"][0]["content"]
    assert result == agent.REFUSED


def test_the_same_call_over_and_over_is_steered_by_a_message_with_the_tools_unchanged(tmp_path):
    (tmp_path / "a.txt").write_text("a\n")
    workspace = tools.Workspace(tmp_path)
    same = ("", [call("read_file", path="a.txt")])
    scripted = ScriptedBrain(*([same] * agent.STUCK_REPEATS), ("Here's what I found.", []))
    answer = agent.run_turn([{"role": "user", "content": "look"}], scripted, workspace,
                            permissions.Gate(workspace, permissions.refuse), Screen())
    assert answer == "Here's what I found."
    assert all(sent["tools"] == scripted.sent[0]["tools"] for sent in scripted.sent)
    assert scripted.sent[-1]["messages"][-1] == {"role": "user", "content": agent.STUCK_NOTE}


def test_a_model_that_stays_stuck_after_the_note_is_stopped(tmp_path):
    (tmp_path / "a.txt").write_text("a\n")
    workspace = tools.Workspace(tmp_path)
    same = ("", [call("read_file", path="a.txt")])
    scripted = ScriptedBrain(*([same] * (agent.STUCK_REPEATS + 1)))
    messages = [{"role": "user", "content": "look"}]
    answer = agent.run_turn(messages, scripted, workspace, permissions.Gate(workspace, permissions.refuse), Screen())
    assert answer == agent.STUCK_STOPPED
    assert messages[-1] == {"role": "assistant", "content": agent.STUCK_STOPPED}


def test_bad_arguments_from_the_model_are_reported_back(tmp_path):
    workspace = tools.Workspace(tmp_path)
    broken = {"id": "x", "type": "function", "function": {"name": "read_file", "arguments": "{oops"}}
    scripted = ScriptedBrain(("", [broken]), ("Sorry.", []))
    agent.run_turn([{"role": "user", "content": "read"}], scripted, workspace,
                   permissions.Gate(workspace, permissions.refuse), Screen())
    result = [m for m in scripted.sent[1]["messages"] if m["role"] == "tool"][0]["content"]
    assert result.startswith("Error") and "JSON" in result


def tool_results_sent(scripted, round_index):
    return [m["content"] for m in scripted.sent[round_index]["messages"] if m["role"] == "tool"]


def test_booleans_written_as_words_are_read_strictly(tmp_path):
    (tmp_path / "twice.txt").write_text("alpha\nalpha\n")
    workspace = tools.Workspace(tmp_path)
    scripted = ScriptedBrain(
        ("", [call("read_file", path="twice.txt")]),
        ("", [call("edit_file", "c2", path="twice.txt", old_text="alpha", new_text="gamma", replace_all="false"),
              call("run_command", "c3", command="cat twice.txt", background="false"),
              call("edit_file", "c4", path="twice.txt", old_text="alpha", new_text="gamma", replace_all="maybe")]),
        ("Done.", []),
    )
    agent.run_turn([{"role": "user", "content": "edit"}], scripted, workspace,
                   permissions.Gate(workspace, permissions.refuse, mode=permissions.AUTO_EDIT), Screen())
    edited, ran, unclear = tool_results_sent(scripted, 2)[1:]
    assert edited.startswith("Error") and "2 places" in edited
    assert (tmp_path / "twice.txt").read_text() == "alpha\nalpha\n"
    assert "alpha" in ran and "Job" not in ran
    assert unclear.startswith("Error") and "replace_all" in unclear


def test_a_path_the_system_rejects_is_an_error_for_the_model_not_a_crash(tmp_path):
    workspace = tools.Workspace(tmp_path)
    scripted = ScriptedBrain(("", [call("read_file", path="a\x00b"), call("write_file", "c2", path="b\x00", content=""),
                                   call("search", "c3", pattern="x", path="c\x00")]), ("Sorry.", []))
    answer = agent.run_turn([{"role": "user", "content": "read"}], scripted, workspace,
                            permissions.Gate(workspace, permissions.refuse), Screen())
    assert answer == "Sorry."
    results = tool_results_sent(scripted, 1)
    assert len(results) == 3
    assert all(result.startswith("Error") and "null" in result for result in results)


LONG_JOB_STEPS = 150


def test_a_long_job_carries_on_for_as_many_steps_as_it_needs(tmp_path):
    (tmp_path / "a.txt").write_text("".join(f"line {number}\n" for number in range(LONG_JOB_STEPS)))
    workspace = tools.Workspace(tmp_path)
    replies = [("", [call("read_file", f"c{i}", path="a.txt", offset=i + 1, limit=1)]) for i in range(LONG_JOB_STEPS)]
    scripted = ScriptedBrain(*replies, ("Read every line.", []))
    answer = agent.run_turn([{"role": "user", "content": "go"}], scripted, workspace,
                            permissions.Gate(workspace, permissions.refuse), Screen())
    assert answer == "Read every line."
    assert len(scripted.sent) == LONG_JOB_STEPS + 1


def test_an_explore_helper_looks_around_on_its_own_and_reports_back(tmp_path):
    (tmp_path / "a.py").write_text("SECRET = 1\n")
    workspace = tools.Workspace(tmp_path)
    scripted = ScriptedBrain(
        ("", [call("explore", task="where is SECRET defined?")]),
        ("", [call("search", "s1", pattern="SECRET")]),
        ("It's in a.py line 1.", []),
        ("SECRET is defined in a.py.", []),
    )
    answer = agent.run_turn([{"role": "user", "content": "find SECRET"}], scripted, workspace,
                            permissions.Gate(workspace, permissions.refuse), Screen())
    assert answer == "SECRET is defined in a.py."
    helper_request = scripted.sent[1]
    assert helper_request["messages"][-1]["content"] == "where is SECRET defined?"
    assert {d["function"]["name"] for d in helper_request["tools"]} <= set(agent.EXPLORE_TOOLS)
    result = [m for m in scripted.sent[3]["messages"] if m["role"] == "tool"][0]["content"]
    assert result == "It's in a.py line 1."


def test_the_explore_helper_cannot_change_anything(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    workspace = tools.Workspace(tmp_path)
    scripted = ScriptedBrain(
        ("", [call("explore", task="tidy a.py")]),
        ("", [call("write_file", "w1", path="a.py", content="gone")]),
        ("I couldn't change it.", []),
        ("Done looking.", []),
    )
    agent.run_turn([{"role": "user", "content": "go"}], scripted, workspace,
                   permissions.Gate(workspace, lambda q, p="": "y"), Screen())
    assert (tmp_path / "a.py").read_text() == "x = 1\n"


def test_changes_and_commands_that_ran_are_recorded(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    workspace = tools.Workspace(tmp_path)
    scripted = ScriptedBrain(
        ("", [call("read_file", "r0", path="a.py")]),
        ("", [call("edit_file", "e1", path="a.py", old_text="x = 1", new_text="x = 2"),
              call("run_command", "r1", command="rm -rf build")]),
        ("", [call("read_file", "r2", path="a.py")]),
        ("Done.", []),
    )
    asked = iter(["y", "n"])
    done = []
    agent.run_turn([{"role": "user", "content": "go"}], scripted, workspace,
                   permissions.Gate(workspace, lambda q, p="": next(asked)), Screen(), done=done)
    assert done == ["edit_file a.py"]


def test_the_conversation_can_be_squeezed_between_rounds(tmp_path):
    (tmp_path / "a.txt").write_text("a\n")
    workspace = tools.Workspace(tmp_path)
    scripted = ScriptedBrain(("", [call("read_file", path="a.txt")]), ("Read it.", []))
    seen = []
    agent.run_turn([{"role": "user", "content": "look"}], scripted, workspace,
                   permissions.Gate(workspace, permissions.refuse), Screen(), squeeze=lambda m: seen.append(len(m)))
    assert seen == [3]


def test_a_command_cut_short_by_ctrl_c_is_still_recorded(tmp_path, monkeypatch):
    workspace = tools.Workspace(tmp_path)
    scripted = ScriptedBrain(("", [call("run_command", command="make deploy")]))

    def interrupted(name, arguments, workspace):
        raise KeyboardInterrupt

    monkeypatch.setattr(agent.tools, "run", interrupted)
    done = []
    try:
        agent.run_turn([{"role": "user", "content": "go"}], scripted, workspace,
                       permissions.Gate(workspace, lambda q, p="": "y"), Screen(), done=done)
    except KeyboardInterrupt:
        pass
    assert done == ["run_command make deploy (stopped part way)"]


def test_ctrl_c_while_being_asked_records_nothing(tmp_path):
    workspace = tools.Workspace(tmp_path)
    scripted = ScriptedBrain(("", [call("run_command", command="make deploy")]))

    def ctrl_c(question, preview=""):
        raise KeyboardInterrupt

    done = []
    try:
        agent.run_turn([{"role": "user", "content": "go"}], scripted, workspace, permissions.Gate(workspace, ctrl_c),
                       Screen(), done=done)
    except KeyboardInterrupt:
        pass
    assert done == []


def hook_script(tmp_path, name, body):
    path = tmp_path / f"{name}.py"
    path.write_text(body)
    return python_call(path)


def with_hooks(workspace, *listed):
    return extras.Extras(hooks=hooks.Hooks([hooks.Hook(event, matcher, command, 30, "yours")
                                            for event, matcher, command in listed], workspace))


def test_a_pre_tool_hook_can_block_a_call_before_anyone_is_asked(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    workspace = tools.Workspace(tmp_path)
    block = hook_script(tmp_path, "block", "import sys\nsys.stderr.write('a.py is frozen')\nsys.exit(2)\n")
    scripted = ScriptedBrain(("", [call("edit_file", path="a.py", old_text="x = 1", new_text="x = 2")]), ("OK.", []))
    asked = []
    agent.run_turn([{"role": "user", "content": "go"}], scripted, workspace,
                   permissions.Gate(workspace, lambda q, p="": asked.append(q) or "y"), Screen(),
                   extras=with_hooks(workspace, ("PreToolUse", "edit_file", block)))
    result = [m for m in scripted.sent[1]["messages"] if m["role"] == "tool"][0]["content"]
    assert result == f"{agent.HOOK_BLOCKED} a.py is frozen"
    assert asked == [] and (tmp_path / "a.py").read_text() == "x = 1\n"


def test_a_post_tool_hook_can_send_feedback_and_problems_are_shown(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    workspace = tools.Workspace(tmp_path)
    lint = hook_script(tmp_path, "lint", "import sys, json\ndata = json.load(sys.stdin)\n"
                       "sys.stderr.write('lint: ' + data['tool_response'][:6])\nsys.exit(2)\n")
    broken = hook_script(tmp_path, "broken", "import sys\nsys.exit(5)\n")
    scripted = ScriptedBrain(("", [call("read_file", path="a.py")]), ("OK.", []))
    screen = Screen()
    agent.run_turn([{"role": "user", "content": "go"}], scripted, workspace,
                   permissions.Gate(workspace, permissions.refuse), screen,
                   extras=with_hooks(workspace, ("PostToolUse", "read_file", lint), ("PostToolUse", "", broken)))
    result = [m for m in scripted.sent[1]["messages"] if m["role"] == "tool"][0]["content"]
    assert result.startswith("1\tx = 1") and result.endswith(f"{agent.HOOK_FEEDBACK} lint: 1\tx =")
    assert any("exit code 5" in line for line in screen.activity)


def test_a_stop_hook_can_send_the_model_back_to_work_once_then_let_it_stop(tmp_path):
    workspace = tools.Workspace(tmp_path)
    log = tmp_path / "stops.txt"
    gate_keeper = hook_script(tmp_path, "stop", "import json, sys\ndata = json.load(sys.stdin)\n"
                              f"open({str(log)!r}, 'a').write(str(data['stop_hook_active']) + '\\n')\n"
                              "if not data['stop_hook_active']:\n    sys.stderr.write('run the tests first')\n"
                              "    sys.exit(2)\n")
    scripted = ScriptedBrain(("Done.", []), ("Tests pass, done.", []))
    messages = [{"role": "user", "content": "go"}]
    answer = agent.run_turn(messages, scripted, workspace, permissions.Gate(workspace, permissions.refuse), Screen(),
                            extras=with_hooks(workspace, ("Stop", "", gate_keeper)))
    assert answer == "Tests pass, done."
    assert scripted.sent[1]["messages"][-1] == {"role": "user", "content": f"{agent.STOP_HOOK_LEAD} run the tests first"}
    assert log.read_text().split() == ["False", "True"]


def test_helpers_do_not_run_stop_hooks(tmp_path):
    workspace = tools.Workspace(tmp_path)
    log = tmp_path / "stops.txt"
    counter = hook_script(tmp_path, "count", f"open({str(log)!r}, 'a').write('stop\\n')\n")
    scripted = ScriptedBrain(("", [call("explore", task="look")]), ("Nothing here.", []), ("All done.", []))
    agent.run_turn([{"role": "user", "content": "go"}], scripted, workspace,
                   permissions.Gate(workspace, permissions.refuse), Screen(),
                   extras=with_hooks(workspace, ("Stop", "", counter)))
    assert log.read_text().split() == ["stop"]


class StubHub:
    def __init__(self):
        self.calls = []

    def definitions(self):
        return [tools.function("mcp__notes__find", "Find notes", {"q": tools.TEXT}, []),
                tools.function("mcp__notes__delete", "Delete a note", {"id": tools.TEXT}, [])]

    def tool(self, name):
        known = {"mcp__notes__find": True, "mcp__notes__delete": False}
        return type("Tool", (), {"read_only": known[name], "server": "notes"})() if name in known else None

    def run(self, name, arguments):
        self.calls.append((name, arguments))
        return f"{name} ran"


def test_extra_tools_are_offered_and_mcp_calls_go_through_the_gate(tmp_path):
    workspace = tools.Workspace(tmp_path)
    hub = StubHub()
    scripted = ScriptedBrain(("", [call("mcp__notes__find", q="x"), call("mcp__notes__delete", "c2", id="1")]),
                             ("OK.", []))
    asked = []
    agent.run_turn([{"role": "user", "content": "go"}], scripted, workspace,
                   permissions.Gate(workspace, lambda q, p="": asked.append(q) or "n"), Screen(),
                   extras=extras.Extras(hub=hub))
    offered = {definition["function"]["name"] for definition in scripted.sent[0]["tools"]}
    assert {"mcp__notes__find", "mcp__notes__delete", "read_file"} <= offered
    results = [m["content"] for m in scripted.sent[1]["messages"] if m["role"] == "tool"]
    assert results == [agent.REFUSED, agent.REFUSED] and len(asked) == 2
    assert hub.calls == []


def test_read_only_mcp_tools_work_in_plan_mode_and_others_are_refused(tmp_path):
    workspace = tools.Workspace(tmp_path)
    hub = StubHub()
    scripted = ScriptedBrain(("", [call("mcp__notes__find", q="x"), call("mcp__notes__delete", "c2", id="1")]),
                             ("OK.", []))
    agent.run_turn([{"role": "user", "content": "go"}], scripted, workspace,
                   permissions.Gate(workspace, lambda q, p="": "y", mode=permissions.PLAN), Screen(),
                   extras=extras.Extras(hub=hub))
    results = [m["content"] for m in scripted.sent[1]["messages"] if m["role"] == "tool"]
    assert results == ["mcp__notes__find ran", permissions.PLAN_REFUSAL]


def test_the_skill_tool_loads_a_skill_without_asking(tmp_path):
    workspace = tools.Workspace(tmp_path)
    folder = tmp_path / "personal" / skills.SKILLS_FOLDER / "deploy"
    folder.mkdir(parents=True)
    (folder / skills.SKILL_FILE).write_text("---\ndescription: deploying\n---\nRun make deploy.\n")
    found = skills.find(tmp_path / "personal", workspace, False)
    skill_call = {"id": "s1", "type": "function", "function": {"name": "skill", "arguments": json.dumps({"name": "deploy"})}}
    scripted = ScriptedBrain(("", [skill_call]), ("OK.", []))
    agent.run_turn([{"role": "user", "content": "deploy"}], scripted, workspace,
                   permissions.Gate(workspace, permissions.refuse), Screen(), extras=extras.Extras(skills=found))
    result = [m["content"] for m in scripted.sent[1]["messages"] if m["role"] == "tool"][0]
    assert result.startswith("Run make deploy.")


class HelperBrain:
    def __init__(self, helpers):
        self.barrier = threading.Barrier(helpers, timeout=5)
        self.main_replies = [("", [call("explore", "e1", task="find A"), call("explore", "e2", task="find B")]),
                             ("Both found.", [])]
        self.sent = []

    def reply(self, messages, tools, on_text):
        if messages[0].get("content") == agent.EXPLORER_PROMPT:
            self.barrier.wait()
            return {"role": "assistant", "content": f"answer to {messages[-1]['content']}", "tool_calls": []}
        self.sent.append([dict(m) for m in messages])
        content, calls = self.main_replies.pop(0)
        return {"role": "assistant", "content": content, "tool_calls": calls}


def test_several_explore_helpers_run_at_the_same_time_and_report_in_order(tmp_path):
    workspace = tools.Workspace(tmp_path)
    brain = HelperBrain(helpers=2)
    answer = agent.run_turn([{"role": "user", "content": "go"}], brain, workspace,
                            permissions.Gate(workspace, permissions.refuse), Screen())
    assert answer == "Both found."
    results = [(m["tool_call_id"], m["content"]) for m in brain.sent[1] if m["role"] == "tool"]
    assert results == [("e1", "answer to find A"), ("e2", "answer to find B")]


class OutsideReadingHelperBrain:
    def __init__(self, outside):
        self.outside = outside
        self.main_replies = [("", [call("explore", "e1", task="read it"), call("explore", "e2", task="look")])]

    def reply(self, messages, tools, on_text):
        if messages[0].get("content") == agent.EXPLORER_PROMPT:
            if messages[1]["content"] == "read it" and len(messages) == 2:
                return {"role": "assistant", "content": "", "tool_calls": [call("read_file", "r1", path=str(self.outside))]}
            return {"role": "assistant", "content": "done", "tool_calls": []}
        content, calls = self.main_replies.pop(0)
        return {"role": "assistant", "content": content, "tool_calls": calls}


def test_ctrl_c_while_a_parallel_helper_asks_leaves_no_question_behind(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret\n")
    workspace = tools.Workspace(project)
    reads, asking_threads, typed_later = [], [], threading.Event()
    monkeypatch.setitem(tools.HANDLERS, "read_file", lambda workspace, **arguments: reads.append(arguments) or "read")

    def ask(question, preview=""):
        asking_threads.append(threading.current_thread())
        if threading.current_thread() is threading.main_thread():
            raise KeyboardInterrupt
        _thread.interrupt_main()
        typed_later.wait(5)
        return permissions.YES

    running_before = set(threading.enumerate())
    with pytest.raises(KeyboardInterrupt):
        agent.run_turn([{"role": "user", "content": "go"}], OutsideReadingHelperBrain(outside), workspace,
                       permissions.Gate(workspace, ask), Screen())
    typed_later.set()
    for thread in set(threading.enumerate()) - running_before:
        thread.join(5)
    assert reads == []
    assert asking_threads == [threading.main_thread()]


def test_a_parallel_helpers_question_is_asked_on_the_main_thread_and_a_yes_lets_it_run(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    workspace = tools.Workspace(project)
    reads, asking_threads = [], []
    monkeypatch.setitem(tools.HANDLERS, "read_file", lambda workspace, **arguments: reads.append(arguments) or "read")
    gate = permissions.Gate(workspace, lambda q, p="": asking_threads.append(threading.current_thread()) or "y")
    brain = OutsideReadingHelperBrain(tmp_path / "secret.txt")
    brain.main_replies.append(("Read.", []))
    assert agent.run_turn([{"role": "user", "content": "go"}], brain, workspace, gate, Screen()) == "Read."
    assert asking_threads == [threading.main_thread()] and len(reads) == 1


class FloodingHub(StubHub):
    def run(self, name, arguments):
        self.calls.append((name, arguments))
        return "x" * (agent.MAX_RESULT_CHARS * 3)


def test_a_huge_tool_result_is_cut_before_it_reaches_the_model(tmp_path):
    workspace = tools.Workspace(tmp_path)
    scripted = ScriptedBrain(("", [call("mcp__notes__find", q="x")]), ("OK.", []))
    agent.run_turn([{"role": "user", "content": "go"}], scripted, workspace,
                   permissions.Gate(workspace, lambda q, p="": "y"), Screen(), extras=extras.Extras(hub=FloodingHub()))
    result = [m["content"] for m in scripted.sent[1]["messages"] if m["role"] == "tool"][0]
    assert len(result) <= agent.MAX_RESULT_CHARS + 200 and "characters cut" in result


def test_mcp_calls_that_ran_are_recorded_as_done(tmp_path):
    workspace = tools.Workspace(tmp_path)
    done = []
    scripted = ScriptedBrain(("", [call("mcp__notes__delete", id="1")]), ("OK.", []))
    agent.run_turn([{"role": "user", "content": "go"}], scripted, workspace,
                   permissions.Gate(workspace, lambda q, p="": "y"), Screen(), done=done,
                   extras=extras.Extras(hub=StubHub()))
    assert done == ["mcp__notes__delete"]


def test_a_stopped_helper_asks_nothing_and_runs_no_more_tools(tmp_path):
    workspace = tools.Workspace(tmp_path)
    asked = []
    gate = permissions.Gate(workspace, lambda q, p="": asked.append(q) or "y")
    helpers = agent.Helpers(ScriptedBrain(), workspace, gate, Screen(), extras.NONE)
    helpers.stopped.set()
    assert helpers.ask("Run: rm -rf x?") == permissions.NO and asked == []


class StoppingBrain(ScriptedBrain):
    def __init__(self, stopped, *replies):
        super().__init__(*replies)
        self.stopped = stopped

    def reply(self, messages, tools, on_text):
        found = super().reply(messages, tools, on_text)
        self.stopped.set()
        return found


def test_a_helper_stopped_while_the_model_answers_runs_none_of_its_calls(tmp_path):
    workspace = tools.Workspace(tmp_path)
    stopped = threading.Event()
    ran = []
    gate = permissions.Gate(workspace, lambda q, p="": ran.append(q) or "y")
    brain = StoppingBrain(stopped, ("", [call("run_command", command=MAKE_A_FILE)]))
    answer = agent.run_turn([{"role": "user", "content": "go"}], brain, workspace, gate, Screen(), stopped=stopped)
    assert answer == agent.HELPER_STOPPED
    assert not (tmp_path / "made").exists() and ran == []


def test_helpers_run_on_daemon_threads(tmp_path):
    workspace = tools.Workspace(tmp_path)
    kinds = []

    class Recording(HelperBrain):
        def reply(self, messages, tools, on_text):
            if messages[0].get("content") == agent.EXPLORER_PROMPT:
                kinds.append(threading.current_thread().daemon)
            return super().reply(messages, tools, on_text)

    agent.run_turn([{"role": "user", "content": "find both"}], Recording(2), workspace,
                   permissions.Gate(workspace, permissions.refuse), Screen())
    assert kinds == [True, True]


def decision_hook(tmp_path, decision):
    body = ("import json\nprint(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', "
            f"'permissionDecision': {decision!r}, 'permissionDecisionReason': 'hook rule', "
            "'additionalContext': 'note from the hook'}}))\n")
    return hook_script(tmp_path, decision, body)


def test_a_hook_allow_skips_the_question_but_not_plan_mode(tmp_path):
    workspace = tools.Workspace(tmp_path)
    asked = []
    extra = with_hooks(workspace, ("PreToolUse", "", decision_hook(tmp_path, "allow")))
    scripted = ScriptedBrain(("", [call("run_command", command=MAKE_A_FILE)]), ("OK.", []))
    agent.run_turn([{"role": "user", "content": "go"}], scripted, workspace,
                   permissions.Gate(workspace, lambda q, p="": asked.append(q) or "n"), Screen(), extras=extra)
    result = [m["content"] for m in scripted.sent[1]["messages"] if m["role"] == "tool"][0]
    assert asked == [] and (tmp_path / "made").exists() and "note from the hook" in result
    (tmp_path / "made").unlink()
    scripted = ScriptedBrain(("", [call("run_command", command=MAKE_A_FILE)]), ("OK.", []))
    agent.run_turn([{"role": "user", "content": "go"}], scripted, workspace,
                   permissions.Gate(workspace, lambda q, p="": "y", mode=permissions.PLAN), Screen(), extras=extra)
    assert not (tmp_path / "made").exists()


def test_a_hook_ask_asks_even_for_a_look_only_call(tmp_path):
    (tmp_path / "a.txt").write_text("a\n")
    workspace = tools.Workspace(tmp_path)
    asked = []
    extra = with_hooks(workspace, ("PreToolUse", "", decision_hook(tmp_path, "ask")))
    scripted = ScriptedBrain(("", [call("read_file", path="a.txt")]), ("OK.", []))
    agent.run_turn([{"role": "user", "content": "go"}], scripted, workspace,
                   permissions.Gate(workspace, lambda q, p="": asked.append(q) or "n"), Screen(), extras=extra)
    assert len(asked) == 1 and "hook rule" in asked[0]


def test_a_hook_deny_refuses_with_its_reason(tmp_path):
    workspace = tools.Workspace(tmp_path)
    extra = with_hooks(workspace, ("PreToolUse", "", decision_hook(tmp_path, "deny")))
    scripted = ScriptedBrain(("", [call("run_command", command=MAKE_A_FILE)]), ("OK.", []))
    agent.run_turn([{"role": "user", "content": "go"}], scripted, workspace,
                   permissions.Gate(workspace, lambda q, p="": "y"), Screen(), extras=extra)
    result = [m["content"] for m in scripted.sent[1]["messages"] if m["role"] == "tool"][0]
    assert "hook rule" in result and not (tmp_path / "made").exists()


def test_a_change_that_cannot_work_is_reported_without_asking_first(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    workspace = tools.Workspace(tmp_path)
    asked = []
    scripted = ScriptedBrain(("", [call("edit_file", path="a.py", old_text="x = 1", new_text="x = 2")]), ("OK.", []))
    agent.run_turn([{"role": "user", "content": "go"}], scripted, workspace,
                   permissions.Gate(workspace, lambda q, p="": asked.append(q) or "y"), Screen())
    result = [m["content"] for m in scripted.sent[1]["messages"] if m["role"] == "tool"][0]
    assert asked == [] and "read_file" in result


def test_the_first_call_to_an_mcp_server_asks_once_even_for_a_read_only_tool(tmp_path):
    workspace = tools.Workspace(tmp_path)
    hub = StubHub()
    asked = []
    scripted = ScriptedBrain(("", [call("mcp__notes__find", q="x"), call("mcp__notes__find", "c2", q="y")]),
                             ("OK.", []))
    agent.run_turn([{"role": "user", "content": "go"}], scripted, workspace,
                   permissions.Gate(workspace, lambda q, p="": asked.append(q) or "y"), Screen(),
                   extras=extras.Extras(hub=hub))
    assert len(asked) == 1 and "notes" in asked[0]
    assert [name for name, _ in hub.calls] == ["mcp__notes__find", "mcp__notes__find"]


def test_refusing_an_mcp_server_s_first_use_runs_nothing(tmp_path):
    workspace = tools.Workspace(tmp_path)
    hub = StubHub()
    scripted = ScriptedBrain(("", [call("mcp__notes__find", q="x")]), ("OK.", []))
    agent.run_turn([{"role": "user", "content": "go"}], scripted, workspace,
                   permissions.Gate(workspace, lambda q, p="": "n"), Screen(), extras=extras.Extras(hub=hub))
    assert hub.calls == []


def test_a_reply_that_only_announces_its_next_step_is_sent_back_once_to_do_it(tmp_path):
    (tmp_path / "ops.py").write_text("x = 1\n")
    workspace = tools.Workspace(tmp_path)
    scripted = ScriptedBrain(
        ("Let's check the file to see how it's defined.", []),
        ("", [call("read_file", path="ops.py")]),
        ("ops.py sets x to 1.", []),
    )
    messages = [{"role": "user", "content": "what does ops.py set?"}]
    answer = agent.run_turn(messages, scripted, workspace, permissions.Gate(workspace, lambda q, p="": "y"), Screen())
    assert answer == "ops.py sets x to 1."
    nudge = scripted.sent[1]["messages"][-1]
    assert nudge["role"] == "user" and agent.DO_IT_NOW in nudge["content"]


def test_an_announced_step_is_only_nudged_once_per_request(tmp_path):
    workspace = tools.Workspace(tmp_path)
    scripted = ScriptedBrain(("I'll run the tests next.", []), ("Let me run the tests now.", []))
    answer = agent.run_turn([{"role": "user", "content": "test it"}], scripted, workspace,
                            permissions.Gate(workspace, lambda q, p="": "y"), Screen())
    assert answer == "Let me run the tests now."
    assert len(scripted.sent) == 2


@pytest.mark.parametrize("final", [
    "Fixed: subtract now subtracts, and all 3 tests pass.",
    "Should I run the full test suite too?",
    "The file defines add, subtract and multiply.",
])
def test_a_finished_answer_or_a_question_is_not_nudged(tmp_path, final):
    workspace = tools.Workspace(tmp_path)
    scripted = ScriptedBrain((final, []))
    assert agent.run_turn([{"role": "user", "content": "go"}], scripted, workspace,
                          permissions.Gate(workspace, lambda q, p="": "y"), Screen()) == final
    assert len(scripted.sent) == 1


def test_web_search_is_offered_only_with_a_brain_that_searches_and_its_results_are_marked(tmp_path):
    workspace = tools.Workspace(tmp_path)
    searched = []

    def search(query, results):
        searched.append((query, results))
        return "1. A result"

    scripted = ScriptedBrain(("", [call("web_search", query="python json")]), ("Found it.", []))
    with_search = extras.Extras(search=search)
    agent.run_turn([{"role": "user", "content": "look it up"}], scripted, workspace,
                   permissions.Gate(workspace, lambda q, p="": "y"), Screen(), extras=with_search)
    assert "web_search" in [d["function"]["name"] for d in scripted.sent[0]["tools"]]
    assert "web_search" not in [d["function"]["name"] for d in agent.offered_definitions(extras.NONE)]
    assert searched == [("python json", extras.SEARCH_RESULTS)]
    result = scripted.sent[-1]["messages"][-1]["content"]
    assert "1. A result" in result and "not instructions" in result
    assert with_search.without_stop_hooks().handles("web_search")


def test_a_failed_search_is_an_error_result(tmp_path):
    workspace = tools.Workspace(tmp_path)

    def search(query, results):
        raise brain.BrainError("This Arty brain has no web search")

    scripted = ScriptedBrain(("", [call("web_search", query="x")]), ("OK.", []))
    agent.run_turn([{"role": "user", "content": "go"}], scripted, workspace,
                   permissions.Gate(workspace, lambda q, p="": "y"), Screen(), extras=extras.Extras(search=search))
    assert scripted.sent[-1]["messages"][-1]["content"] == "Error: This Arty brain has no web search"


class OneHelperBrain:
    def __init__(self):
        self.helper_turns = []
        self.main_replies = [("", [call("explore", "e1", task="find A")]), ("Done.", [])]
        self.sent = []

    def reply(self, messages, tools, on_text):
        if messages[0].get("content") == agent.EXPLORER_PROMPT:
            self.helper_turns.append([dict(m) for m in messages])
            return {"role": "assistant", "content": f"answer {len(self.helper_turns)}", "tool_calls": []}
        self.sent.append([dict(m) for m in messages])
        content, calls = self.main_replies.pop(0)
        return {"role": "assistant", "content": content, "tool_calls": calls}


def test_a_subagent_stop_hook_sees_the_helper_finish_and_can_send_it_back(tmp_path):
    workspace = tools.Workspace(tmp_path)
    seen = tmp_path / "seen.jsonl"
    body = (f"import json, sys\npayload = json.load(sys.stdin)\nopen({str(seen)!r}, 'a').write(json.dumps(payload) + '\\n')\n"
            "if not payload['stop_hook_active']:\n    print('check the tests folder too', file=sys.stderr)\n    sys.exit(2)\n")
    brain = OneHelperBrain()
    agent.run_turn([{"role": "user", "content": "go"}], brain, workspace, permissions.Gate(workspace, permissions.refuse),
                   Screen(), extras=with_hooks(workspace, ("SubagentStop", "", hook_script(tmp_path, "sub", body))))
    payloads = [json.loads(line) for line in seen.read_text().splitlines()]
    assert [p["stop_hook_active"] for p in payloads] == [False, True]
    assert payloads[0]["hook_event_name"] == "SubagentStop" and payloads[0]["agent_type"] == "explore"
    assert payloads[0]["task"] == "find A" and payloads[0]["last_message"] == "answer 1"
    assert "check the tests folder too" in brain.helper_turns[1][-1]["content"]
    assert [m["content"] for m in brain.sent[1] if m["role"] == "tool"] == ["answer 2"]


def test_in_plan_mode_the_model_presents_a_plan_and_carries_on_once_it_is_approved(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    workspace = tools.Workspace(tmp_path)
    scripted = ScriptedBrain(
        ("", [call("read_file", path="a.py")]),
        ("", [call("present_plan", "p1", plan="1. Change x to 2")]),
        ("", [call("edit_file", "e1", path="a.py", old_text="x = 1", new_text="x = 2")]),
        ("Done.", []),
    )
    gate = permissions.Gate(workspace, lambda q, p="": "y", mode=permissions.PLAN)
    gate.review = lambda plan: (permissions.YES, "")
    agent.run_turn([{"role": "user", "content": "plan it"}], scripted, workspace, gate, Screen())
    assert "present_plan" in [d["function"]["name"] for d in scripted.sent[0]["tools"]]
    assert gate.mode == permissions.DEFAULT
    assert (tmp_path / "a.py").read_text() == "x = 2\n"


def test_the_plan_tool_is_only_offered_in_plan_mode(tmp_path):
    workspace = tools.Workspace(tmp_path)
    scripted = ScriptedBrain(("OK.", []))
    agent.run_turn([{"role": "user", "content": "hi"}], scripted, workspace,
                   permissions.Gate(workspace, permissions.refuse), Screen())
    assert "present_plan" not in [d["function"]["name"] for d in scripted.sent[0]["tools"]]


class CustomHelperBrain:
    def __init__(self, main_replies, helper_replies):
        self.main_replies, self.helper_replies = list(main_replies), list(helper_replies)
        self.main, self.helper = [], []

    def reply(self, messages, tools, on_text):
        if "You review changes." in messages[0]["content"]:
            self.helper.append({"messages": [dict(m) for m in messages], "tools": tools})
            content, calls = self.helper_replies.pop(0)
        else:
            self.main.append({"messages": [dict(m) for m in messages], "tools": tools})
            content, calls = self.main_replies.pop(0)
        return {"role": "assistant", "content": content, "tool_calls": calls}


def helper_call(helper_name, task):
    return {"id": "h1", "type": "function",
            "function": {"name": "helper", "arguments": json.dumps({"name": helper_name, "task": task})}}


def reviewer(*allowed):
    return helpers.Helper("reviewer", "Reviews a diff for bugs", "You review changes.", tuple(allowed), "yours")


def test_a_custom_helper_runs_with_its_own_prompt_and_only_its_tools(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    workspace = tools.Workspace(tmp_path)
    scripted = CustomHelperBrain(
        [("", [helper_call("reviewer", "review a.py")]), ("Reviewed.", [])],
        [("", [call("read_file", path="a.py")]), ("No bugs in a.py.", [])])
    agent.run_turn([{"role": "user", "content": "review"}], scripted, workspace,
                   permissions.Gate(workspace, permissions.refuse), Screen(),
                   extras=extras.Extras(helpers={"reviewer": reviewer("read_file", "search")}))
    offered = {d["function"]["name"]: d for d in scripted.main[0]["tools"]}
    assert "reviewer" in offered["helper"]["function"]["description"]
    assert offered["helper"]["function"]["parameters"]["properties"]["name"]["enum"] == ["reviewer"]
    assert sorted(d["function"]["name"] for d in scripted.helper[0]["tools"]) == ["read_file", "search"]
    assert scripted.helper[0]["messages"][1]["content"] == "review a.py"
    assert [m["content"] for m in scripted.main[1]["messages"] if m["role"] == "tool"] == ["No bugs in a.py."]


def test_a_helper_allowed_to_edit_asks_like_the_main_agent_and_a_looking_one_cannot_edit(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    workspace = tools.Workspace(tmp_path)
    edit = [("", [call("read_file", path="a.py")]),
            ("", [call("edit_file", "e1", path="a.py", old_text="x = 1", new_text="x = 2")]), ("Edited.", [])]
    for allowed, expected in ((("read_file", "edit_file"), "x = 2\n"), (("read_file",), "x = 1\n")):
        (tmp_path / "a.py").write_text("x = 1\n")
        asked = []
        scripted = CustomHelperBrain([("", [helper_call("reviewer", "fix a.py")]), ("OK.", [])],
                                     list(edit))
        agent.run_turn([{"role": "user", "content": "go"}], scripted, workspace,
                       permissions.Gate(workspace, lambda q, p="": asked.append(q) or "y"), Screen(),
                       extras=extras.Extras(helpers={"reviewer": reviewer(*allowed)}))
        assert (tmp_path / "a.py").read_text() == expected


def test_an_unknown_helper_name_is_an_error_and_no_helpers_means_no_tool(tmp_path):
    workspace = tools.Workspace(tmp_path)
    scripted = CustomHelperBrain([("", [helper_call("nobody", "x")]), ("OK.", [])], [])
    agent.run_turn([{"role": "user", "content": "go"}], scripted, workspace,
                   permissions.Gate(workspace, permissions.refuse), Screen(),
                   extras=extras.Extras(helpers={"reviewer": reviewer("read_file")}))
    result = [m["content"] for m in scripted.main[1]["messages"] if m["role"] == "tool"][0]
    assert result.startswith("Error") and "reviewer" in result
    assert "helper" not in [d["function"]["name"] for d in agent.offered_definitions(extras.NONE)]


def test_a_model_that_claims_the_work_after_approval_without_changing_anything_is_sent_back_once(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    workspace = tools.Workspace(tmp_path)
    scripted = ScriptedBrain(
        ("", [call("read_file", path="a.py")]),
        ("", [call("edit_file", "e0", path="a.py", old_text="x = 1", new_text="x = 2")]),
        ("", [call("present_plan", "p1", plan="1. Change x to 2")]),
        ("I changed a.py: x is now 2.", []),
        ("", [call("edit_file", "e1", path="a.py", old_text="x = 1", new_text="x = 2")]),
        ("Changed x to 2.", []),
    )
    gate = permissions.Gate(workspace, lambda q, p="": "y", mode=permissions.PLAN)
    gate.review = lambda plan: (permissions.YES, "")
    messages = [{"role": "user", "content": "plan it"}]
    answer = agent.run_turn(messages, scripted, workspace, gate, Screen())
    assert (tmp_path / "a.py").read_text() == "x = 2\n"
    assert answer == "Changed x to 2."
    approval = [m["content"] for m in messages if m["role"] == "tool"][2]
    assert "Nothing has been changed yet" in approval
    assert sum(m["content"] == agent.NOTHING_CHANGED_YET for m in messages if m["role"] == "user") == 1


class WaitingScreen(Screen):
    def __init__(self):
        super().__init__()
        self.waiting = False
        self.events = []

    def thinking(self):
        screen = self

        class Context:
            def __enter__(self):
                screen.waiting = True
                screen.events.append("start")

            def __exit__(self, *details):
                screen.waiting = False
                screen.events.append("stop")

        return Context()


class WatchedBrain(ScriptedBrain):
    def __init__(self, screen, *replies):
        super().__init__(*replies)
        self.screen = screen
        self.waited = []

    def reply(self, messages, tools, on_text):
        self.waited.append(self.screen.waiting)
        return super().reply(messages, tools, on_text)


def test_every_wait_for_the_model_is_shown_as_thinking_and_ends_with_the_reply(tmp_path):
    (tmp_path / "a.txt").write_text("a\n")
    workspace = tools.Workspace(tmp_path)
    screen = WaitingScreen()
    watched = WatchedBrain(screen, ("", [call("read_file", path="a.txt")]), ("Done.", []))
    agent.run_turn([{"role": "user", "content": "look"}], watched, workspace,
                   permissions.Gate(workspace, permissions.refuse), screen)
    assert watched.waited == [True, True]
    assert screen.events == ["start", "stop", "start", "stop"]


def test_a_screen_without_a_thinking_line_still_works(tmp_path):
    workspace = tools.Workspace(tmp_path)
    answer = agent.run_turn([{"role": "user", "content": "hi"}], ScriptedBrain(("Hello.", [])), workspace,
                            permissions.Gate(workspace, permissions.refuse), Screen())
    assert answer == "Hello."


@pytest.mark.parametrize("name, arguments, shown", [
    ("read_file", {"path": "greet.py"}, "Read greet.py"),
    ("edit_file", {"path": "greet.py", "old_text": "a", "new_text": "b"}, "Edit greet.py"),
    ("write_file", {"path": "new.py", "content": ""}, "Write new.py"),
    ("run_command", {"command": "pytest -q"}, "Run pytest -q"),
    ("search", {"pattern": "def greet", "path": "src"}, "Search for def greet in src"),
    ("list_files", {"pattern": "**/*.py"}, "List **/*.py"),
    ("web_fetch", {"url": "https://docs.python.org/3/"}, "Fetch https://docs.python.org/3/"),
    ("web_search", {"query": "pytest fixtures"}, "Search the web for pytest fixtures"),
    ("todo_write", {"items": []}, "Update the checklist"),
    ("explore", {"task": "find the config loader"}, "Explore: find the config loader"),
    ("mcp__notes__find", {"query": "deploy"}, "notes: find deploy"),
    ("something_new", {}, "something_new"),
])
def test_a_step_is_named_in_plain_words(name, arguments, shown):
    assert agent.step_line(name, arguments) == shown


def test_a_long_or_several_line_command_is_shortened_to_one_line():
    shown = agent.step_line("run_command", {"command": "python - <<'EOF'\n" + "x" * 300 + "\nEOF"})
    assert "\n" not in shown and len(shown) <= agent.MAX_STEP_CHARS + len("Run ") + 1


@pytest.mark.parametrize("name, result, shown, is_problem", [
    ("read_file", "1\tdef greet(name):\n2\t    return name", "2 lines", False),
    ("read_file", "1\ta\n2\tb\n... 38 more lines; read on with offset=3", "2 lines (38 more below)", False),
    ("run_command", "....\n1 passed in 0.01s\n[exit code 0]", "1 passed in 0.01s (exit code 0)", False),
    ("run_command", "E   AssertionError\n1 failed\n[exit code 1]", "1 failed (exit code 1)", True),
    ("run_command", "[exit code 0]", "no output (exit code 0)", False),
    ("run_command", "....\n" + "=" * 40 + " 1 passed in 0.01s " + "=" * 40 + "\n[exit code 0]",
     "1 passed in 0.01s (exit code 0)", False),
    ("run_command", "-" * 30 + "\n[exit code 0]", "no output (exit code 0)", False),
    ("search", "a.py:1:def greet\nb.py:4:greet()", "2 matches", False),
    ("search", "No matches for greet", "No matches for greet", False),
    ("list_files", "a.py\nb.py\nc.py", "3 files", False),
    ("list_files", "a.py\nb.py\n... and 30 more", "32 files", False),
    ("list_files", "a.py", "1 file", False),
    ("edit_file", "Edited greet.py\nlint: fine", "Edited greet.py", False),
    ("edit_file", "Error: old_text wasn't found in greet.py\nmore detail", "Error: old_text wasn't found in greet.py",
     True),
    ("edit_file", permissions.REFUSED, "Not done: you said no", False),
    ("run_command", permissions.REFUSED_LEAD + " They said what to do instead: x.", "Not done: you said no", False),
])
def test_a_result_is_summed_up_in_one_short_line(name, result, shown, is_problem):
    assert agent.outcome_line(name, result) == (shown, is_problem)


class StepScreen(Screen):
    def __init__(self):
        super().__init__()
        self.shown = []

    def show_step(self, line):
        self.shown.append(("step", line))

    def show_outcome(self, line, is_problem):
        self.shown.append(("outcome", line, is_problem))


def test_each_call_shows_a_step_then_its_outcome(tmp_path):
    (tmp_path / "a.txt").write_text("one\ntwo\n")
    workspace = tools.Workspace(tmp_path)
    screen = StepScreen()
    scripted = ScriptedBrain(("", [call("read_file", path="a.txt"), call("search", "c2", pattern="zzz")]),
                             ("Done.", []))
    agent.run_turn([{"role": "user", "content": "look"}], scripted, workspace,
                   permissions.Gate(workspace, permissions.refuse), screen)
    assert screen.shown == [("step", "Read a.txt"), ("outcome", "2 lines", False),
                            ("step", "Search for zzz"), ("outcome", "No matches for zzz", False)]
    assert screen.activity == []


def test_a_command_result_line_stays_short_enough_for_one_row():
    shown, _failed = agent.outcome_line("run_command", "x" * 300 + "\n[exit code 0]")
    assert len(f"  └ {shown}") <= 90 and shown.endswith("(exit code 0)")


class ChangingFileBrain(ScriptedBrain):
    def __init__(self, path, *replies):
        super().__init__(*replies)
        self.path = path

    def reply(self, messages, tools, on_text):
        self.path.write_text(f"version {len(self.sent)}\n")
        return super().reply(messages, tools, on_text)


def test_the_same_call_with_a_new_result_each_time_is_not_taken_for_being_stuck(tmp_path):
    workspace = tools.Workspace(tmp_path)
    same = ("", [call("read_file", path="a.txt")])
    scripted = ChangingFileBrain(tmp_path / "a.txt", *([same] * (agent.STUCK_REPEATS + 2)), ("It kept changing.", []))
    answer = agent.run_turn([{"role": "user", "content": "watch a.txt"}], scripted, workspace,
                            permissions.Gate(workspace, permissions.refuse), Screen())
    assert answer == "It kept changing."
    assert all(m["content"] != agent.STUCK_NOTE for sent in scripted.sent for m in sent["messages"])


class LongBuild:
    def __init__(self, polls_before_done):
        self.polls_left = polls_before_done

    def wait_for_end(self, seconds):
        self.polls_left -= 1

    def unread(self):
        return "built\n" if self.polls_left <= 0 else ""

    def state(self):
        return tools.STILL_RUNNING if self.polls_left > 0 else "finished with exit code 0"


def test_waiting_on_a_long_background_build_is_not_taken_for_being_stuck(tmp_path):
    workspace = tools.Workspace(tmp_path)
    polls = agent.STUCK_REPEATS * 3
    workspace.jobs["1"] = LongBuild(polls)
    poll = ("", [call("command_output", job="1")])
    scripted = ScriptedBrain(*([poll] * polls), ("The build finished.", []))
    answer = agent.run_turn([{"role": "user", "content": "build it"}], scripted, workspace,
                            permissions.Gate(workspace, permissions.refuse), Screen())
    assert answer == "The build finished."
    assert all(m["content"] != agent.STUCK_NOTE for sent in scripted.sent for m in sent["messages"])


@pytest.mark.parametrize("question", [
    "I've made index.html. Shall I continue with the styles?",
    "The server part is written. Would you like me to proceed with the page?",
    "Step 2 of 4 is done. Let me know if you'd like me to keep going.",
])
def test_asking_whether_to_carry_on_is_answered_yes(tmp_path, question):
    workspace = tools.Workspace(tmp_path)
    scripted = ScriptedBrain((question, []), ("", [call("list_files")]), ("All four steps are done.", []))
    answer = agent.run_turn([{"role": "user", "content": "make me a calculator"}], scripted, workspace,
                            permissions.Gate(workspace, permissions.refuse), Screen())
    assert answer == "All four steps are done."
    assert scripted.sent[1]["messages"][-1] == {"role": "user", "content": agent.CARRY_ON_ANSWER}


@pytest.mark.parametrize("question", [
    "Would you like me to build it for you?",
    "Which port should the server use?",
])
def test_a_real_question_for_the_user_is_left_for_them(tmp_path, question):
    workspace = tools.Workspace(tmp_path)
    scripted = ScriptedBrain((question, []))
    assert agent.run_turn([{"role": "user", "content": "how would I make a calculator?"}], scripted, workspace,
                          permissions.Gate(workspace, permissions.refuse), Screen()) == question
    assert len(scripted.sent) == 1


def checklist(*pairs):
    return [{"text": text, "status": status} for text, status in pairs]


def test_an_unfinished_checklist_sends_the_model_back_to_it(tmp_path):
    workspace = tools.Workspace(tmp_path)
    scripted = ScriptedBrain(
        ("", [call("todo_write", items=checklist(("Write app.py", "done"), ("Try it with curl", "pending")))]),
        ("Here is your app.", []),
        ("", [call("todo_write", "c2", items=checklist(("Write app.py", "done"), ("Try it with curl", "done")))]),
        ("Built app.py and tried it with curl. Run it with: python app.py", []),
    )
    answer = agent.run_turn([{"role": "user", "content": "make me an app"}], scripted, workspace,
                            permissions.Gate(workspace, permissions.refuse), Screen())
    assert answer.startswith("Built app.py")
    nudge = scripted.sent[2]["messages"][-1]
    assert nudge["role"] == "user" and nudge["content"].startswith(agent.UNFINISHED_LEAD)
    assert "[ ] Try it with curl" in nudge["content"] and "Write app.py" not in nudge["content"]


def test_the_checklist_nudge_comes_once_until_the_model_works_again(tmp_path):
    workspace = tools.Workspace(tmp_path)
    workspace.todos = checklist(("Pick a port", "pending"))
    scripted = ScriptedBrain(("I need you to pick a port.", []), ("Please tell me which port to use.", []))
    answer = agent.run_turn([{"role": "user", "content": "serve it"}], scripted, workspace,
                            permissions.Gate(workspace, permissions.refuse), Screen())
    assert answer == "Please tell me which port to use."
    assert len(scripted.sent) == 2


def test_plan_mode_and_helpers_are_not_pushed_on_by_the_checklist(tmp_path):
    workspace = tools.Workspace(tmp_path)
    workspace.todos = checklist(("Write app.py", "pending"))
    planning = ScriptedBrain(("Here is the plan.", []))
    agent.run_turn([{"role": "user", "content": "plan it"}], planning, workspace,
                   permissions.Gate(workspace, permissions.refuse, mode=permissions.PLAN), Screen())
    helping = ScriptedBrain(("Found it in a.py.", []))
    agent.run_turn([{"role": "user", "content": "find it"}], helping, workspace,
                   permissions.Gate(workspace, permissions.refuse), Screen(), can_explore=False)
    assert len(planning.sent) == 1 and len(helping.sent) == 1


def test_a_reply_cut_off_at_the_length_limit_is_sent_back_to_carry_on_in_parts(tmp_path):
    workspace = tools.Workspace(tmp_path)
    cut = "```python\n" + "x = 1\n" * 1000 + "\n\n" + agent.CUT_SHORT_NOTE
    scripted = ScriptedBrain((cut, []), ("", [call("list_files")]), ("Written in parts.", []))
    messages = [{"role": "user", "content": "write a big file"}]
    answer = agent.run_turn(messages, scripted, workspace, permissions.Gate(workspace, permissions.refuse), Screen())
    assert answer == "Written in parts."
    kept, nudge = scripted.sent[1]["messages"][-2:]
    assert kept["role"] == "assistant" and len(kept["content"]) <= agent.CUT_REPLY_KEPT_CHARS + 1
    assert nudge == {"role": "user", "content": agent.CUT_SHORT_ANSWER}


class ProgressScreen(Screen):
    def __init__(self):
        super().__init__()
        self.outcomes, self.checklists, self.labels, self.counted = [], [], [], []

    def show_outcome(self, line, is_problem):
        self.outcomes.append(line)

    def show_todos(self, items):
        self.checklists.append([dict(item) for item in items])

    def working(self, label):
        self.labels.append(label)
        return __import__("contextlib").nullcontext()

    def count_reply(self, reply):
        self.counted.append(reply.get("content"))


def test_the_checklist_is_drawn_after_each_update_with_how_much_is_done(tmp_path):
    workspace = tools.Workspace(tmp_path)
    items = checklist(("Write app.py", "done"), ("Try it", "in_progress"))
    scripted = ScriptedBrain(("", [call("todo_write", items=items)]), ("", [call("todo_write", "c2", items="nope")]),
                             ("Still trying it.", []), ("Done.", []))
    screen = ProgressScreen()
    agent.run_turn([{"role": "user", "content": "go"}], scripted, workspace,
                   permissions.Gate(workspace, permissions.refuse), screen)
    assert screen.outcomes[0] == "1 of 2 done"
    assert screen.checklists == [items]


def test_each_tool_run_shows_a_working_line_and_each_reply_is_counted(tmp_path):
    (tmp_path / "a.txt").write_text("a\n")
    workspace = tools.Workspace(tmp_path)
    scripted = ScriptedBrain(("", [call("read_file", path="a.txt")]), ("Read it.", []))
    screen = ProgressScreen()
    agent.run_turn([{"role": "user", "content": "read"}], scripted, workspace,
                   permissions.Gate(workspace, permissions.refuse), screen)
    assert screen.labels == ["Read a.txt"]
    assert screen.counted == ["", "Read it."]


def test_a_command_stopped_at_its_timeout_shows_as_a_problem_not_as_its_last_output():
    result = tools.timed_out_note(120, 1800) + " * Serving Flask app 'app'\n"
    assert agent.outcome_line("run_command", result) == ("stopped after 120 s while still running", True)


LAST_LIVE_REPLY = ("I'll test the application by attempting to download a video from YouTube.\n\nLet's try "
                   "downloading a sample video to ensure everything works correctly.\n\nI'll test the application by "
                   "attempting to download a video from YouTube.\n\nI did not try downloading a sample video or use a "
                   "YouTube URL for testing.\n\nI did not test the application or attempt to download a video from "
                   "YouTube.")


@pytest.mark.parametrize("reply, nudge", [
    ("The page is written. I'll test it with curl now.", agent.DO_IT_NOW),
    ("Next I will install the packages in a virtual environment.", agent.DO_IT_NOW),
    (LAST_LIVE_REPLY, agent.DO_IT_NOW),
    ("The server is written.\n\nI did not test the application.", agent.NOT_DONE_ANSWER),
])
def test_a_reply_that_says_what_it_will_do_or_has_not_done_is_sent_back_to_do_it(tmp_path, reply, nudge):
    workspace = tools.Workspace(tmp_path)
    scripted = ScriptedBrain((reply, []), ("", [call("list_files")]), ("Tested it: the page loads.", []))
    answer = agent.run_turn([{"role": "user", "content": "make me an app"}], scripted, workspace,
                            permissions.Gate(workspace, permissions.refuse), Screen())
    assert answer == "Tested it: the page loads."
    assert scripted.sent[1]["messages"][-1] == {"role": "user", "content": nudge}


def test_offering_more_with_let_me_know_is_not_taken_for_an_announced_step():
    assert not agent.announces_a_next_step("Built calc.py. Let me know if you'd like me to add a history feature.")
