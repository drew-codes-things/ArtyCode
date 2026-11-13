import json
import threading
import time
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

from arty_cli import oauth, settings

SIGN_INS_FILE = "mcp-sign-ins.json"
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

    def client(self) -> oauth.Client:
        return oauth.Client(self.client_id, self.client_secret, self.auth_method)


def record_from(discovery: oauth.Discovery, client: oauth.Client, tokens: oauth.Tokens) -> Record:
    return Record(discovery.server_url, discovery.resource, discovery.issuer, discovery.token_endpoint,
                  client.client_id, client.client_secret, client.auth_method, tokens.access_token,
                  tokens.refresh_token, tokens.expires_at, tokens.scope or discovery.scope)


def with_tokens(record: Record, tokens: oauth.Tokens) -> Record:
    return replace(record, access_token=tokens.access_token, refresh_token=tokens.refresh_token,
                   expires_at=tokens.expires_at, scope=tokens.scope or record.scope)


def stored_record(entry) -> Record | None:
    if not isinstance(entry, dict):
        return None
    wanted = {field.name: field.type for field in fields(Record)}
    if any(not isinstance(entry.get(name), (int, float) if kind is float else str) for name, kind in wanted.items()):
        return None
    return Record(**{name: entry[name] for name in wanted})


class Store:
    def __init__(self, personal_folder: Path):
        self.path = Path(personal_folder) / SIGN_INS_FILE
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

    def get(self, name: str) -> Record | None:
        with self.lock:
            return stored_record(self.table().get(name))

    def put(self, name: str, record: Record) -> Path:
        with self.lock:
            table = self.table()
            table[name] = asdict(record)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            settings.write_private_json(self.path, {SIGN_INS_KEY: table}, indent=2)
        return self.path

    def remove(self, name: str) -> bool:
        with self.lock:
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
        if not oauth.is_safe_address(record.token_endpoint):
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

    def is_expiring(self) -> bool:
        return bool(self.record.expires_at) and self.clock() >= self.record.expires_at - RENEW_EARLY_SECONDS

    def bearer(self) -> str:
        if self.is_expiring():
            self.renew(self.record.access_token)
        return self.record.access_token

    def renew(self, rejected: str) -> None:
        with self.lock:
            if self.record.access_token != rejected:
                return
            try:
                tokens = oauth.refreshed(self.record.token_endpoint, self.record.resource, self.record.client(),
                                         self.record.refresh_token, self.fetch, self.clock)
            except oauth.OAuthError as error:
                raise SignInError(f"your sign-in to {self.name} has run out and couldn't be renewed ({error})") from None
            self.record = with_tokens(self.record, tokens)
            self.store.put(self.name, self.record)
