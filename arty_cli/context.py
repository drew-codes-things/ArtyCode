import json

CHARS_PER_TOKEN = 3.5
DEFAULT_WINDOW_TOKENS = 16_384
COMPACT_SHARE = 0.75


def counted_part(message: dict):
    return [message.get("role"), message.get("content"), message.get("tool_calls")]


def counted_chars(messages: list[dict]) -> int:
    return len(json.dumps([counted_part(message) for message in messages], ensure_ascii=False, default=str))


def estimated_tokens(messages: list[dict], client) -> int:
    return int(counted_chars(messages) / CHARS_PER_TOKEN)


def window_tokens(client) -> int:
    reported = getattr(client, "context_tokens", None)
    found = reported() if callable(reported) else None
    if isinstance(found, int) and found > 0:
        return found
    return DEFAULT_WINDOW_TOKENS


def compact_limit(client) -> int:
    return int(window_tokens(client) * COMPACT_SHARE)
