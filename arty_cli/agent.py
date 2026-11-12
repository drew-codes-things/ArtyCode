import contextlib
import json
import queue
import re
import threading

from arty_cli import extras as extra_tools
from arty_cli import checks, context, finish, hooks, offering, permissions, prune, remote, tools

STUCK_REPEATS = 3
ANNOUNCED_STEP = re.compile(r"\b(let'?s|let me(?! know)|i'?ll|i will|i'?m going to|next,? i)\b[^.?!]{0,80}\b(check|look|"
                            r"read|open|run|search|find|inspect|examine|fix|edit|change|update|try|see|test|start|"
                            r"create|write|install|build|add|implement|verify|download|launch|serve)\b", re.I)
NOT_DONE_YET = re.compile(r"\bI (?:did not|didn't|have not|haven't) (?:yet |actually )?(?:test|try|tried|run|ran|check|"
                          r"start|install|create|write|download|open|verify)", re.I)
NOT_DONE_ANSWER = "You said you haven't done that yet. Do it now with your tools, then report what it showed."
ANNOUNCED_STEP_TAIL_CHARS = 300
DO_IT_NOW = "Do that now with your tools rather than describing it."
ASKS_TO_CARRY_ON = re.compile(r"\b(?:(?:shall|should|can|may) i|would you like me to|do you want me to|want me to|"
                              r"let me know if you(?:'d like| would like| want) me to)\s+(?:continue|proceed|go ahead|"
                              r"go on|carry on|keep going|move on)\b", re.I)
CARRY_ON_ANSWER = "Yes, carry on until the whole job is done; you don't need to ask me before each step."
UNFINISHED_LEAD = "Your checklist still has unfinished items:"
UNFINISHED_ASK = ("Carry on with them now. Only stop to ask me if you are blocked or need a decision only I can "
                  "make; if an item is already done or no longer needed, update the checklist.")
CUT_SHORT_NOTE = "(I hit my reply length limit, so this was cut short. Ask me to continue.)"
CUT_SHORT_ANSWER = ("Your last reply was cut off at the reply length limit, so nothing in it was used. Carry on from "
                    "where you were in smaller steps: write a long file in parts (create it with the first part, "
                    "then add the rest with edit_file).")
CUT_REPLY_KEPT_CHARS = 600
CUT_SHORT, ANNOUNCED, NOT_DONE, ASKED, UNFINISHED = "cut short", "announced", "not done", "asked", "unfinished"
NUDGES_PER_STRETCH = {CUT_SHORT: 3, ANNOUNCED: 1, NOT_DONE: 1, ASKED: 1, UNFINISHED: 1}
UNFINISHED_STATUSES = ("pending", "in_progress")
MAX_EXPLORE_ANSWER_CHARS = 8_000
REFUSED = permissions.REFUSED
RECORDED_TOOLS = tools.CHANGING_TOOLS + ("run_command",)
STUCK_NOTE = ("You have made the same tool calls several times in a row with the same results. Don't call them "
              "again: answer now with what you have, or say what is blocking you.")
STUCK_STOPPED = "I stopped because I kept repeating the same step. Tell me how you'd like to go on."
STUCK_WINDOW = 8
ANCHOR_ROUNDS = 8
ANCHOR_REQUEST_CHARS = 600
ANCHOR_LEAD = "Carry on with the request. It was:"
ANCHOR_CHECKLIST_LEAD = "Your checklist so far:"
TODO_TOOL = "todo_write"
NEAR_WINDOW_NOTE = ("The last prompt used {used} of the model's {window} tokens, near the end of the model's context "
                    "window; older results will be hidden to make room.")
WINDOW_OVERFLOW = re.compile(r"context (?:window|length|size)|too long|too many tokens|exceeds", re.I)
STUCK_FAILING_NOTE = ("You have made the same tool calls several times and they keep failing with the same error. "
                      "Don't make that call again: do it another way (for an edit that keeps missing, replace the "
                      "whole function or rewrite the file), or say what is blocking you.")
