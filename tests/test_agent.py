import json
import shlex
import sys
import threading

from arty_cli import agent, extras, hooks, permissions, skills, tools


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


def test_a_long_job_stops_after_the_step_limit(tmp_path):
    (tmp_path / "a.txt").write_text("a\n")
    workspace = tools.Workspace(tmp_path)
    replies = [("", [call("read_file", f"c{i}", path="a.txt", offset=i + 1)]) for i in range(agent.MAX_ROUNDS + 1)]
    answer = agent.run_turn([{"role": "user", "content": "go"}], ScriptedBrain(*replies), workspace,
                            permissions.Gate(workspace, permissions.refuse), Screen())
    assert answer == agent.STEP_LIMIT_REACHED


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
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(path))}"


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
    brain = StoppingBrain(stopped, ("", [call("run_command", command="touch made")]))
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
    scripted = ScriptedBrain(("", [call("run_command", command="touch made")]), ("OK.", []))
    agent.run_turn([{"role": "user", "content": "go"}], scripted, workspace,
                   permissions.Gate(workspace, lambda q, p="": asked.append(q) or "n"), Screen(), extras=extra)
    result = [m["content"] for m in scripted.sent[1]["messages"] if m["role"] == "tool"][0]
    assert asked == [] and (tmp_path / "made").exists() and "note from the hook" in result
    (tmp_path / "made").unlink()
    scripted = ScriptedBrain(("", [call("run_command", command="touch made")]), ("OK.", []))
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
    scripted = ScriptedBrain(("", [call("run_command", command="touch made")]), ("OK.", []))
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
