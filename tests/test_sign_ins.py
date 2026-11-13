import json
import multiprocessing
import threading
import time
from dataclasses import asdict

import pytest

from arty_cli import oauth, sign_ins
from tests import sign_in_writers

WRITERS, ROUNDS = 6, 40
SERVER = sign_in_writers.SERVER
NOW = 1_000_000.0
HELD_SECONDS = 0.3


def first_record(**changes) -> sign_ins.Record:
    return sign_ins.Record(**{**asdict(sign_in_writers.record("fake", 0)), "access_token": "a1",
                              "refresh_token": "r1", **changes})


class TokenServer:
    def __init__(self):
        self.refreshes = []
        self.down = False

    def __call__(self, url, data=None, headers=None, method=None):
        if self.down:
            raise oauth.Unavailable(f"Couldn't reach {url}: [Errno -3] Temporary failure in name resolution")
        self.refreshes.append(data)
        number = len(self.refreshes) + 1
        answer = {"access_token": f"a{number}", "refresh_token": f"r{number}", "token_type": "Bearer",
                  "expires_in": 3600}
        return 200, {}, json.dumps(answer).encode()


def saved_store(tmp_path, record: sign_ins.Record | None = None) -> sign_ins.Store:
    store = sign_ins.Store(tmp_path)
    store.put("fake", record or first_record())
    return store


def test_many_processes_saving_sign_ins_at_once_lose_nothing(tmp_path):
    context = multiprocessing.get_context("spawn")
    names = [f"service{number}" for number in range(WRITERS)]
    barrier, stop = context.Barrier(WRITERS + 1), context.Event()
    writers = [context.Process(target=sign_in_writers.write_rounds, args=(str(tmp_path), name, ROUNDS, barrier))
               for name in names]
    reader = context.Process(target=sign_in_writers.read_until, args=(str(tmp_path), names, stop, barrier))
    for process in [*writers, reader]:
        process.start()
    for process in writers:
        process.join(120)
    stop.set()
    reader.join(30)
    assert [process.exitcode for process in [*writers, reader]] == [0] * (WRITERS + 1)
    table = sign_ins.Store(tmp_path).table()
    assert sorted(table) == names
    assert all(entry["refresh_token"] == f"refresh-{name}-{ROUNDS - 1}" for name, entry in table.items())


def test_a_session_adopts_a_token_another_session_renewed_instead_of_refreshing_again(tmp_path):
    saved_store(tmp_path)
    token_server = TokenServer()
    first = sign_ins.Store(tmp_path).credentials("fake", SERVER, token_server, lambda: NOW)
    second = sign_ins.Store(tmp_path).credentials("fake", SERVER, token_server, lambda: NOW)
    first.renew("a1")
    second.renew("a1")
    assert len(token_server.refreshes) == 1
    assert first.current() == second.current() == "a2"
    assert sign_ins.Store(tmp_path).get("fake").refresh_token == "r2"


def test_renewing_a_sign_in_that_was_removed_meanwhile_asks_for_a_new_sign_in(tmp_path):
    store = saved_store(tmp_path)
    credentials = store.credentials("fake", SERVER, TokenServer(), lambda: NOW)
    store.remove("fake")
    with pytest.raises(sign_ins.SignInError, match="was removed") as raised:
        credentials.renew("a1")
    assert raised.value.needs_sign_in


def test_renewed_tokens_that_cannot_be_saved_are_kept_and_reported(tmp_path):
    store = saved_store(tmp_path)
    credentials = store.credentials("fake", SERVER, TokenServer(), lambda: NOW)

    def unwritable(name, record):
        raise PermissionError(13, "Permission denied", str(store.path))

    store.saved = unwritable
    credentials.renew("a1")
    assert credentials.current() == "a2"
    notice = credentials.take_notice()
    assert "couldn't save it" in notice and "/mcp login fake" in notice and "a2" not in notice
    assert credentials.take_notice() == ""


