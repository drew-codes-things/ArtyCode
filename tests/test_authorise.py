import json

import pytest

from arty_cli import authorise
from tests.fake_servers import serving

STARTED = json.dumps({"device_code": "dev-secret", "user_code": "ABCD-EFGH", "verify_path": "/ios/?authorise=ABCD-EFGH",
                      "interval": 2, "expires_in": 600})


class Clock:
    def __init__(self):
        self.now = 0.0
        self.slept = []

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds

    def time(self):
        return self.now


def test_start_then_pending_then_key():
    pending = json.dumps({"status": "pending"})
    responses = [(200, STARTED), (202, pending), (202, pending), (200, json.dumps({"api_key": "app-key"}))]
    clock = Clock()
    with serving(responses, "application/json") as (url, recorder):
        request = authorise.start(url, {"CF-Access-Client-Id": "id"}, name="Arty CLI on test")
        key = authorise.wait_for_key(url, {}, request, sleep=clock.sleep, clock=clock.time)
    assert key == "app-key"
    assert request.user_code == "ABCD-EFGH" and request.interval == 2
    assert request.verify_url == f"{url}/ios/?authorise=ABCD-EFGH"
    assert recorder.requests[0]["path"] == "/device/start"
    assert recorder.requests[0]["body"] == {"name": "Arty CLI on test"}
    sent_headers = {name.lower(): value for name, value in recorder.requests[0]["headers"].items()}
    assert sent_headers["cf-access-client-id"] == "id"
    assert [entry["body"] for entry in recorder.requests[1:]] == [{"device_code": "dev-secret"}] * 3
    assert clock.slept == [2, 2]


def test_a_denied_request_stops_at_once():
    with serving([(200, STARTED), (403, json.dumps({"error": "denied"}))], "application/json") as (url, _recorder):
        request = authorise.start(url, {})
        with pytest.raises(authorise.AuthoriseError, match="turned down"):
            authorise.wait_for_key(url, {}, request, sleep=lambda seconds: None)


def test_an_expired_code_says_so():
    with serving([(200, STARTED), (410, json.dumps({"error": "expired"}))], "application/json") as (url, _recorder):
        request = authorise.start(url, {})
        with pytest.raises(authorise.AuthoriseError, match="ran out"):
            authorise.wait_for_key(url, {}, request, sleep=lambda seconds: None)


def test_waiting_gives_up_when_the_code_lifetime_passes():
    clock = Clock()
    request = authorise.Request("d", "U", "https://x", interval=5, expires_in=12)
    pending = json.dumps({"status": "pending"})
    with serving([(202, pending)] * 5, "application/json") as (url, recorder):
        with pytest.raises(authorise.AuthoriseError, match="ran out"):
            authorise.wait_for_key(url, {}, request, sleep=clock.sleep, clock=clock.time)
    assert len(recorder.requests) == 3


def test_an_old_brain_without_device_support_points_to_key_login():
    with serving([(404, json.dumps({"error": "not found"}))], "application/json") as (url, _recorder):
        with pytest.raises(authorise.AuthoriseError, match="too old"):
            authorise.start(url, {})


def test_silly_interval_and_lifetime_values_are_bounded():
    odd = json.dumps({"device_code": "d", "user_code": "U", "verify_path": "/ios/?authorise=U", "interval": 0, "expires_in": 10**9})
    with serving([(200, odd)], "application/json") as (url, _recorder):
        request = authorise.start(url, {})
    assert request.interval == 1 and request.expires_in == authorise.LONGEST_WAIT_SECONDS


def test_an_unreachable_address_is_a_plain_error():
    with pytest.raises(authorise.AuthoriseError, match="Can't reach Arty"):
        authorise.start("http://127.0.0.1:9", {})


def test_the_approve_page_is_on_the_same_site_as_the_api_address():
    started = json.dumps({"device_code": "d", "user_code": "U", "verify_path": "/ios/?authorise=U"})
    with serving([(200, started)], "application/json") as (url, _recorder):
        request = authorise.start(f"{url}/v1", {})
    assert request.verify_url == f"{url}/ios/?authorise=U"


def test_a_verify_path_that_points_elsewhere_is_refused():
    started = json.dumps({"device_code": "d", "user_code": "U", "verify_path": "//evil.example/ios/"})
    with serving([(200, started)], "application/json") as (url, _recorder):
        with pytest.raises(authorise.AuthoriseError):
            authorise.start(url, {})
