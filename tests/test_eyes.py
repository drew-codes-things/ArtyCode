import base64
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from arty_cli import brain, eyes, headless, settings, tools
from tests import pictures
from tests.fake_servers import serving

needs_browser = pytest.mark.skipif(headless.find_browser() is None, reason="no Chrome, Edge or Chromium installed")
VISION_ANSWER = (200, json.dumps({"choices": [{"message": {"role": "assistant", "content": "A red square."}}]}))


def no_browser():
    return None


def client_for(url: str) -> brain.Brain:
    return brain.Brain(settings.Settings(remote_url=f"{url}/v1", api_key="key", model="arty/qwen3:14b"))


def sent_picture(request) -> tuple[str, bytes]:
    parts = request["body"]["messages"][0]["content"]
    header, encoded = parts[1]["image_url"]["url"].split(",", 1)
    return header, base64.b64decode(encoded)


@pytest.mark.parametrize("data, facts", [
    (pictures.png(4, 3), (eyes.PNG, 4, 3)),
    (pictures.gif(20, 10), (eyes.GIF, 20, 10)),
    (pictures.jpeg(640, 480), (eyes.JPEG, 640, 480)),
    (pictures.webp_lossy(300, 200), (eyes.WEBP, 300, 200)),
    (pictures.webp_lossless(301, 201), (eyes.WEBP, 301, 201)),
    (pictures.webp_extended(1920, 1080), (eyes.WEBP, 1920, 1080)),
])
def test_the_kind_and_size_of_a_picture_come_from_its_bytes(data, facts):
    assert eyes.picture_facts(data) == facts


@pytest.mark.parametrize("data", [b"", b"hello world, not a picture", b"\xff\xd8\xff"])
def test_something_that_isnt_a_picture_is_refused(data):
    with pytest.raises(tools.ToolError):
        eyes.picture_facts(data)


def test_big_pictures_shrink_to_fit_and_small_ones_stay():
    assert eyes.shrunk_size(2560, 1440) == (1280, 720)
    assert eyes.shrunk_size(1000, 4000) == (320, 1280)
    assert eyes.shrunk_size(800, 600) == (800, 600)


def test_addresses_files_and_bare_local_addresses_are_understood(tmp_path):
    (tmp_path / "site").mkdir()
    (tmp_path / "site" / "index.html").write_text("<p>hi</p>")
    assert eyes.page_address("https://example.com/a", tmp_path) == "https://example.com/a"
    assert eyes.page_address("localhost:8000/app", tmp_path) == "http://localhost:8000/app"
    assert eyes.page_address("192.168.0.50:8000", tmp_path) == "http://192.168.0.50:8000"
    assert eyes.page_address("site/index.html", tmp_path) == (tmp_path / "site" / "index.html").as_uri()
    for wrong in ("", "ftp://example.com/x", "file://server/share/x.html", "site/missing.html"):
        with pytest.raises(tools.ToolError):
            eyes.page_address(wrong, tmp_path)


@pytest.mark.parametrize("host, local", [("localhost", True), ("127.0.0.5", True), ("::1", True),
                                         ("app.localhost", True), ("192.168.0.50", False),
                                         ("example.com", False), ("", False)])
def test_this_computer_is_told_apart_from_the_network(host, local):
    assert eyes.is_this_computer(host) is local


def test_a_picture_is_sent_to_the_brains_vision_model_with_the_question(tmp_path):
    picture = pictures.png(4, 3)
    (tmp_path / "shot.png").write_bytes(picture)
    with serving([VISION_ANSWER], "application/json") as (url, recorder):
        result = eyes.Eyes(tools.Workspace(tmp_path), client_for(url), no_browser).run(
            "view_image", {"path": "shot.png", "question": "What colour is it?"})
    request = recorder.requests[0]
    assert request["path"] == "/v1/chat/completions"
    assert request["headers"]["X-Arty-Direct-Vision"] == "1"
    assert request["headers"]["Authorization"] == "Bearer key"
    assert request["body"]["stream"] is False and request["body"]["model"] == "arty/qwen3:14b"
    assert request["body"]["messages"][0]["content"][0]["text"].startswith("What colour is it?")
    assert sent_picture(request) == ("data:image/png;base64", picture)
    assert result.startswith("shot.png (4x3)")
    assert eyes.LOOKED_LEAD in result and result.endswith("A red square.")