EDITING_TOOLS = ("edit_file", "multi_edit")
SMALL_FILE_LINES = 200
REPEATED_MISS = ("\nThat is miss {count} on {path}. Stop copying small pieces: read it again and replace the whole "
                 "function or block in one edit_file, with old_text copied from that read")
SMALL_FILE_WAY = ", or, as {path} is short ({lines} lines), rewrite it with write_file and overwrite: true"
HOOK_BLOCKED = "A hook stopped this call:"
HOOK_FEEDBACK = "Hook feedback:"
HOOK_CONTEXT = "Note from a hook:"
STOP_HOOK_LEAD = "A Stop hook asked you to carry on:"
MAX_STOP_CONTINUES = 3
SUBAGENT_STOP_LEAD = "A SubagentStop hook asked you to carry on:"
EXPLORE_NAME = "explore"
NOTHING_CHANGED_YET = ("You haven't changed anything since the plan was approved; the earlier attempts were refused. "
                       "Make the changes with your tools now, then say what you changed.")
HELPER_TOOL = "helper"
MAX_STEP_CHARS = 100
STEP_TARGETS = ("path", "pattern", "command", "url", "query", "task", "job", "name")
STEP_LABELS = {"read_file": "Read", "write_file": "Write", "edit_file": "Edit", "multi_edit": "Edit",
               "notebook_edit": "Edit the notebook", "list_files": "List", "search": "Search for", "run_command": "Run",
               "command_output": "Read the output of job", "stop_command": "Stop job",
               "todo_write": "Update the checklist", "web_fetch": "Fetch", "web_search": "Search the web for",
               "explore": "Explore", HELPER_TOOL: "Hand to the helper", "skill": "Load the skill",
               permissions.PLAN_TOOL: "Present the plan"}
STEP_JOINS = {"explore": ": "}
MCP_PREFIX, MCP_SEPARATOR = "mcp__", "__"
NUMBERED_LINE = re.compile(r"\d+\t")
MORE_LINES = re.compile(r"^\.\.\. (\d+) more lines", re.M)
MORE_ITEMS = re.compile(r"^\.\.\. and (\d+) more$", re.M)
RULE_CHARACTERS = "=-_*# "
MAX_OUTPUT_LINE_CHARS = 72
EXIT_CODE = re.compile(r"\[exit code (-?\d+)\]")
ERROR_LEAD = "Error"
SAID_NO = "Not done: you said no"
COUNTED_RESULTS = {"search": ("match", "matches"), "list_files": ("file", "files")}
NOTHING_FOUND_LEADS = ("No matches", "No files")
HELPER_ROLE = ("You are a helper inside ArtyCode, called by the main agent for one job. Do it with your tools, then "
               "reply briefly with what you found or did, naming files and line numbers.")
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
EXPLORER_PROMPT = ("You are a helper inside ArtyCode. Answer the one question you are given by looking through the "
                   "project with your tools. Only look; never try to change anything. Reply briefly with what you "
                   "found, naming files and line numbers.")
PLAN_DEFINITION = tools.function(
    permissions.PLAN_TOOL,
    "Plan mode is on. When you have looked enough, present your plan here: the steps, the files you will change "
    "and how you will check it works. The user approves it (plan mode ends and you carry it out) or rejects it "
    "with feedback (revise the plan and present it again).",
    {"plan": tools.TEXT}, ["plan"])


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


def one_line(text: str, limit: int = MAX_STEP_CHARS) -> str:
    first = next((line.strip() for line in str(text).splitlines() if line.strip()), "")
    return first if len(first) <= limit else first[:limit - 1] + "…"


def step_target(name: str, arguments: dict) -> str:
    if name == "search":
        folder = arguments.get("path")
        return f"{arguments.get('pattern') or ''}{f' in {folder}' if folder else ''}"
    if name == HELPER_TOOL:
        return f"{arguments.get('name') or ''}: {arguments.get('task') or ''}"
    return str(next((arguments[key] for key in STEP_TARGETS if arguments.get(key)), ""))


