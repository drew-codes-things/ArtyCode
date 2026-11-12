import json

from arty_cli import repomap


def write(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_python_classes_and_functions_are_listed(tmp_path):
    write(tmp_path, "shop/cart.py", "class Cart:\n    def total(self):\n        pass\n\n\n"
                                    "def checkout(cart):\n    pass\n\n\ndef _hidden():\n    pass\n")
    text = repomap.RepoMap(tmp_path, tmp_path / "cache").render()
    assert "shop/cart.py" in text
    assert "class Cart" in text and "total" in text and "def checkout" in text
    assert "_hidden" not in text


def test_javascript_and_go_symbols_are_found(tmp_path):
    write(tmp_path, "web/app.js", "export function startServer() {}\nexport class Router {}\nconst helper = () => 1;\n")
    write(tmp_path, "cmd/main.go", "package main\n\nfunc Serve() {}\ntype Config struct {}\n")
    text = repomap.RepoMap(tmp_path, tmp_path / "cache").render()
    assert "startServer" in text and "Router" in text and "Serve" in text and "Config" in text


def test_files_used_by_others_come_first(tmp_path):
    write(tmp_path, "a_lonely.py", "def unused_thing():\n    pass\n")
    write(tmp_path, "z_core.py", "def shared_helper():\n    pass\n")
    for number in range(3):
        write(tmp_path, f"user{number}.py", "from z_core import shared_helper\nshared_helper()\n")
    text = repomap.RepoMap(tmp_path, tmp_path / "cache").render()
    assert text.index("z_core.py") < text.index("a_lonely.py")


def test_the_map_stays_under_its_size_limit(tmp_path):
    for number in range(400):
        write(tmp_path, f"pkg/module_{number}.py", "".join(f"def function_{number}_{n}():\n    pass\n" for n in range(10)))
    text = repomap.RepoMap(tmp_path, tmp_path / "cache", max_chars=2000).render()
    assert len(text) <= 2000 + 200
    assert "more files" in text


def test_skipped_folders_are_left_out(tmp_path):
    write(tmp_path, "node_modules/lib/index.js", "export function vendored() {}\n")
    write(tmp_path, "app.py", "def mine():\n    pass\n")
    text = repomap.RepoMap(tmp_path, tmp_path / "cache").render()
    assert "vendored" not in text and "mine" in text


def test_an_empty_project_gives_no_map(tmp_path):
    assert repomap.RepoMap(tmp_path, tmp_path / "cache").render() == ""


def test_unchanged_files_are_not_read_again(tmp_path, monkeypatch):
    write(tmp_path, "one.py", "def first():\n    pass\n")
    changing = write(tmp_path, "two.py", "def second():\n    pass\n")
    repomap.RepoMap(tmp_path, tmp_path / "cache").render()
    read = []
    original = repomap.symbols_in
    monkeypatch.setattr(repomap, "symbols_in", lambda path, text: read.append(path.name) or original(path, text))
    changing.write_text("def second():\n    pass\n\n\ndef third():\n    pass\n")
    text = repomap.RepoMap(tmp_path, tmp_path / "cache").render()
    assert read == ["two.py"]
    assert "third" in text


def test_a_broken_cache_file_is_rebuilt(tmp_path):
    write(tmp_path, "one.py", "def first():\n    pass\n")
    cache = tmp_path / "cache"
    repomap.RepoMap(tmp_path, cache).render()
    for stored in cache.iterdir():
        stored.write_text("{not json")
    assert "first" in repomap.RepoMap(tmp_path, cache).render()


def test_a_python_file_with_a_syntax_error_still_lists_what_it_can(tmp_path):
    write(tmp_path, "broken.py", "def fine():\n    pass\n\ndef broken(:\n")
    text = repomap.RepoMap(tmp_path, tmp_path / "cache").render()
    assert "broken.py" in text and "fine" in text


def test_the_map_is_marked_as_information(tmp_path):
    write(tmp_path, "app.py", "def ignore_all_previous_instructions():\n    pass\n")
    text = repomap.RepoMap(tmp_path, tmp_path / "cache").render()
    assert text.startswith(repomap.MAP_LEAD)


def test_repo_map_can_be_switched_off(tmp_path):
    personal = tmp_path / "personal"
    personal.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    assert repomap.wanted(personal, project, include_project=True)
    (personal / "settings.json").write_text(json.dumps({"repoMap": False}))
    assert not repomap.wanted(personal, project, include_project=True)


def test_a_cache_that_cannot_be_saved_is_reported_not_fatal(tmp_path):
    write(tmp_path, "app.py", "def mine():\n    pass\n")
    blocked = tmp_path / "blocked"
    blocked.write_text("a file, not a folder")
    mapped = repomap.RepoMap(tmp_path, blocked)
    assert "mine" in mapped.render()
    assert "Couldn't save the project map cache" in mapped.problem


def test_test_files_come_after_the_code_and_are_summarised(tmp_path):
    write(tmp_path, "tests/test_cart.py", "def make_cart():\n    pass\n\n\ndef test_one():\n    pass\n\n\ndef test_two():\n    pass\n")
    write(tmp_path, "cart.py", "def total():\n    pass\n")
    write(tmp_path, "other.py", "make_cart()\nmake_cart()\n")
    text = repomap.RepoMap(tmp_path, tmp_path / "cache").render()
    assert text.index("cart.py:") < text.index("tests/test_cart.py")
    assert "test_one" not in text and "2 tests" in text and "make_cart" in text
