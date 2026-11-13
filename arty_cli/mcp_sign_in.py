import json
import os
import re
import secrets
import string
import sys
import webbrowser
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from arty_cli import mcp, oauth, screen, settings, sign_ins
from arty_cli.screen import Terminal

MCP_WORD = "mcp"
ADD, LOGIN, REMOVE = "add", "login", "remove"
ACTIONS = (ADD, LOGIN, REMOVE)
NAME_OPTION = "--name"
EXIT_GOOD, EXIT_FAILED, EXIT_USAGE = 0, 1, 2
CUSTOM_LABEL = "Another service, by its MCP address"
PICK_QUESTION = "Add which MCP service? It opens in your browser for you to sign in."
ADDRESS_QUESTION = "MCP server address (https://...): "
PASTE_QUESTION = "Paste the address the browser ended on (Enter if it finished by itself): "
USAGE = ("Use: arty mcp add [service or https address] [--name NAME], arty mcp login NAME, or arty mcp remove NAME. "
         "Services: {services}.")
NAME_PARTS_DROPPED = ("mcp", "www", "api", "app")
SAFE_NAME = re.compile(r"[^a-z0-9-]+")
FALLBACK_NAME = "service"
LOWEST_PASTE_PORT, PASTE_PORTS = 49152, 16383
DISPLAY_VARIABLES = ("DISPLAY", "WAYLAND_DISPLAY")
SSH_VARIABLES = ("SSH_CONNECTION", "SSH_TTY")
NO_TERMINAL = ("Signing in needs a terminal to come back to. Run `arty mcp add {name}` in a terminal on this "
               "computer, or on another computer and copy its settings.json entry and mcp-sign-ins.json across.")
NO_BROWSER_NO_LOOPBACK = ("This computer can't open a browser and ArtyCode can't listen on {host} here, so the "
                          "sign-in can't finish by itself. Open the address above on any device, sign in, then "
                          "copy the address the browser ends on (it starts with {redirect}) and paste it here.")
HEADLESS_HELP = ("No browser here (an SSH or headless session). Open the address above on any device and sign in. "
                 "The browser then tries {redirect}, which won't load on another device: copy that whole address "
                 "and paste it here. Or forward the port first (ssh -L {port}:{host}:{port} <this computer>) and "
                 "it finishes by itself.")
FORWARD_ONLY_HELP = ("No browser here and no terminal to paste into. Forward the port first "
                     "(ssh -L {port}:{host}:{port} <this computer>), then open the address above on that computer "
                     "and sign in; it finishes by itself.")


@dataclass(frozen=True)
class Service:
    key: str
    label: str
    url: str
    about: str


CATALOGUE = (
    Service("notion", "Notion", "https://mcp.notion.com/mcp", "pages, databases and comments"),
    Service("linear", "Linear", "https://mcp.linear.app/mcp", "issues, projects and cycles"),
    Service("atlassian", "Atlassian", "https://mcp.atlassian.com/v1/mcp", "Jira and Confluence"),
    Service("sentry", "Sentry", "https://mcp.sentry.dev/mcp", "errors, issues and releases"),
    Service("hugging-face", "Hugging Face", "https://huggingface.co/mcp", "models, datasets and Spaces"),
    Service("cloudflare-bindings", "Cloudflare Workers", "https://bindings.mcp.cloudflare.com/mcp",
            "Workers, KV, R2 and D1"),
    Service("cloudflare-observability", "Cloudflare Observability", "https://observability.mcp.cloudflare.com/mcp",
            "Workers logs and analytics"),
    Service("stripe", "Stripe", "https://mcp.stripe.com", "payments, customers and the Stripe docs"),
    Service("paypal", "PayPal", "https://mcp.paypal.com/mcp", "invoices, orders and payments"),
    Service("vercel", "Vercel", "https://mcp.vercel.com", "projects, deployments and logs"),
    Service("canva", "Canva", "https://mcp.canva.com/mcp", "designs and exports"),
    Service("webflow", "Webflow", "https://mcp.webflow.com/mcp", "sites, pages and CMS"),
)


class SignInStopped(Exception):
    pass


@dataclass(frozen=True)
class World:
    environ: Mapping = field(default_factory=lambda: os.environ)
    platform: str = sys.platform
    open_browser: Callable = webbrowser.open
    fetch: Callable = oauth.fetched
    is_interactive: Callable = screen.is_interactive
    callback: Callable = oauth.Callback
    wait_seconds: float = oauth.SIGN_IN_SECONDS