def step_label(name: str) -> str:
    if name.startswith(MCP_PREFIX) and name.count(MCP_SEPARATOR) >= 2:
        _prefix, server, tool = name.split(MCP_SEPARATOR, 2)
        return f"{server}: {tool}"
    return STEP_LABELS.get(name, name)


def step_line(name: str, arguments: dict) -> str:
    target = one_line(step_target(name, arguments))
    return f"{step_label(name)}{STEP_JOINS.get(name, ' ')}{target}".strip() if target else step_label(name)


def checklist_outcome(result: str) -> str:
    marks = [line[:3] for line in result.splitlines()]
    done = marks.count(tools.TODO_MARKS["done"])
    listed = sum(mark in tools.TODO_MARKS.values() for mark in marks)
    return f"{done} of {listed} done" if listed else one_line(result)


def counted(number: int, thing: str, things: str = "") -> str:
    return f"{number} {thing if number == 1 else things or thing + 's'}"


def read_outcome(result: str) -> str:
    numbered = sum(1 for line in result.splitlines() if NUMBERED_LINE.match(line))
    if not numbered:
        return one_line(result)
    more = MORE_LINES.search(result)
    return counted(numbered, "line") + (f" ({more.group(1)} more below)" if more else "")


def command_outcome(result: str) -> tuple[str, bool]:
    if result.startswith(tools.STOPPED_LEAD):
        return one_line(result.split(",", 1)[0].removeprefix("[")), True
    lines = [line.strip() for line in result.splitlines() if line.strip()]
    ending = EXIT_CODE.fullmatch(lines[-1]) if lines else None
    if ending is None:
        return one_line(result), False
    last = one_line(lines[-2].strip(RULE_CHARACTERS), MAX_OUTPUT_LINE_CHARS) if len(lines) > 1 else ""
    last = last or "no output"
    return f"{last} (exit code {ending.group(1)})", ending.group(1) != "0"


def listed_count(result: str) -> int:
    shown = len([line for line in result.splitlines() if not line.startswith("...")])
    more = MORE_ITEMS.search(result)
    return shown + (int(more.group(1)) if more else 0)


def outcome_line(name: str, result: str) -> tuple[str, bool]:
    if result.startswith(ERROR_LEAD):
        return one_line(result), True
    if result.startswith(permissions.REFUSED_LEAD):
        return SAID_NO, False
    if name == "run_command":
        return command_outcome(result)
    if name == "read_file":
        return read_outcome(result), False
    if name == "todo_write":
        return checklist_outcome(result), False
    if name in COUNTED_RESULTS and not result.startswith(NOTHING_FOUND_LEADS):
        return counted(listed_count(result), *COUNTED_RESULTS[name]), False
    return one_line(result), False


def show_step(screen, name: str, arguments: dict) -> None:
    shown = getattr(screen, "show_step", None)
    line = step_line(name, arguments)
    if shown is None:
        screen.show_activity(line)
    else:
        shown(line)


def helper_definition(found: dict) -> dict:
    listed = "\n".join(f"- {helper.name}: {helper.description}" for helper in found.values())
    return tools.function(
        HELPER_TOOL,
        "Hand a job to one of the user's own helpers, each with its own instructions, tools and fresh conversation; "
        f"it reports back when done. Call it several times in one reply to run up to four at once. Helpers:\n{listed}",
        {"name": {"type": "string", "enum": list(found)}, "task": tools.TEXT}, ["name", "task"])


def request_of(messages: list[dict]) -> str:
    last = messages[-1] if messages else {}
    return remote.shown_content(last.get("content")).strip() if last.get("role") == "user" else ""


def updates_checklist(calls: list[dict]) -> bool:
    return any((call.get("function") or {}).get("name") == TODO_TOOL for call in calls)