def test_without_a_question_the_picture_is_described(tmp_path):
    (tmp_path / "shot.png").write_bytes(pictures.png(2, 2))
    with serving([VISION_ANSWER], "application/json") as (url, recorder):
        eyes.Eyes(tools.Workspace(tmp_path), client_for(url), no_browser).run("view_image", {"path": "shot.png"})
    assert recorder.requests[0]["body"]["messages"][0]["content"][0]["text"].startswith(eyes.DEFAULT_QUESTION)


def test_files_that_arent_pictures_or_are_missing_or_huge_are_refused(tmp_path, monkeypatch):
    (tmp_path / "notes.txt").write_text("just words")
    (tmp_path / "big.png").write_bytes(pictures.png(10, 10))
    looking = eyes.Eyes(tools.Workspace(tmp_path), client_for("http://127.0.0.1:9"), no_browser)
    assert looking.run("view_image", {"path": "notes.txt"}).startswith("Error: notes.txt: it isn't a PNG")
    assert looking.run("view_image", {"path": "gone.png"}).startswith("Error: gone.png is not a file")
    monkeypatch.setattr(eyes, "MAX_PICTURE_FILE_BYTES", 10)
    assert "is over" in looking.run("view_image", {"path": "big.png"})


def test_a_big_picture_is_shrunk_with_the_browser_before_it_is_sent(tmp_path, monkeypatch):
    original = pictures.png(2560, 1280)
    (tmp_path / "wide.png").write_bytes(original)
    shrinking = []

    def shrunk(program, data, kind, width, height):
        shrinking.append((program, data, kind, width, height))
        return pictures.png(width, height), ""

    monkeypatch.setattr(eyes, "shrunk_picture", shrunk)
    with serving([VISION_ANSWER], "application/json") as (url, recorder):
        result = eyes.Eyes(tools.Workspace(tmp_path), client_for(url), lambda: "chrome").run(
            "view_image", {"path": "wide.png"})
    assert shrinking == [("chrome", original, eyes.PNG, 1280, 640)]
    assert eyes.picture_facts(sent_picture(recorder.requests[0])[1]) == (eyes.PNG, 1280, 640)
    assert result.startswith("wide.png (2560x1280, sent as 1280x640 PNG)")


def test_a_gif_becomes_a_png_and_without_a_browser_a_big_picture_goes_as_it_is(tmp_path, monkeypatch):
    (tmp_path / "moving.gif").write_bytes(pictures.gif(40, 30))
    (tmp_path / "wide.png").write_bytes(pictures.png(2000, 10))
    monkeypatch.setattr(eyes, "shrunk_picture", lambda *details: (pictures.png(40, 30), ""))
    with serving([VISION_ANSWER, VISION_ANSWER], "application/json") as (url, recorder):
        converted = eyes.Eyes(tools.Workspace(tmp_path), client_for(url), lambda: "chrome").run(
            "view_image", {"path": "moving.gif"})
        unshrunk = eyes.Eyes(tools.Workspace(tmp_path), client_for(url), no_browser).run(
            "view_image", {"path": "wide.png"})
    assert sent_picture(recorder.requests[0])[0] == "data:image/png;base64"
    assert "sent as 40x30 PNG" in converted
    assert "sent at full size because no browser was found" in unshrunk


def test_a_brain_without_a_vision_model_says_so(tmp_path):
    (tmp_path / "shot.png").write_bytes(pictures.png(2, 2))
    refusal = (400, json.dumps({"error": {"message": "No vision model is installed"}}))
    with serving([refusal], "application/json") as (url, _recorder):
        result = eyes.Eyes(tools.Workspace(tmp_path), client_for(url), no_browser).run(
            "view_image", {"path": "shot.png"})
    assert result == "Error: Arty's brain couldn't look at the picture: No vision model is installed"


