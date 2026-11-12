from arty_cli import brain

DESKTOP = "desktop"
BRAIN = "brain"
PLACES = (DESKTOP, BRAIN)


def searcher(client, place: str):
    if place not in PLACES:
        raise brain.BrainError(f"search_with must be one of {', '.join(PLACES)}, not {place!r}.")
    if place == BRAIN:
        return lambda query, results: client.brain_tool(brain.SEARCH_TOOL, {"query": query, "results": results})
    return client.search_web
