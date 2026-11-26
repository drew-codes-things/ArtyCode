import http.client
import io
import json
import logging
import os
import subprocess
import threading
from pathlib import Path

import pytest

from arty_cli import __version__, settings, update
from arty_cli.update import read_release as real_read_release

OLD, NEW = "a" * 40, "b" * 40


class Git:
    def __init__(self, **answers):
        self.answers = {"status --porcelain": "", "symbolic-ref --short refs/remotes/origin/HEAD": "origin/main",
                        "rev-parse --abbrev-ref HEAD": "main", "rev-list --count HEAD..origin/main": "3",
                        "rev-parse HEAD": OLD, "show origin/main:arty_cli/__init__.py": '__version__ = "0.2.0"\n',
                        **answers}
        self.ran = []
        self.merged = False

    def __call__(self, command, **options):
        self.ran.append(list(command))
        said = " ".join(command[3:]) if command[0] == "git" else " ".join(command)
        if command[0] == "git" and command[3] == "merge":
            self.merged = self.answers.get("merge", 0) == 0
            return subprocess.CompletedProcess(command, self.answers.get("merge", 0), "", "not possible to fast-forward")
        if said == "rev-parse HEAD" and self.merged:
            return subprocess.CompletedProcess(command, 0, NEW + "\n", "")
        if said in self.answers:
            answer = self.answers[said]
            code = answer if isinstance(answer, int) else 0
            return subprocess.CompletedProcess(command, code, "" if isinstance(answer, int) else answer + "\n", "failed")
        return subprocess.CompletedProcess(command, 0, "", "")

    def said(self):
        return [" ".join(command) for command in self.ran]


@pytest.fixture
def clone(tmp_path):
    (tmp_path / "ArtyCode" / ".git").mkdir(parents=True)
    (tmp_path / "ArtyCode" / "pyproject.toml").write_text("[project]\nname = 'arty-cli'\n")
    (tmp_path / "ArtyCode" / "arty_cli").mkdir()
    return tmp_path / "ArtyCode"


def world_for(clone, git, **changes):
    fields = {"package_dir": clone / "arty_cli", "prefix": str(clone / ".venv"), "executable": "/venv/bin/python",
              "run": git, "read_url": lambda url: '__version__ = "0.2.0"\n', "platform": "linux", **changes}
    return update.World(**fields)


def updated(world, *arguments):
    out = io.StringIO()
    return update.main(list(arguments), out=out, world=world), out.getvalue()


def test_a_clean_clone_on_its_default_branch_is_fetched_fast_forwarded_and_reinstalled(clone):
    git = Git()
    code, shown = updated(world_for(clone, git))
    said = git.said()
    assert code == 0
    assert f"git -C {clone} fetch origin" in said
    assert f"git -C {clone} merge --ff-only origin/main" in said
    assert said[-1] == f"/venv/bin/python -m pip install --quiet -e {clone}"
    assert said.index(f"git -C {clone} fetch origin") < said.index(f"git -C {clone} merge --ff-only origin/main")
    assert __version__ in shown and "git checkout" in shown and str(clone) in shown
    assert "aaaaaaa" in shown and "bbbbbbb" in shown and "3 commits" in shown and "reinstalled" in shown
    assert not any("sudo" in command for command in said)


def test_a_clone_with_uncommitted_changes_is_left_alone(clone):
    git = Git(**{"status --porcelain": " M arty_cli/cli.py"})
    code, shown = updated(world_for(clone, git))
    assert code == 1
    assert "uncommitted" in shown and "nothing was changed" in shown
    assert not any("fetch" in command or "merge" in command or "pip" in command for command in git.said())


def test_a_clone_on_another_branch_is_left_alone(clone):
    git = Git(**{"rev-parse --abbrev-ref HEAD": "tui"})
    code, shown = updated(world_for(clone, git))
    assert code == 1
    assert "tui" in shown and "main" in shown
    assert not any("merge" in command or "pip" in command for command in git.said())


def test_a_clone_that_is_already_up_to_date_says_so_and_does_not_reinstall(clone):
    git = Git(**{"rev-list --count HEAD..origin/main": "0"})
    code, shown = updated(world_for(clone, git))
    assert code == 0 and "up to date" in shown
    assert not any("merge" in command or "pip" in command for command in git.said())


def test_a_clone_that_cannot_fast_forward_is_reported_and_not_reinstalled(clone):
    git = Git(merge=1)
    code, shown = updated(world_for(clone, git))
    assert code == 1 and "fast-forward" in shown
    assert not any("pip" in command for command in git.said())


def test_a_fetch_that_fails_is_reported_plainly(clone):
    git = Git(**{"fetch origin": 1})
    code, shown = updated(world_for(clone, git))
    assert code == 1 and "fetch" in shown
    assert not any("merge" in command for command in git.said())