def anchor_note(request: str, todos: list[dict]) -> str:
    shown = tools.clipped(request, ANCHOR_REQUEST_CHARS)
    if not todos:
        return f"{ANCHOR_LEAD}\n{shown}"
    listed = "\n".join(f"{tools.TODO_MARKS.get(item.get('status'), '')} {item.get('text', '')}".strip()
                       for item in todos)
    return f"{ANCHOR_LEAD}\n{shown}\n{ANCHOR_CHECKLIST_LEAD}\n{listed}"


def offered_definitions(extras, situational=None) -> list[dict]:
    custom = [helper_definition(extras.helpers)] if extras.helpers else []
    return offering.kept(tools.DEFINITIONS + [EXPLORE_DEFINITION] + custom + extras.definitions(), situational)


def planning_definitions(gate) -> list[dict]:
    return [PLAN_DEFINITION] if gate.mode == permissions.PLAN else []


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


class WaitingQuestion:
    def __init__(self, question: str, preview: str, scope: str):
        self.question = question
        self.preview = preview
        self.scope = scope
        self.answer = permissions.NO
        self.answered = threading.Event()


class Helpers:
    def __init__(self, brain, workspace, gate, screen, extras):
        self.brain = brain
        self.workspace = workspace
        self.gate = gate
        self.screen = screen
        self.extras = extras.without_stop_hooks()
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.asking_thread = threading.current_thread()
        self.waiting = queue.Queue()

    def ask(self, question: str, preview: str = "", scope: str = "") -> str:
        if self.stopped.is_set():
            return permissions.NO
        if threading.current_thread() is not self.asking_thread:
            return self.asked_for_helper(question, preview, scope)
        with self.lock:
            return permissions.NO if self.stopped.is_set() else self.gate.ask(question, preview, scope)

    def asked_for_helper(self, question: str, preview: str, scope: str) -> str:
        waiting = WaitingQuestion(question, preview, scope)
        self.waiting.put(waiting)
        while not waiting.answered.wait(HELPER_JOIN_SECONDS):
            if self.stopped.is_set():
                return permissions.NO
        return permissions.NO if self.stopped.is_set() else waiting.answer

    def answer_waiting_questions(self) -> None:
        while not self.waiting.empty():
            waiting = self.waiting.get_nowait()
            waiting.answer = self.ask(waiting.question, waiting.preview, waiting.scope)
            waiting.answered.set()

    def helper_gate(self, mode: str):
        found = permissions.Gate(self.workspace, self.ask, mode=mode)
        found.always = self.gate.always
        found.rules = self.gate.rules
        found.approved_servers = self.gate.approved_servers
        return found

    def explore(self, task: str) -> str:
        return self.helped(EXPLORE_NAME, EXPLORER_PROMPT, task, explorer_definitions() + self.extras.definitions(),
                           permissions.PLAN)

    def custom(self, found: dict, arguments: dict) -> str:
        name = str(arguments.get("name") or "")
        helper = found.get(name)
        if helper is None:
            return f"Error: there is no helper called {name}. Helpers: {', '.join(found) or 'none'}"
        offered = [definition for definition in tools.DEFINITIONS + self.extras.definitions()
                   if definition["function"]["name"] in helper.tools]
        planning = self.gate.mode == permissions.PLAN or not helper.changes_things()
        return self.helped(helper.name, f"{HELPER_ROLE}\n\n{helper.prompt}", str(arguments.get("task") or ""),
                           offered, permissions.PLAN if planning else self.gate.mode)

    def kept_in_room(self, offered: list[dict]):
        limit = context.compact_limit(self.brain, offered)

        def squeeze(messages: list[dict], overflowed: bool = False) -> None:
            tightest = prune.overflow_limit(messages, self.brain, limit) if overflowed else limit
            prune.prune(messages, tightest, self.brain)
            prune.cut_newest(messages, tightest, self.brain)

        return squeeze

    def helped(self, kind: str, prompt: str, task: str, offered: list[dict], mode: str) -> str:
        gate = self.helper_gate(mode)
        messages = [{"role": "system", "content": prompt}, {"role": "user", "content": task}]
        continued = 0
        while True:
            answer = run_turn(messages, self.brain, self.workspace, gate, Quiet(self.screen, self.lock),
                              offered=offered, can_explore=False, extras=self.extras, stop_hooks=False,
                              stopped=self.stopped, squeeze=self.kept_in_room(offered))
            if self.stopped.is_set() or not self.sent_back(kind, task, answer, messages, continued):
                return answer[:MAX_EXPLORE_ANSWER_CHARS] or "The helper found nothing to report."
            continued += 1

    def sent_back(self, kind: str, task: str, answer: str, messages: list[dict], continued: int) -> bool:
        outcome = self.extras.hook("SubagentStop", {"stop_hook_active": continued > 0, "agent_type": kind,
                                                    "task": task, "last_message": answer})
        show_problems(self.screen, outcome)
        if not outcome.blocked or continued >= MAX_STOP_CONTINUES:
            return False
        messages.append({"role": "user", "content": f"{SUBAGENT_STOP_LEAD} {outcome.feedback}".strip()})
        return True


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


