import getpass
import os
import socket
import webbrowser
from dataclasses import replace
from urllib.parse import urlparse

from arty_cli import authorise, brain, settings, webfetch
from arty_cli.screen import Terminal

FAILED = 1
NO_ADDRESS = ("ArtyCode doesn't know where your Arty brain is yet, so nothing was saved. Set its address, then run "
              "arty-cli login again:")
HOME_ADDRESS = "http://<brain address>:8081/v1"
ANYWHERE_ADDRESS = "https://<your address>/v1"
ACCESS_NOTE = "with ARTY_ACCESS_CLIENT_ID and ARTY_ACCESS_CLIENT_SECRET too if Cloudflare Access guards it"


def address_help(windows: bool) -> str:
    if windows:
        home, anywhere = f'$env:ARTY_URL = "{HOME_ADDRESS}"', f'$env:ARTY_REMOTE_URL = "{ANYWHERE_ADDRESS}"'
        keep = f'To keep it for new terminals: setx ARTY_URL "{HOME_ADDRESS}"'
    else:
        home, anywhere = f"export ARTY_URL={HOME_ADDRESS}", f"export ARTY_REMOTE_URL={ANYWHERE_ADDRESS}"
        keep = "To keep it, add the line to ~/.bashrc."
    return "\n".join((NO_ADDRESS, f"  At home:        {home}", f"  From anywhere:  {anywhere}",
                      f"                  {ACCESS_NOTE}", f"  {keep}"))


def knows_where_arty_is(current: settings.Settings) -> bool:
    return bool(current.base_url or current.remote_url)


def arty_with_key(terminal: Terminal, current: settings.Settings) -> str:
    return getpass.getpass("Arty API key: ").strip()


def reaches_the_internet(url: str) -> bool:
    host = urlparse(url).hostname or ""
    try:
        found = {entry[4][0] for entry in socket.getaddrinfo(host, None)}
    except (socket.gaierror, UnicodeError):
        return False
    return any(webfetch.is_public(address) for address in found)


def arty_by_authorising(terminal: Terminal, current: settings.Settings) -> str:
    route = (brain.routes(replace(current, base_url="")) or brain.routes(current) or [None])[0]
    if route is None:
        raise authorise.AuthoriseError("Give at least one Arty brain address.")
    if route.url.startswith("http://"):
        if reaches_the_internet(route.url):
            raise authorise.AuthoriseError(f"{route.url} is a public address over plain http, so the approval and "
                                           "key could be read or swapped on the way. Use the https address instead.")
        terminal.note("Authorising over plain http: only do this on your own home network.", "error")
    request = authorise.start(route.url, route.headers)
    terminal.note(f"Approve this computer in Arty. The code is {request.user_code}", "accent")
    terminal.note(f"Opening {request.verify_url} (open it yourself if no browser appears).")
    webbrowser.open(request.verify_url)
    terminal.note("Waiting for you to approve it...")
    return authorise.wait_for_key(route.url, route.headers, request)


LOGIN_CHOICES = (
    ("API key (paste a key made in Arty)", arty_with_key),
    ("Browser login (opens Arty in your browser to approve this computer)", arty_by_authorising),
)


def chosen_login(terminal: Terminal):
    terminal.note("How should ArtyCode log in to Arty?", "accent")
    for number, (label, _handler) in enumerate(LOGIN_CHOICES, 1):
        terminal.note(f"  {number}. {label}")
    while True:
        answer = input(f"Choose 1-{len(LOGIN_CHOICES)}: ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(LOGIN_CHOICES):
            return LOGIN_CHOICES[int(answer) - 1][1]


def login(terminal: Terminal) -> int:
    current = settings.load()
    if not knows_where_arty_is(current):
        first, *rest = address_help(os.name == "nt").splitlines()
        terminal.note(first, "error")
        for line in rest:
            terminal.note(line, "reset")
        return FAILED
    handler = chosen_login(terminal)
    try:
        key = handler(terminal, current)
    except authorise.AuthoriseError as error:
        terminal.note(f"{error} Nothing was saved.", "error")
        return FAILED
    if not key:
        terminal.note("No key was given. Nothing was saved.", "error")
        return FAILED
    terminal.note(f"Saved to {settings.remember(api_key=key)}. Arty answers from now on.", "green")
    return 0