def test_check_on_a_clone_only_fetches_and_counts(clone):
    git = Git()
    code, shown = updated(world_for(clone, git), "--check")
    assert code == 0
    assert "3 commits" in shown and "arty update" in shown
    assert not any("merge" in command or "pip" in command for command in git.said())


@pytest.mark.parametrize("prefix, marker, word, command", [
    ("/home/sam/.local/share/arty-cli/venv", True, "installer", "/venv/bin/python -m pip install --upgrade"),
    ("/home/sam/.local/pipx/venvs/arty-cli", False, "pipx", "pipx upgrade arty-cli"),
    ("/home/sam/project/.venv", False, "pip", "/venv/bin/python -m pip install --upgrade"),
])
def test_other_installs_get_the_one_command_to_run(tmp_path, prefix, marker, word, command):
    home = tmp_path / prefix.lstrip("/")
    package = home / "lib" / "python3.13" / "site-packages" / "arty_cli"
    package.mkdir(parents=True)
    if marker:
        (home.parent / update.INSTALL_MARKER).write_text("")
    git = Git()
    code, shown = updated(update.World(package_dir=package, prefix=str(home), executable="/venv/bin/python", run=git,
                                       read_url=lambda url: '__version__ = "0.2.0"\n', platform="linux"))
    assert code == 0
    assert word in shown and command in shown and __version__ in shown
    assert git.ran == []
    assert "sudo" not in shown


def test_check_reads_the_newest_version_and_says_when_one_is_out(tmp_path):
    package = tmp_path / "site-packages" / "arty_cli"
    package.mkdir(parents=True)
    asked = []

    def read_url(url):
        asked.append(url)
        return '__version__ = "9.0.0"\n'
    world = update.World(package_dir=package, prefix=str(tmp_path), executable="python", run=Git(), read_url=read_url,
                         platform="linux")
    code, shown = updated(world, "--check")
    assert code == 0 and "9.0.0" in shown and __version__ in shown
    assert asked == [update.VERSION_URL] and asked[0].startswith("https://")


def test_check_says_when_this_is_the_newest_and_when_it_cannot_tell(tmp_path):
    package = tmp_path / "site-packages" / "arty_cli"
    package.mkdir(parents=True)
    same = update.World(package_dir=package, prefix=str(tmp_path), executable="python", run=Git(),
                        read_url=lambda url: f'__version__ = "{__version__}"\n', platform="linux")
    assert "newest" in updated(same, "--check")[1]

    def offline(url):
        raise OSError("no network")
    code, shown = updated(update.World(package_dir=package, prefix=str(tmp_path), executable="python", run=Git(),
                                       read_url=offline, platform="linux"), "--check")
    assert code == 1 and "Couldn't check" in shown


def test_versions_compare_by_number_not_by_text():
    assert update.is_newer("0.10.0", "0.9.9") and not update.is_newer("0.1.0", "0.1.0")
    assert not update.is_newer("not a version", "0.1.0")


def test_unknown_options_are_refused_with_the_usage():
    out = io.StringIO()
    assert update.main(["--force"], out=out, world=update.World(package_dir=Path("/x/arty_cli"))) == 2
    assert "arty update [--check | --skip]" in out.getvalue()


DAY = 24 * 60 * 60
NEWER, OLDER = "99.0.0", "0.0.1"


def release_answer(tag: str) -> str:
    return json.dumps({"tag_name": tag, "html_url": "https://example.test/elsewhere", "body": "notes"})


class Answer:
    def __init__(self, body: bytes):
        self.body = body
        self.asked_for = None

    def __enter__(self):
        return self

    def __exit__(self, *_details):
        return False

    def read(self, most: int) -> bytes:
        self.asked_for = most
        return self.body[:most]


class Opener:
    def __init__(self, body: bytes = b"{}"):
        self.answer = Answer(body)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        return self.answer


class Reads:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.count = 0

    def __call__(self):
        self.count += 1
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.mark.parametrize("tag, version", [("v1.4.0", "1.4.0"), ("1.2", "1.2"), ("desktop-v2.0.1", "2.0.1")])
def test_the_released_version_is_read_from_the_tag_with_or_without_a_prefix(tag, version):
    assert update.released_version(release_answer(tag)) == version


@pytest.mark.parametrize("text", ["not json", "[]", '{"tag_name": 5}', '{"name": "v1.0"}', '{"tag_name": "latest"}'])
def test_a_release_answer_without_a_usable_version_is_refused(text):
    with pytest.raises(ValueError):
        update.released_version(text)


