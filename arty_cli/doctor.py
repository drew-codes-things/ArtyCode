import os
import shutil
import stat
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from arty_cli import __version__, brain, context, headless, sandbox, settings, websearch

TICK = "✓"
CROSS = "✗"
OPTIONAL = "(optional)"
ALL_GOOD = "Everything ArtyCode needs is in place."
USAGE = "arty doctor takes no options: it only checks and prints."
EXIT_GOOD, EXIT_MISSING, EXIT_USAGE = 0, 1, 2
OTHERS_CAN_READ = 0o077
REFUSED_CODES = (401, 403)
SEARCH_PROBE = "arty"
SEARCH_RESULTS = 1
LOG_IN = "run `arty login`"
SMALL_WINDOW_TOKENS = 16_384


@dataclass(frozen=True)
class Check:
    passed: bool
    line: str
    needed: bool = True

    def shown(self) -> str:
        note = "" if self.passed or self.needed else f" {OPTIONAL}"
        return f"{TICK if self.passed else CROSS} {self.line}{note}"


@dataclass(frozen=True)
class World:
    environ: Mapping = field(default_factory=lambda: os.environ)
    platform: str = sys.platform
    make_client: Callable = brain.Brain
    open_url: Callable = urllib.request.urlopen
    which: Callable = shutil.which
    find_browser: Callable = headless.find_browser
    sandbox_problem: Callable = sandbox.platform_problem
    terminal_size: Callable = shutil.get_terminal_size
    is_terminal: Callable = lambda: sys.stdout.isatty()


def good(line: str) -> Check:
    return Check(True, line)


def missing(line: str) -> Check:
    return Check(False, line)


def extra(passed: bool, line: str) -> Check:
    return Check(passed, line, needed=False)


def settings_check(world: World, config: settings.Settings) -> Check:
    path = settings.config_path(world.environ)
    if not path.exists():
        if config.is_ready():
            return good("Settings come from environment variables (no settings file)")
        return missing(f"Settings: no file at {path}, so ArtyCode isn't logged in: {LOG_IN}")
    mode = stat.S_IMODE(path.stat().st_mode)
    if world.platform != "win32" and mode & OTHERS_CAN_READ:
        return missing(f"Settings file {path} can be read by other users (mode {mode:o}): chmod 600 {path}")
    return good(f"Settings file found and private: {path}")


def which_address(config: settings.Settings, url: str) -> str:
    return "home address" if url == config.base_url.rstrip("/") else "address from anywhere"


def key_problem(world: World, config: settings.Settings, route: brain.Route) -> str:
    headers = {"Authorization": f"Bearer {config.api_key}", "User-Agent": f"arty-cli/{__version__}", **route.headers}
    request = urllib.request.Request(f"{route.url}{brain.OFFERED_MODELS}", headers=headers)
    try:
        with world.open_url(request, timeout=brain.LIST_SECONDS):
            return ""
    except urllib.error.HTTPError as error:
        if error.code in REFUSED_CODES:
            return f"the brain refused this computer's key ({error.code}): {LOG_IN}"
        return f"the brain answered {error.code} when asked"
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as error:
        return f"the brain stopped answering ({brain.plain_reason(error)})"


def models_check(client, config: settings.Settings) -> Check:
    try:
        listed = [model for model, _ in client.available_models()]
    except brain.BrainError as error:
        return missing(f"Models: {error}")
    count = f"{len(listed)} model{'' if len(listed) == 1 else 's'}"
    known = "" if config.model in listed else ", which is not in that list (pick one with /model)"
    return good(f"Models: read {count}; this computer uses {config.model}{known}")


def coding_check(client) -> Check:
    try:
        marked = [str(entry.get("id")) for entry in client.model_entries(brain.DOWNLOADED_MODELS)
                  if entry.get(brain.CODING_MARK) is True]
    except brain.BrainError as error:
        return extra(False, f"Coding models: {error}")
    if marked:
        return extra(True, f"Coding models are marked: {', '.join(marked)}")
    return extra(False, "Coding models aren't marked by this brain, so /model lists every model")


