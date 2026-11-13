# Use

[Back to the README](../README.md)

## Starting it

```bash
cd my-project
arty                                         # chat (arty-cli works too)
arty -p "what does main.py do?"              # ask once and exit
arty -p "list the TODOs" --output-format json  # ask once, one JSON object back
arty -p "fix the build" --output-format stream-json  # one JSON line per step as it works
arty --continue                              # carry on with the last conversation here
arty --resume <id>                           # an earlier one (/resume picks one in the chat)
arty --remote                                # chat, shared with your phone and starfire.ai (see Remote control)
```

## Output for scripts

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

## In the chat

| In the chat | What it does |
|---|---|
| `@path` | Attach a file with numbered lines, as if Arty had read it (so it can edit it at once); pictures go to the model's vision |
| `# note` | Add a line to this project's `ARTY.md`; the model is told at once |
| `!command` | Run a command yourself; the model sees the output |
| `line \` | Carry on typing on the next line |
| `/undo` | Put back the last file change Arty made (again for the one before); a file Arty created is removed. Refused if the file changed again since |
| `/rewind` | A numbered list of your requests since ArtyCode started (or since `/clear` or a summary), with how many file changes going back would undo. `/rewind 2` drops request 2 and everything after it from the conversation and puts back the files Arty changed since, newest first, using `/undo`'s snapshots; a file you changed yourself since is left alone and the rewind of files stops there |
| `/model` | A numbered list of every model downloaded on your Arty brain. The current one is marked with ● |
| `/model <number or name>` | Switch to that model (`/model 3`, `/model arty/qwen2.5:72b`). Remembered for next time |
| `/init` | Look through the project and write `ARTY.md` (notes read every session) |
| `/review` | Review the uncommitted changes |
| `/compact` | Swap the conversation for a summary (also done automatically when it gets long) |
| `/mcp`, `/skills`, `/helpers`, `/hooks` | What's set up of each ([MCP servers](mcp-servers.md), [skills](skills.md), [helpers](helpers.md), [hooks](hooks.md)) |
| `/remote`, `/remote name <new name>`, `/remote off` | Share this chat with the Arty apps and starfire.ai, rename it there, or stop sharing ([Remote control](remote.md)) |
| `/rules`, `/rules on`, `/rules tell-only`, `/rules off` | Show or change the built-in working rules for this session ([Built-in working rules](working-rules.md)) |
| `?` or `/help` | The commands and keys, in short groups (your own commands last) |
| `/last` (or Ctrl-O in the interface) | Everything the last step with output gave back, where only a few lines were shown |
| `/mode`, `/plan`, `/status`, `/todos`, `/memory`, `/resume`, `/clear`, `/exit` | The rest |

## Project notes, trusted folders and saved conversations

`ARTY.md` in the project and in the config folder (notes for every project) are added to what the model is told, and so
is a project's `AGENTS.md` (the shared file other coding tools read), after `ARTY.md` when both are there. The
first time you start ArtyCode in a folder that has its own `ARTY.md` or `AGENTS.md`, `.arty/commands`, `.arty/skills`,
`.arty/settings.json` (hooks and permission rules) or `.arty/mcp.json`, it asks whether you trust that folder (a cloned
repo could put instructions or programs there); until you say yes, they are left out. If `.arty/settings.json` or
`.arty/mcp.json` change later (a pull, a branch switch), it asks again. Adding a `# note` never trusts a folder.
Your own prompts go in `.arty/commands/<name>.md` (or `commands/` in the config folder) and run as `/<name> words`,
with `$ARGUMENTS` replaced by the words. Conversations are saved per project in the config folder's `sessions/`, after every step (so a crash or a closed terminal loses at most the step in progress), with the checklist and the last 20 `/undo` steps. `/resume` picks one from a list in the interface (or `/resume <id>` at the plain prompt) and carries on with it in place; a resumed conversation gets today's system prompt, notes and rules.
While it works, a dim status line says what it is doing, for how long the request has been going, how many tokens the
model has written so far and how to stop it: `• Thinking… (1m 05s · ↓ 1.2k tokens · esc to interrupt)` while it waits
for the model, `• Run npm install (2m 10s · ...)` while a step runs. It is wiped as soon as the answer or a step
appears, and a dim `✻ Thought for 12s` line takes its place when the model took a second or more before its answer
or step (if the brain streams its reasoning apart from the answer, that counts towards the tokens; it is never
printed). All of this is only when both output and errors go to a terminal, so piped and `-p` output stays plain.
On a terminal the answer, notes and steps wrap between words at the window's width, with a word that would run past
the edge moved to the next line; code blocks, diffs and the output of `!commands` are left as written. When a request that
took steps ends, one line says how long it worked, how many steps it took and how many tokens the model wrote.
Esc stops the current request (so does Ctrl-C at the plain prompt), helpers included (what it already changed or ran, MCP calls too, stays noted
in the conversation); a question waiting for your answer reads keys normally. Ctrl-D leaves.
