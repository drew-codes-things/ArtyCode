import json
import threading

from arty_cli import extras as extra_tools
from arty_cli import hooks, permissions, tools

MAX_ROUNDS = 40
STUCK_REPEATS = 3
MAX_EXPLORE_ANSWER_CHARS = 8_000
MAX_RESULT_CHARS = 60_000
REFUSED = permissions.REFUSED
RECORDED_TOOLS = tools.CHANGING_TOOLS + ("run_command",)
STEP_LIMIT_REACHED = "I stopped after a lot of steps. Tell me if you want me to keep going."
STUCK_NOTE = ("You have made the same tool calls several times in a row with the same results. Don't call them "
              "again: answer now with what you have, or say what is blocking you.")
STUCK_STOPPED = "I stopped because I kept repeating the same step. Tell me how you'd like to go on."
HOOK_BLOCKED = "A hook stopped this call:"
HOOK_FEEDBACK = "Hook feedback:"
HOOK_CONTEXT = "Note from a hook:"
STOP_HOOK_LEAD = "A Stop hook asked you to carry on:"
MAX_STOP_CONTINUES = 3
MAX_HELPERS = 4
HELPER_STOPPED = "The helper was stopped."
HELPER_JOIN_SECONDS = 0.2
EXPLORE_TOOLS = ("read_file", "list_files", "search", "web_fetch", "run_command")
EXPLORE_DEFINITION = tools.function(
    "explore",
    "Hand a looking-only job to a helper with a fresh conversation, e.g. 'find where sessions are saved and how' "
    "or 'what calls parse_config?'. It reads and searches on its own and reports back, keeping this conversation "
    "short. It can't change anything. Call explore several times in one reply to run up to four helpers at once.",
    {"task": tools.TEXT}, ["task"])
EXPLORER_PROMPT = ("You are a helper inside arty-cli. Answer the one question you are given by looking through the "
                   "project with your tools. Only look; never try to change anything. Reply briefly with what you "
                   "found, naming files and line numbers.")


