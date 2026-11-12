import shutil
import subprocess
import sys
import threading

from arty_cli import settings

DESKTOP_PROGRAM = "notify-send"
APP_NAME = "ArtyCode"
MOST_NOTICE_CHARS = 200
LINUX = "linux"


def wanted(config) -> bool:
    return not isinstance(config, settings.Settings) or settings.is_on(config, "notify")


def desktop_command(title: str, text: str, platform: str = sys.platform, find=shutil.which) -> list[str] | None:
    if not platform.startswith(LINUX):
        return None
    program = find(DESKTOP_PROGRAM)
    if program is None:
        return None
    return [program, f"--app-name={APP_NAME}", title, text[:MOST_NOTICE_CHARS]]


class Notifier:
    def __init__(self, bell, start=subprocess.Popen, command=desktop_command):
        self.bell = bell
        self.start = start
        self.command = command

    def notice(self, title: str, text: str) -> bool:
        self.bell()
        command = self.command(title, text)
        if command is None:
            return False
        try:
            process = self.start(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError:
            return False
        threading.Thread(target=process.wait, daemon=True).start()
        return True