def reason_not_to_run(name: str, arguments: dict, workspace, gate, extras, decision: str, reason: str) -> str | None:
    checks_first = name in tools.CHANGING_TOOLS and gate.mode != permissions.PLAN
    problem = tools.change_problem(workspace, name, arguments) if checks_first else ""
    return problem or gate_refusal(name, arguments, gate, extras, decision, reason)


def done_line(name: str, arguments: dict, result: str) -> str:
    line = activity_line(name, arguments)
    return f"{line}: {command_outcome(result)[0]}" if name == "run_command" else line


def performed(name: str, arguments: dict, workspace, gate, extras, done: list, decision: str = "",
              reason: str = "") -> str:
    try:
        refusal = reason_not_to_run(name, arguments, workspace, gate, extras, decision, reason)
    except (tools.ToolError, OSError, ValueError) as error:
        return f"Error: {error}"
    if refusal:
        return refusal
    if extras.handles(name):
        result = extras.run(name, arguments)
        if extras.may_change_things(name):
            done.append(activity_line(name, arguments))
        return result
    recorded = name in RECORDED_TOOLS
    watched = checks.watch(workspace, name, arguments)
    try:
        result = tools.run(name, arguments, workspace)
    except KeyboardInterrupt:
        if recorded:
            done.append(f"{activity_line(name, arguments)} (stopped part way)")
        raise
    finish.record(workspace, name, arguments, result)
    if recorded and (name == "run_command" or not result.startswith("Error")):
        done.append(done_line(name, arguments, result))
    return checks.checked_result(workspace, watched, result)


def call_parts(call: dict) -> tuple[str, dict | None, str]:
    function = call.get("function") or {}
    name = function.get("name", "")
    try:
        arguments = parsed_arguments(function.get("arguments"))
    except ValueError as error:
        return name, None, f"Error: the arguments for {name} weren't valid JSON ({error}); send them again"
    try:
        return name, tools.typed_arguments(name, arguments), ""
    except tools.ToolError as error:
        return name, None, f"Error: {error}; send the call again"


def working(screen, line: str, on_main_thread: bool):
    shown = getattr(screen, "working", None)
    return shown(line) if shown is not None and on_main_thread else contextlib.nullcontext()


