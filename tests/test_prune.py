import json

from arty_cli import prune


class Client:
    def context_tokens(self):
        return 24_576


def call(number, name, **arguments):
    return {"id": f"c{number}", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def round_of(number, name, result, **arguments):
    return [{"role": "assistant", "content": None, "tool_calls": [call(number, name, **arguments)]},
            {"role": "tool", "tool_call_id": f"c{number}", "content": result}]


def conversation(*rounds):
    messages = [{"role": "system", "content": "You are Arty."}, {"role": "user", "content": "fix it"}]
    for found in rounds:
        messages += found
    return messages


def tool_contents(messages):
    return [message["content"] for message in messages if message["role"] == "tool"]


def test_an_older_read_of_a_file_read_again_later_is_dropped():
    messages = conversation(round_of(1, "read_file", "1\told text " * 300, path="a.py"),
                            round_of(2, "read_file", "1\tnew text " * 300, path="a.py"),
                            round_of(3, "run_command", "ok\n[exit code 0]", command="pytest"))
    prune.stub_stale(messages)
    first, second, third = tool_contents(messages)
    assert first.startswith(prune.STUB_LEAD) and "a.py" in first
    assert second.startswith("1\tnew text") and third.startswith("ok")


def test_a_read_of_a_file_changed_later_is_dropped_and_only_the_newest_run_of_a_command_is_kept():
    messages = conversation(round_of(1, "read_file", "1\tx = 1", path="a.py"),
                            round_of(2, "run_command", "1 failed\n[exit code 1]", command="pytest -q"),
                            round_of(3, "edit_file", "Edited a.py", path="a.py", old_text="x = 1", new_text="x = 2"),
                            round_of(4, "run_command", "1 passed\n[exit code 0]", command="pytest -q"))
    prune.stub_stale(messages)
    read, first_run, edit, last_run = tool_contents(messages)
    assert read.startswith(prune.STUB_LEAD) and first_run.startswith(prune.STUB_LEAD)
    assert edit == "Edited a.py" and last_run.startswith("1 passed")


def test_long_arguments_of_past_writes_and_edits_are_shortened_but_calls_stay_matched():
    messages = conversation(round_of(1, "write_file", "Created a.py", path="a.py", content="x = 1\n" * 2_000),
                            round_of(2, "run_command", "ok\n[exit code 0]", command="python a.py"))
    prune.stub_stale(messages)
    arguments = json.loads(messages[2]["tool_calls"][0]["function"]["arguments"])
    assert arguments["path"] == "a.py" and len(arguments["content"]) < 400
    assert [message.get("tool_call_id") for message in messages if message["role"] == "tool"] == ["c1", "c2"]


def test_the_newest_round_is_never_touched():
    messages = conversation(round_of(1, "read_file", "1\told", path="a.py"),
                            round_of(2, "write_file", "Created a.py", path="a.py", content="x" * 5_000))
    prune.stub_stale(messages)
    assert json.loads(messages[-2]["tool_calls"][0]["function"]["arguments"])["content"] == "x" * 5_000


def test_pruning_stubs_the_oldest_results_until_the_conversation_is_under_the_target():
    rounds = [round_of(number, "search", f"hit {number} " * 2_000, pattern=f"p{number}") for number in range(1, 8)]
    messages = conversation(*rounds)
    limit = 8_000
    prune.prune(messages, limit, Client())
    assert prune.estimated(messages, Client()) <= limit * prune.TARGET_SHARE
    contents = tool_contents(messages)
    assert contents[0].startswith(prune.STUB_LEAD) and contents[-1].startswith("hit 7")


def test_paths_whose_reads_were_all_dropped_are_reported_as_out_of_view():
    messages = conversation(round_of(1, "read_file", "1\tx", path="a.py"),
                            round_of(2, "read_file", "1\ty", path="b.py"),
                            round_of(3, "read_file", "1\ty", path="b.py"))
    prune.stub_stale(messages)
    for message in messages[:4]:
        if message["role"] == "tool":
            message["content"] = prune.stub_for("read_file", {"path": "a.py"})
    assert prune.paths_out_of_view(messages) == {"a.py"}


def test_a_hidden_result_keeps_its_outcome():
    assert prune.stub_for("run_command", {"command": "pytest -q"}, "....F\n3 failed, 9 passed\n[exit code 1]") == (
        "[Hidden to save room: run_command pytest -q (3 failed, 9 passed, exit code 1). Do it again if you need it.]")
    assert prune.stub_for("read_file", {"path": "calc.py"}, "1\ta\n2\tb\n3\tc") == (
        "[Hidden to save room: read_file calc.py (3 lines). Do it again if you need it.]")


def test_the_newest_rounds_are_kept_whole_when_hiding_older_ones_is_enough():
    rounds = [round_of(number, "search", f"hit {number} " * 600, pattern=f"p{number}") for number in range(1, 10)]
    messages = conversation(*rounds)
    limit = prune.estimated(messages, Client()) - 100
    prune.prune(messages, limit, Client())
    contents = tool_contents(messages)
    assert all(not text.startswith(prune.STUB_LEAD) for text in contents[-prune.KEPT_ROUNDS:])
    assert contents[0].startswith(prune.STUB_LEAD)


def test_the_newest_result_for_a_file_being_edited_is_never_hidden():
    miss = "Error: old_text was not found. The closest lines in the file are these:\n1\tx = 1"
    rounds = [round_of(1, "read_file", "1\tx = 1", path="a.py"),
              round_of(2, "edit_file", miss, path="a.py", old_text="x = 2", new_text="x = 3")]
    rounds += [round_of(number, "search", f"hit {number} " * 2_000, pattern=f"p{number}") for number in range(3, 10)]
    messages = conversation(*rounds)
    prune.prune(messages, 4_000, Client())
    contents = tool_contents(messages)
    assert contents[1] == miss
    assert contents[2].startswith(prune.STUB_LEAD)
