import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from arty_cli import webfetch

PAGE = b"""<html><head><title>Docs</title><style>p{color:red}</style><script>var x = 1;</script></head>
<body><h1>Install</h1><p>Run <code>pip install thing</code>.</p><nav>skip me?</nav></body></html>"""


def test_a_page_becomes_readable_text():
    text = webfetch.page_text(PAGE.decode())
    assert "Install" in text and "pip install thing" in text
    assert "color:red" not in text and "var x" not in text


class Site(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, *args):
        pass


@pytest.fixture
def site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/docs"
    server.shutdown()


def test_fetching_a_page_returns_its_text(site):
    text = webfetch.fetch(site, allow_private=True)
    assert text.startswith(site)
    assert "pip install thing" in text


def test_only_web_addresses_are_fetched():
    with pytest.raises(webfetch.FetchError, match="http"):
        webfetch.fetch("file:///etc/passwd")


class Moving(BaseHTTPRequestHandler):
    target = ""

    def do_GET(self):
        if self.path == "/docs":
            Site.do_GET(self)
            return
        self.send_response(302)
        self.send_header("Location", Moving.target)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def moving():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Moving)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()


def test_a_fetched_page_is_marked_as_data_not_instructions(site):
    text = webfetch.fetch(site, allow_private=True)
    assert "not instructions" in text
    assert text.index("not instructions") < text.index("pip install thing")


def test_a_redirect_to_another_site_is_not_followed(moving, site):
    Moving.target = site.replace("127.0.0.1", "localhost")
    with pytest.raises(webfetch.FetchError, match="localhost"):
        webfetch.fetch(f"http://127.0.0.1:{moving}/old", allow_private=True)


def test_a_redirect_on_the_same_site_is_followed(moving, site):
    Moving.target = "/docs"
    assert "pip install thing" in webfetch.fetch(f"http://127.0.0.1:{moving}/old", allow_private=True)


def test_a_redirect_to_another_port_or_to_plain_http_is_not_followed(moving, site):
    Moving.target = site
    with pytest.raises(webfetch.FetchError, match="another site"):
        webfetch.fetch(f"http://127.0.0.1:{moving}/old", allow_private=True)
    assert not webfetch.same_origin("https://example.com/a", "http://example.com/b")
    assert webfetch.same_origin("http://example.com/a", "https://example.com/b")
    assert webfetch.same_origin("https://example.com/a", "https://example.com:443/b")
    assert not webfetch.same_origin("https://example.com/a", "https://example.com:8443/b")


@pytest.mark.parametrize("url", ["http://127.0.0.1:9/", "http://localhost/", "http://192.168.1.1/",
                                 "http://10.0.0.5/", "http://169.254.169.254/latest/meta-data/", "http://[::1]/",
                                 "http://172.16.0.1/", "http://0.0.0.0/"])
def test_private_loopback_and_link_local_addresses_are_never_fetched(url):
    with pytest.raises(webfetch.FetchError, match="local"):
        webfetch.fetch(url)


def test_every_redirect_step_is_checked_for_a_local_address():
    with pytest.raises(webfetch.FetchError, match="local"):
        webfetch.checked_redirect("http://localhost/a", "http://localhost/b")