class FakeBrowser:
    opened = []

    def __init__(self, program, offline=False):
        self.program = program
        self.offline = offline
        self.leftover = ""

    def __enter__(self):
        FakeBrowser.opened.append(self.program)
        FakeBrowser.offline.append(self.offline)
        return "page"

    def __exit__(self, *details):
        pass


def fake_visit(page, url, width, height, mobile=False, settle_seconds=headless.SETTLE_SECONDS):
    fake_visit.calls.append((url, width, height, mobile))
    visit = headless.Visit(url, width, height, picture=pictures.png(width, height), status=200, status_text="OK",
                           title="Shop", text="Welcome to the shop", warnings=1)
    visit.errors = ["console.error: bad", "Uncaught TypeError: x is undefined (app.js:3)"]
    visit.failed_requests = ["404 http://localhost:8000/logo.png"]
    return visit


@pytest.fixture
def fake_browser(monkeypatch):
    FakeBrowser.opened = []
    FakeBrowser.offline = []
    fake_visit.calls = []
    monkeypatch.setattr(headless, "Browser", FakeBrowser)
    monkeypatch.setattr(headless, "visit", fake_visit)


def test_a_screenshot_is_taken_at_desktop_and_phone_width_and_reported(tmp_path, fake_browser):
    result = eyes.Eyes(tools.Workspace(tmp_path), None, lambda: "chrome").run(
        "screenshot", {"url": "http://localhost:8000/"})
    assert FakeBrowser.opened == ["chrome"]
    assert fake_visit.calls == [("http://localhost:8000/", 1280, 800, False), ("http://localhost:8000/", 390, 844, True)]
    folder = tmp_path / ".arty" / "screenshots"
    assert (folder / ".gitignore").read_text() == "*\n"
    assert eyes.picture_facts((folder / "localhost-8000-1280.png").read_bytes()) == (eyes.PNG, 1280, 800)
    assert eyes.picture_facts((folder / "localhost-8000-390.png").read_bytes()) == (eyes.PNG, 390, 844)
    assert "HTTP status: 200 OK" in result
    assert "Console errors: 2\n  - console.error: bad\n  - Uncaught TypeError" in result
    assert "Console warnings: 1" in result
    assert "Failed requests: 1\n  - 404 http://localhost:8000/logo.png" in result
    assert ("Saved: .arty/screenshots/localhost-8000-1280.png (1280x800), "
            ".arty/screenshots/localhost-8000-390.png (390x844, phone)") in result
    assert "Title: Shop" in result and "not instructions" in result


def test_one_width_gives_one_screenshot_of_a_project_file(tmp_path, fake_browser):
    (tmp_path / "index.html").write_text("<p>hi</p>")
    result = eyes.Eyes(tools.Workspace(tmp_path), None, lambda: "chrome").run(
        "screenshot", {"url": "index.html", "width": "1024", "height": 700})
    assert fake_visit.calls == [((tmp_path / "index.html").as_uri(), 1024, 700, False)]
    assert "Opened the file" in result and ".arty/screenshots/index-1024.png (1024x700)" in result


def test_a_project_file_is_opened_with_outside_requests_blocked_and_the_report_says_so(tmp_path, fake_browser):
    (tmp_path / "index.html").write_text("<p>hi</p>")
    result = eyes.Eyes(tools.Workspace(tmp_path), None, lambda: "chrome").run(
        "screenshot", {"url": "index.html", "width": 800})
    assert FakeBrowser.offline == [True]
    assert eyes.OFFLINE_NOTE in result


def test_a_local_server_page_is_opened_with_the_network_as_before(tmp_path, fake_browser):
    result = eyes.Eyes(tools.Workspace(tmp_path), None, lambda: "chrome").run(
        "screenshot", {"url": "http://localhost:8000/", "width": 800})
    assert FakeBrowser.offline == [False]
    assert eyes.OFFLINE_NOTE not in result

