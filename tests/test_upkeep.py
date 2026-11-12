import io

from arty_cli import cli, doctor, update, upkeep


def test_doctor_and_update_are_the_only_upkeep_commands():
    assert upkeep.handles(["doctor"]) and upkeep.handles(["update"]) and upkeep.handles(["update", "--check"])
    assert not upkeep.handles([]) and not upkeep.handles(["login"])
    assert not upkeep.handles(["doctor", "who", "is", "that"]) and not upkeep.handles(["-p", "doctor"])


def test_arty_doctor_and_arty_update_run_without_starting_a_chat(monkeypatch):
    ran = []
    monkeypatch.setattr(doctor, "main", lambda argv, **rest: ran.append(("doctor", argv)) or 0)
    monkeypatch.setattr(update, "main", lambda argv, **rest: ran.append(("update", argv)) or 3)
    monkeypatch.setattr(cli, "parse_arguments", lambda argv: (_ for _ in ()).throw(AssertionError("a chat was started")))
    assert cli.main(["doctor"]) == 0
    assert cli.main(["update", "--check"]) == 3
    assert ran == [("doctor", []), ("update", ["--check"])]


def test_a_question_that_starts_with_doctor_is_still_a_question(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    seen = []
    monkeypatch.setattr(cli, "parse_arguments", lambda argv: seen.append(argv) or (_ for _ in ()).throw(SystemExit(0)))
    try:
        cli.main(["doctor", "who?"])
    except SystemExit:
        pass
    assert seen == [["doctor", "who?"]]
