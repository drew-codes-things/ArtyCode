# Long jobs

[Back to the README](../README.md)

A request carries on for as many steps as it needs: there is no cap on steps, rounds or time, so "make me a
calculator" or "make me a yt-dlp download web application" runs until the job is done, checked and summed up (what was
built, how it was checked and the command to run it). The model is told to keep a checklist for jobs with several
steps and to work through it without asking whether to carry on. If it stops early anyway, it is sent back once each
time: when it only says what it will do next or that it hasn't done something yet (tested it, say), when it asks "shall I continue?", when its checklist still has unfinished
items, and (up to three times) when its reply was cut off at the brain's reply length limit, with a note to write long
files in parts. A real question for you, or a second stop in a row, ends the request as usual. The only thing that
stops a job on its own is the same step with the same result over and over, or the same failing call three times in eight steps (a miss, a read, the same miss again). It is warned once first, and only that same failing call made again after the warning stops it, with the reason shown on screen; doing something else carries on. Waiting on a
background build that is still running doesn't count. Every eight steps without a new checklist, the model is
reminded of the request (shortened to 600 characters) and the checklist so far, so a small model keeps sight of the job
deep into a long request.

The model's instructions come in short labelled sections (looking, changing files, tests, commands, working through a
job, finishing); guidance for tools that are only offered sometimes lives in those tools' own descriptions.

The finish check: when the model ends a request while the last test run it made in that request still fails (a
non-zero exit, or output such as `13 failed, 17 passed`, `FAILED (failures=2)` or `Tests: 2 failed`, so a pytest piped
through `tail` counts too), ArtyCode sends it back with that summary line and the end of the test output, asking it to
keep fixing or to explain clearly why the tests can't pass. A dim `Finish check:` line shows when that happens. It is
sent at most twice per request, the second time only if fewer tests fail than the first time, never in plan mode, and
not for a request you stopped. Set `finish_check` to `off` in the config file (or `ARTY_CLI_FINISH_CHECK=off`) to
turn it off.

Test runs are also compared within a request. When files were changed since the best run of the same test command so
far and the new run is worse (it fails where that run passed, by its exit code, so runners without counts such as
`go test` are understood; or, both failing, tests no longer run at all, as when a test file stops importing, or fewer
tests pass, or as many pass and more fail; a run that passes is never worse, so removing an obsolete test is fine),
the run's result tells the model so plainly, with both counts and the tests that newly fail, and a dim
`Tests check:` line shows it. From then on the model is also offered `undo_edits`, which puts the files back as they
were at that better run using `/undo`'s snapshots; it asks you first, every time, naming how many changes and which
files it would put back (**a** and **s** count as yes for that once and save nothing), is refused in plan mode, and
nothing is put back unless the model calls it.

Waiting for the model has no time limit. Connecting gives up after 30 seconds, but once connected ArtyCode waits as
long as the brain keeps the reply alive (Arty's brain sends a `: working` line every 15 seconds while a model loads or
writes), so a long task that keeps sending that line is never cut off. If nothing at all arrives for 120 seconds,
not even that line (the brain was restarted part way, say), the request ends with "Arty's brain stopped answering"
instead of waiting forever; send it again once the brain is back.

## Long conversations

The conversation size is estimated in tokens (about 3.5 characters each until the brain reports a real count: when its `usage.prompt_tokens` shows a prompt was bigger than estimated, the estimate is raised to match for the rest of the session, and a prompt within 10% of the window is noted on screen). The room kept for the reply is the `reply_tokens` the brain's model list reports for the model in use (the smallest listed when that model isn't there), or 2,048 tokens. When it no longer leaves room in the model's context
window (`context_tokens` if set, otherwise the size the brain reports for the model in use, then the smallest it lists, otherwise 12288 tokens) for the tool list and a full reply, older
results are hidden first, with no model call, once the conversation passes 90% of that room: each becomes one line
naming the step and what it showed (`[Hidden to save room: run_command pytest -q (3 failed, 9 passed, exit code 1).
...]`). Earlier reads of the same lines of a file that was read again or changed since go first, then all but the
newest run of a command and the long text of past writes and edits (shortened once), then the oldest results outside
the newest five steps, then outside the newest four, and so on, until the conversation is at 50% of the room. Calls and
their results stay in place, the newest step is never touched, the newest read, edit or miss of each file that was
changed is kept, and a file whose reads were all hidden has to be read again before it is edited. When that leaves no
read of the file changed last since its last change, that file is attached, numbered, to the newest result (it counts
as read), and the footer's context meter is measured again. Only if all that is not enough is it summarised. If the brain still refuses a conversation as longer than the model's window (a 413
`context_length_exceeded`, or a streamed reply ending in that error), the refusal is not shown as the answer: the
conversation is cut down harder and sent once more, and a second refusal ends the request with a message saying so. `/status` shows the
estimate. A summary in the middle of a request keeps the request itself, the checklist, the changes made and the
commands run with what each showed, the end of the last command's output and the file changed last (numbered, so it
can be edited at once), then the work carries on; the conversation sent to be summarised has big tool results and file
contents shortened so it fits. If summarising fails at the start of a request, it carries on unsummarised; if it fails
in the middle, the work carries on from the request, the checklist and what already ran.

## When Arty is busy

When Arty's brain answers 429 or 503 (all its jobs are taken), or a gateway in front of it answers 502 or 504, ArtyCode
waits and tries again, about 1, 2, 4 seconds apart and then every 30 seconds, with a little randomness, or as long as
the brain's Retry-After asks (up to a minute), showing `busy, retrying in N s`. A connection lost part way through a
reply, or a brain that can't be reached after it has answered once, is retried the same way (`lost the connection to
Arty's brain (...), retrying in N s`), and the reply is asked for again, so a long job carries on where it was. This
goes on for up to 15 minutes before the request stops with the error; Esc or Ctrl-C stops it sooner. A brain that has
never answered since ArtyCode started is reported at once. When the brain's reply is its own "hit an error" note, or
it answers 500, it is tried three times in all before the error is shown.
