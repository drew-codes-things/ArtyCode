import contextlib
import json
import threading
import time
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

from arty_cli import file_lock, oauth, settings

SIGN_INS_FILE = "mcp-sign-ins.json"
SIGN_INS_LOCK_FILE = "mcp-sign-ins.lock"
SIGN_INS_KEY = "signIns"
RENEW_EARLY_SECONDS = 60


class SignInError(Exception):
    def __init__(self, message: str, needs_sign_in: bool = True):
        super().__init__(message)
        self.needs_sign_in = needs_sign_in


@dataclass(frozen=True)
class Record:
    server_url: str
    resource: str
    issuer: str
    token_endpoint: str
    client_id: str
    client_secret: str
    auth_method: str
    access_token: str
    refresh_token: str
    expires_at: float
    scope: str
    revocation_endpoint: str = ""

    def client(self) -> oauth.Client:
        return oauth.Client(self.client_id, self.client_secret, self.auth_method)


def record_from(discovery: oauth.Discovery, client: oauth.Client, tokens: oauth.Tokens) -> Record:
    return Record(discovery.server_url, discovery.resource, discovery.issuer, discovery.token_endpoint,
                  client.client_id, client.client_secret, client.auth_method, tokens.access_token,
                  tokens.refresh_token, tokens.expires_at, tokens.scope or discovery.scope,
                  discovery.revocation_endpoint)


def with_tokens(record: Record, tokens: oauth.Tokens) -> Record:
    return replace(record, access_token=tokens.access_token, refresh_token=tokens.refresh_token,
                   expires_at=tokens.expires_at, scope=tokens.scope or record.scope)


def stored_record(entry) -> Record | None:
    if not isinstance(entry, dict):
        return None
    found = {}
    for field in fields(Record):
        value = entry.get(field.name, field.default)
        if not isinstance(value, (int, float) if field.type is float else str):
            return None
        found[field.name] = value
    return Record(**found)


class Store:
    def __init__(self, personal_folder: Path):
        self.path = Path(personal_folder) / SIGN_INS_FILE
        self.lock_path = Path(personal_folder) / SIGN_INS_LOCK_FILE
        self.lock = threading.Lock()

    def table(self) -> dict:
        if not self.path.is_file():
            return {}
        try:
            found = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise SignInError(f"Couldn't read the saved sign-ins in {self.path}: {error}", needs_sign_in=False) from None
        table = found.get(SIGN_INS_KEY) if isinstance(found, dict) else None
        return table if isinstance(table, dict) else {}

    def stored(self, name: str) -> Record | None:
        return stored_record(self.table().get(name))

    def get(self, name: str) -> Record | None:
        if not self.path.is_file():
            return None
        try:
            with self.held():
                return self.stored(name)
        except OSError as error:
            raise SignInError(f"Couldn't lock the saved sign-ins with {self.lock_path}: {error}",
                              needs_sign_in=False) from None

    @contextlib.contextmanager
    def held(self):
        with self.lock, file_lock.held(self.lock_path):
            yield

    def saved(self, name: str, record: Record) -> Path:
        table = self.table()
        table[name] = asdict(record)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        settings.write_private_json(self.path, {SIGN_INS_KEY: table}, indent=2)
        return self.path

    def put(self, name: str, record: Record) -> Path:
        with self.held():
            return self.saved(name, record)

    def remove(self, name: str) -> bool:
        if not self.path.is_file():
            return False
        with self.held():
            table = self.table()
            if table.pop(name, None) is None:
                return False
            settings.write_private_json(self.path, {SIGN_INS_KEY: table}, indent=2)
            return True

    def credentials(self, name: str, server_url: str, fetch=oauth.fetched, clock=time.time) -> "Credentials":
        record = self.get(name)
        if record is None:
            raise SignInError(f"there is no saved sign-in called {name}")
        if record.server_url != server_url:
            raise SignInError(f"the sign-in {name} was made for {record.server_url}, not {server_url}, so it isn't "
                              "sent there", needs_sign_in=False)
        if not oauth.is_safe_endpoint(record.token_endpoint, record.server_url):
            raise SignInError(f"the sign-in {name} has an unsafe token address", needs_sign_in=False)
        return Credentials(self, name, record, fetch, clock)


class Credentials:
    def __init__(self, store: Store, name: str, record: Record, fetch=oauth.fetched, clock=time.time):
        self.store = store
        self.name = name
        self.record = record
        self.fetch = fetch
        self.clock = clock
        self.lock = threading.Lock()
        self.notice = ""

    def is_expiring(self) -> bool:
        return bool(self.record.expires_at) and self.clock() >= self.record.expires_at - RENEW_EARLY_SECONDS

    def is_still_valid(self) -> bool:
        return not self.record.expires_at or self.clock() < self.record.expires_at

    def current(self) -> str:
        return self.record.access_token

    def bearer(self) -> str:
        if self.is_expiring():
            try:
                self.renew(self.record.access_token)
            except SignInError as error:
                if error.needs_sign_in or not self.is_still_valid():
                    raise
        return self.record.access_token

    def take_notice(self) -> str:
        notice, self.notice = self.notice, ""
        return notice

    def renew(self, rejected: str) -> None:
        with self.lock:
            if self.record.access_token != rejected:
                return
            with contextlib.ExitStack() as stack:
                self.renew_from_latest(rejected, self.lock_problem(stack))

    def lock_problem(self, stack: contextlib.ExitStack) -> str:
        try:
            stack.enter_context(self.store.held())
        except OSError as error:
            return f"couldn't lock the saved sign-ins ({error})"
        return ""

    def renew_from_latest(self, rejected: str, lock_problem: str) -> None:
        latest = self.latest()
        if latest.access_token != rejected:
            self.record = latest
            return
        try:
            tokens = oauth.refreshed(latest.token_endpoint, latest.resource, latest.client(), latest.refresh_token,
                                     self.fetch, self.clock)
        except oauth.Unavailable as error:
            raise SignInError(f"couldn't reach the sign-in server to renew your sign-in to {self.name} ({error}); "
                              "try again in a moment", needs_sign_in=False) from None
        except oauth.OAuthError as error:
            raise SignInError(f"your sign-in to {self.name} has run out and couldn't be renewed ({error})") from None
        self.record = with_tokens(latest, tokens)
        self.keep(lock_problem)

    def latest(self) -> Record:
        try:
            saved = self.store.stored(self.name)
        except SignInError:
            return self.record
        if saved is None:
            raise SignInError(f"the saved sign-in {self.name} was removed")
        is_same_sign_in = (saved.server_url == self.record.server_url
                           and oauth.is_safe_endpoint(saved.token_endpoint, saved.server_url))
        return saved if is_same_sign_in else self.record

    def keep(self, lock_problem: str) -> None:
        try:
            self.store.saved(self.name, self.record)
        except OSError as error:
            self.notice = (f"ArtyCode renewed your sign-in to {self.name} but couldn't save it ({error}). It works "
                           f"until you quit; after that you may need /mcp login {self.name}.")
            return
        if lock_problem:
            self.notice = (f"ArtyCode renewed your sign-in to {self.name} but {lock_problem}, so another ArtyCode "
                           "renewing it at the same moment may need you to sign in again.")
