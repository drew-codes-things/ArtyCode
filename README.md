<div align="center">

# Arty CLI

**A coding assistant in your terminal: it reads, edits and runs code in the folder you start it in, and asks before it changes anything. It runs on your own Arty.**

![Python](https://img.shields.io/badge/python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white)
![Dependencies](https://img.shields.io/badge/dependencies-none-success?style=flat-square)
[![License](https://img.shields.io/badge/license-PolyForm%20Strict%201.0.0-red?style=flat-square)](LICENSE)

[Project home](https://github.com/drew-codes-things/ArtyCode) · [Arty, the assistant it belongs to](https://github.com/drew-codes-things/Arty)

</div>

---

## Overview

`arty-cli` is a coding assistant for the terminal on Debian (and other Linux) and Windows. You ask in plain words
("why does the login test fail?", "add a --dry-run flag"), and it looks through the project, makes the change and
runs the tests. The file tools and the shell always run on your computer. The thinking happens on your own
[Arty](https://github.com/drew-codes-things/Arty) brain (`arty/qwen3:14b` unless you pick another), so your
code only ever goes to your own server. You need an Arty API key, or to approve this computer in Arty from your browser.

| Tool | What it does | Asks first? |
|---|---|---|
| `read_file`, `list_files`, `search` | Read and find code in the project; a Jupyter notebook reads as numbered cells with their text output. Big files and searches stop at a size limit and say where to read on | Only outside the project folder |
| `edit_file`, `write_file` | Change or create files, keeping each file's line endings. An existing file has to be read first, and is refused if it changed on disk since; every write is atomic (a temporary file, then a swap) | Yes, showing the diff (`--auto-edit` skips this inside the project) |
| `multi_edit` | Several edits to one file in one step, applied in order; all or nothing, so if one edit doesn't match exactly once nothing is changed. One question with the whole diff, and one `/undo` puts it all back | Yes, like an edit |
| `notebook_edit` | Replace, insert or delete one cell of a `.ipynb` notebook (a replaced code cell's old output is cleared) | Yes, like an edit |
| `run_command` | Tests, builds, git (bash on Linux, PowerShell on Windows) | Yes, except look-only commands (below) |
| `command_output`, `stop_command` | Read or stop a command started in the background (a dev server, a long build); background commands end when arty-cli does | No |
| `todo_write` | A checklist for jobs with several steps, shown as it goes (`/todos`) | No |
| `web_fetch` | Read a public web page (documentation, an issue), marked as information rather than instructions | Yes, once per site with **a** |
| `web_search` | Search the web through your Arty brain's own search (its `research__web_search` tool, so no other search key is needed); titles, links and snippets, marked as information rather than instructions. Helpers can search too | Yes, once per session with **a** (also in plan mode, since it changes nothing) |
| `explore` | A helper with its own fresh conversation that looks through the project and reports back; up to four run at once | It can only look |
| `skill` | Loads one of your skills (below) when the job matches it | No |
| `mcp__<server>__<tool>` | Tools from your MCP servers (below) | The first use of each server asks; after that, tools the server marks read-only don't, the others do |

Answer **y** (yes), **n** (no) or **a** (always). "Always" lasts until you quit: for edits it covers every edit, for a
command only that exact command. In a trusted project there is also **s** (save as a project rule): the answer is
written to `.arty/settings.json` as an allow rule (below) for that exact command, that file, that site or that tool, so
later sessions don't ask either. When no rule fits (a chained command, say), **s** lasts the session like **a**. When there's no terminal to ask in (piped input), anything that needs an OK is refused.
Every tool result the model sees is cut to 60,000 characters at most.

`--permission-mode` (or `/mode` in the chat) sets how much is asked: `default`; `auto-edit` (edits inside the project
without asking); `plan` (look and plan only, nothing changed or run; `/plan` switches it; when the model has a plan it
presents it with the `present_plan` tool and you answer **y** to approve it, so plan mode ends and the work carries on
in `default`, **a** to approve it and carry on in `auto-edit`, or **n** to keep planning, with what should change); `allow-all` (nothing asked,
only for a folder you can throw away). The chat prompt starts with the current mode (`plan ›`), and **Shift+Tab** at
the prompt moves on to the next one, keeping what you have typed: default, auto-edit, plan, then default again.
allow-all is never reached that way, only with `/mode allow-all` or the flag. On Linux the prompt uses readline (so
the arrow keys and history work too); on Windows it reads keys itself and handles typing, Backspace, Delete, the
left and right arrows, Home, End, and up and down for earlier lines.

### Look-only commands

`ls`, `cat`, `grep`, `git status`, `git diff` and the like run without a question when they stay inside the project.
Every path is read the way the shell would: `~`, `~+` and `~-`, wildcards (each match is followed through links), and
Windows backslash, drive and network paths. Anything that reaches outside the project, or can't be read, asks first,
in plan mode and in helpers too. A command also asks when it uses an option that writes, runs a program or follows
links out of the project (`rg --pre`, `find -fprint`, `find -L`, `grep -R`, `ls -L`, `du -L`, `tree -l`, `tree -o`,
`file -C`, `diff -r`, `git log --output`, `git branch -D`), and look-only git asks when the repository's own git
settings name a program git would run (an fsmonitor, an external diff, a textconv or filter driver, a gpg program).

### Permission rules

Saved rules skip or forbid questions. Put them in `settings.json` in the config folder (every project) or in
`.arty/settings.json` in a trusted project:

```json
{
  "permissions": {
    "allow": ["run_command(npm test*)", "edit_file(src/*)", "web_fetch(https://docs.python.org/*)", "mcp__notes__find"],
    "deny": ["run_command(rm *)", "read_file(*.env)", "mcp__billing__*"]
  }
}
```

A rule is a tool name, or a tool name with a wildcard pattern in brackets that is matched against the path (relative
to the project), the command, the address or the list pattern. Deny wins over everything, including `allow-all` and
hooks, and is checked against each part of a chained command. An allow rule never matches a command with `;`, `&&`,
`|`, `$` or redirection in it, and never lets plan mode change anything.

## Arty, Arty CLI and starfire.ai

These three projects are made to work together, and each one links to the other two.

| Project | What it is | What it does for the other two |
|---|---|---|
| [Arty](https://github.com/drew-codes-things/Arty) | The assistant: the brain and its API, tools, memory and voice, plus the desktop, Android, iPhone and Quest apps | Answers for both. Arty CLI and starfire.ai send their chats to Arty's API and use its models and tools. Arty's approve page sends a browser that has no Arty key to starfire.ai |
| [Arty CLI](https://github.com/drew-codes-things/ArtyCode) (ArtyCode) | Arty in your terminal: a coding agent that reads, edits and runs code in a project, asking first | Talks only to Arty. Logs in with an Arty API key, or by approving the computer in the browser |
| [starfire.ai](https://github.com/drew-codes-things/starfire.ai) | A web chat dashboard for local and hosted models | Lets you chat with Arty in the browser. Its admin-only **Arty devices** page approves a computer that is logging in (Arty CLI's browser login), lists and turns off approved devices, and makes a new key with a QR code for a phone |

How to link them:

- **Arty to starfire.ai**: set `BRAIN_STARFIRE_URL` in the brain's `brain.env` to starfire's address, so the approve
  page can send people there.
- **starfire.ai to Arty**: add Arty under **Settings > Connections** with its address and API key, and set
  `ARTY_BASE_URL` in starfire's `.env` to exactly the same address (the **Arty devices** page needs it).
  `ARTY_PHONE_URL`, `ARTY_ACCESS_CLIENT_ID` and `ARTY_ACCESS_CLIENT_SECRET` are optional, for phone setup codes that
  work away from home.
- **Arty CLI to Arty**: set `ARTY_URL` (home network) or `ARTY_REMOTE_URL` (from anywhere, with
  `ARTY_ACCESS_CLIENT_ID` and `ARTY_ACCESS_CLIENT_SECRET` if Cloudflare Access guards it), then run `arty login`.

Arty works without the other two. Arty CLI needs Arty. starfire.ai works with other models on its own, but its
**Arty devices** page needs Arty.

## Install

Python 3.11 or newer.

**Debian (and other Linux):** from an arty-cli folder,

```bash
./install.sh
```

It makes its own environment in `~/.local/share/arty-cli` (marked as its own with a `.arty-cli-install` file) and adds
`arty` and `arty-cli` to `~/.local/bin`. If something else is already called `arty` there, it is left alone and you
use `arty-cli`. `./install.sh --uninstall` removes it again and keeps your settings; it refuses to touch a folder it
didn't make. `ARTY_CLI_SOURCE` installs from another folder, a `.tar.gz`/`.zip` URL or a git URL (https only).

**Windows:** in PowerShell, from an arty-cli folder,

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

It finds Python (the `py` launcher first), makes its own environment in `%LOCALAPPDATA%\arty-cli` and adds `arty` and
`arty-cli` to your PATH. Open a new terminal afterwards. `.\install.ps1 -Uninstall` removes it and keeps your
settings in `%APPDATA%\arty-cli`. The same `ARTY_CLI_SOURCE` works.

Then type `arty`. The first run asks how to log in.

With [pipx](https://pipx.pypa.io) instead: `pipx install .`

## Logging in

`arty login` (or `arty-cli login`) offers exactly two ways in:

1. **API key.** The brain's address on your network (`http://<AI_ADDRESS>:8081/v1`), optionally the address from
   anywhere (`https://<your address>/v1`) with its Cloudflare Access client id and secret, and an Arty API key.
2. **Browser login.** Give the addresses as above, then arty-cli shows a short code and opens Arty in your browser.
   Approve it there and arty-cli receives a key of its own for this computer. From outside your home network this
   needs the Cloudflare Access id and secret first. Plain `http://` is only accepted for an address on your own
   network; a public address has to use https.

Everything is saved to `~/.config/arty-cli/config.json` (`%APPDATA%\arty-cli\config.json` on Windows), readable only
by you. For Arty the home address is tried first, then the other. Before the key is sent to a plain `http://` home
address, the brain there has to prove it knows the key (it answers a random challenge at `/v1/identity` without the
key being sent; a per-device key is proved through its SHA-256), so on another network whatever happens to sit at that
address never sees it.

These environment variables override the file: `ARTY_URL`, `ARTY_REMOTE_URL`, `ARTY_API_KEY`,
`ARTY_ACCESS_CLIENT_ID`, `ARTY_ACCESS_CLIENT_SECRET` and `ARTY_CLI_MODEL`.

## Use

```bash
cd my-project
arty                                         # chat (arty-cli works too)
arty -p "what does main.py do?"              # ask once and exit
arty -p "list the TODOs" --output-format json  # ask once, one JSON object back
arty -p "fix the build" --output-format stream-json  # one JSON line per step as it works
arty --continue                              # carry on with the last conversation here
arty --resume <id>                           # an earlier one (/resume lists them)
```

`--output-format json` prints nothing while it works and then one line:
`{"type": "result", "is_error": false, "result": "...", "session_id": "...", "model": "..."}`.
The exit code is 1 when the request failed.

`--output-format stream-json` prints one JSON object per line as the work happens, for scripts that want to follow
along:

| `type` | When | Fields |
|---|---|---|
| `system` | first, once | `subtype: "init"`, `session_id`, `model`, `cwd`, `permission_mode`, `tools` |
| `text` | each piece of the model's answer as it arrives | `text` |
| `tool_use` | the model calls a tool | `id`, `name`, `input` |
| `tool_result` | the tool has finished | `tool_use_id`, `name`, `content`, `is_error` |
| `result` | last, once | the same fields as `json` above |

Notes and questions still go to standard error, so standard output is only the JSON lines.

| In the chat | What it does |
|---|---|
| `@path` | Attach a file; pictures go to the model's vision |
| `# note` | Add a line to this project's `ARTY.md`; the model is told at once |
| `!command` | Run a command yourself; the model sees the output |
| `line \` | Carry on typing on the next line |
| `/undo` | Put back the last file change Arty made (again for the one before); a file Arty created is removed. Refused if the file changed again since |
| `/model` | A numbered list of every model downloaded on your Arty brain. The current one is marked with ● |
| `/model <number or name>` | Switch to that model (`/model 3`, `/model arty/qwen2.5:72b`). Remembered for next time |
| `/init` | Look through the project and write `ARTY.md` (notes read every session) |
| `/review` | Review the uncommitted changes |
| `/compact` | Swap the conversation for a summary (also done automatically when it gets long) |
| `/mcp`, `/skills`, `/hooks` | What's set up of each (below) |
| `/mode`, `/plan`, `/status`, `/todos`, `/memory`, `/resume`, `/clear`, `/help`, `/exit` | The rest |

`ARTY.md` in the project and in the config folder (notes for every project) are added to what the model is told. The
first time you start arty-cli in a folder that has its own `ARTY.md`, `.arty/commands`, `.arty/skills`,
`.arty/settings.json` (hooks and permission rules) or `.arty/mcp.json`, it asks whether you trust that folder (a cloned
repo could put instructions or programs there); until you say yes, they are left out. If `.arty/settings.json` or
`.arty/mcp.json` change later (a pull, a branch switch), it asks again. Adding a `# note` never trusts a folder.
Your own prompts go in `.arty/commands/<name>.md` (or `commands/` in the config folder) and run as `/<name> words`,
with `$ARGUMENTS` replaced by the words. Conversations are saved per project in the config folder's `sessions/`.
Ctrl-C stops the current request, helpers included (what it already changed or ran, MCP calls too, stays noted in the
conversation); Ctrl-D leaves.

### Long conversations

The conversation size is estimated in tokens, and it is summarised when it reaches three quarters of the model's
context window (16k for Arty's local models). `/status` shows the estimate. If summarising fails, the request carries
on unsummarised.

### When Arty is busy

When Arty's brain answers 429 or 503 (all its jobs are taken), arty-cli waits and tries again, up to four tries, about
1, 2 and 4 seconds apart with a little randomness, or as long as the brain's Retry-After asks (up to a minute), showing
`busy, retrying in N s`.

## Skills

A skill is a folder with a `SKILL.md`: saved instructions for one kind of job, plus any files it needs.

```
~/.config/arty-cli/skills/release-notes/SKILL.md     (yours, every project; %APPDATA%\arty-cli on Windows)
my-project/.arty/skills/deploy/SKILL.md              (this project, once trusted)
```

```markdown
---
name: release-notes
description: Write release notes from the git log since the last tag
---
1. Run git describe --tags --abbrev=0, then git log <tag>..HEAD --oneline.
2. Group the changes into Added, Fixed and Changed.
```

The model is told each skill's name and description and loads the full instructions with the `skill` tool when a job
matches. `/skills` lists them.

## Hooks

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
| `Notification` | arty-cli is waiting for your answer to a question | `message`, `notification_type`: `permission_prompt` | Tell you somewhere else (a desktop notice, a phone); its output is ignored |
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

## MCP servers

MCP servers add their tools to the model's list: local ones that arty-cli starts as a program (stdio), and ones
already running somewhere that it reaches over HTTP (the streamable HTTP transport). Put them under `mcpServers` in
`settings.json` in the config folder, or in `.arty/mcp.json` in a trusted project:

```json
{
  "mcpServers": {
    "notes": {"command": "npx", "args": ["-y", "some-notes-mcp-server"], "env": {"NOTES_DIR": "/home/me/notes"}},
    "tickets": {"type": "http", "url": "https://tickets.example/mcp", "headers": {"Authorization": "Bearer ${TICKETS_TOKEN}"}}
  }
}
```

A server with a `url` (http or https) is reached over HTTP, with its `headers` sent on every request; `${NAME}` in a
header is filled from that environment variable, so a token can stay out of the file (a missing one is reported and
that server is skipped). arty-cli keeps the session the server gives it, answers its pings, and ends the session when
it quits.

They start with the session and stop when it ends; one that fails to start is reported and the rest carry on. A bare
program name is looked up on your PATH, never in the project or the current folder. Their tools appear as
`mcp__<server>__<tool>`. The first call to each server in a session asks; after that, tools the server marks read-only
run without asking and the others ask each time (answer **a** to stop asking for that tool). `/mcp` lists the servers
and their tools.

## Looks

Arty's own red on your terminal's background: the wordmark at the start, prompts, questions and the current model are
in Arty red (#e5383b, in 24-bit colour when `COLORTERM` says the terminal has it, the nearest of the 256 colours
otherwise). Errors are bold yellow so they never look like the accent; activity is dim; diffs are green
and red. `NO_COLOR`, or output that isn't a terminal, gives plain text.

## Limits

- On Arty, the brain runs one model on the GPU at a time, so the first coding request after chatting with Arty (and
  the first chat after coding) waits while the other model loads.
- Beta: tested on Debian; the Windows build and `install.ps1` have not been run on Windows yet.
- `web_fetch` checks that an address is public before connecting; a name whose address changes in between could
  still reach a local one.
- Not yet: editor (IDE) integration.

## Tests

```bash
python -m venv .venv && .venv/bin/pip install -e . pytest
.venv/bin/python -m pytest -q tests
```

The tests run against small local stand-in servers; no API key or network is used.