def context_check(client) -> Check:
    try:
        window = client.context_tokens()
    except brain.BrainError as error:
        return extra(False, f"Context window: {error}")
    if not window:
        return extra(False, "Context window: the brain doesn't report it, so ArtyCode assumes "
                            f"{context.DEFAULT_WINDOW_TOKENS} tokens")
    if window < SMALL_WINDOW_TOKENS:
        return extra(False, f"Context window: {window} tokens, below {SMALL_WINDOW_TOKENS}, so long files and long "
                            "jobs are cut down and summarised often; give the model a bigger num_ctx on the brain")
    return extra(True, f"Context window: {window} tokens")


def search_check(client, config: settings.Settings) -> Check:
    place = config.search_with or websearch.DESKTOP
    try:
        if place == websearch.DESKTOP and client.desktop_search(SEARCH_PROBE) is not None:
            return extra(True, "Web search works through the browser on Arty's desktop")
        client.brain_tool(brain.SEARCH_TOOL, {"query": SEARCH_PROBE, "results": SEARCH_RESULTS})
    except brain.BrainError as error:
        return extra(False, f"Web search isn't working: {error}")
    fallback = " (the desktop browser didn't answer)" if place == websearch.DESKTOP else ""
    return extra(True, f"Web search works through the brain{fallback}")


def skipped(why: str) -> list[Check]:
    return [missing(f"API key: not checked, {why}"), missing(f"Models: not checked, {why}"),
            extra(False, f"Coding models: not checked, {why}"), extra(False, f"Context window: not checked, {why}"),
            extra(False, f"Web search: not checked, {why}")]


def brain_checks(world: World, config: settings.Settings) -> list[Check]:
    if not config.is_ready():
        return [missing(f"Arty's brain: no address and key yet: {LOG_IN}"), *skipped("ArtyCode isn't logged in")]
    client = world.make_client(config)
    try:
        route = client.pick_route()
    except brain.BrainError as error:
        return [missing(str(error)), *skipped("the brain didn't answer")]
    reached = good(f"Arty's brain answered at {route.url} (the {which_address(config, route.url)})")
    problem = key_problem(world, config, route)
    if problem:
        return [reached, missing(f"API key: {problem}"), *skipped("the key wasn't accepted")[1:]]
    return [reached, good("API key accepted by the brain"), models_check(client, config), coding_check(client),
            context_check(client), search_check(client, config)]


def sandbox_check(world: World) -> Check:
    problem = world.sandbox_problem(Path.cwd())
    tool = "bubblewrap" if world.platform.startswith("linux") else "the sandbox"
    if problem:
        return extra(False, f"Sandbox ({tool}): {problem}, so --sandbox can't be used")
    return extra(True, f"Sandbox ready ({tool})")


def computer_checks(world: World) -> list[Check]:
    browser = world.find_browser()
    git = world.which("git")
    size = world.terminal_size()
    colour = world.is_terminal() and not world.environ.get("NO_COLOR")
    kind = "Terminal" if world.is_terminal() else "Not a terminal (output is piped)"
    return [
        sandbox_check(world),
        extra(bool(browser), f"Browser for screenshots: {browser}" if browser else
              "Browser for screenshots: none found, so pages can't be looked at"),
        extra(bool(git), f"git found: {git}" if git else "git isn't installed, so changes can't be reviewed or undone"),
        good(f"{kind}: {size.columns}x{size.lines}, colour {'on' if colour else 'off'}"),
    ]


def run(world: World) -> list[Check]:
    try:
        config = settings.load(environ=world.environ)
    except (settings.SettingsError, OSError, ValueError, TypeError) as error:
        config = settings.Settings()
        return [missing(f"Settings file can't be read: {error}"), *brain_checks(world, config), *computer_checks(world)]
    return [settings_check(world, config), *brain_checks(world, config), *computer_checks(world)]


def summary(checks: list[Check]) -> str:
    lacking = sum(1 for check in checks if check.needed and not check.passed)
    if not lacking:
        return ALL_GOOD
    things = "thing" if lacking == 1 else "things"
    return f"ArtyCode needs {lacking} more {things} (the lines marked {CROSS} without {OPTIONAL})."


def main(argv: list[str], out=None, world: World | None = None) -> int:
    out = out or sys.stdout
    if argv:
        out.write(f"{USAGE}\nUse: arty doctor\n")
        return EXIT_USAGE
    checks = run(world or World())
    for check in checks:
        out.write(f"{check.shown()}\n")
    out.write(f"{summary(checks)}\n")
    return EXIT_GOOD if summary(checks) == ALL_GOOD else EXIT_MISSING
