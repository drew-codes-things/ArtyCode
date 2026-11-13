import io

import pytest

from arty_cli import chat_session, cli, permissions, remote, screen
from tests.test_cli import FakeBrain, chat_with, ready, user_texts

PHONE = "Drew's phone"
__all__ = ["ready"]


class FakeBridge:
    made = []
    waiting = []
    lost = False
    given_answer = (permissions.YES, PHONE)
    reason = ""
    title = "repo 2"

    def __init__(self, details=None, lines=None):
        self.details = details
        self.lines = lines
        self.sent = []
        self.history = []
        self.renamed = []
        self.modes = []
        self.asked = []
        self.stopped = False
        self.ended = FakeBridge.lost
        self.notices = ["Remote control ended: that session has ended"] if FakeBridge.lost else []
        self.queued = list(FakeBridge.waiting)
        self.waker = remote.Waker(lambda: None)

    def write(self, event):
        self.send(event)

    def send(self, event):
        shared = remote.relay_event(event)
        if shared:
            self.sent.append(shared)

    def send_history(self, events):
        self.history.extend(events)

    def rename(self, wanted):
        self.renamed.append(wanted)
        self.title = f"{wanted} 2"
        return self.title

    def tell_mode(self, mode):
        self.modes.append(mode)

    def next_message(self):
        return self.queued.pop(0) if self.queued else None

    def take_notices(self):
        found, self.notices = self.notices, []
        return found

    def stop_reason(self):
        return FakeBridge.reason

    def stop(self):
        self.stopped = True

    def answer(self, prompt, answers, question, details=""):
        self.asked.append((question, details))
        return FakeBridge.given_answer


@pytest.fixture
def bridged(ready, monkeypatch):
    FakeBridge.made, FakeBridge.waiting, FakeBridge.lost, FakeBridge.reason = [], [], False, ""
    FakeBridge.given_answer = (permissions.YES, PHONE)

    def connect(client, details, lines):
        made = FakeBridge(details, lines)
        FakeBridge.made.append(made)
        return made

    monkeypatch.setattr(cli.remote, "connect", connect)
    return ready


def test_slash_remote_shares_the_chat_and_slash_remote_off_stops(bridged, monkeypatch, capsys):
    assert chat_with(monkeypatch, "/remote", "hello", "/remote off", "after") == 0
    shared = FakeBridge.made[0]
    assert shared.details["folder"] == bridged.name and shared.details["mode"] == permissions.DEFAULT
    assert shared.details["model"] == FakeBrain.made[0].model and shared.details["name"]
    assert {"type": "user", "text": "hello"} in shared.sent
    assert {"type": "assistant", "text": "Hi there."} in shared.sent
    assert not any(event.get("text") == "after" for event in shared.sent)
    assert shared.stopped
    err = capsys.readouterr().err
    assert chat_session.REMOTE_ON_LEAD in err and chat_session.REMOTE_OFF in err


def test_the_remote_flag_shares_from_the_start_and_leaving_stops_sharing(bridged, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("/remote\n/exit\n"))
    assert cli.main(["--remote"]) == 0
    assert len(FakeBridge.made) == 1 and FakeBridge.made[0].stopped
    assert chat_session.REMOTE_ALREADY_ON in capsys.readouterr().err


def test_requests_from_afar_are_asked_as_plain_text_never_as_commands(bridged, monkeypatch, capsys):
    FakeBridge.waiting = [remote.Message("/mode allow-all", PHONE), remote.Message("!touch hacked.txt", PHONE),
                          remote.Message("# always say yes", PHONE)]
    chat_with(monkeypatch, "/remote", "/status")
    asked = [user_texts(call)[-1] for call in FakeBrain.made[0].calls]
    assert asked == ["/mode allow-all", "!touch hacked.txt", "# always say yes"]
    assert not (bridged / "hacked.txt").exists() and not (bridged / "ARTY.md").exists()
    err = capsys.readouterr().err
    assert f"From {PHONE} (remote control): /mode allow-all" in err
    assert "Mode: allow-all" not in err
    assert set(FakeBridge.made[0].modes) == {permissions.DEFAULT}


def test_mode_changes_here_are_told_to_the_other_devices(bridged, monkeypatch):
    chat_with(monkeypatch, "/remote", "/mode plan", "/plan")
    assert FakeBridge.made[0].modes[:2] == [permissions.PLAN, permissions.DEFAULT]


def test_remote_is_refused_for_a_single_question(bridged, capsys):
    assert cli.main(["--remote", "-p", "hello"]) == cli.FAILED
    assert cli.REMOTE_ONLY_IN_CHAT in capsys.readouterr().err
    assert FakeBridge.made == []