def service_named(wanted: str) -> Service | None:
    wanted = wanted.strip().lower()
    return next((service for service in CATALOGUE if wanted in (service.key, service.label.lower())), None)


def service_keys() -> str:
    return ", ".join(service.key for service in CATALOGUE)


def default_name(url: str) -> str:
    labels = [label for label in (urlsplit(url).hostname or "").lower().split(".")[:-1]
              if label not in NAME_PARTS_DROPPED]
    found = SAFE_NAME.sub("-", labels[-1] if labels else "").strip("-")
    return found or FALLBACK_NAME


def has_browser(world: World) -> bool:
    has_display = any(world.environ.get(name) for name in DISPLAY_VARIABLES)
    over_ssh = any(world.environ.get(name) for name in SSH_VARIABLES)
    if over_ssh and not has_display:
        return False
    return has_display or not world.platform.startswith("linux")


def lettered(count: int) -> list[str]:
    return list(string.ascii_lowercase[:count])


def picked_service(terminal: Terminal) -> Service | None:
    keys = lettered(len(CATALOGUE) + 1)
    rows = [(service.label, service.about) for service in CATALOGUE] + [(CUSTOM_LABEL, "")]
    if terminal.console:
        choice = terminal.console.choose(PICK_QUESTION, [(key, f"{label}  {about}".rstrip())
                                                         for key, (label, about) in zip(keys, rows)])
    else:
        terminal.note(PICK_QUESTION, "accent")
        for key, (label, about) in zip(keys, rows):
            terminal.note(f"  {terminal.paint(key, 'accent')}  {label}  {terminal.paint(about, 'dim')}".rstrip(), "reset")
        choice = typed_choice(terminal, keys)
    if not choice:
        return None
    if choice == keys[-1]:
        address = terminal.asked(ADDRESS_QUESTION).strip()
        return Service(default_name(address), address, address, "") if address else None
    return CATALOGUE[keys.index(choice)]


def typed_choice(terminal: Terminal, keys: list[str]) -> str:
    while True:
        try:
            answer = terminal.asked(f"Choose {keys[0]}-{keys[-1]} (Enter to cancel): ").strip().lower()
        except EOFError:
            return ""
        if not answer or answer in keys:
            return answer


def personal_settings_path(personal: Path) -> Path:
    return Path(personal) / mcp.SETTINGS_FILE


def personal_table(personal: Path) -> dict:
    path = personal_settings_path(personal)
    if not path.is_file():
        return {}
    try:
        found = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SignInStopped(f"Couldn't read {path} ({error}), so nothing was changed.") from None
    if not isinstance(found, dict) or not isinstance(found.get(mcp.SERVERS_KEY, {}), dict):
        raise SignInStopped(f"{path} isn't laid out as ArtyCode expects, so nothing was changed.")
    return found


def saved_entry(personal: Path, name: str) -> dict | None:
    entry = personal_table(personal).get(mcp.SERVERS_KEY, {}).get(name)
    return entry if isinstance(entry, dict) else None


def save_entry(personal: Path, name: str, url: str) -> Path:
    table = personal_table(personal)
    table.setdefault(mcp.SERVERS_KEY, {})[name] = {"url": url, mcp.SIGN_IN_KEY: name}
    path = personal_settings_path(personal)
    path.parent.mkdir(parents=True, exist_ok=True)
    settings.write_private_json(path, table, indent=2)
    return path


def drop_entry(personal: Path, name: str) -> bool:
    table = personal_table(personal)
    if table.get(mcp.SERVERS_KEY, {}).pop(name, None) is None:
        return False
    settings.write_private_json(personal_settings_path(personal), table, indent=2)
    return True


def is_signed_in_entry(entry: dict | None, name: str) -> bool:
    return entry is not None and entry.get(mcp.SIGN_IN_KEY) == name


def is_ours(entry: dict | None, url: str, name: str) -> bool:
    return entry is None or (entry.get("url") == url and is_signed_in_entry(entry, name))


def opened_callback(world: World):
    try:
        return world.callback()
    except OSError:
        return None


def finished_code(terminal: Terminal, pending: oauth.Pending, callback, world: World, shown_browser: bool) -> str:
    if callback is not None and (shown_browser or not world.is_interactive()):
        with terminal.working("Waiting for you to sign in in the browser"):
            return callback.wait(world.wait_seconds)
    return pasted_code(terminal, pending, callback)


