from arty_cli import doctor, update

DOCTOR, UPDATE = "doctor", "update"


def handles(argv: list[str]) -> bool:
    if argv == [DOCTOR]:
        return True
    return bool(argv) and argv[0] == UPDATE and all(argument.startswith("-") for argument in argv[1:])


def run(argv: list[str]) -> int:
    if argv[0] == DOCTOR:
        return doctor.main(argv[1:])
    return update.main(argv[1:])