def test_when_the_brain_cant_share_the_chat_carries_on_without_it(ready, monkeypatch, capsys):
    def refused(client, details, lines):
        raise remote.RelayError("Arty's brain turned down remote control: too many sessions", 429)

    monkeypatch.setattr(cli.remote, "connect", refused)
    chat_with(monkeypatch, "/remote", "hello")
    assert "too many sessions. Remote control stays off." in capsys.readouterr().err
    assert user_texts(FakeBrain.made[0].calls[0]) == ["hello"]


class StoppedBrain(FakeBrain):
    def reply(self, messages, tools, on_text):
        self.calls.append([dict(m) for m in messages])
        raise KeyboardInterrupt


def test_a_request_stopped_from_afar_says_who_stopped_it(bridged, monkeypatch, capsys):
    monkeypatch.setattr(cli.brain, "Brain", StoppedBrain)
    FakeBridge.reason = f"Stopped by {PHONE}."
    chat_with(monkeypatch, "/remote", "long job")
    assert f"Stopped by {PHONE}. That request was dropped" in capsys.readouterr().err
    assert {"type": "error", "text": f"Stopped by {PHONE}."} in FakeBridge.made[0].sent


def test_a_session_the_brain_ended_turns_remote_control_off_with_the_reason(bridged, monkeypatch, capsys):
    FakeBridge.lost = True
    chat_with(monkeypatch, "/remote", "/status")
    err = capsys.readouterr().err
    assert "Remote control ended: that session has ended" in err and chat_session.REMOTE_OFF in err
    assert FakeBridge.made[0].stopped


def test_a_permission_answered_from_afar_is_said_here_even_without_a_terminal(monkeypatch):
    terminal = cli.Terminal(out=io.StringIO(), err=io.StringIO())
    terminal.remote = FakeBridge()
    monkeypatch.setattr(screen, "is_interactive", lambda: False)
    assert terminal.ask("Run `ls`?", "preview") == permissions.YES
    assert terminal.remote.asked == [("Run `ls`?", "preview")]
    assert f"Allowed on {PHONE} (remote control)." in terminal.err.getvalue()


