import threading
import time
from dataclasses import dataclass

from arty_cli import brain

STALE_SECONDS = 60
WAIT_POLL_SECONDS = 0.02
CURRENT_MARK = "●"
CURRENT_WORD = "current"
NO_MODELS = "Arty's brain listed no models. /model <name> still switches to one by name."


@dataclass(frozen=True)
class Choice:
    model: str
    about: str


class Choices:
    def __init__(self, listed: list[Choice]):
        self.listed = listed

    def pick(self, number: str) -> Choice | None:
        if not number.isdigit() or not 1 <= int(number) <= len(self.listed):
            return None
        return self.listed[int(number) - 1]

    def lines(self, model: str) -> list[str]:
        shown, width = [], len(str(len(self.listed)))
        for number, choice in enumerate(self.listed, 1):
            mark = "●" if choice.model == model else " "
            about = f"  {choice.about}" if choice.about and choice.about != choice.model else ""
            shown.append(f"{mark} {number:>{width}}. {choice.model}{about}")
        return shown


def listed_for(client, current_model: str = "") -> list[Choice]:
    found = [Choice(model, about) for model, about in client.available_models()]
    if not found:
        raise brain.BrainError(NO_MODELS)
    if current_model and all(choice.model != current_model for choice in found):
        found.insert(0, Choice(current_model, "current"))
    return found


def described(choice: Choice) -> str:
    return choice.about if choice.about not in (choice.model, CURRENT_WORD) else ""


def picker_text(choice: Choice, current: str) -> str:
    marked = f"({CURRENT_WORD})" if choice.model == current else ""
    return "  ".join(part for part in (choice.model, described(choice), marked) if part)


def menu_meanings(listed: Choices, current: str) -> dict[str, str]:
    meanings = {}
    for choice in listed.listed:
        about = described(choice)
        if choice.model == current:
            about = ", ".join(part for part in (f"{CURRENT_MARK} {CURRENT_WORD}", about) if part)
        meanings[choice.model] = about
    return meanings


class Listing:
    def __init__(self, fetch, clock=time.monotonic):
        self.fetch = fetch
        self.clock = clock
        self.choices: Choices | None = None
        self.failure: Exception | None = None
        self.loaded_at: float | None = None
        self.attempted_at: float | None = None
        self.loading = threading.Lock()
        self.refreshing = False
        self.listeners = []

    def when_loaded(self, listener) -> None:
        self.listeners.append(listener)

    def is_current(self, moment: float | None) -> bool:
        return moment is not None and self.clock() - moment < STALE_SECONDS

    def fresh(self) -> Choices | None:
        return self.choices if self.is_current(self.loaded_at) else None

    def load(self) -> Choices:
        with self.loading:
            loaded = self.fresh()
            if loaded is not None:
                return loaded
            self.attempted_at = self.clock()
            listed = Choices(self.fetch())
            self.choices, self.loaded_at, self.failure = listed, self.clock(), None
        for listener in list(self.listeners):
            listener()
        return listed

    def available(self) -> Choices | None:
        if not self.is_current(self.attempted_at) and not self.refreshing:
            self.refreshing = True
            threading.Thread(target=self.refresh, daemon=True).start()
        return self.choices

    def wait(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while self.refreshing and self.choices is None and time.monotonic() < deadline:
            time.sleep(WAIT_POLL_SECONDS)

    def refresh(self) -> None:
        try:
            self.load()
        except Exception as error:
            self.failure = error
        finally:
            self.refreshing = False
