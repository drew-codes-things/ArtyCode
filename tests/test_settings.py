import json
import os
import stat
import sys
from pathlib import Path

import pytest

from arty_cli import settings


def test_a_fresh_install_uses_the_default_model_and_is_not_ready(tmp_path):
    loaded = settings.load(tmp_path / "missing.json", environ={})
    assert loaded.model == settings.DEFAULT_MODEL
    assert not loaded.is_ready()


def test_the_environment_overrides_the_config_file(tmp_path):
    path = tmp_path / "config.json"
    settings.save(settings.Settings(base_url="http://brain.lan:8081/v1", api_key="from-file"), path)
    loaded = settings.load(path, environ={"ARTY_API_KEY": "from-env", "ARTY_CLI_MODEL": "arty/other"})
    assert (loaded.base_url, loaded.api_key, loaded.model) == ("http://brain.lan:8081/v1", "from-env", "arty/other")
    assert loaded.is_ready()


def test_a_remote_address_alone_is_enough(tmp_path):
    loaded = settings.load(tmp_path / "none.json", environ={"ARTY_REMOTE_URL": "https://arty.example/v1", "ARTY_API_KEY": "k"})
    assert loaded.is_ready()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_the_saved_config_is_private(tmp_path):
    path = settings.save(settings.Settings(api_key="secret"), tmp_path / "deep" / "config.json")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_windows_keeps_the_config_in_appdata():
    folder = settings.config_dir(environ={"APPDATA": r"C:\Users\me\AppData\Roaming"}, platform="win32")
    assert str(folder).endswith("arty-cli")
    assert "Roaming" in str(folder)


def test_linux_follows_xdg_config_home():
    folder = settings.config_dir(environ={"XDG_CONFIG_HOME": "/tmp/cfg"}, platform="linux")
    assert folder == Path("/tmp/cfg") / "arty-cli"


def test_a_broken_config_file_says_where_it_is(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{not json")
    with pytest.raises(settings.SettingsError, match=str(path).replace("\\", "\\\\")):
        settings.load(path, environ={})


ARTY_ONLY_FIELDS = {"base_url", "remote_url", "api_key", "access_client_id", "access_client_secret", "model"}


def test_unknown_keys_in_a_saved_config_are_ignored_and_dropped_on_the_next_save(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"base_url": "http://brain.lan:8081/v1", "api_key": "k", "provider": "other",
                                "other_api_key": "old-a", "other_model": "big-model", "web_search": "on"}))
    loaded = settings.load(path, environ={"ARTY_CLI_PROVIDER": "other", "OTHER_API_KEY": "env-a"})
    assert loaded.is_ready()
    assert all(secret not in repr(loaded) for secret in ("old-a", "env-a"))
    settings.remember(path, model="arty/qwen3:14b")
    assert set(json.loads(path.read_text())) == ARTY_ONLY_FIELDS


def test_the_default_model_is_the_one_that_writes_real_tool_calls():
    assert settings.DEFAULT_MODEL == "arty/qwen3:14b"


def test_a_saved_old_default_moves_to_the_new_default_but_a_chosen_model_stays(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"base_url": "http://brain.lan:8081/v1", "model": "arty/qwen2.5-coder:14b"}))
    assert settings.load(path, environ={}).model == settings.DEFAULT_MODEL
    assert settings.load(path, environ={"ARTY_CLI_MODEL": "arty/qwen2.5-coder:14b"}).model == "arty/qwen2.5-coder:14b"
    path.write_text(json.dumps({"base_url": "http://brain.lan:8081/v1", "model": "arty/qwen2.5:72b"}))
    assert settings.load(path, environ={}).model == "arty/qwen2.5:72b"


def disk_fills_up(data, handle, **options):
    handle.write('{"half')
    raise OSError(28, "No space left on device")


def test_a_save_that_fails_part_way_leaves_the_old_config_whole(tmp_path, monkeypatch):
    path = settings.save(settings.Settings(api_key="kept"), tmp_path / "config.json")
    monkeypatch.setattr(settings.json, "dump", disk_fills_up)
    with pytest.raises(OSError):
        settings.save(settings.Settings(api_key="new"), path)
    monkeypatch.undo()
    assert settings.load(path, environ={}).api_key == "kept"
    assert [found.name for found in tmp_path.iterdir()] == ["config.json"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_saving_over_an_open_config_makes_it_private(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{}")
    path.chmod(0o644)
    settings.save(settings.Settings(api_key="secret"), path)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_limits_can_come_from_the_environment_as_whole_numbers(tmp_path):
    loaded = settings.load(tmp_path / "none.json", environ={"ARTY_CLI_CONTEXT_TOKENS": "32768",
                                                             "ARTY_CLI_MAX_COMMAND_SECONDS": "3600"})
    assert settings.positive_number(loaded, "context_tokens") == 32768
    assert settings.positive_number(loaded, "max_command_seconds") == 3600
    assert settings.positive_number(settings.Settings(), "context_tokens") is None


@pytest.mark.parametrize("given", ["lots", "0", "-5", "1.5"])
def test_a_limit_that_is_not_a_whole_number_says_where_to_fix_it(given):
    with pytest.raises(settings.SettingsError, match="ARTY_CLI_MAX_COMMAND_SECONDS"):
        settings.positive_number(settings.Settings(max_command_seconds=given), "max_command_seconds")


def test_a_saved_config_keeps_set_limits_and_leaves_unset_ones_out(tmp_path):
    path = tmp_path / "config.json"
    settings.save(settings.Settings(api_key="k", context_tokens="24576"), path)
    saved = json.loads(path.read_text())
    assert saved["context_tokens"] == "24576" and "max_command_seconds" not in saved


@pytest.mark.parametrize("given, wanted", [("", True), ("on", True), ("OFF", False), ("false", False), ("0", False),
                                           ("yes", True)])
def test_the_finish_check_is_on_unless_switched_off(given, wanted):
    assert settings.is_on(settings.Settings(finish_check=given), "finish_check") is wanted


def test_the_finish_check_can_be_switched_off_from_the_environment(tmp_path):
    loaded = settings.load(tmp_path / "none.json", {"ARTY_CLI_FINISH_CHECK": "off"})
    assert settings.is_on(loaded, "finish_check") is False


def test_a_finish_check_setting_that_is_not_on_or_off_says_where_to_fix_it():
    with pytest.raises(settings.SettingsError, match="ARTY_CLI_FINISH_CHECK"):
        settings.is_on(settings.Settings(finish_check="sometimes"), "finish_check")
