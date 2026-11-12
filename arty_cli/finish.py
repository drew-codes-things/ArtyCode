import re
import threading
from dataclasses import dataclass, field

TEST_COMMAND = re.compile(r"\b(pytest|py\.test|unittest|tox|nox|jest|vitest|mocha|rspec|phpunit|ctest)\b|"
                          r"\b(npm|yarn|pnpm|bun|deno|go|cargo|make|dotnet|mvn|gradle|gradlew|mix|swift)\s+"
                          r"(?:run\s+)?test\b")
EXIT_CODE_AT_THE_END = re.compile(r"\[(?:job \S+ finished with )?exit code (-?\d+)\]\s*$")
FAILURE_COUNTS = re.compile(r"\b(\d+) (?:failed|failures?|errors?)\b", re.I)
UNITTEST_COUNTS = re.compile(r"(?:failures|errors)=(\d+)")
UNITTEST_FAILED = re.compile(r"^FAILED \(")
MAX_FOLLOW_UPS = 2
TAIL_LINES = 25
MAX_TAIL_CHARS = 2_500
FOLLOW_UP = ("Before you finish: the last test run still fails ({summary}), from `{command}`. The end of its output:"
             "\n```\n{tail}\n```\n"
             "Keep fixing the code until the tests pass and run them again to show it. If they can't pass, explain "
             "clearly why, naming the tests that still fail.")
SHOWN_NOTE = "Finish check: the last test run still fails ({summary}), so Arty was asked to keep going"


@dataclass(frozen=True)
class TestRun:
    command: str
    exit_code: int
    failed: bool
    summary: str
    tail: str
    failures: int | None


def is_test_command(command: str) -> bool:
    return bool(TEST_COMMAND.search(command or ""))


def counted_failures(line: str) -> int | None:
    counts = UNITTEST_COUNTS.findall(line) if UNITTEST_FAILED.match(line) else FAILURE_COUNTS.findall(line)
    return sum(int(count) for count in counts) if counts else None


def failure_line(lines: list[str]) -> tuple[str, int | None]:
    for line in reversed(lines):
        failures = counted_failures(line.strip())
        if failures:
            return line.strip(), failures
    return "", None


def output_tail(lines: list[str]) -> str:
    tail = "\n".join(lines[-TAIL_LINES:]).strip()
    return tail if len(tail) <= MAX_TAIL_CHARS else "..." + tail[-MAX_TAIL_CHARS:]


def test_run_of(command: str, result: str) -> TestRun | None:
    ended = EXIT_CODE_AT_THE_END.search(result or "")
    if not is_test_command(command) or ended is None:
        return None
    lines = [line for line in result[:ended.start()].splitlines() if line.strip()]
    summary, failures = failure_line(lines)
    exit_code = int(ended.group(1))
    failed = exit_code != 0 or bool(failures)
    if failed and not summary:
        summary = lines[-1].strip() if lines else f"exit code {exit_code}"
    return TestRun(command, exit_code, failed, summary, output_tail(lines), failures)


def has_fewer_failures(run: TestRun, before: int | None) -> bool:
    return run.failures is not None and before is not None and run.failures < before


@dataclass
class FinishCheck:
    enabled: bool = True
    last: TestRun | None = None
    follow_ups: int = 0
    failures_when_asked: int | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def start_request(self) -> None:
        with self.lock:
            self.last, self.follow_ups, self.failures_when_asked = None, 0, None

    def record(self, command: str, result: str) -> None:
        run = test_run_of(command, result)
        if run is not None:
            with self.lock:
                self.last = run

    def follow_up(self) -> str:
        with self.lock:
            run = self.last
            if not self.enabled or run is None or not run.failed or self.follow_ups >= MAX_FOLLOW_UPS:
                return ""
            if self.follow_ups and not has_fewer_failures(run, self.failures_when_asked):
                return ""
            self.follow_ups += 1
            self.failures_when_asked = run.failures
        return FOLLOW_UP.format(summary=run.summary, command=run.command, tail=run.tail)


def check_of(workspace) -> FinishCheck | None:
    return getattr(workspace, "finish_check", None)


def is_on(workspace) -> bool:
    found = check_of(workspace)
    return found is not None and found.enabled


def start_request(workspace) -> None:
    found = check_of(workspace)
    if found is not None:
        found.start_request()


def ran_command(workspace, name: str, arguments: dict) -> str:
    if name == "run_command":
        return str(arguments.get("command") or "")
    if name == "command_output":
        job = workspace.jobs.get(str(arguments.get("job")))
        return job.command if job is not None else ""
    return ""


def record(workspace, name: str, arguments: dict, result: str) -> None:
    found = check_of(workspace)
    if found is not None:
        found.record(ran_command(workspace, name, arguments), result)


def sent_back(workspace, planning: bool, messages: list[dict], screen) -> bool:
    found = check_of(workspace)
    note = found.follow_up() if found is not None and not planning else ""
    if not note:
        return False
    shown = getattr(screen, "show_activity", None)
    if shown is not None:
        shown(SHOWN_NOTE.format(summary=found.last.summary))
    messages.append({"role": "user", "content": note})
    return True
