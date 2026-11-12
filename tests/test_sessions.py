import json
import os
import stat
import sys
from datetime import datetime

import pytest

from arty_cli import sessions


def test_the_last_conversation_in_a_project_comes_back(tmp_path):
    store = sessions.Store(tmp_path / "sessions")
    first = store.start(tmp_path / "proj")
    first.save([{"role": "user", "content": "one"}], "arty/a")
    second = store.start(tmp_path / "proj")
    second.save([{"role": "user", "content": "two"}], "arty/b")
    latest = store.latest(tmp_path / "proj")
    assert latest.messages == [{"role": "user", "content": "two"}]
    assert latest.model == "arty/b"


def test_projects_keep_their_own_conversations(tmp_path):
    store = sessions.Store(tmp_path / "sessions")
    store.start(tmp_path / "a").save([{"role": "user", "content": "a"}], "m")
    assert store.latest(tmp_path / "b") is None


def test_conversations_are_listed_newest_first_and_found_by_id(tmp_path):
    store = sessions.Store(tmp_path / "sessions")
    ids = []
    for word in ("one", "two", "three"):
        saved = store.start(tmp_path / "p")
        saved.save([{"role": "user", "content": word}], "m")
        ids.append(saved.id)
    listed = store.listed(tmp_path / "p")
    assert [entry.id for entry in listed] == ids[::-1]
    assert listed[0].first_request == "three"
    assert store.load(tmp_path / "p", ids[0]).messages[0]["content"] == "one"


def test_conversations_started_within_one_clock_tick_get_their_own_ids_in_order(tmp_path, monkeypatch):
    class StuckClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 12, 9, 30, 0, 125000)

    monkeypatch.setattr(sessions, "datetime", StuckClock)
    store = sessions.Store(tmp_path / "sessions")
    first = store.start(tmp_path / "p")
    first.save([{"role": "user", "content": "one"}], "m")
    second = store.start(tmp_path / "p")
    third = store.start(tmp_path / "p")
    second.save([{"role": "user", "content": "two"}], "m")
    third.save([{"role": "user", "content": "three"}], "m")
    assert len({first.id, second.id, third.id}) == 3
    assert [entry.first_request for entry in store.listed(tmp_path / "p")] == ["three", "two", "one"]
    assert sessions.Store(tmp_path / "sessions").start(tmp_path / "p").id not in {first.id, second.id, third.id}


def test_a_damaged_conversation_file_is_skipped(tmp_path):
    store = sessions.Store(tmp_path / "sessions")
    store.start(tmp_path / "p").save([{"role": "user", "content": "good"}], "m")
    folder = store.folder(tmp_path / "p")
    (folder / "99999999-999999-000000.json").write_text("{broken")
    assert store.latest(tmp_path / "p").messages[0]["content"] == "good"


def test_a_save_that_fails_part_way_leaves_the_old_conversation_whole(tmp_path, monkeypatch):
    saved = sessions.Store(tmp_path / "sessions").start(tmp_path / "proj")
    saved.save([{"role": "user", "content": "kept"}], "arty/a")

    def disk_fills_up(data, handle, **options):
        handle.write('{"half')
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(sessions.json, "dump", disk_fills_up)
    with pytest.raises(OSError):
        saved.save([{"role": "user", "content": "new"}], "arty/a")
    monkeypatch.undo()
    assert json.loads(saved.path.read_text())["messages"] == [{"role": "user", "content": "kept"}]
    assert [found.name for found in saved.path.parent.iterdir()] == [saved.path.name]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_saving_over_an_open_conversation_makes_it_private(tmp_path):
    saved = sessions.Store(tmp_path / "sessions").start(tmp_path / "proj")
    saved.save([], "arty/a")
    saved.path.chmod(0o644)
    saved.save([{"role": "user", "content": "x"}], "arty/a")
    assert stat.S_IMODE(os.stat(saved.path).st_mode) == 0o600


def test_the_checklist_and_undo_steps_are_saved_and_come_back(tmp_path):
    store = sessions.Store(tmp_path / "sessions")
    saved = store.start(tmp_path / "p")
    file = tmp_path / "p" / "a.py"
    saved.save([{"role": "user", "content": "go"}], "m", todos=[{"text": "Write a.py", "status": "done"}],
               checkpoints=[sessions.tools.Checkpoint(file, None, "x = 1\n")])
    found = store.load(tmp_path / "p", saved.id)
    assert found.todos == [{"text": "Write a.py", "status": "done"}]
    assert found.checkpoints == [sessions.tools.Checkpoint(file, None, "x = 1\n")]


def test_only_the_newest_undo_steps_are_saved(tmp_path):
    saved = sessions.Store(tmp_path / "sessions").start(tmp_path / "p")
    many = [sessions.tools.Checkpoint(tmp_path / f"{number}.py", None, "x") for number in range(50)]
    saved.save([], "m", checkpoints=many)
    stored = json.loads(saved.path.read_text())["checkpoints"]
    assert len(stored) == sessions.MAX_SAVED_CHECKPOINTS and stored[-1]["path"].endswith("49.py")


def test_a_session_saved_before_checklists_were_kept_still_loads(tmp_path):
    store = sessions.Store(tmp_path / "sessions")
    saved = store.start(tmp_path / "p")
    saved.path.parent.mkdir(parents=True)
    saved.path.write_text(json.dumps({"model": "m", "messages": [{"role": "user", "content": "hi"}]}))
    found = store.load(tmp_path / "p", saved.id)
    assert found.todos == [] and found.checkpoints == []