def result_of(call: dict, helpers: Helpers, can_explore: bool, done: list, extras) -> str:
    name, arguments, problem = call_parts(call)
    if arguments is None:
        return problem
    show_step(helpers.screen, name, arguments)
    before = extras.hook("PreToolUse", {"tool_name": name, "tool_input": arguments}, name)
    show_problems(helpers.screen, before)
    if before.blocked:
        blocked = f"{HOOK_BLOCKED} {before.feedback}".strip()
        tell(helpers.screen, "show_outcome", one_line(blocked), True)
        return blocked
    with working(helpers.screen, step_line(name, arguments), threading.current_thread() is helpers.asking_thread):
        result = tool_result(name, arguments, helpers, can_explore, done, extras, before)
    tell(helpers.screen, "show_outcome", *outcome_line(name, result))
    if name == "todo_write" and not result.startswith(ERROR_LEAD):
        tell(helpers.screen, "show_todos", helpers.workspace.todos)
    result = tools.clipped(result, helpers.workspace.result_chars)
    if before.context:
        result = f"{result}\n\n{HOOK_CONTEXT} {before.context}"
    after = extras.hook("PostToolUse", {"tool_name": name, "tool_input": arguments, "tool_response": result}, name)
    show_problems(helpers.screen, after)
    if after.context:
        result = f"{result}\n\n{HOOK_CONTEXT} {after.context}"
    return f"{result}\n\n{HOOK_FEEDBACK} {after.feedback}".rstrip() if after.blocked else result


def tool_result(name: str, arguments: dict, helpers: Helpers, can_explore: bool, done: list, extras, before) -> str:
    if name == "explore":
        if not can_explore:
            return "Error: a helper can't start another helper; look for it yourself"
        return helpers.explore(str(arguments.get("task", "")))
    if name == HELPER_TOOL and can_explore and extras.helpers:
        return helpers.custom(extras.helpers, arguments)
    if name == permissions.PLAN_TOOL and can_explore:
        return helpers.gate.review_plan(str(arguments.get("plan") or ""))
    return performed(name, arguments, helpers.workspace, helpers.gate, extras, done, before.decision,
                     before.feedback)


def is_explore(call: dict) -> bool:
    return (call.get("function") or {}).get("name") in (EXPLORE_NAME, HELPER_TOOL)


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
            helpers.answer_waiting_questions()
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


def is_approval(call: dict, result: str) -> bool:
    named = (call.get("function") or {}).get("name") == permissions.PLAN_TOOL
    return named and result.startswith(permissions.PLAN_APPROVED_LEAD)


def tell(screen, event: str, *details) -> None:
    shown = getattr(screen, event, None)
    if shown is not None:
        shown(*details)


def thinking(screen):
    shown = getattr(screen, "thinking", None)
    return shown() if shown is not None else contextlib.nullcontext()


def signature_of(calls: list[dict], results: list[str]) -> str:
    return json.dumps([(call.get("function") or {}).get("name") for call in calls]
                      + [(call.get("function") or {}).get("arguments") for call in calls] + results)


def failing_signatures(calls: list[dict], results: list[str]) -> set[str]:
    return {json.dumps([(call.get("function") or {}).get("name"), (call.get("function") or {}).get("arguments"),
                        result]) for call, result in zip(calls, results) if result.startswith(ERROR_LEAD)}


def repeated_failure(window: list[set]) -> str | None:
    counted = {}
    for signatures in window:
        for signature in signatures:
            counted[signature] = counted.get(signature, 0) + 1
    return next((signature for signature, count in counted.items() if count >= STUCK_REPEATS), None)


def file_lines_of(workspace, path: str) -> int:
    try:
        return len(tools.read_text(workspace.resolve(path)).splitlines())
    except (tools.ToolError, OSError, ValueError):
        return 0


def miss_advice(workspace, path: str, count: int) -> str:
    lines = file_lines_of(workspace, path)
    small = SMALL_FILE_WAY.format(path=path, lines=lines) if 0 < lines <= SMALL_FILE_LINES else ""
    return REPEATED_MISS.format(count=count, path=path) + small + "."


def advised_results(calls: list[dict], results: list[str], misses: dict, workspace) -> list[str]:
    advised = []
    for call, result in zip(calls, results):
        name, arguments, _problem = call_parts(call)
        is_miss = name in EDITING_TOOLS and result.startswith(ERROR_LEAD) and tools.NOT_FOUND in result
        if not is_miss or arguments is None:
            advised.append(result)
            continue
        path = str(arguments.get("path") or "")
        misses[path] = misses.get(path, 0) + 1
        advised.append(result + miss_advice(workspace, path, misses[path]) if misses[path] >= 2 else result)
    return advised


