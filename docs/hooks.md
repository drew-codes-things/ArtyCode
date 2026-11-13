# Hooks

[Back to the README](../README.md)

Hooks run your own commands when something happens, set in `settings.json` in the config folder (every project) or
`.arty/settings.json` in a trusted project:

```json
{
  "hooks": {
    "PreToolUse": [{"matcher": "edit_file|write_file", "hooks": [{"type": "command", "command": "./check-path.sh"}]}],
    "PostToolUse": [{"matcher": "edit_file", "hooks": [{"type": "command", "command": "ruff check --quiet .", "timeout": 30}]}],
    "Stop": [{"hooks": [{"type": "command", "command": "./tests-must-pass.sh"}]}],
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "date"}]}]
  }
}
```

Each hook gets a JSON description on standard input (`session_id`, `hook_event_name`, `cwd`, and `tool_name`,
`tool_input`, `tool_response`, `prompt` or `stop_hook_active` depending on the event) and runs in the project folder.
`matcher` is a regular expression for the tool name; empty or `*` means every tool.

Four more events:

| Event | When | Extra fields | What it can do |
|---|---|---|---|
| `SessionStart` | a chat or `-p` request starts, `--continue`/`--resume` picks one up, or `/clear` | `source`: `startup`, `resume` or `clear` (`matcher` matches it) | Anything printed (or `additionalContext`) is added to the conversation |
| `PreCompact` | before the conversation is summarised | `trigger`: `manual` (`/compact`) or `auto` (`matcher` matches it) | Exit code 2 or `{"decision": "block"}` skips the summary; anything printed is added to the summary instructions |
| `Notification` | ArtyCode is waiting for your answer to a question | `message`, `notification_type`: `permission_prompt` | Tell you somewhere else (a desktop notice, a phone); its output is ignored |
| `SubagentStop` | an `explore` helper has finished | `agent_type`, `task`, `last_message`, `stop_hook_active` | Exit code 2 or `{"decision": "block", "reason": "..."}` sends the helper back to work with the reason (at most 3 times) | A hook that runs past its timeout
(60 seconds unless set) is stopped together with everything it started.

| Exit code | PreToolUse | PostToolUse | Stop | UserPromptSubmit |
|---|---|---|---|---|
| 0 | carry on | carry on | stop | carry on; anything printed is added to your request |
| 2 | the call is blocked and its error output goes to the model | the error output goes to the model | the model carries on with the error output as its next instruction (at most 3 times a request) | the request is stopped and the error output shown to you |
| other | shown to you, carry on | shown to you | shown to you | shown to you |

With exit code 0, a hook can print a JSON object instead:

| Field | Effect |
|---|---|
| `{"hookSpecificOutput": {"permissionDecision": "allow"\|"deny"\|"ask", "permissionDecisionReason": "..."}}` | PreToolUse only. `allow` skips the question (never plan mode or a deny rule), `deny` blocks the call with the reason, `ask` asks even for a look-only call |
| `{"hookSpecificOutput": {"additionalContext": "..."}}` | Added to the tool result (tool events) or to your request (UserPromptSubmit) |
| `{"decision": "block", "reason": "..."}` | Blocks, as exit code 2 does, with the reason |
| `{"continue": false, "stopReason": "..."}` | Stops, with the reason |

`/hooks` lists what's set up.