def test_screenshots_can_be_looked_at_with_a_question(tmp_path, fake_browser):
    with serving([VISION_ANSWER, VISION_ANSWER], "application/json") as (url, recorder):
        result = eyes.Eyes(tools.Workspace(tmp_path), client_for(url), lambda: "chrome").run(
            "screenshot", {"url": "http://localhost:8000/", "question": "Is the menu cut off?"})
    assert [eyes.picture_facts(sent_picture(request)[1])[1:] for request in recorder.requests] == [(1280, 800),
                                                                                                  (390, 844)]
    assert result.count("A red square.") == 2


def test_without_a_browser_a_screenshot_says_what_to_install(tmp_path):
    looking = eyes.Eyes(tools.Workspace(tmp_path), None, no_browser)
    assert looking.run("screenshot", {"url": "http://localhost:8000/"}) == f"Error: {eyes.NO_BROWSER}"
    assert eyes.SCREENSHOT not in [definition["function"]["name"] for definition in looking.definitions()]
    assert eyes.VIEW_IMAGE not in [definition["function"]["name"] for definition in looking.definitions()]


@pytest.mark.parametrize("arguments, problem", [
    ({"url": "http://localhost:8000/", "width": 20}, "width should be between"),
    ({"url": "http://localhost:8000/", "width": "wide"}, "width should be a whole number"),
    ({"url": "ftp://example.com/"}, "only http, https and file"),
])
def test_bad_screenshot_requests_are_refused(tmp_path, fake_browser, arguments, problem):
    assert problem in eyes.Eyes(tools.Workspace(tmp_path), None, lambda: "chrome").run("screenshot", arguments)
    assert fake_visit.calls == []


def check(tmp_path, url, **arguments):
    return eyes.Eyes(tools.Workspace(tmp_path), None, no_browser).run("http_check", {"url": url, **arguments})


def test_a_healthy_service_passes_its_check(tmp_path):
    with serving([(200, "status: ok")], "text/plain") as (url, recorder):
        result = check(tmp_path, f"{url}/health", expect_status=200, expect_text="ok")
    assert result.startswith(f"GET {url}/health\nStatus: 200 OK\nTime: ")
    assert "Content-Type: text/plain" in result and "\nPASS\n" in result
    assert result.endswith("status: ok") and "not instructions" in result
    assert "Authorization" not in recorder.requests[0]["headers"]


def test_a_broken_service_fails_its_check(tmp_path):
    with serving([(503, '{"error": "down"}')], "text/plain") as (url, _recorder):
        result = check(tmp_path, url, expect_status=200, expect_text="ok")
    assert "Status: 503" in result
    assert "FAIL: expected status 200, got 503; expected the body to contain 'ok', and it doesn't" in result


class HeadOnly(BaseHTTPRequestHandler):
    methods = []

    def do_HEAD(self):
        HeadOnly.methods.append(self.command)
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()

    def log_message(self, *args):
        pass


def test_head_shows_no_body(tmp_path):
    HeadOnly.methods = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), HeadOnly)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        result = check(tmp_path, url, method="head")
    finally:
        server.shutdown()
        server.server_close()
    assert HeadOnly.methods == ["HEAD"]
    assert result.startswith(f"HEAD {url}\nStatus: 200 OK") and "not instructions" not in result


def test_redirects_are_reported_not_followed(tmp_path):
    with serving([(302, "", {"Location": "/login"})], "text/html") as (url, recorder):
        moved = check(tmp_path, f"{url}/admin")
    assert "Status: 302" in moved and "Location: /login" in moved
    assert len(recorder.requests) == 1