def waits_on_running_jobs(calls: list[dict], results: list[str]) -> bool:
    names = {(call.get("function") or {}).get("name") for call in calls}
    return names == {"command_output"} and all(result.rstrip().endswith(f"{tools.STILL_RUNNING}]")
                                               for result in results)


def asked_to_carry_on(messages: list[dict], extras, screen, continued: int) -> bool:
    outcome = extras.hook("Stop", {"stop_hook_active": continued > 0})
    show_problems(screen, outcome)
    if not outcome.blocked or continued >= MAX_STOP_CONTINUES:
        return False
    messages.append({"role": "user", "content": f"{STOP_HOOK_LEAD} {outcome.feedback}".strip()})
    return True


def announces_a_next_step(content: str) -> bool:
    tail = content.strip()[-ANNOUNCED_STEP_TAIL_CHARS:]
    return bool(tail) and not tail.endswith("?") and bool(ANNOUNCED_STEP.search(tail))


def asks_to_carry_on(content: str) -> bool:
    return bool(ASKS_TO_CARRY_ON.search(content.strip()[-ANNOUNCED_STEP_TAIL_CHARS:]))


def says_it_is_not_done(content: str) -> bool:
    return bool(NOT_DONE_YET.search(content.strip()[-ANNOUNCED_STEP_TAIL_CHARS:]))


def is_cut_short(content: str) -> bool:
    return content.rstrip().endswith(CUT_SHORT_NOTE)


def unfinished_items(todos: list[dict]) -> list[dict]:
    return [item for item in todos if item.get("status") in UNFINISHED_STATUSES]


def unfinished_note(todos: list[dict]) -> str:
    listed = "\n".join(f"{tools.TODO_MARKS[item['status']]} {item.get('text', '')}" for item in unfinished_items(todos))
    return f"{UNFINISHED_LEAD}\n{listed}\n{UNFINISHED_ASK}"


def nudge_for(content: str, workspace, gate, carries_the_job: bool, used: list[str]) -> tuple[str, str]:
    keeps_working = carries_the_job and gate.mode != permissions.PLAN
    checks = ((CUT_SHORT, is_cut_short(content), CUT_SHORT_ANSWER),
              (ANNOUNCED, announces_a_next_step(content), DO_IT_NOW),
              (NOT_DONE, says_it_is_not_done(content), NOT_DONE_ANSWER),
              (ASKED, keeps_working and asks_to_carry_on(content), CARRY_ON_ANSWER),
              (UNFINISHED, keeps_working and bool(unfinished_items(workspace.todos)), ""))
    for reason, applies, answer in checks:
        if applies and used.count(reason) < NUDGES_PER_STRETCH[reason]:
            return reason, answer or unfinished_note(workspace.todos)
    return "", ""


def kept_reply(content: str, reason: str) -> str:
    if reason != CUT_SHORT or len(content) <= CUT_REPLY_KEPT_CHARS:
        return content
    return content[:CUT_REPLY_KEPT_CHARS] + "…"


def is_window_overflow(error: Exception) -> bool:
    return bool(WINDOW_OVERFLOW.search(str(error)))


def replied(brain, messages: list[dict], offered: list[dict], screen, squeeze) -> dict:
    try:
        return brain.reply(messages, tools=offered, on_text=screen.show_text)
    except Exception as error:
        if squeeze is None or not is_window_overflow(error):
            raise
    squeeze(messages, True)
    return brain.reply(messages, tools=offered, on_text=screen.show_text)


