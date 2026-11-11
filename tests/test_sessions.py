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


def test_a_damaged_conversation_file_is_skipped(tmp_path):
    store = sessions.Store(tmp_path / "sessions")
    store.start(tmp_path / "p").save([{"role": "user", "content": "good"}], "m")
    folder = store.folder(tmp_path / "p")
    (folder / "99999999-999999-000000.json").write_text("{broken")
    assert store.latest(tmp_path / "p").messages[0]["content"] == "good"
