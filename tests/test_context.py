from arty_cli import context


class Client:
    def __init__(self, window=None, reply=None):
        self.window = window
        self.reply = reply

    def context_tokens(self):
        return self.window

    def reply_tokens(self):
        return self.reply


TOOLS = [{"type": "function", "function": {"name": "read_file", "description": "x" * 3500}}]


def test_the_default_window_matches_the_brains_own_context_size():
    assert context.window_tokens(Client()) == 12_288


def test_the_summary_point_leaves_room_for_the_tools_and_the_reply():
    limit = context.compact_limit(Client(), TOOLS)
    assert limit + context.tool_tokens(TOOLS) + context.REPLY_ROOM_TOKENS <= context.DEFAULT_WINDOW_TOKENS
    assert limit > context.DEFAULT_WINDOW_TOKENS // 2


def test_a_bigger_window_moves_the_summary_point_later():
    assert context.compact_limit(Client(32_768), TOOLS) > context.compact_limit(Client(), TOOLS)


def test_a_tiny_window_still_leaves_some_room_for_the_conversation():
    assert context.compact_limit(Client(2_000), TOOLS) >= 2_000 * context.SMALLEST_SHARE


def test_the_reply_room_follows_the_brain_and_falls_back_to_2048():
    assert context.reply_room(Client(24_576)) == context.REPLY_ROOM_TOKENS == 2_048
    assert context.reply_room(Client(24_576, 4_096)) == 4_096
    assert context.compact_limit(Client(24_576, 4_096), TOOLS) < context.compact_limit(Client(24_576), TOOLS)


MESSAGES = [{"role": "user", "content": "word " * 700}]


def test_a_brain_counting_more_tokens_than_estimated_moves_the_estimate_up_at_once():
    client = Client(24_576)
    before = context.estimated_tokens(MESSAGES, client)
    context.calibrate(client, MESSAGES, [], {"usage": {"prompt_tokens": before * 3 // 2}})
    assert abs(context.estimated_tokens(MESSAGES, client) - before * 3 // 2) <= 2


def test_a_smaller_count_is_ignored_because_a_cached_prompt_reports_fewer_tokens():
    client = Client(24_576)
    before = context.estimated_tokens(MESSAGES, client)
    context.calibrate(client, MESSAGES, [], {"usage": {"prompt_tokens": before // 3}})
    context.calibrate(client, MESSAGES, [], {})
    assert context.estimated_tokens(MESSAGES, client) == before


def test_the_ratio_never_goes_below_a_floor():
    client = Client(24_576)
    context.calibrate(client, MESSAGES, [], {"usage": {"prompt_tokens": 10_000_000}})
    assert context.chars_per_token(client) == context.SMALLEST_CHARS_PER_TOKEN


def test_a_prompt_near_the_window_is_reported():
    assert context.near_window(Client(24_576), {"usage": {"prompt_tokens": 23_000}})
    assert not context.near_window(Client(24_576), {"usage": {"prompt_tokens": 12_000}})
    assert not context.near_window(Client(24_576), {})
