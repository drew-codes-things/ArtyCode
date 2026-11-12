import json

CHARS_PER_TOKEN = 3.5
DEFAULT_WINDOW_TOKENS = 12_288
REPLY_ROOM_TOKENS = 2_048
SAFETY_SHARE = 0.9
SMALLEST_SHARE = 0.25
RESULT_SHARE = 0.25
SMALLEST_RESULT_CHARS = 2_000
CUT_RESULT_SHARE = 0.9


def counted_part(message: dict):
    return [message.get("role"), message.get("content"), message.get("tool_calls")]


def counted_chars(messages: list[dict]) -> int:
    return len(json.dumps([counted_part(message) for message in messages], ensure_ascii=False, default=str))


def estimated_tokens(messages: list[dict], client) -> int:
    return int(counted_chars(messages) / CHARS_PER_TOKEN)


def tool_tokens(offered: list[dict]) -> int:
    return int(len(json.dumps(offered, ensure_ascii=False, default=str)) / CHARS_PER_TOKEN) if offered else 0


def text_tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN)


def reported_number(client, name: str) -> int | None:
    reported = getattr(client, name, None)
    found = reported() if callable(reported) else None
    return found if isinstance(found, int) and not isinstance(found, bool) and found > 0 else None


def window_tokens(client) -> int:
    return reported_number(client, "context_tokens") or DEFAULT_WINDOW_TOKENS


def reply_room(client) -> int:
    return reported_number(client, "reply_tokens") or REPLY_ROOM_TOKENS


def compact_limit(client, offered: list[dict] | None = None) -> int:
    window = window_tokens(client)
    room = int((window - tool_tokens(offered or []) - reply_room(client)) * SAFETY_SHARE)
    return max(room, int(window * SMALLEST_SHARE))


def result_chars(limit_tokens: int) -> int:
    return max(SMALLEST_RESULT_CHARS, int(limit_tokens * RESULT_SHARE * CHARS_PER_TOKEN))


def room_chars(limit_tokens: int, used_tokens: int) -> int:
    return max(SMALLEST_RESULT_CHARS, int((limit_tokens - used_tokens) * CHARS_PER_TOKEN * CUT_RESULT_SHARE))
