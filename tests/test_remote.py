import os
import select
import signal
import threading
import time

import pytest

from arty_cli import __version__, brain, permissions, remote
from tests import fake_relay

ANSWERS = {"y": permissions.YES, "n": permissions.NO, "a": permissions.ALWAYS}
PHONE = "Drew's phone"


def wait_until(check, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def relay():
    with fake_relay.Relay() as running:
        yield running


def relay_client(relay):
    return remote.Relay(relay.url, {"Authorization": "Bearer key", "User-Agent": f"arty-cli/{__version__}"})


class Lines:
    def __init__(self, *typed):
        self.typed = list(typed)
        self.discarded = 0

    def line(self, seconds):
        if self.typed:
            return self.typed.pop(0)
        time.sleep(min(seconds, 0.02))
        return None

    def discard(self):
        self.discarded += 1


class Calls:
    def __init__(self):
        self.made = 0

    def __call__(self):
        self.made += 1


def started_bridge(relay, lines=None, interrupt=None):
    written = []
    bridge = remote.Bridge(relay_client(relay), remote.Waker(interrupt or Calls()), lines=lines, write=written.append)
    bridge.start({"name": "laptop", "folder": "project", "model": "arty/qwen3:14b", "mode": "default"})
    return bridge, written


def open_request(relay):
    assert wait_until(lambda: relay.events_of("permission"))
    return relay.events_of("permission")[-1]["request_id"]


def test_the_relay_creates_posts_reads_and_ends_with_the_key_and_user_agent(relay):
    client = relay_client(relay)
    assert client.create({"name": "laptop"}) == fake_relay.SESSION_ID
    client.post(fake_relay.SESSION_ID, [{"type": "assistant", "text": "hi"}])
    relay.give({"type": "message", "text": "hello", "by": PHONE})
    assert client.inbox(fake_relay.SESSION_ID, 1) == [{"type": "message", "text": "hello", "by": PHONE}]
    client.end(fake_relay.SESSION_ID)
    assert relay.created == [{"name": "laptop"}]
    assert relay.events == [{"type": "assistant", "text": "hi"}]
    assert relay.ended == [f"{fake_relay.PREFIX}/{fake_relay.SESSION_ID}"]
    assert relay.headers[0]["Authorization"] == "Bearer key"
    assert relay.headers[0]["User-Agent"] == f"arty-cli/{__version__}"


def test_relay_errors_carry_the_status_and_the_brains_reason(relay):
    relay.inbox_status = 410
    with pytest.raises(remote.RelayError) as raised:
        relay_client(relay).inbox(fake_relay.SESSION_ID, 0)
    assert raised.value.status == 410
    assert "ended" in str(raised.value)


def test_an_unreachable_brain_is_a_relay_error_without_a_status():
    with pytest.raises(remote.RelayError) as raised:
        remote.Relay("http://127.0.0.1:9/v1", {}).create({})
    assert raised.value.status == 0


def test_the_relay_uses_the_brains_route_key_and_access_headers():
    class Client:
        settings = type("Settings", (), {"api_key": "secret"})

        def pick_route(self):
            return brain.Route("https://arty.example/v1/", {"CF-Access-Client-Id": "id"})

    made = remote.relay_for(Client())
    assert made.url == "https://arty.example/v1"
    assert made.headers == {"Authorization": "Bearer secret", "User-Agent": f"arty-cli/{__version__}",
                            "CF-Access-Client-Id": "id"}


def test_stream_json_events_become_relay_events():
    assert remote.relay_event({"type": "text", "text": "Hi"}) == {"type": "assistant", "text": "Hi"}
    assert remote.relay_event({"type": "text", "text": ""}) is None
    assert remote.relay_event({"type": "tool_use", "id": "c1", "name": "read_file", "input": {"path": "a.py"}}) == \
        {"type": "tool_call", "tool": "read_file", "text": '{"path": "a.py"}'}
    assert remote.relay_event({"type": "tool_result", "tool_use_id": "c1", "name": "read_file", "content": "x = 1",
                               "is_error": False}) == {"type": "tool_result", "tool": "read_file", "text": "x = 1"}
    assert remote.relay_event({"type": "user", "text": "fix it"}) == {"type": "user", "text": "fix it"}
    assert remote.relay_event({"type": "system", "subtype": "init"}) is None
    assert remote.relay_event({"type": "result", "result": "done"}) is None


def test_batches_merge_streamed_text_and_stay_inside_the_relays_limits():
    pieces = [{"type": "assistant", "text": "a"}] * 3 + [{"type": "tool_call", "tool": "t", "text": "x" * 9000}]
    first = remote.batched(pieces)
    assert first == [[{"type": "assistant", "text": "aaa"}, {"type": "tool_call", "tool": "t", "text": "x" * 8000}]]
    long_answer = remote.batched([{"type": "assistant", "text": "y" * 8001}])
    assert [len(event["text"]) for event in long_answer[0]] == [8000, 1]
    many = remote.batched([{"type": "status", "text": str(number)} for number in range(120)])
    assert [len(batch) for batch in many] == [50, 50, 20]


def test_the_waker_only_wakes_a_waiting_prompt_and_only_stops_a_running_turn():
    calls = Calls()
    waker = remote.Waker(calls)
    assert waker.wake_prompt() is False and waker.stop_turn() is False
    with waker.during(remote.AT_PROMPT):
        assert waker.stop_turn() is False
        assert waker.wake_prompt() is True
        assert waker.wake_prompt() is False
    assert calls.made == 1


def test_the_signal_from_a_wake_interrupts_only_while_still_at_that_state():
    waker = remote.Waker(Calls())
    with waker.during(remote.IN_TURN):
        waker.stop_turn()
        with pytest.raises(KeyboardInterrupt):
            waker.on_signal(signal.SIGINT, None)
        waker.stop_turn()
    waker.on_signal(signal.SIGINT, None)
    with pytest.raises(KeyboardInterrupt):
        waker.on_signal(signal.SIGINT, None)


@pytest.mark.skipif(not hasattr(signal, "pthread_kill"), reason="POSIX signal delivery")
def test_a_remote_stop_breaks_into_a_main_thread_blocked_on_input():
    waker = remote.Waker()
    reading, writing = os.pipe()
    waker.install()
    try:
        with waker.during(remote.IN_TURN):
            threading.Timer(0.2, waker.stop_turn).start()
            started = time.monotonic()
            with pytest.raises(KeyboardInterrupt):
                select.select([reading], [], [], 5)
        assert time.monotonic() - started < 3
    finally:
        waker.uninstall()
        os.close(reading)
        os.close(writing)
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


def test_on_linux_a_typed_answer_is_read_without_blocking():
    ready = [False, True, True]
    data = [b"y\n", b""]
    flushed = []
    lines = remote.PosixLines(7, wait=lambda descriptor, seconds: ready.pop(0), read=lambda descriptor, size: data.pop(0),
                              flush=flushed.append)
    assert lines.line(0.2) is None
    assert lines.line(0.2) == "y\n"
    with pytest.raises(EOFError):
        lines.line(0.2)
    lines.discard()
    assert flushed == [7]


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def console_lines(keys):
    given, written, clock = list(keys), [], Clock()
    lines = remote.ConsoleLines(lambda: bool(given), lambda: given.pop(0), written.append, clock.sleep, clock)
    return lines, written, given


def test_on_windows_an_answer_is_typed_edited_and_read_without_blocking():
    lines, written, _given = console_lines(["n", "o", "\x08", "\xe0", "K", "y", "\r"])
    assert lines.line(0.2) == "ny"
    assert "".join(written) == "no\b \by\r\n"


def test_on_windows_no_key_means_no_answer_yet_and_ctrl_c_stops():
    lines, _written, _given = console_lines([])
    assert lines.line(0.2) is None
    lines, _written, _given = console_lines(["\x03"])
    with pytest.raises(KeyboardInterrupt):
        lines.line(0.2)


def test_on_windows_a_half_typed_answer_is_dropped_when_another_device_answers():
    lines, _written, given = console_lines(["y"])
    assert lines.line(0.2) is None
    given.extend(["e", "s"])
    lines.discard()
    assert given == [] and lines.typed == []


def test_the_answer_reader_is_picked_for_the_platform(monkeypatch):
    assert remote.answer_lines(lambda: False) is None
    monkeypatch.setattr(remote.os, "name", "posix")
    monkeypatch.setattr(remote.sys, "stdin", type("Stdin", (), {"fileno": lambda self: 0})())
    assert isinstance(remote.answer_lines(lambda: True), remote.PosixLines)


def test_starting_shares_the_details_and_stopping_flushes_events_then_ends(relay):
    bridge, _written = started_bridge(relay)
    bridge.write({"type": "text", "text": "Hel"})
    bridge.write({"type": "text", "text": "lo"})
    bridge.stop()
    assert relay.created == [{"name": "laptop", "folder": "project", "model": "arty/qwen3:14b", "mode": "default"}]
    assert "".join(event["text"] for event in relay.events_of("assistant")) == "Hello"
    assert relay.ended == [f"{fake_relay.PREFIX}/{fake_relay.SESSION_ID}"]
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


def test_an_answer_from_another_device_wins_and_the_half_typed_one_is_dropped(relay):
    lines = Lines()
    bridge, written = started_bridge(relay, lines)
    answering = threading.Thread(target=lambda: relay.give(
        {"type": "answer", "request_id": open_request(relay), "allow": True, "by": PHONE}))
    answering.start()
    try:
        assert bridge.answer("Run `ls`? [y/n/a]: ", ANSWERS, "Run `ls`?", "") == (permissions.YES, PHONE)
    finally:
        answering.join()
        bridge.stop()
    assert lines.discarded == 1
    assert written[0] == "Run `ls`? [y/n/a]: "
    assert relay.events_of("permission")[0]["text"] == "Run `ls`?"
    assert relay.events_of("permission_answered") == [
        {"type": "permission_answered", "request_id": relay.events_of("permission")[0]["request_id"], "allow": True,
         "by": PHONE}]


def test_an_answer_typed_here_first_wins_and_a_late_one_from_afar_is_ignored(relay):
    bridge, written = started_bridge(relay, Lines("maybe\n", "n\n"))
    try:
        assert bridge.answer("Edit a.py? ", ANSWERS, "Edit a.py?", "+x = 2") == (permissions.NO, "")
        request_id = open_request(relay)
        relay.give({"type": "answer", "request_id": request_id, "allow": True, "by": PHONE})
        assert wait_until(lambda: relay.inbox.empty())
        time.sleep(0.1)
        assert bridge.answers == {}
    finally:
        bridge.stop()
    assert written == ["Edit a.py? ", "Edit a.py? "]
    assert wait_until(lambda: relay.events_of("permission_answered"))
    assert relay.events_of("permission")[0]["text"] == "Edit a.py?\n+x = 2"
    assert relay.events_of("permission_answered")[0]["by"] == remote.HERE
    assert relay.events_of("permission_answered")[0]["allow"] is False


def test_another_device_can_only_allow_once_never_always(relay):
    bridge, _written = started_bridge(relay)
    answering = threading.Thread(target=lambda: relay.give(
        {"type": "answer", "request_id": open_request(relay), "allow": False, "by": PHONE}))
    answering.start()
    try:
        assert bridge.answer("Run? ", ANSWERS, "Run?") == (permissions.NO, PHONE)
    finally:
        answering.join()
        bridge.stop()


def test_without_a_terminal_only_another_device_can_answer(relay):
    bridge, _written = started_bridge(relay, None)
    answering = threading.Thread(target=lambda: relay.give(
        {"type": "answer", "request_id": open_request(relay), "allow": True, "by": PHONE}))
    answering.start()
    try:
        assert bridge.answer("Run? ", ANSWERS, "Run?") == (permissions.YES, PHONE)
    finally:
        answering.join()
        bridge.stop()


def test_messages_from_afar_queue_in_order_and_wake_the_prompt(relay):
    calls = Calls()
    bridge, _written = started_bridge(relay, interrupt=calls)
    try:
        with bridge.waker.during(remote.AT_PROMPT):
            relay.give({"type": "message", "text": "first", "by": PHONE}, {"type": "message", "text": "second", "by": PHONE},
                       {"type": "message", "text": "   ", "by": PHONE})
            assert wait_until(lambda: bridge.messages.qsize() == 2)
        assert bridge.next_message() == remote.Message("first", PHONE)
        assert bridge.next_message() == remote.Message("second", PHONE)
        assert bridge.next_message() is None
        assert calls.made == 1
    finally:
        bridge.waker.uninstall()
        bridge.stop()


def test_a_stop_from_afar_interrupts_a_running_turn_and_names_who(relay):
    calls = Calls()
    bridge, _written = started_bridge(relay, interrupt=calls)
    try:
        with bridge.waker.during(remote.IN_TURN):
            relay.give({"type": "interrupt", "by": PHONE})
            assert wait_until(lambda: calls.made == 1)
        assert bridge.stop_reason() == f"Stopped by {PHONE}."
        assert bridge.stop_reason() == ""
    finally:
        bridge.stop()


def test_a_stop_from_afar_with_nothing_running_says_so(relay):
    calls = Calls()
    bridge, _written = started_bridge(relay, interrupt=calls)
    relay.give({"type": "interrupt", "by": PHONE})
    assert wait_until(lambda: relay.events_of("status"))
    bridge.stop()
    assert calls.made == 0
    assert relay.events_of("status")[0]["text"] == remote.NOTHING_TO_STOP
    assert bridge.stop_reason() == ""


def test_text_from_afar_cannot_send_terminal_control_codes(relay):
    bridge, _written = started_bridge(relay)
    relay.give({"type": "message", "text": "hi\x1b[2J there\nnext line", "by": "phone\x07"})
    assert wait_until(lambda: not bridge.messages.empty())
    bridge.stop()
    assert bridge.next_message() == remote.Message("hi[2J there\nnext line", "phone")


def test_mode_changes_are_told_once_each(relay):
    bridge, _written = started_bridge(relay)
    bridge.tell_mode("default")
    bridge.tell_mode("plan")
    bridge.tell_mode("plan")
    bridge.stop()
    assert relay.events_of("status") == [{"type": "status", "mode": "plan", "text": "Mode: plan"}]


def test_a_session_the_brain_forgot_ends_remote_control_with_a_notice(relay):
    calls = Calls()
    bridge, _written = started_bridge(relay, interrupt=calls)
    with bridge.waker.during(remote.AT_PROMPT):
        relay.inbox_status = 404
        assert wait_until(lambda: bridge.ended)
    bridge.stop()
    assert calls.made == 1
    assert any("Remote control ended" in notice for notice in bridge.take_notices())
    assert relay.ended == []


@pytest.mark.parametrize("status, why", [(401, "API key"), (403, "isn't allowed")])
def test_a_refused_key_or_device_ends_remote_control_and_says_why(relay, status, why):
    bridge, _written = started_bridge(relay)
    relay.inbox_status = status
    try:
        assert wait_until(lambda: bridge.ended)
    finally:
        bridge.stop()
    notices = bridge.take_notices()
    assert len(notices) == 1 and "Remote control ended" in notices[0] and why in notices[0]


def test_other_relay_errors_are_retried_until_it_answers_again(relay, monkeypatch):
    monkeypatch.setattr(remote, "RETRY_PAUSE_SECONDS", 0.05)
    bridge, _written = started_bridge(relay)
    relay.inbox_status = 502
    time.sleep(0.3)
    relay.inbox_status = 200
    relay.give({"type": "message", "text": "still here", "by": PHONE})
    try:
        assert wait_until(lambda: (message := bridge.next_message()) is not None and message.text == "still here")
        assert not bridge.ended
    finally:
        bridge.stop()


def test_a_permission_without_a_terminal_is_refused_once_the_session_is_gone(relay):
    bridge, _written = started_bridge(relay, None)
    relay.inbox_status = 410
    assert wait_until(lambda: bridge.ended)
    try:
        assert bridge.answer("Run? ", ANSWERS, "Run?") == (permissions.NO, "")
    finally:
        bridge.stop()


class StoppedLines(Lines):
    def line(self, seconds):
        raise KeyboardInterrupt


def test_a_stop_while_a_permission_waits_tells_the_other_devices_it_is_no_longer_waiting(relay):
    bridge, _written = started_bridge(relay, StoppedLines())
    bridge.stopped_by = PHONE
    try:
        with pytest.raises(KeyboardInterrupt):
            bridge.answer("Run? ", ANSWERS, "Run?")
    finally:
        bridge.stop()
    request_id = relay.events_of("permission")[0]["request_id"]
    assert relay.events_of("permission_answered") == [
        {"type": "permission_answered", "request_id": request_id, "allow": False, "by": PHONE}]
    assert bridge.stop_reason() == f"Stopped by {PHONE}."
