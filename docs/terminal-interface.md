# The terminal interface

[Back to the README](../README.md)

On a terminal ArtyCode runs as its own full-screen interface (built on [prompt_toolkit](https://github.com/prompt-toolkit/python-prompt-toolkit),
with [Pygments](https://pygments.org) colouring code). It opens in the terminal's alternate screen: what was on the
terminal goes away while it runs and comes back when you leave, with `/exit`, Ctrl-D, Ctrl-C twice, or when it stops
on an error. The Arty logo and the session's details are the first thing in the conversation and scroll away as it
grows; the input box and the footer stay at the bottom of the terminal. On the way out it prints the command that
carries the chat on, once something has been asked:

```
Resume this chat: arty --resume 20261113-101500-123456
```

(`--project <folder>` is added when you started it for another folder.) A chat looks like this:

```
     ▄████▄
    ██▀  ▀██               ██
   ██   ▄▄▄██▄ ██▄████   ███████  ██    ██
  ██▄▄██▀▀  ██ ██▀         ██     ██    ██   ArtyCode 0.1.0
  ██▀       ██ ██          ▀█▄▄▄  ▀██▄▄███   coding
                                  ▄▄▄▄▄▄█▀

  model    arty/qwen3:14b
  folder   ~/calc-demo
  context  24,576 tokens
  mode     default: asks before it changes anything
  rules    off

  Esc stops a request · Enter while Arty works queues your next message
  ? for shortcuts · /help for every command · @ attaches a file
  No ARTY.md here yet: /init looks through the project and writes notes Arty reads every time.

› fix the add function in calc.py

✻ Thought for 1s
● Update the checklist
  └ 0 of 3 done
    ☐ Read calc.py
    ☐ Fix add
    ☐ Run the tests

✻ Thought for 1s
● Read calc.py
  └ 2 lines

✻ Thought for 1s
● Run python3 -c "for n in range(1, 41): print(f'line {n} of the long output')"
Run: python3 -c "for n in range(1, 41): print(f'line {n} of the long output')" → Yes
  └ line 40 of the long output (exit code 0)
    line 1 of the long output
    line 2 of the long output
    line 3 of the long output
    … +37 lines (ctrl+o or /last to see all)

✻ Thought for 1s
● Edit calc.py
    1  def add(a, b):
    2 -    return a - b
    2 +    return a + b
Edit calc.py? → Yes
  └ Edited calc.py +1 -1

✻ Thought for 1s
● Run python3 -c "import calc; assert calc.add(2, 3) == 6, 'add(2, 3) gave 5'"
Run: python3 -c "import calc; assert calc.add(2, 3) == 6, 'add(2, 3) gave 5'" → Yes
  └ AssertionError: add(2, 3) gave 5 (exit code 1)
    … 2 lines above (ctrl+o or /last to see all)
        import calc; assert calc.add(2, 3) == 6, 'add(2, 3) gave 5'
                            ^^^^^^^^^^^^^^^^^^^
    AssertionError: add(2, 3) gave 5

✻ Thought for 1s
● Update the checklist
  └ 3 of 3 done
    ☑ Read calc.py
    ☑ Fix add
    ☑ Run the tests

✻ Thought for 1s
● What I changed

  The bug was in calc.py: add subtracted instead of adding.

  • Fixed add so it returns a + b
  • Ran a quick check, which failed on purpose to show an error

  │ def add(a, b):
  │     return a + b

  Run the check again with python3 -m pytest.

Worked for 12s · 6 steps · ↓ 2.1k tokens


╭────────────────────────────────────────────────────────────────────────────────────────────────────────────╮
│ › Ask Arty to fix, explain or build something                                                              │
╰────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
  default mode (shift+tab to cycle) · ? for shortcuts                       arty/qwen3:14b · 90% context left
```

- **Header.** The Arty logo in block letters, in Arty's red, with `ArtyCode` and the version beside it, then the
  model, folder, context window, mode and rules, then two short tips. Under 60 columns the logo is a smaller one,
  and under 40 it is a single line.
- **Scrolling.** The conversation scrolls inside the interface, since the terminal's own scrollback isn't used while
  it runs: **PageUp** and **PageDown** move a page, the mouse wheel three lines. While you are scrolled back a line
  above the box says how many lines are below; sending a message, or a question from Arty, brings you back to the
  newest lines. The mouse is the interface's while it runs, so to select text with it hold **Shift** as you drag
  (most terminals).
- **Your messages** are drawn as a shaded block starting with `›`, so they stand out from what Arty writes.
- **Answers** are rendered as they stream: headings and `**bold**` turn bold, `` `code` `` and lists are drawn
  without their marks, and fenced code keeps its spacing behind a `│` with its syntax coloured.
- **Steps.** Each step is a `●` line in plain words (`Read greet.py`, `Run pytest -q`) with its result hung under it
  after `└`. The bullet turns green when the step went well and red when it failed. Command, search and listing
  output shows its first three lines and how many more there are (the last three for a failed command, where the error
  is); **Ctrl-O** or `/last` prints the whole result of the last step with output.
- **Edits** are shown as a diff with line numbers, removed lines red and added lines green, and the result line counts
  them (`└ Edited greet.py +1 -1`). In auto-edit mode, where nothing is asked first, the diff is drawn after the edit.
- **Status.** One line above the box shows a spinner, what is happening (`Thinking…` or the step), for how long, the
  tokens so far and that Esc interrupts. On a narrow window the details shorten before the label does.
- **The box** has a border and a placeholder, and grows as you type several lines. Under it the footer shows the mode,
  `? for shortcuts`, and on the right the model, the git branch and how much of the context is left before the
  conversation gets summarised; on a narrow window the extras drop away first.
- **Shortcuts.** `?` in an empty box opens a panel of keys under it; Enter there shows `/help`, Esc or `?` closes it,
  and typing anything else closes it and keeps what you typed.
- **Typing while Arty works.** The box is always live. Enter while a request runs queues your message (shown as
  `queued: ...` above the box). A plain message reaches Arty at its next step, so it can change course mid-request;
  a `/command`, `!command` or `# note` waits until the turn ends. **Esc** stops the running request, keeping every step
  that finished (and what it showed) in the conversation and dropping only the unfinished one. **Ctrl-C** clears
  what you typed; on an empty line it asks once more and the second Ctrl-C leaves. **Ctrl-D** on an empty line leaves.
- **Menus.** `/` opens a list of commands under the box that narrows as you type, each with what it does; after
  `/model ` it lists the models; `@` completes file paths inside the project. Up and Down move through it, Tab or
  Enter takes the highlighted entry, Esc closes it.
- **History.** Up and Down walk through earlier requests, kept per project between sessions (in the config folder's
  `history/`, readable only by you). **Ctrl-R** searches them.
- **Several lines.** Alt+Enter (or Ctrl-J, or a line ending in `\`) starts another line; pasted text of several lines
  stays one message.
- **Questions.** Permission questions, plan approval and the model list from `/model` are asked in a bordered panel
  that takes the box's place: move with Up and Down or press the answer's letter or number, Enter confirms, Esc
  refuses. Nothing is chosen until you pick, so Enter on its own does nothing. Keys that arrive in the first half
  second, or while you are still typing without a pause, are ignored, and a letter that is not one of the answers
  clears the choice: a sentence typed ahead while Arty works can never answer a question. The answer is then noted in the conversation (`Edit greet.py? → Yes`). "What should Arty do instead?" and
  plan feedback are typed in the box (what you were writing is put back afterwards). With remote control on, an answer
  from another device still wins or loses the same way as before.
- **Notices.** When a permission question is waiting, or a request finishes after more than 30 seconds, the terminal
  bell rings and, on Linux with `notify-send`, a desktop notice appears. Set `notify` to `off` in the config file (or
  `ARTY_CLI_NOTIFY=off`) to turn both off.
- **Resizing.** Making the window narrower or wider redraws the whole screen for the new width: long lines wrap
  again, and the header switches between the logo sizes.

`--plain` keeps the line-at-a-time prompt instead: a `›` between two rules with the mode bar under it, read with
readline on Linux (arrow keys and history work) and key by key on Windows. The plain prompt is also what you get
when output isn't a terminal, with `-p`, and when the interface can't start (it says why). It prints the same
`Resume this chat:` line when you leave, with the name you started ArtyCode as. Anything typed before a question is
shown there is thrown away when the question appears, so only an answer typed after it counts.

## Looks

Arty's own red on your terminal's background: the wordmark at the start, prompts, questions and the current model are
in Arty red (#e5383b, in 24-bit colour when `COLORTERM` says the terminal has it, the nearest of the 256 colours
otherwise). Errors are bold yellow so they never look like the accent; activity is dim; diffs are green
and red. Each step the model takes is one line after a red `●` in plain words (`Read greet.py`, `Run pytest -q`,
`Search for def greet in src`), with a short dim result under it (`└ 40 lines`, `└ 1 passed in 0.02s (exit code 0)`,
`└ Edited greet.py`, `└ Not done: you said no`); an error or a command that exits with a failure code is shown in the
error colour. The model still gets the whole result. `NO_COLOR`, or output that isn't a terminal, gives plain text.

The terminal interface keeps to the terminal's own foreground and background and a few colours that read on dark and
light themes alike: Arty red for the brand, the prompt and waiting steps, green and red for good and failed steps and
for diffs, cyan for code, dim for details. The only shading is your own messages, a grey block with its own text
colour so it reads on any background (a light one when `COLORFGBG` says the terminal is light). Windows Terminal
(`WT_SESSION`) gets 24-bit colour; `TERM=linux` gets the basic eight. With `NO_COLOR` the borders, bullets and layout
stay and every colour goes.
