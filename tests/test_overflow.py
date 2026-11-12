import json

import pytest

from arty_cli import agent, brain, cli, settings
from tests.fake_servers import serving
from tests.scripted import ScriptedBrain, project, session_for
from tests.test_follow_ups import BRAINS_FOLLOW_UP

assert project

MODEL = "arty/mimo-v2.6:9b"
WINDOW = 24576
REFUSAL = (f"This conversation is longer than mimo-v2.6:9b's context window of {WINDOW} tokens, so the model did not "
           "read it. Shorten or compact the conversation and send it again.")
DETAILS = {"message": REFUSAL, "type": "context_length_exceeded", "code": 413, "model": "mimo-v2.6:9b",
           "context_tokens": WINDOW}


def chunk_line(delta: dict, finish=None, **extra) -> str:
    chunk = {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}], **extra}
    return f"data: {json.dumps(chunk)}"


def refused_stream(before: str = "") -> list[str]:
    text = f"{before}\n\n{REFUSAL}" if before else REFUSAL
    lines = [": working", chunk_line({"role": "assistant", "content": before})] if before else [": working"]
    return lines + [chunk_line({"content": text.removeprefix(before)}), chunk_line({}, "stop", error=DETAILS),
                    "data: [DONE]"]


def answered_stream(text: str) -> str:
    return "\n\n".join([chunk_line({"role": "assistant", "content": text}, "stop"), "data: [DONE]"]) + "\n\n"


def test_a_streamed_refusal_for_length_is_an_overflow_and_its_text_is_never_shown():
    shown = []
    with pytest.raises(brain.WindowOverflow) as raised:
        brain.read_reply(refused_stream(), shown.append)
    assert shown == []
    assert raised.value.window == WINDOW and raised.value.model == "mimo-v2.6:9b"
    assert "context window of 24576 tokens" in str(raised.value)


def test_text_sent_before_a_streamed_refusal_is_still_shown_but_the_refusal_is_not():
    shown = []
    with pytest.raises(brain.WindowOverflow):
        brain.read_reply(refused_stream("Looking at it."), shown.append)
    assert shown == ["Looking at it."]


def test_an_answer_that_only_mentions_the_context_window_is_shown_whole():
    shown = []
    lines = [chunk_line({"content": "Each model has its own context window."}), chunk_line({"content": " Done."}, "stop"),
             "data: [DONE]"]
    reply = brain.read_reply(lines, shown.append)
    assert "".join(shown) == reply["content"] == "Each model has its own context window. Done."


def test_an_answer_ending_in_a_mention_of_the_context_window_is_shown_when_the_stream_ends():
    shown = []
    lines = [chunk_line({"content": "It fits the context window."}, "stop"), "data: [DONE]"]
    assert brain.read_reply(lines, shown.append)["content"] == "It fits the context window."
    assert shown == ["It fits the context window."]


def test_another_kind_of_error_in_the_last_chunk_does_not_count_as_an_overflow():
    lines = [chunk_line({"content": "ok"}), chunk_line({}, "stop", error={"type": "other", "message": "x"}),
             "data: [DONE]"]
    assert brain.read_reply(lines, lambda text: None)["content"] == "ok"


def brain_at(url: str) -> brain.Brain:
    found = brain.Brain(settings.Settings(remote_url=f"{url}/v1", api_key="k", model=MODEL, context_tokens="24576"))
    found.route = brain.Route(f"{url}/v1", {})
    return found


def asked(client):
    return client.reply([{"role": "user", "content": "hi"}], tools=[], on_text=lambda text: None)


def test_a_413_for_length_is_an_overflow_naming_the_window():
    with serving([(413, json.dumps({"error": DETAILS}))], "text/event-stream") as (url, recorder):
        with pytest.raises(brain.WindowOverflow) as raised:
            asked(brain_at(url))
    assert len(recorder.requests) == 1
    assert raised.value.window == WINDOW and str(raised.value) == REFUSAL