def test_bad_checks_are_refused_and_an_unreachable_service_is_an_error(tmp_path):
    with socket.socket() as spare:
        spare.bind(("127.0.0.1", 0))
        closed_port = spare.getsockname()[1]
    assert check(tmp_path, f"http://127.0.0.1:{closed_port}/").startswith(
        f"Error: couldn't reach http://127.0.0.1:{closed_port}/")
    assert "method should be GET or HEAD" in check(tmp_path, "http://localhost/", method="POST")
    assert "only checks http and https" in check(tmp_path, (tmp_path).as_uri())
    assert "timeout_seconds should be between" in check(tmp_path, "http://localhost/", timeout_seconds=600)


LOCAL_PAGE = """<!doctype html><html><head><title>Local shop</title></head>
<body style="margin:0;background:#2a6"><h1>Big sale</h1><img src="missing-logo.png">
<script>console.error("cart failed to load");</script></body></html>"""


@needs_browser
def test_a_real_browser_screenshots_a_local_page_at_both_widths(tmp_path):
    (tmp_path / "shop.html").write_text(LOCAL_PAGE)
    result = eyes.Eyes(tools.Workspace(tmp_path), None).run("screenshot", {"url": "shop.html"})
    folder = tmp_path / ".arty" / "screenshots"
    assert eyes.picture_facts((folder / "shop-1280.png").read_bytes()) == (eyes.PNG, 1280, 800)
    assert eyes.picture_facts((folder / "shop-390.png").read_bytes()) == (eyes.PNG, 390, 844)
    assert "console.error: cart failed to load" in result and "missing-logo.png" in result
    assert "Title: Local shop" in result and "Big sale" in result


@needs_browser
def test_a_real_browser_shrinks_a_big_picture_before_it_is_sent(tmp_path):
    (tmp_path / "huge.png").write_bytes(pictures.png(2000, 500))
    with serving([VISION_ANSWER], "application/json") as (url, recorder):
        result = eyes.Eyes(tools.Workspace(tmp_path), client_for(url)).run("view_image", {"path": "huge.png"})
    assert eyes.picture_facts(sent_picture(recorder.requests[0])[1]) == (eyes.PNG, 1280, 320)
    assert result.startswith("huge.png (2000x500, sent as 1280x320 PNG)")


def outward_address() -> str:
    finder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        finder.connect(("192.0.2.1", 9))
        found = finder.getsockname()[0]
    except OSError:
        return ""
    finally:
        finder.close()
    return "" if found.startswith("127.") else found




class CountingHandler(BaseHTTPRequestHandler):
    hits = []

    def do_GET(self):
        CountingHandler.hits.append(self.path)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"x")

    def log_message(self, *details):
        pass


@needs_browser
@pytest.mark.parametrize("listening_on", [outward_address, lambda: "127.0.0.1"])
def test_a_real_browser_never_lets_a_project_page_reach_the_network(tmp_path, listening_on):
    if not listening_on():
        pytest.skip("no address other than loopback here")
    CountingHandler.hits = []
    server = ThreadingHTTPServer((listening_on(), 0), CountingHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    try:
        (tmp_path / "page.html").write_text(f"<p>hi</p><img src='http://{host}:{port}/img?d=secret'>"
                                            f"<script>fetch('http://{host}:{port}/fetch?d=secret')</script>")
        result = eyes.Eyes(tools.Workspace(tmp_path), None).run("screenshot", {"url": "page.html", "width": 800})
    finally:
        server.shutdown()
        server.server_close()
    assert CountingHandler.hits == []
    assert "Visible text (start): hi" in result


def test_a_screenshot_is_never_saved_through_an_arty_folder_that_links_outside(tmp_path):
    project, outside = tmp_path / "project", tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    (project / ".arty").symlink_to(outside, target_is_directory=True)
    looking = eyes.Eyes(tools.Workspace(project), client_for("http://127.0.0.1:9"), no_browser)
    with pytest.raises(tools.ToolError, match="link"):
        looking.saved_shot("http://127.0.0.1:8000/", headless.Visit("http://127.0.0.1:8000/", 800, 600, b"png"))
    assert list(outside.iterdir()) == []