def pasted_code(terminal: Terminal, pending: oauth.Pending, callback) -> str:
    while True:
        try:
            pasted = terminal.asked(PASTE_QUESTION).strip()
        except EOFError:
            raise SignInStopped("The sign-in was stopped before it finished.") from None
        if pasted:
            return oauth.code_from_address(pasted, pending.state)
        if callback is not None and callback.finished():
            return callback.wait(0)
        terminal.note("Nothing has come back yet. Paste the address, or finish signing in first.")


def explain_headless(terminal: Terminal, callback, redirect: str, can_paste: bool) -> None:
    parts = urlsplit(redirect)
    if callback is None:
        terminal.note(NO_BROWSER_NO_LOOPBACK.format(host=oauth.LOOPBACK_HOST, redirect=redirect), "error")
        return
    explained = HEADLESS_HELP if can_paste else FORWARD_ONLY_HELP
    terminal.note(explained.format(redirect=f"{parts.scheme}://{parts.netloc}{parts.path}", port=parts.port,
                                   host=oauth.LOOPBACK_HOST))


def stepped(terminal: Terminal, label: str, work, done: Callable[[object], str]):
    terminal.show_step(label)
    try:
        found = work()
    except oauth.OAuthError as error:
        terminal.show_outcome(str(error), True)
        raise SignInStopped(f"Not signed in: {error}") from None
    terminal.show_outcome(done(found), False)
    return found


def signed_in(terminal: Terminal, name: str, label: str, url: str, store: sign_ins.Store, world: World,
              bound_issuer: str = "") -> sign_ins.Record:
    discovery = stepped(terminal, f"Find {label}'s sign-in", lambda: oauth.discover(url, world.fetch),
                        lambda found: f"Signs in at {found.issuer}")
    if bound_issuer and oauth.comparable(discovery.issuer) != oauth.comparable(bound_issuer):
        raise SignInStopped(f"{label} now signs in at {discovery.issuer}, not {bound_issuer} as before, so ArtyCode "
                            f"stopped. If that is expected, run arty mcp remove {name} and add it again.")
    callback = opened_callback(world)
    if callback is None and not world.is_interactive():
        raise SignInStopped(NO_TERMINAL.format(name=name))
    redirect = callback.redirect_uri if callback else unheard_redirect()
    try:
        client = stepped(terminal, f"Register ArtyCode with {label}",
                         lambda: oauth.register(discovery, redirect, world.fetch), lambda _found: "Registered")
        pending = oauth.pending_sign_in(discovery, client, redirect)
        if callback is not None:
            callback.listen(pending.state)
        code = browser_code(terminal, label, pending, callback, world)
        tokens = stepped(terminal, f"Finish signing in to {label}",
                         lambda: oauth.exchange(discovery, client, pending, code, world.fetch),
                         lambda _found: "Signed in")
    except oauth.OAuthError as error:
        raise SignInStopped(f"Not signed in: {error}") from None
    finally:
        if callback is not None:
            callback.close()
    record = sign_ins.record_from(discovery, client, tokens)
    store.put(name, record)
    return record


def browser_code(terminal: Terminal, label: str, pending: oauth.Pending, callback, world: World) -> str:
    terminal.show_step(f"Sign in to {label} in the browser")
    shown = has_browser(world) and bool(world.open_browser(pending.url))
    terminal.show_outcome("Opened the sign-in page" if shown else "Open the sign-in page yourself", False)
    terminal.note(f"Sign-in page: {pending.url}")
    if shown:
        terminal.note("If no browser appeared, open that address yourself.")
    else:
        explain_headless(terminal, callback, pending.redirect_uri, world.is_interactive())
    return finished_code(terminal, pending, callback, world, shown)


def unheard_redirect() -> str:
    port = LOWEST_PASTE_PORT + secrets.randbelow(PASTE_PORTS)
    return f"http://{oauth.LOOPBACK_HOST}:{port}{oauth.CALLBACK_PATH}"


