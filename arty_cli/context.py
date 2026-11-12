import json

CHARS_PER_TOKEN = 3.5
SMALLEST_CHARS_PER_TOKEN = 2.0
RATIO_ATTRIBUTE = "chars_per_token"
MIN_CALIBRATION_TOKENS = 200
NEAR_WINDOW_SHARE = 0.9
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


def tool_chars(offered: list[dict]) -> int:
    return len(json.dumps(offered, ensure_ascii=False, default=str)) if offered else 0


def chars_per_token(client) -> float:
    found = getattr(client, RATIO_ATTRIBUTE, None)
    return found if isinstance(found, (int, float)) and not isinstance(found, bool) and found > 0 else CHARS_PER_TOKEN


def estimated_tokens(messages: list[dict], client) -> int:
    return int(counted_chars(messages) / chars_per_token(client))


def tool_tokens(offered: list[dict], client=None) -> int:
    return int(tool_chars(offered) / chars_per_token(client))


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
    room = int((window - tool_tokens(offered or [], client) - reply_room(client)) * SAFETY_SHARE)
    return max(room, int(window * SMALLEST_SHARE))


def result_chars(limit_tokens: int, client=None) -> int:
    return max(SMALLEST_RESULT_CHARS, int(limit_tokens * RESULT_SHARE * chars_per_token(client)))


def room_chars(limit_tokens: int, used_tokens: int, client=None) -> int:
    room = (limit_tokens - used_tokens) * chars_per_token(client) * CUT_RESULT_SHARE
    return max(SMALLEST_RESULT_CHARS, int(room))


def prompt_tokens_of(reply: dict) -> int | None:
    usage = reply.get("usage") if isinstance(reply, dict) else None
    counted = usage.get("prompt_tokens") if isinstance(usage, dict) else None
    return counted if isinstance(counted, int) and not isinstance(counted, bool) and counted > 0 else None


def calibrate(client, messages: list[dict], offered: list[dict], reply: dict) -> None:
    counted = prompt_tokens_of(reply)
    if counted is None or counted < MIN_CALIBRATION_TOKENS:
        return
    measured = (counted_chars(messages) + tool_chars(offered)) / counted
    if measured < chars_per_token(client):
        setattr(client, RATIO_ATTRIBUTE, max(measured, SMALLEST_CHARS_PER_TOKEN))


def near_window(client, reply: dict) -> bool:
    counted = prompt_tokens_of(reply)
    return counted is not None and counted >= window_tokens(client) * NEAR_WINDOW_SHARE