def test_a_lock_that_cannot_be_taken_still_renews_and_says_so(tmp_path, monkeypatch):
    store = saved_store(tmp_path)
    credentials = store.credentials("fake", SERVER, TokenServer(), lambda: NOW)

    def no_lock():
        raise PermissionError(13, "Permission denied", str(store.lock_path))

    monkeypatch.setattr(store, "held", no_lock)
    credentials.renew("a1")
    assert credentials.current() == "a2" and "couldn't lock" in credentials.take_notice()


def test_an_unreachable_sign_in_server_is_not_a_lost_sign_in(tmp_path):
    token_server = TokenServer()
    token_server.down = True
    credentials = saved_store(tmp_path).credentials("fake", SERVER, token_server, lambda: NOW)
    with pytest.raises(sign_ins.SignInError, match="couldn't reach the sign-in server") as raised:
        credentials.renew("a1")
    assert not raised.value.needs_sign_in


def test_a_still_valid_token_is_used_while_the_sign_in_server_cannot_be_reached(tmp_path):
    clock = {"now": NOW}
    token_server = TokenServer()
    store = saved_store(tmp_path, first_record(expires_at=NOW + 30))
    credentials = store.credentials("fake", SERVER, token_server, lambda: clock["now"])
    token_server.down = True
    assert credentials.bearer() == "a1"
    clock["now"] = NOW + 31
    with pytest.raises(sign_ins.SignInError, match="couldn't reach"):
        credentials.bearer()
    token_server.down = False
    assert credentials.bearer() == "a2"


def test_a_refused_renewal_still_asks_for_a_new_sign_in(tmp_path):
    def refusing(url, data=None, headers=None, method=None):
        return 400, {}, b'{"error": "invalid_grant"}'

    credentials = saved_store(tmp_path, first_record(expires_at=NOW + 30)).credentials("fake", SERVER, refusing,
                                                                                      lambda: NOW)
    with pytest.raises(sign_ins.SignInError, match="has run out") as raised:
        credentials.bearer()
    assert raised.value.needs_sign_in


def test_a_sign_in_saved_before_revocation_was_kept_still_loads(tmp_path):
    entry = asdict(first_record())
    entry.pop("revocation_endpoint")
    (tmp_path / sign_ins.SIGN_INS_FILE).write_text(json.dumps({sign_ins.SIGN_INS_KEY: {"fake": entry}}))
    assert sign_ins.Store(tmp_path).get("fake") == first_record()


def test_a_saved_token_address_on_a_private_network_is_not_used_for_a_remote_server(tmp_path):
    saved_store(tmp_path, first_record(token_endpoint="https://10.0.0.1/token"))
    with pytest.raises(sign_ins.SignInError, match="unsafe token address"):
        sign_ins.Store(tmp_path).credentials("fake", SERVER)


def test_reading_a_sign_in_waits_while_another_session_is_writing(tmp_path):
    store = saved_store(tmp_path)
    released, taken = threading.Event(), threading.Event()

    def write_slowly():
        with sign_ins.Store(tmp_path).held():
            taken.set()
            released.wait(5)

    writer = threading.Thread(target=write_slowly, daemon=True)
    writer.start()
    assert taken.wait(5)
    threading.Timer(HELD_SECONDS, released.set).start()
    started = time.monotonic()
    assert store.get("fake") == first_record()
    writer.join(5)
    assert time.monotonic() - started >= HELD_SECONDS * 0.8


def test_a_sign_in_that_cannot_be_locked_for_reading_is_a_plain_error(tmp_path, monkeypatch):
    store = saved_store(tmp_path)

    def no_lock():
        raise PermissionError(13, "Permission denied", str(store.lock_path))

    monkeypatch.setattr(store, "held", no_lock)
    with pytest.raises(sign_ins.SignInError, match="Couldn't lock") as raised:
        store.get("fake")
    assert not raised.value.needs_sign_in
