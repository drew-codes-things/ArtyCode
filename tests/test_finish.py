import pytest

from arty_cli import finish

PYTEST_FAILING = ("test_calc.py ..FF.F\n"
                  "FAILED test_calc.py::test_divide - ZeroDivisionError\n"
                  "=========== 13 failed, 17 passed in 0.52s ===========\n[exit code 0]")
PYTEST_PASSING = "test_calc.py ......\n=========== 30 passed in 0.31s ===========\n[exit code 0]"


@pytest.mark.parametrize("command", ["python3 -m pytest -q 2>&1 | tail -40", "pytest", "npm test", "npm run test",
                                     "python -m unittest discover", "cargo test", "go test ./...", "yarn test",
                                     "make test", "npx jest", "./gradlew test"])
def test_test_commands_are_recognised(command):
    assert finish.is_test_command(command)


@pytest.mark.parametrize("command", ["python3 calc.py", "ls tests", "cat test_calc.py", "npm install", "git status"])
def test_other_commands_are_not_test_runs(command):
    assert not finish.is_test_command(command)


def test_a_pipe_that_hides_the_exit_code_still_shows_pytest_failures():
    run = finish.test_run_of("python3 -m pytest 2>&1 | tail -40", PYTEST_FAILING)
    assert run.failed and run.failures == 13
    assert run.summary == "=========== 13 failed, 17 passed in 0.52s ==========="
    assert "FAILED test_calc.py::test_divide" in run.tail


def test_a_passing_run_is_not_a_failure():
    assert not finish.test_run_of("pytest", PYTEST_PASSING).failed


@pytest.mark.parametrize("output, failures", [
    ("Ran 4 tests in 0.01s\n\nFAILED (failures=2, errors=1)\n[exit code 1]", 3),
    ("Tests:       2 failed, 5 passed, 7 total\n[exit code 1]", 2),
    ("test result: FAILED. 3 passed; 1 failed; 0 ignored\n[exit code 101]", 1),
    ("--- FAIL: TestAdd (0.00s)\nFAIL\texample.com/calc\t0.002s\n[exit code 1]", None),
    ("=== 1 error in 0.10s ===\n[job 2 finished with exit code 2]", 1),
])
def test_failures_are_read_from_each_kind_of_test_output(output, failures):
    run = finish.test_run_of("pytest", output)
    assert run.failed and run.failures == failures


def test_a_run_without_an_ending_is_not_counted():
    assert finish.test_run_of("pytest", "[stopped after 120 s while still running]") is None
    assert finish.test_run_of("pytest", "The user didn't allow that, so it wasn't done.") is None
    assert finish.test_run_of("python3 calc.py", "boom\n[exit code 1]") is None


def failing_check():
    check = finish.FinishCheck()
    check.record("python3 -m pytest | tail -5", PYTEST_FAILING)
    return check


def test_a_failing_last_run_asks_the_model_to_keep_fixing_with_the_summary_and_the_output():
    note = failing_check().follow_up()
    assert "13 failed, 17 passed" in note
    assert "FAILED test_calc.py::test_divide" in note
    assert "explain" in note


def test_a_later_passing_run_means_no_follow_up():
    check = failing_check()
    check.record("pytest", PYTEST_PASSING)
    assert check.follow_up() == ""


def test_at_most_two_follow_ups_and_only_while_the_failures_drop():
    check = failing_check()
    assert check.follow_up()
    check.record("pytest", PYTEST_FAILING.replace("13 failed", "4 failed"))
    assert check.follow_up()
    check.record("pytest", PYTEST_FAILING.replace("13 failed", "1 failed"))
    assert check.follow_up() == ""


def test_no_second_follow_up_when_the_failures_did_not_drop():
    check = failing_check()
    assert check.follow_up()
    assert check.follow_up() == ""
    check.record("pytest", PYTEST_FAILING)
    assert check.follow_up() == ""


def test_a_new_request_starts_counting_again_and_forgets_the_old_run():
    check = failing_check()
    assert check.follow_up()
    check.start_request()
    assert check.follow_up() == ""
    check.record("pytest", PYTEST_FAILING)
    assert check.follow_up()


def test_switched_off_it_never_follows_up():
    check = finish.FinishCheck(enabled=False)
    check.record("pytest", PYTEST_FAILING)
    assert check.follow_up() == ""
