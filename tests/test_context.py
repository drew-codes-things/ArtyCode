from arty_cli import context


class Client:
    def __init__(self, window=None):
        self.window = window

    def context_tokens(self):
        return self.window


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
