import os
import shutil
import sys
from pathlib import Path

SERIAL = "PRETEND0001"
MODEL = "Pretend Pixel"
PHONE_ROOT = "/sdcard"


def phone_path(phone: Path, given: str) -> Path:
    if not given.startswith(PHONE_ROOT):
        raise SystemExit(f"adb: error: remote object '{given}' does not exist")
    return phone / given[len(PHONE_ROOT):].lstrip("/")


def shell(phone: Path, words: list[str]) -> int:
    if words[:2] == ["getprop", "ro.product.model"]:
        print(MODEL)
        return 0
    if words and words[0] == "ls":
        folder = phone_path(phone, words[-1])
        for entry in sorted(folder.iterdir()):
            print(entry.name)
        return 0
    print(f"/system/bin/sh: {words[0] if words else ''}: inaccessible or not found", file=sys.stderr)
    return 127


def pull(phone: Path, words: list[str]) -> int:
    source = phone_path(phone, words[0])
    target = Path(words[1] if len(words) > 1 else ".")
    target = target / source.name if target.is_dir() else target
    shutil.copyfile(source, target)
    print(f"{words[0]}: 1 file pulled, 0 skipped.")
    return 0


def main(arguments: list[str]) -> int:
    phone = Path(os.environ["FAKE_ADB_PHONE"])
    with open(os.environ["FAKE_ADB_LOG"], "a", encoding="utf-8") as log:
        log.write(" ".join(arguments) + "\n")
    if arguments[:2] == ["-s", SERIAL]:
        arguments = arguments[2:]
    if arguments == ["devices"]:
        print(f"List of devices attached\n{SERIAL}\tdevice\n")
        return 0
    if arguments and arguments[0] == "shell":
        return shell(phone, arguments[1:])
    if arguments and arguments[0] == "pull":
        return pull(phone, arguments[1:])
    print(f"adb: unknown command {' '.join(arguments)}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