def added(terminal: Terminal, personal: Path, service: Service, name: str, store: sign_ins.Store,
          world: World) -> mcp.ServerConfig:
    if not oauth.is_safe_address(service.url):
        raise SignInStopped(f"{service.url!r} isn't an https address, so ArtyCode won't sign in to it.")
    if not is_ours(saved_entry(personal, name), service.url, name):
        raise SignInStopped(f"You already have an MCP server called {name}. Pick another name with --name, or "
                            f"remove it first with arty mcp remove {name}.")
    signed_in(terminal, name, service.label, service.url, store, world)
    path = save_entry(personal, name, service.url)
    terminal.note(f"Saved {name} in {path}; the sign-in is kept in {store.path}.", "green")
    return mcp.ServerConfig(name, "", [], {}, "yours", url=service.url, sign_in=name)


def signed_in_again(terminal: Terminal, personal: Path, name: str, store: sign_ins.Store,
                    world: World) -> mcp.ServerConfig:
    record = store.get(name)
    entry = saved_entry(personal, name)
    url = entry.get("url") if entry is not None else (record.server_url if record else "")
    if not isinstance(url, str) or not url or (entry is not None and not is_signed_in_entry(entry, name)):
        raise SignInStopped(f"There's no signed-in MCP server called {name} in your settings. Add one with "
                            "/mcp add or arty mcp add.")
    if record is not None and record.server_url != url:
        raise SignInStopped(f"The saved sign-in {name} was made for {record.server_url}, but your settings now say "
                            f"{url}. Run arty mcp remove {name} and add it again.")
    service = service_named(name)
    label = service.label if service and service.url == url else name
    signed_in(terminal, name, label, url, store, world, record.issuer if record else "")
    if entry is None:
        save_entry(personal, name, url)
    terminal.note(f"Signed in to {name} again.", "green")
    return mcp.ServerConfig(name, "", [], {}, "yours", url=url, sign_in=name)


def removed(terminal: Terminal, personal: Path, name: str, store: sign_ins.Store) -> bool:
    entry = saved_entry(personal, name)
    if entry is not None and not is_signed_in_entry(entry, name):
        terminal.note(f"{name} isn't a service you signed in to; change it in {personal_settings_path(personal)}.",
                      "error")
        return False
    dropped_entry = drop_entry(personal, name)
    dropped_sign_in = store.remove(name)
    if not (dropped_entry or dropped_sign_in):
        terminal.note(f"There's no MCP server called {name} in your settings.", "error")
        return False
    terminal.note(f"Removed {name} and its saved sign-in.", "green")
    return True


def wanted_service(wanted: str) -> Service | None:
    found = service_named(wanted)
    if found is not None:
        return found
    if urlsplit(wanted).scheme in mcp.WEB_SCHEMES:
        return Service(default_name(wanted), wanted, wanted, "")
    return None


def handles(argv: list[str]) -> bool:
    return len(argv) >= 2 and argv[0] == MCP_WORD and argv[1] in ACTIONS


def split_name_option(rest: list[str]) -> tuple[list[str], str]:
    if NAME_OPTION in rest:
        place = rest.index(NAME_OPTION)
        if place + 1 >= len(rest):
            return rest, ""
        return rest[:place] + rest[place + 2:], rest[place + 1]
    return rest, ""


def is_good_name(name: str) -> bool:
    return bool(mcp.SIGN_IN_NAME.match(name))


def run(argv: list[str], terminal: Terminal | None = None, world: World | None = None) -> int:
    terminal, world = terminal or Terminal(), world or World()
    personal = settings.config_dir()
    store = sign_ins.Store(personal)
    action, (rest, name) = argv[1], split_name_option(argv[2:])
    usage = USAGE.format(services=service_keys())
    try:
        if action == ADD:
            service = wanted_service(rest[0]) if rest else (picked_service(terminal) if world.is_interactive() else None)
            if service is None or len(rest) > 1 or (name and not is_good_name(name)):
                terminal.note(usage, "error")
                return EXIT_USAGE
            added(terminal, personal, service, name or service.key, store, world)
            terminal.note("Start ArtyCode and its tools are there; /mcp shows them.")
            return EXIT_GOOD
        if len(rest) != 1 or not is_good_name(rest[0]):
            terminal.note(usage, "error")
            return EXIT_USAGE
        if action == LOGIN:
            signed_in_again(terminal, personal, rest[0], store, world)
            return EXIT_GOOD
        return EXIT_GOOD if removed(terminal, personal, rest[0], store) else EXIT_FAILED
    except (SignInStopped, sign_ins.SignInError) as error:
        terminal.note(f"{error} Nothing was saved.", "error")
        return EXIT_FAILED
