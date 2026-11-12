import pytest

from arty_cli import brain, websearch


class Client:
    def __init__(self):
        self.calls = []

    def search_web(self, query, results):
        self.calls.append(("desktop", query, results))
        return "desktop page"

    def brain_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return "brain results"


def test_the_desktop_is_the_default_place_to_search():
    client = Client()
    assert websearch.searcher(client, websearch.DESKTOP)("rust traits", 5) == "desktop page"
    assert client.calls == [("desktop", "rust traits", 5)]


def test_brain_searches_skip_the_desktop():
    client = Client()
    assert websearch.searcher(client, websearch.BRAIN)("rust traits", 5) == "brain results"
    assert client.calls == [(brain.SEARCH_TOOL, {"query": "rust traits", "results": 5})]


def test_an_unknown_place_is_refused_by_name():
    with pytest.raises(brain.BrainError, match="search_with must be one of desktop, brain"):
        websearch.searcher(Client(), "carrier-pigeon")
