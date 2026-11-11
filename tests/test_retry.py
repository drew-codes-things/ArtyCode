import json

import pytest

from arty_cli import brain, retry, settings
from tests.fake_servers import serving

BRAIN_ANSWER = "data: " + json.dumps({"choices": [{"delta": {"content": "ok"}}]}) + "\n\ndata: [DONE]\n\n"
BUSY = json.dumps({"error": {"message": "Arty is busy with 4 jobs"}})


@pytest.fixture
def waits(monkeypatch):
    slept, shown = [], []
    monkeypatch.setattr(retry, "sleep", slept.append)
    monkeypatch.setattr(retry, "show_wait", shown.append)
    return slept, shown


def brain_at(url):
    found = brain.Brain(settings.Settings(base_url=f"{url}/v1", api_key="k"))
    found.route = brain.Route(f"{url}/v1", {})
    return found


def test_waits_double_with_jitter_and_retry_after_wins():
    assert retry.wait_before(0, 7.0) == 7.0
    for attempt in range(3):
        base = retry.FIRST_WAIT_SECONDS * 2 ** attempt
        assert base * (1 - retry.JITTER) <= retry.wait_before(attempt, None) <= base * (1 + retry.JITTER)
    assert retry.wait_before(10, None) <= retry.MAX_WAIT_SECONDS * (1 + retry.JITTER)
    assert retry.wait_before(0, 10_000.0) <= retry.MAX_RETRY_AFTER_SECONDS


def test_the_brain_busy_503_is_retried(waits):
    slept, shown = waits
    with serving([(503, BUSY), (200, BRAIN_ANSWER)], "text/event-stream") as (url, recorder):
        reply = brain_at(url).reply([{"role": "user", "content": "hi"}], [], lambda text: None)
    assert reply["content"] == "ok" and len(shown) == 1


def test_a_retry_after_header_sets_the_wait(waits):
    slept, _shown = waits
    with serving([(503, BUSY, {"Retry-After": "3"}), (200, BRAIN_ANSWER)], "text/event-stream") as (url, _r):
        brain_at(url).reply([{"role": "user", "content": "hi"}], [], lambda text: None)
    assert slept == [3.0]


def test_a_long_retry_delay_is_honoured_up_to_a_minute():
    assert retry.wait_before(0, 55.1) == 55.1
    assert retry.wait_before(0, 600.0) == retry.MAX_RETRY_AFTER_SECONDS