def test_the_release_check_asks_github_once_over_https_with_a_short_timeout_and_no_user_data():
    opener = Opener(release_answer("v1.4.0").encode())
    assert update.released_version(real_read_release(opener=opener)) == "1.4.0"
    [(request, timeout)] = opener.requests
    assert request.full_url == "https://api.github.com/repos/drew-codes-things/ArtyCode/releases/latest"
    assert timeout == 5
    assert dict(request.header_items()) == {"User-agent": f"ArtyCode/{__version__}",
                                             "Accept": "application/vnd.github+json"}
    assert request.data is None


def test_the_release_check_refuses_plain_http():
    opener = Opener()
    with pytest.raises(ValueError, match="https"):
        real_read_release("http://api.github.com/repos/drew-codes-things/ArtyCode/releases/latest", opener=opener)
    assert opener.requests == []


def test_a_release_answer_over_256_kb_is_refused_without_reading_the_rest():
    opener = Opener(b"x" * (300 * 1024))
    with pytest.raises(ValueError, match="larger"):
        real_read_release(opener=opener)
    assert opener.answer.asked_for == 256 * 1024 + 1


def test_a_newer_release_gives_a_one_line_notice_with_the_release_page(tmp_path):
    line = update.startup_notice(tmp_path / "state.json", "", Reads(release_answer(f"v{NEWER}")), lambda: 1000.0)
    assert line.startswith(f"Version {NEWER} is available: https://github.com/drew-codes-things/ArtyCode/releases/latest")
    assert "arty update --skip" in line and "\n" not in line and "\u2014" not in line


@pytest.mark.parametrize("tag", [f"v{__version__}", f"v{OLDER}"])
def test_no_notice_when_this_version_is_the_newest(tmp_path, tag):
    assert update.startup_notice(tmp_path / "state.json", "", Reads(release_answer(tag)), lambda: 1000.0) == ""


def test_a_skipped_version_shows_no_notice_but_a_later_one_does(tmp_path):
    later = "100.0.0"
    reads = Reads(release_answer(f"v{NEWER}"), release_answer(f"v{later}"))
    assert update.startup_notice(tmp_path / "state.json", NEWER, reads, lambda: 1000.0) == ""
    assert later in update.startup_notice(tmp_path / "state.json", NEWER, reads, lambda: 1000.0 + DAY)


def test_github_is_asked_at_most_once_a_day_and_the_answer_is_remembered(tmp_path):
    state = tmp_path / "config" / "state.json"
    reads = Reads(release_answer(f"v{NEWER}"), release_answer(f"v{OLDER}"))
    assert NEWER in update.startup_notice(state, "", reads, lambda: 1000.0)
    assert NEWER in update.startup_notice(state, "", reads, lambda: 1000.0 + DAY - 1)
    assert reads.count == 1
    assert json.loads(state.read_text()) == {"checked_at": 1000.0, "latest": NEWER}
    if os.name == "posix":
        assert state.stat().st_mode & 0o777 == 0o600
    assert update.startup_notice(state, "", reads, lambda: 1000.0 + DAY) == ""
    assert reads.count == 2


@pytest.mark.parametrize("failure", [OSError("offline"), TimeoutError("timed out"), ValueError("bad json"),
                                     http.client.IncompleteRead(b"")])
def test_a_failed_check_shows_nothing_logs_one_line_and_is_tried_again_next_start(tmp_path, caplog, failure):
    state = tmp_path / "state.json"
    reads = Reads(failure, release_answer(f"v{NEWER}"))
    with caplog.at_level(logging.INFO, logger="arty_cli.update"):
        assert update.startup_notice(state, "", reads, lambda: 1000.0) == ""
    assert len(caplog.records) == 1 and caplog.records[0].levelno == logging.INFO
    assert not state.exists()
    assert NEWER in update.startup_notice(state, "", reads, lambda: 1001.0)


def test_a_damaged_state_file_means_checking_again(tmp_path):
    state = tmp_path / "state.json"
    state.write_text('{"checked_at": "soon", "latest": 7}')
    reads = Reads(release_answer(f"v{NEWER}"))
    assert NEWER in update.startup_notice(state, "", reads, lambda: 1000.0)
    assert reads.count == 1


def test_a_state_that_cannot_be_saved_still_shows_the_notice(tmp_path, caplog):
    blocked = tmp_path / "file"
    blocked.write_text("")
    with caplog.at_level(logging.INFO, logger="arty_cli.update"):
        line = update.startup_notice(blocked / "state.json", "", Reads(release_answer(f"v{NEWER}")), lambda: 1.0)
    assert NEWER in line and len(caplog.records) == 1


