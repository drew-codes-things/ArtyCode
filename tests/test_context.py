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

