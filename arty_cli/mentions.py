import base64
import mimetypes
import re

from arty_cli import tools

MENTION = re.compile(r"(?<![\w@])@([^\s@]+)")
IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
MAX_IMAGE_BYTES = 5_000_000


def mentioned_files(text: str, workspace):
    for match in MENTION.finditer(text):
        name = match.group(1).rstrip(".,;:!?)")
        try:
            path = workspace.resolve(name)
        except tools.ToolError:
            continue
        if path.is_file() and path.is_relative_to(workspace.root):
            yield name, path


def image_part(path) -> dict | None:
    kind = mimetypes.guess_type(path.name)[0]
    if kind not in IMAGE_TYPES or path.stat().st_size > MAX_IMAGE_BYTES:
        return None
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{kind};base64,{encoded}"}}


def text_block(name: str, path, limit: int) -> str | None:
    try:
        content = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None
    return f'<file path="{name}">\n{content[:limit]}</file>'


def expand(text: str, workspace):
    blocks, pictures = [], []
    for name, path in mentioned_files(text, workspace):
        picture = image_part(path)
        if picture:
            pictures.append(picture)
            continue
        block = text_block(name, path, workspace.result_chars)
        if block:
            blocks.append(block)
    combined = "\n\n".join([text, *blocks])
    if not pictures:
        return combined
    return [{"type": "text", "text": combined}, *pictures]