def test_the_startup_notice_is_found_in_the_background_and_shown_once():
    release = threading.Event()

    def find():
        release.wait(5)
        return "Version 9 is available"

    notice = update.StartupNotice(find).start()
    assert notice.take() == ""
    release.set()
    assert notice.take(5) == "Version 9 is available"
    assert notice.take(5) == ""


def skip_world(tmp_path):
    return update.World(package_dir=Path("/x/arty_cli"), config_folder=tmp_path)


def test_skip_hides_the_notice_for_the_newest_version_seen(tmp_path):
    (tmp_path / update.NOTICE_STATE_NAME).write_text(json.dumps({"checked_at": 1.0, "latest": NEWER}))
    code, shown = updated(skip_world(tmp_path), "--skip")
    assert code == 0 and f"version {NEWER} won't show again" in shown
    assert settings.load(tmp_path / settings.CONFIG_NAME, {}).skipped_update == NEWER


def test_skip_stores_only_the_skipped_version_when_the_setup_is_environment_only(tmp_path):
    (tmp_path / update.NOTICE_STATE_NAME).write_text(json.dumps({"checked_at": 1.0, "latest": NEWER}))
    updated(skip_world(tmp_path), "--skip")
    assert json.loads((tmp_path / settings.CONFIG_NAME).read_text()) == {"skipped_update": NEWER}


def test_skip_with_no_newer_version_seen_changes_nothing(tmp_path):
    code, shown = updated(skip_world(tmp_path), "--skip")
    assert code == 0 and "no notice to hide" in shown
    assert not (tmp_path / settings.CONFIG_NAME).exists()


def test_check_and_skip_together_are_refused_with_the_usage(tmp_path):
    code, shown = updated(skip_world(tmp_path), "--check", "--skip")
    assert code == 2 and "arty update [--check | --skip]" in shown


NESTING_BOMB = '{"tag_name":' + "[" * 100_000 + "]" * 100_000 + "}"
HUGE_VERSION = "9" * 5000
MOST_VERSION_CHARACTERS = 32


def test_a_deeply_nested_release_answer_shows_nothing_and_is_logged(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="arty_cli.update"):
        assert update.startup_notice(tmp_path / "state.json", "", lambda: NESTING_BOMB, lambda: 1000.0) == ""
    assert len(caplog.records) == 1
    assert not (tmp_path / "state.json").exists()


def test_a_release_tag_with_a_5000_digit_number_is_refused():
    with pytest.raises(ValueError, match="longer"):
        update.released_version(release_answer(f"v{HUGE_VERSION}"))


def test_a_version_of_the_longest_allowed_length_is_read():
    longest = "1" * MOST_VERSION_CHARACTERS
    assert update.released_version(release_answer(longest)) == longest


def test_a_5000_digit_remembered_version_is_ignored(tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"checked_at": 1000.0, "latest": HUGE_VERSION}))
    assert update.remembered_latest(json.loads(state.read_text())) == ""
    assert update.startup_notice(state, "", Reads(), lambda: 1000.0) == ""


def test_skip_ignores_a_5000_digit_remembered_version(tmp_path):
    (tmp_path / update.NOTICE_STATE_NAME).write_text(json.dumps({"checked_at": 1.0, "latest": HUGE_VERSION}))
    code, shown = updated(skip_world(tmp_path), "--skip")
    assert code == 0 and "no notice to hide" in shown
    assert not (tmp_path / settings.CONFIG_NAME).exists()


def test_a_failure_while_building_the_notice_line_is_caught_and_logged(tmp_path, caplog, monkeypatch):
    def broken(latest, skipped, current=__version__):
        raise ValueError("cannot compare")

    monkeypatch.setattr(update, "notice_line", broken)
    with caplog.at_level(logging.INFO, logger="arty_cli.update"):
        assert update.startup_notice(tmp_path / "state.json", "", Reads(release_answer(f"v{NEWER}")), lambda: 1000.0) == ""
    assert len(caplog.records) == 1


def test_the_background_notice_survives_any_failure_and_logs_it(caplog):
    def find():
        raise RuntimeError("unexpected")

    notice = update.StartupNotice(find)
    with caplog.at_level(logging.INFO, logger="arty_cli.update"):
        notice.look()
    assert notice.take() == ""
    assert len(caplog.records) == 1 and "unexpected" in caplog.records[0].getMessage()


def test_a_deeply_nested_state_file_means_checking_again_and_skip_changes_nothing(tmp_path):
    state = tmp_path / update.NOTICE_STATE_NAME
    state.write_text("[" * 100_000 + "]" * 100_000)
    assert update.read_notice_state(state) == {}
    code, shown = updated(skip_world(tmp_path), "--skip")
    assert code == 0 and "no notice to hide" in shown
    assert not (tmp_path / settings.CONFIG_NAME).exists()