def test_a_413_of_another_kind_stays_a_plain_brain_error():
    with serving([(413, json.dumps({"error": {"message": "too big a body"}}))], "text/event-stream") as (url, _rec):
        with pytest.raises(brain.BrainError) as raised:
            asked(brain_at(url))
    assert not isinstance(raised.value, brain.WindowOverflow)


class Refusing:
    def __init__(self, refusals: int):
        self.refusals = refusals
        self.model = MODEL
        self.sent = 0

    def reply(self, messages, tools, on_text):
        self.sent += 1
        if self.sent <= self.refusals:
            raise brain.WindowOverflow(REFUSAL, "mimo-v2.6:9b", WINDOW)
        return {"role": "assistant", "content": "Done.", "tool_calls": []}


class Screen:
    def show_text(self, text):
        pass


def test_an_overflow_is_compacted_with_force_and_sent_again_once():
    squeezed = []
    client = Refusing(1)
    reply = agent.replied(client, [{"role": "user", "content": "hi"}], [], Screen(),
                          lambda messages, overflowed=False: squeezed.append(overflowed))
    assert reply["content"] == "Done." and client.sent == 2 and squeezed == [True]


def test_a_second_overflow_is_a_clear_error_and_is_not_sent_a_third_time():
    squeezed = []
    client = Refusing(5)
    with pytest.raises(brain.WindowOverflow) as raised:
        agent.replied(client, [{"role": "user", "content": "hi"}], [], Screen(),
                      lambda messages, overflowed=False: squeezed.append(overflowed))
    assert client.sent == 2 and squeezed == [True]
    said = str(raised.value)
    assert "still" in said and "24576" in said and "/clear" in said and REFUSAL not in said


def refused_once(steps):
    refusals = []

    def refuse(messages):
        refusals.append(len(messages))
        raise brain.WindowOverflow(REFUSAL, "mimo-v2.6:9b", WINDOW)

    return [refuse if step == "refuse" else step for step in steps], refusals


def test_a_turn_the_brain_refuses_as_too_long_carries_on_after_compacting(project):
    (project / "a.txt").write_text("alpha\n" * 200)
    steps, refusals = refused_once([[("read_file", {"path": "a.txt"})], "refuse", "Done."])
    client = ScriptedBrain(steps)
    session = session_for(project, client)
    assert session.ask("read a.txt")
    assert refusals and session.messages[-1]["content"] == "Done."
    shown = session.terminal.out.getvalue() + session.terminal.err.getvalue()
    assert REFUSAL not in shown
    resent = client.prompts[-1]
    later = [str(message["content"]) for message in resent[1:] if message["role"] == "user"]
    assert all(text == "read a.txt" or BRAINS_FOLLOW_UP.search(text) for text in later), later


def test_a_turn_still_too_long_after_compacting_ends_with_the_clear_error(project):
    steps, refusals = refused_once(["refuse", "refuse", "Never sent."])
    client = ScriptedBrain(steps)
    session = session_for(project, client)
    assert not session.ask("hello")
    shown = session.terminal.err.getvalue()
    assert len(refusals) == 2 and "still" in shown and "/clear" in shown
    assert cli.ASK_AGAIN not in shown
    assert client.steps == ["Never sent."]


def test_a_real_streamed_refusal_never_reaches_the_screen_and_the_request_is_sent_again(project, monkeypatch):
    monkeypatch.setattr(cli.Session, "squeeze", lambda self, messages, overflowed=False: None)
    refused = "\n\n".join(refused_stream()) + "\n\n"
    listing = json.dumps({"data": [{"id": MODEL, "context_tokens": WINDOW, "reply_tokens": 4096}]})
    responses = [(200, listing), (200, refused), (200, answered_stream("All done."))]
    with serving(responses, "text/event-stream") as (url, recorder):
        session = session_for(project, brain_at(url))
        answered = session.ask("hello")
    assert answered, session.terminal.err.getvalue()
    assert [sent["path"] for sent in recorder.requests] == ["/v1/models?all=1"] + ["/v1/chat/completions"] * 2
    shown = session.terminal.out.getvalue() + session.terminal.err.getvalue()
    assert "All done." in shown and "context window" not in shown
    assert session.messages[-1]["content"] == "All done."