def parsed_arguments(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    arguments = json.loads(raw or "{}")
    if not isinstance(arguments, dict):
        raise ValueError("the arguments should be a JSON object")
    return arguments


def activity_line(name: str, arguments: dict) -> str:
    target = (arguments.get("path") or arguments.get("pattern") or arguments.get("command") or arguments.get("url")
              or arguments.get("task") or arguments.get("job") or "")
    return f"{name} {target}".strip()


def offered_definitions(extras) -> list[dict]:
    return tools.DEFINITIONS + [EXPLORE_DEFINITION] + extras.definitions()


def explorer_definitions() -> list[dict]:
    return [definition for definition in tools.DEFINITIONS if definition["function"]["name"] in EXPLORE_TOOLS]


class Quiet:
    def __init__(self, screen, lock=None):
        self.screen = screen
        self.lock = lock or threading.Lock()

    def show_text(self, text: str) -> None:
        pass

    def show_activity(self, line: str) -> None:
        with self.lock:
            self.screen.show_activity(f"  {line}")


class Helpers:
    def __init__(self, brain, workspace, gate, screen, extras):
        self.brain = brain
        self.workspace = workspace
        self.gate = gate
        self.screen = screen
        self.extras = extras.without_stop_hooks()
        self.lock = threading.Lock()
        self.stopped = threading.Event()

    def ask(self, question: str, preview: str = "") -> str:
        if self.stopped.is_set():
            return permissions.NO
        with self.lock:
            return permissions.NO if self.stopped.is_set() else self.gate.ask(question, preview)

    def explore(self, task: str) -> str:
        helper_gate = permissions.Gate(self.workspace, self.ask, mode=permissions.PLAN)
        helper_gate.always = self.gate.always
        helper_gate.rules = self.gate.rules
        helper_gate.approved_servers = self.gate.approved_servers
        messages = [{"role": "system", "content": EXPLORER_PROMPT}, {"role": "user", "content": task}]
        answer = run_turn(messages, self.brain, self.workspace, helper_gate, Quiet(self.screen, self.lock),
                          offered=explorer_definitions(), can_explore=False, extras=self.extras, stop_hooks=False,
                          stopped=self.stopped)
        return answer[:MAX_EXPLORE_ANSWER_CHARS] or "The helper found nothing to report."


def show_problems(screen, outcome) -> None:
    for problem in outcome.problems:
        screen.show_activity(f"hook: {problem}")


def gate_refusal(name: str, arguments: dict, gate, extras, decision: str = "", reason: str = "") -> str | None:
    denied = gate.rule_refusal(name, arguments)
    if denied:
        return denied
    if decision == hooks.ALLOW and gate.mode != permissions.PLAN:
        return None
    if decision == hooks.ASK:
        return gate.confirm(f"A hook asks you to confirm {activity_line(name, arguments)}"
                            f"{f' ({reason})' if reason else ''}?")
    return extras.check(name, arguments, gate) if extras.handles(name) else gate.check(name, arguments)


def performed(name: str, arguments: dict, workspace, gate, extras, done: list, decision: str = "",
              reason: str = "") -> str:
    checks_first = name in tools.CHANGING_TOOLS and gate.mode != permissions.PLAN
    problem = tools.change_problem(workspace, name, arguments) if checks_first else ""
    if problem:
        return problem
    refusal = gate_refusal(name, arguments, gate, extras, decision, reason)
    if refusal:
        return refusal
    if extras.handles(name):
        result = extras.run(name, arguments)
        if extras.may_change_things(name):
            done.append(activity_line(name, arguments))
        return result
    recorded = name in RECORDED_TOOLS
    try:
        result = tools.run(name, arguments, workspace)
    except KeyboardInterrupt:
        if recorded:
            done.append(f"{activity_line(name, arguments)} (stopped part way)")
        raise
    if recorded and (name == "run_command" or not result.startswith("Error")):
        done.append(activity_line(name, arguments))
    return result


def call_parts(call: dict) -> tuple[str, dict | None, str]:
    function = call.get("function") or {}
    name = function.get("name", "")
    try:
        return name, parsed_arguments(function.get("arguments")), ""
    except ValueError as error:
        return name, None, f"Error: the arguments for {name} weren't valid JSON ({error}); send them again"


def result_of(call: dict, helpers: Helpers, can_explore: bool, done: list, extras) -> str:
    name, arguments, problem = call_parts(call)
    if arguments is None:
        return problem
    helpers.screen.show_activity(activity_line(name, arguments))
    before = extras.hook("PreToolUse", {"tool_name": name, "tool_input": arguments}, name)
    show_problems(helpers.screen, before)
    if before.blocked:
        return f"{HOOK_BLOCKED} {before.feedback}".strip()
    if name == "explore":
        if not can_explore:
            return "Error: a helper can't start another helper; look for it yourself"
        result = helpers.explore(str(arguments.get("task", "")))
    else:
        result = performed(name, arguments, helpers.workspace, helpers.gate, extras, done, before.decision,
                           before.feedback)
    result = tools.clipped(result, MAX_RESULT_CHARS)
    if before.context:
        result = f"{result}\n\n{HOOK_CONTEXT} {before.context}"
    after = extras.hook("PostToolUse", {"tool_name": name, "tool_input": arguments, "tool_response": result}, name)
    show_problems(helpers.screen, after)
    if after.context:
        result = f"{result}\n\n{HOOK_CONTEXT} {after.context}"
    return f"{result}\n\n{HOOK_FEEDBACK} {after.feedback}".rstrip() if after.blocked else result


def is_explore(call: dict) -> bool:
    return (call.get("function") or {}).get("name") == "explore"


def results_of_helpers(calls: list[dict], exploring: list[int], helpers: Helpers, done: list, extras,
                       results: list[str]) -> None:
    slots = threading.Semaphore(MAX_HELPERS)

    def run_one(index: int) -> None:
        with slots:
            try:
                results[index] = HELPER_STOPPED if helpers.stopped.is_set() else result_of(
                    calls[index], helpers, True, done, extras)
            except Exception as error:
                results[index] = f"Error: the helper failed ({error})"

    threads = [threading.Thread(target=run_one, args=(index,), daemon=True) for index in exploring]
    for thread in threads:
        thread.start()
    for thread in threads:
        while thread.is_alive():
            thread.join(HELPER_JOIN_SECONDS)


def results_of(calls: list[dict], helpers: Helpers, can_explore: bool, done: list, extras) -> list[str]:
    exploring = [index for index, call in enumerate(calls) if is_explore(call)] if can_explore else []
    results = [""] * len(calls)
    if len(exploring) > 1:
        try:
            results_of_helpers(calls, exploring, helpers, done, extras, results)
        except KeyboardInterrupt:
            helpers.stopped.set()
            raise
    for index, call in enumerate(calls):
        if len(exploring) <= 1 or index not in exploring:
            results[index] = result_of(call, helpers, can_explore, done, extras)
    return results


def signature_of(calls: list[dict]) -> str:
    return json.dumps([(call.get("function") or {}).get("name") for call in calls]
                      + [(call.get("function") or {}).get("arguments") for call in calls])


def asked_to_carry_on(messages: list[dict], extras, screen, continued: int) -> bool:
    outcome = extras.hook("Stop", {"stop_hook_active": continued > 0})
    show_problems(screen, outcome)
    if not outcome.blocked or continued >= MAX_STOP_CONTINUES:
        return False
    messages.append({"role": "user", "content": f"{STOP_HOOK_LEAD} {outcome.feedback}".strip()})
    return True


def run_turn(messages: list[dict], brain, workspace, gate, screen, offered=None, can_explore=True, done=None,
             squeeze=None, extras=None, stop_hooks=True, stopped=None) -> str:
    extras = extras or extra_tools.NONE
    offered = offered_definitions(extras) if offered is None else offered
    done = [] if done is None else done
    helpers = Helpers(brain, workspace, gate, screen, extras)
    recent, continued, steered_from = [], 0, None
    for _round in range(MAX_ROUNDS + 1):
        if stopped is not None and stopped.is_set():
            return HELPER_STOPPED
        reply = brain.reply(messages, tools=offered, on_text=screen.show_text)
        if stopped is not None and stopped.is_set():
            return HELPER_STOPPED
        content = reply.get("content") or ""
        calls = reply.get("tool_calls") or []
        if not calls:
            messages.append({"role": "assistant", "content": content})
            if stop_hooks and asked_to_carry_on(messages, extras, screen, continued):
                continued += 1
                continue
            return content
        messages.append({"role": "assistant", "content": content or None, "tool_calls": calls})
        for call, result in zip(calls, results_of(calls, helpers, can_explore, done, extras)):
            messages.append({"role": "tool", "tool_call_id": call.get("id") or "", "content": result})
        if squeeze:
            squeeze(messages)
        signature = signature_of(calls)
        if signature == steered_from:
            messages.append({"role": "assistant", "content": STUCK_STOPPED})
            return STUCK_STOPPED
        recent = (recent + [signature])[-STUCK_REPEATS:]
        if len(recent) == STUCK_REPEATS and len(set(recent)) == 1:
            messages.append({"role": "user", "content": STUCK_NOTE})
            steered_from, recent = signature, []
    messages.append({"role": "assistant", "content": STEP_LIMIT_REACHED})
    return STEP_LIMIT_REACHED