def run_turn(messages: list[dict], brain, workspace, gate, screen, offered=None, can_explore=True, done=None,
             squeeze=None, extras=None, stop_hooks=True, stopped=None, steer=None, on_round=None) -> str:
    extras = extras or extra_tools.NONE
    offered = offered_definitions(extras) + planning_definitions(gate) if offered is None else offered
    if stop_hooks:
        checks.start_turn(workspace)
        finish.start_request(workspace)
    done = [] if done is None else done
    helpers = Helpers(brain, workspace, gate, screen, extras)
    request, since_anchor = request_of(messages), 0
    recent, continued, steered_from, nudges = [], 0, None, []
    approved_with, reminded = None, False
    failures, steered_failures, misses, warned_near_window = [], set(), {}, False
    while True:
        if stopped is not None and stopped.is_set():
            return HELPER_STOPPED
        with thinking(screen):
            reply = replied(brain, messages, offered, screen, squeeze)
        tell(screen, "count_reply", reply)
        context.calibrate(brain, messages, offered, reply)
        if context.near_window(brain, reply) and not warned_near_window:
            warned_near_window = True
            tell(screen, "show_activity", NEAR_WINDOW_NOTE.format(used=context.prompt_tokens_of(reply),
                                                                  window=context.window_tokens(brain)))
        if stopped is not None and stopped.is_set():
            return HELPER_STOPPED
        content = reply.get("content") or ""
        calls = reply.get("tool_calls") or []
        if not calls:
            if approved_with is not None and not reminded and len(done) == approved_with:
                messages.append({"role": "assistant", "content": content})
                reminded = True
                messages.append({"role": "user", "content": NOTHING_CHANGED_YET})
                continue
            reason, answer = nudge_for(content, workspace, gate, can_explore, nudges) if offered else ("", "")
            messages.append({"role": "assistant", "content": kept_reply(content, reason)})
            if reason:
                nudges.append(reason)
                messages.append({"role": "user", "content": answer})
                continue
            if stop_hooks and finish.sent_back(workspace, gate.mode == permissions.PLAN, messages, screen):
                continue
            if stop_hooks and checks.sent_back(workspace, messages, content, screen):
                continue
            if stop_hooks and asked_to_carry_on(messages, extras, screen, continued):
                continued += 1
                continue
            return content
        nudges = []
        messages.append({"role": "assistant", "content": content or None, "tool_calls": calls})
        for call in calls:
            tell(screen, "show_tool_call", call)
        raw_results = results_of(calls, helpers, can_explore, done, extras)
        results = advised_results(calls, raw_results, misses, workspace)
        for call, result in zip(calls, results):
            messages.append({"role": "tool", "tool_call_id": call.get("id") or "", "content": result})
            tell(screen, "show_tool_result", call, result)
            if is_approval(call, result):
                approved_with = len(done)
        if squeeze:
            squeeze(messages)
        if on_round:
            on_round()
        steered = steer() if steer else []
        if steered:
            messages.extend(steered)
            recent, failures, since_anchor = [], [], 0
            continue
        since_anchor = 0 if updates_checklist(calls) else since_anchor + 1
        if waits_on_running_jobs(calls, results):
            continue
        signature = signature_of(calls, raw_results)
        failures = (failures + [failing_signatures(calls, raw_results)])[-STUCK_WINDOW:]
        failing = repeated_failure(failures)
        if signature == steered_from or (failing is not None and failing in steered_failures):
            messages.append({"role": "assistant", "content": STUCK_STOPPED})
            return STUCK_STOPPED
        if failing is not None:
            messages.append({"role": "user", "content": STUCK_FAILING_NOTE})
            steered_failures.add(failing)
            continue
        recent = (recent + [signature])[-STUCK_REPEATS:]
        if len(recent) == STUCK_REPEATS and len(set(recent)) == 1:
            messages.append({"role": "user", "content": STUCK_NOTE})
            steered_from, recent = signature, []
            continue
        if request and since_anchor >= ANCHOR_ROUNDS:
            messages.append({"role": "user", "content": anchor_note(request, workspace.todos)})
            since_anchor = 0
