import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from arty_cli import __version__

REQUEST_SECONDS = 15
PENDING = 202
DENIED = 403
EXPIRED = 410
SLOWEST_POLL_SECONDS = 30
LONGEST_WAIT_SECONDS = 900


class AuthoriseError(Exception):
    pass


@dataclass(frozen=True)
class Request:
    device_code: str
    user_code: str
    verify_url: str
    interval: int
    expires_in: int


def device_name() -> str:
    return f"Arty CLI on {socket.gethostname()}"


def post(url: str, body: dict, headers: dict) -> tuple[int, dict]:
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
        "Content-Type": "application/json", "User-Agent": f"arty-cli/{__version__}", **headers})
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_SECONDS) as response:
            return response.status, read_json(response.read())
    except urllib.error.HTTPError as error:
        return error.code, read_json(error.read())
    except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
        raise AuthoriseError(f"Can't reach Arty at {url}: {error}") from None


def read_json(raw: bytes) -> dict:
    try:
        parsed = json.loads(raw or b"{}")
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def bounded_seconds(value, fallback: int, highest: int) -> int:
    return min(max(value, 1), highest) if isinstance(value, int) and not isinstance(value, bool) else fallback


def approve_page(api_url: str, verify_path: str) -> str:
    if not verify_path.startswith("/") or verify_path.startswith("//") or "\\" in verify_path:
        raise AuthoriseError("Arty sent an approve link that isn't on its own site; not opening it.")
    site = urllib.parse.urlsplit(api_url)
    return f"{site.scheme}://{site.netloc}{verify_path}"


def start(api_url: str, headers: dict, name: str = "") -> Request:
    status, answer = post(f"{api_url}/device/start", {"name": name or device_name()}, headers)
    if status == 404:
        raise AuthoriseError("This Arty is too old to authorise devices. Log in with an API key instead.")
    fields = ("device_code", "user_code", "verify_path")
    if status != 200 or not all(isinstance(answer.get(field), str) and answer[field] for field in fields):
        raise AuthoriseError(f"Arty didn't start the authorisation ({status}): {answer.get('error') or 'no details'}")
    return Request(answer["device_code"], answer["user_code"], approve_page(api_url, answer["verify_path"]),
                   bounded_seconds(answer.get("interval"), 3, SLOWEST_POLL_SECONDS),
                   bounded_seconds(answer.get("expires_in"), 600, LONGEST_WAIT_SECONDS))


def wait_for_key(api_url: str, headers: dict, request: Request, sleep=time.sleep, clock=time.monotonic) -> str:
    deadline = clock() + request.expires_in
    while clock() < deadline:
        status, answer = post(f"{api_url}/device/token", {"device_code": request.device_code}, headers)
        if status == 200 and isinstance(answer.get("api_key"), str) and answer["api_key"]:
            return answer["api_key"]
        if status == DENIED:
            raise AuthoriseError("The request was turned down in Arty.")
        if status == EXPIRED:
            break
        if status != PENDING:
            raise AuthoriseError(f"Arty answered the authorisation with an error ({status}): "
                                 f"{answer.get('error') or 'no details'}")
        sleep(request.interval)
    raise AuthoriseError("The code ran out before it was approved. Run `arty-cli login` again.")
