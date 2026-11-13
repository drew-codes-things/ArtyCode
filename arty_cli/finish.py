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
PASSED_COUNT = re.compile(r"\b(\d+) passed\b", re.I)
FAILED_NAMES = (re.compile(r"^(?:FAILED|ERROR) ([^\s(]\S*)"), re.compile(r"^(?:FAIL|ERROR): (\S+)"),
                re.compile(r"^\s*[\u2715\u00d7] (.+?)(?: \(\d+ m?s\))?$"))
MAX_NAMED_TESTS = 8
UNDO_TOOL = "undo_edits"
WORSE_LEAD = "ArtyCode check: your edits made the tests worse."
NEWLY_FAILING_LEAD = "Newly failing:"
WORSE_NOTE = (WORSE_LEAD + " `{command}` gave {before} at an earlier run in this request; now it gives {now}. "
              "{newly}Fix what your last edits broke, or call " + UNDO_TOOL + " to put the files back as they were "
              "at that earlier run, then run the tests again.")
SHOWN_WORSE = "Tests check: Arty's edits made the tests worse ({before}, now {now}); Arty was told and offered undo"
NOTHING_TO_GO_BACK_TO = "Error: there is no better test run in this request to go back to"
TOO_FAR_BACK = "Error: too many changes were made since the better test run for ArtyCode to undo them all"
UNDONE = "Undid {count} change{plural} since the better test run ({before}). Run the tests again to check."
UNDO_STOPPED = "Undid {count} of the changes since the better test run, then stopped: {reason}"


@dataclass(frozen=True)
class TestRun:
    command: str
    exit_code: int
    failed: bool
    summary: str
    tail: str
    failures: int | None
    passed: int | None = None
    failed_names: tuple = ()

    def counts(self) -> str:
        if self.failures is None and self.passed is None:
            return self.summary or f"exit code {self.exit_code}"
        return f"{self.failures or 0} failed, {self.passed or 0} passed"

    def standing(self) -> tuple[int, int]:
        return (self.passed if self.passed is not None else -1), -(self.failures or 0)


@dataclass(frozen=True)
class GoodRun:
    run: TestRun
    last_change: object


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


def passed_count(lines: list[str]) -> int | None:
    for line in reversed(lines):
        found = PASSED_COUNT.search(line)
        if found:
            return int(found.group(1))
    return None


def failed_names(lines: list[str]) -> tuple:
    names = []
    for line in lines:
        found = next((match for pattern in FAILED_NAMES for match in [pattern.match(line)] if match), None)
        if found and found.group(1) not in names:
            names.append(found.group(1))
    return tuple(names)


def same_command(first: str, second: str) -> bool:
    return " ".join(first.split()) == " ".join(second.split())


def newly_failing(run: TestRun, before: TestRun) -> str:
    names = [name for name in run.failed_names if name not in before.failed_names]
    if not names:
        return ""
    more = len(names) - MAX_NAMED_TESTS
    listed = ", ".join(names[:MAX_NAMED_TESTS]) + (f" and {more} more" if more > 0 else "")
    return f"{NEWLY_FAILING_LEAD} {listed}. "


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
    return TestRun(command, exit_code, failed, summary, output_tail(lines), failures, passed_count(lines),
                   failed_names(lines))


def has_fewer_failures(run: TestRun, before: int | None) -> bool:
    return run.failures is not None and before is not None and run.failures < before


@dataclass
class FinishCheck:
    enabled: bool = True
    last: TestRun | None = None
    follow_ups: int = 0
    failures_when_asked: int | None = None
    best: GoodRun | None = None
    went_worse_from: GoodRun | None = None
    notice: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock)

    def start_request(self) -> None:
        with self.lock:
            self.last, self.follow_ups, self.failures_when_asked = None, 0, None
            self.best, self.went_worse_from, self.notice = None, None, ""

    def record(self, command: str, result: str, last_change: object = None) -> str:
        run = test_run_of(command, result)
        if run is None:
            return ""
        with self.lock:
            self.last = run
            return self.compared(run, last_change)

    def compared(self, run: TestRun, last_change: object) -> str:
        best = self.best
        if best is None or not same_command(best.run.command, run.command) or run.standing() >= best.run.standing():
            self.best, self.went_worse_from = GoodRun(run, last_change), None
            return ""
        if last_change is best.last_change:
            return ""
        self.went_worse_from = best
        self.notice = SHOWN_WORSE.format(before=best.run.counts(), now=run.counts())
        return WORSE_NOTE.format(command=run.command, before=best.run.counts(), now=run.counts(),
                                 newly=newly_failing(run, best.run))

    def take_notice(self) -> str:
        with self.lock:
            notice, self.notice = self.notice, ""
        return notice

    def undo_target(self) -> GoodRun | None:
        with self.lock:
            return self.went_worse_from

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


def last_change_of(workspace) -> object:
    return workspace.checkpoints[-1] if workspace.checkpoints else None


def record(workspace, name: str, arguments: dict, result: str) -> str:
    found = check_of(workspace)
    if found is None:
        return ""
    return found.record(ran_command(workspace, name, arguments), result, last_change_of(workspace))


def take_notice(workspace) -> str:
    found = check_of(workspace)
    return found.take_notice() if found is not None else ""


def offers_undo(workspace) -> bool:
    found = check_of(workspace)
    return found is not None and found.undo_target() is not None


def undo_edits(workspace) -> str:
    found = check_of(workspace)
    target = found.undo_target() if found is not None else None
    if target is None:
        return NOTHING_TO_GO_BACK_TO
    if target.last_change is not None and not any(kept is target.last_change for kept in workspace.checkpoints):
        return TOO_FAR_BACK
    count = 0
    while workspace.checkpoints and last_change_of(workspace) is not target.last_change:
        outcome = workspace.undo_last()
        if not outcome.startswith("Undid"):
            return UNDO_STOPPED.format(count=count, reason=outcome)
        count += 1
    with found.lock:
        found.went_worse_from = None
    return UNDONE.format(count=count, plural="" if count == 1 else "s", before=target.run.counts())


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
