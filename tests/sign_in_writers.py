from arty_cli import sign_ins

SERVER = "https://x.example/mcp"


def record(name: str, round_number: int) -> sign_ins.Record:
    return sign_ins.Record(SERVER, SERVER, "https://x.example", "https://x.example/token", "client", "", "none",
                           f"access-{name}-{round_number}", f"refresh-{name}-{round_number}", 0.0, "")


def write_rounds(folder: str, name: str, rounds: int, barrier) -> None:
    store = sign_ins.Store(folder)
    barrier.wait()
    for round_number in range(rounds):
        store.put(name, record(name, round_number))


def read_until(folder: str, names: list[str], stop, barrier) -> None:
    store = sign_ins.Store(folder)
    barrier.wait()
    while not stop.is_set():
        for name in names:
            store.get(name)
