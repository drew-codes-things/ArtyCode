import os
import re
from pathlib import Path

from arty_cli import eyes, tools

NOTEBOOK_TOOL = "notebook_edit"
PICTURE_TOOLS = (eyes.VIEW_IMAGE, eyes.SCREENSHOT)
SITUATIONAL_TOOLS = (NOTEBOOK_TOOL, *PICTURE_TOOLS)
MAX_SCANNED_FILES = 5000
NOTEBOOK_SUFFIXES = {tools.NOTEBOOK_SUFFIX}
PICTURE_SUFFIXES = {".html", ".htm", ".css", ".jsx", ".tsx", ".vue", ".svelte", ".png", ".jpg", ".jpeg", ".gif",
                    ".webp", ".svg"}
NOTEBOOK_WORDS = re.compile(r"\b(notebooks?|jupyter|ipynb)\b", re.I)
PICTURE_WORDS = re.compile(r"\b(pictures?|images?|screenshots?|photos?|web ?pages?|websites?|sites?|html|css|"
                           r"front[- ]?end|ui|layout|browser|png|jpe?g|svg|gif)\b", re.I)


def suffixes_in(root: Path) -> set[str]:
    found, scanned = set(), 0
    for current, folders, files in os.walk(root):
        folders[:] = [name for name in folders if name not in tools.SKIPPED_FOLDERS and not name.startswith(".")]
        for name in files:
            found.add(Path(name).suffix.lower())
            scanned += 1
            if scanned >= MAX_SCANNED_FILES:
                return found
    return found


def wanted_tools(root: Path, request: str = "") -> set[str]:
    suffixes = suffixes_in(root)
    wanted = set()
    if suffixes & NOTEBOOK_SUFFIXES or NOTEBOOK_WORDS.search(request):
        wanted.add(NOTEBOOK_TOOL)
    if suffixes & PICTURE_SUFFIXES or PICTURE_WORDS.search(request):
        wanted.update(PICTURE_TOOLS)
    return wanted


def kept(definitions: list[dict], situational) -> list[dict]:
    if situational is None:
        return definitions
    return [definition for definition in definitions
            if definition["function"]["name"] not in SITUATIONAL_TOOLS or definition["function"]["name"] in situational]