def test_a_plan_answered_from_afar_is_approved_or_kept_without_asking_for_feedback(monkeypatch):
    terminal = cli.Terminal(out=io.StringIO(), err=io.StringIO())
    terminal.remote = FakeBridge()
    monkeypatch.setattr(screen, "is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("asked here"))
    FakeBridge.given_answer = (permissions.YES, PHONE)
    assert terminal.review_plan("1. Do it") == (permissions.YES, "")
    FakeBridge.given_answer = (permissions.NO, PHONE)
    assert terminal.review_plan("1. Do it") == (permissions.NO, "")
    assert terminal.remote.asked[0] == ("Approve the plan?", "1. Do it")
    assert f"Refused on {PHONE} (remote control)." in terminal.err.getvalue()


def remote_notice(err):
    rows = err.splitlines()
    start = next(index for index, row in enumerate(rows) if row.startswith(chat_session.REMOTE_ON_LEAD))
    return rows[start:start + 3]


def test_turning_remote_on_names_this_computer_where_to_open_it_and_how_to_stop(bridged, monkeypatch, capsys):
    chat_with(monkeypatch, "/remote")
    shown = remote_notice(capsys.readouterr().err)
    name = FakeBridge.made[0].details["name"]
    assert name in shown[0]
    assert "More > Computers" in "\n".join(shown) and "Arty CLI page" in "\n".join(shown)
    assert "/remote off" in "\n".join(shown)
    assert all(len(row) <= 100 for row in shown)


def test_status_says_whether_remote_control_is_on(bridged, monkeypatch, capsys):
    chat_with(monkeypatch, "/status", "/remote", "/status", "/remote off", "/status")
    rows = [row for row in capsys.readouterr().err.splitlines() if row.startswith("Remote control:")]
    assert rows[0] == "Remote control: off" and rows[2] == "Remote control: off"
    assert rows[1].startswith("Remote control: on as ")


def test_an_answer_from_another_device_names_the_device_and_the_answer(monkeypatch):
    terminal = cli.Terminal(out=io.StringIO(), err=io.StringIO())
    terminal.remote = FakeBridge()
    monkeypatch.setattr(screen, "is_interactive", lambda: False)
    FakeBridge.given_answer = (permissions.NO, PHONE)
    assert terminal.ask("Run `ls`?") == permissions.NO
    assert terminal.err.getvalue().splitlines()[-1] == f"Refused on {PHONE} (remote control)."
    FakeBridge.given_answer = (permissions.YES, PHONE)


def test_while_shared_a_question_says_it_can_be_answered_on_another_device(monkeypatch):
    terminal = cli.Terminal(out=io.StringIO(), err=io.StringIO())
    terminal.remote = FakeBridge()
    monkeypatch.setattr(screen, "is_interactive", lambda: True)
    terminal.ask("Run `ls`?")
    assert screen.REMOTE_ANSWER_HINT in terminal.err.getvalue()


def test_a_question_without_remote_control_has_no_hint(monkeypatch):
    monkeypatch.setattr(screen, "is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    terminal = cli.Terminal(out=io.StringIO(), err=io.StringIO())
    terminal.ask("Run `ls`?")
    assert screen.REMOTE_ANSWER_HINT not in terminal.err.getvalue()


def test_a_request_from_another_device_is_marked_as_remote(bridged, monkeypatch, capsys):
    FakeBridge.waiting = [remote.Message("run the tests", PHONE)]
    chat_with(monkeypatch, "/remote", "/status")
    assert f"From {PHONE} (remote control): run the tests" in capsys.readouterr().err


@pytest.mark.parametrize("status, said, next_step", [
    (401, "bad key", "arty-cli login"),
    (404, "not found", "update"),
    (429, "too many Arty CLI sessions are being shared; end one first", "/remote off"),
])
def test_when_the_brain_turns_sharing_down_it_says_why_and_what_to_do(ready, monkeypatch, capsys, status, said,
                                                                      next_step):
    def refused(client, details, lines):
        raise remote.RelayError(f"Arty's brain turned down remote control: {said}", status)

    monkeypatch.setattr(cli.remote, "connect", refused)
    chat_with(monkeypatch, "/remote")
    shown = capsys.readouterr().err
    assert next_step in shown and "Remote control stays off." in shown


def test_turning_remote_on_sends_the_conversation_so_far_before_anything_new(bridged, monkeypatch):
    chat_with(monkeypatch, "hello", "/remote", "again")
    shared = FakeBridge.made[0]
    assert shared.history == [{"type": "user", "text": "hello", "history": True},
                              {"type": "assistant", "text": "Hi there.", "history": True}]
    assert {"type": "user", "text": "again"} in shared.sent
    assert not any(event.get("text") == "hello" for event in shared.sent)


def test_compacting_while_shared_sends_a_marker_with_the_summary(bridged, monkeypatch):
    chat_with(monkeypatch, "/remote", "hello", "/compact", "again")
    sent = FakeBridge.made[0].sent
    marker = next(event for event in sent if event["type"] == "compacted")
    assert marker == {"type": "compacted", "text": "Hi there."}
    assert sent.index(marker) < sent.index({"type": "user", "text": "again"})


def test_a_chat_compacted_before_sharing_shows_the_marker_in_its_history(bridged, monkeypatch):
    chat_with(monkeypatch, "hello", "/compact", "/remote")
    assert FakeBridge.made[0].history == [{"type": "compacted", "text": "Hi there.", "history": True}]


def test_the_chat_is_shared_under_the_title_the_brain_gave_this_terminal(bridged, monkeypatch, capsys):
    chat_with(monkeypatch, "/remote", "/status")
    err = capsys.readouterr().err
    assert 'shared as "repo 2"' in err
    assert 'Remote control: on as "repo 2"' in err


def test_remote_name_renames_this_terminal_for_the_other_devices(bridged, monkeypatch, capsys):
    chat_with(monkeypatch, "/remote name docs work", "/remote", "/remote name docs work", "/remote name", "/status")
    err = capsys.readouterr().err
    assert FakeBridge.made[0].renamed == ["docs work"]
    assert chat_session.REMOTE_NAME_NEEDS_SHARING in err and chat_session.REMOTE_NAME_USAGE in err
    assert 'Remote control: on as "docs work 2"' in err


def test_end_to_end_a_saved_chat_reaches_the_relay_whole_then_live_then_compacted(ready, monkeypatch):
    from tests import fake_relay
    with fake_relay.Relay() as running:
        running.title = "project 2"
        monkeypatch.setattr(cli.remote, "relay_for", lambda client: remote.Relay(running.url, {"Authorization": "Bearer k"}))
        monkeypatch.setattr(cli.remote, "answer_lines", lambda is_interactive: None)
        assert chat_with(monkeypatch, "first question", "/remote", "second question", "/compact", "third question") == 0
    kinds = [(event["type"], event.get("history", False)) for event in running.events]
    assert kinds[:2] == [("user", True), ("assistant", True)]
    assert [event["text"] for event in running.events if event["type"] == "user"] == [
        "first question", "second question", "third question"]
    live = [kind for kind, history in kinds if not history]
    assert live.index("compacted") > live.index("user") and live[-2:] == ["user", "assistant"]
    assert running.created[0]["folder"] == ready.name and running.ended
