from arty_cli import chat_session, suggest

GROUPS = (("Settings", {"/model [number|name]": "switch model", "/mode [name]": "how much is asked",
                        "/mcp, /skills": "servers and skills", "Ctrl-D, /exit": "leave", "@path": "attach"}),)


def test_every_slash_name_in_the_help_becomes_a_choice_with_its_meaning():
    choices = suggest.slash_choices(GROUPS, {"/deploy": "ship it (project)"})
    assert choices == {"/model": "switch model", "/mode": "how much is asked", "/mcp": "servers and skills",
                       "/skills": "servers and skills", "/exit": "leave", "/deploy": "ship it (project)"}


def test_only_the_start_of_a_line_offers_commands():
    choices = suggest.slash_choices(GROUPS, {})
    assert suggest.matching("/mo", choices) == ["/model", "/mode"]
    assert suggest.matching("/", choices) == list(choices)
    assert suggest.matching("/model gpt", choices) == []
    assert suggest.matching("look in src/", choices) == []


def test_a_mistyped_command_gets_the_nearest_guess():
    choices = suggest.slash_choices(GROUPS, {})
    assert suggest.closest("/skils", choices) == "/skills"
    assert suggest.closest("/zzzzzz", choices) is None
    assert "Did you mean /skills?" in chat_session.unknown_command_note("/skils", choices)
    assert "Did you mean" not in chat_session.unknown_command_note("/zzzzzz", choices)


def test_the_menu_lines_up_meanings_fits_the_width_and_counts_the_rest():
    choices = {f"/c{number:02}": "a long meaning " * 10 for number in range(suggest.MENU_ROWS + 3)}
    lines = suggest.menu_lines(list(choices), choices, 40)
    assert len(lines) == suggest.MENU_ROWS + 1 and all(len(line) <= 39 for line in lines)
    assert lines[0].startswith("  /c00  a long") and "and 3 more" in lines[-1]
    assert suggest.menu_lines([], choices, 40) == []


def test_every_command_the_session_knows_is_offered():
    choices = suggest.slash_choices(chat_session.HELP_GROUPS, {})
    for name in ("/clear", "/model", "/mode", "/plan", "/status", "/todos", "/memory", "/compact", "/undo",
                 "/rewind", "/resume", "/remote", "/rules", "/mcp", "/skills", "/helpers", "/hooks", "/help",
                 "/exit", "/init", "/review"):
        assert name in choices


MODELS = {"arty/qwen3:14b": "14B, all tools", "arty/mimo-v2.6:9b": "9B, sees images", "own/Coder": ""}


def test_after_slash_model_the_brain_s_models_are_offered_with_what_they_do():
    found, meanings, partial = suggest.suggestions("/model arty/", lambda: {}, lambda: MODELS)
    assert found == ["arty/qwen3:14b", "arty/mimo-v2.6:9b"] and meanings is MODELS and partial == "arty/"
    assert suggest.suggestions("/model ", lambda: {}, lambda: MODELS)[0] == list(MODELS)
    assert suggest.suggestions("/model arty/qwen3:14b now", lambda: {}, lambda: MODELS)[0] == []


def test_without_a_model_list_slash_model_offers_nothing_more():
    assert suggest.suggestions("/model ", lambda: {"/model": "switch"}, None)[0] == []
    assert suggest.suggestions("/mo", lambda: {"/model": "switch"}, None)[0] == ["/model"]
