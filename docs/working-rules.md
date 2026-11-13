# Built-in working rules

[Back to the README](../README.md)

Out of the box Arty works by a short set of engineering rules, added to what the model is told (about 1,600
characters):

- read the code and its callers first, match the project's patterns, make the smallest change, never make things up;
- no comments or docstrings in code it writes (shebangs, encoding lines, lint or type suppressions and license headers
  are fine), and never the em dash character;
- ask before anything destructive, otherwise decide, note the assumption and carry on;
- nothing is done until a test, build, lint or run shows it, or the report says `UNVERIFIED`;
- end a task that changes files with a report: Outcome, Changed, Verification, Assumptions and Open items.

ArtyCode keeps the record itself, so the model spends no steps on it: when the rules are on or tell-only, each request
that changed files gets an entry at the end of `.arty/history.md`, written by ArtyCode from what it already holds: the
date and the request, Status (the report's Outcome; otherwise DONE if a command ran after the last change and
UNVERIFIED if not; PARTIAL for a request stopped part way), the files changed, What (the report's Changed), Why (its
Assumptions), Verified by (each command run, with what it showed), Not verified and Left open (its Open items).
When `.arty/history.md` or the `.arty` folder is a link (a project could ship one pointing at a file of yours),
ArtyCode says so and writes nothing; the same goes for saving a rule to `.arty/settings.json` and for screenshots.

They are also checked, not only told:

- After every `write_file`, `edit_file`, `multi_edit` or `notebook_edit`, the lines that request added are checked for
  comments (Python, shell and other `#` languages, and the `//` and `/* */` family: JavaScript, TypeScript, Go, Rust,
  C, Java, Kotlin, Swift, C#, CSS and more) and for em dashes (in any file). The tool result names each line to fix.
- When the model says it has finished a request that changed files, it is sent back if comments or em dashes it added
  are still there (up to three times), once if nothing was run to check the work after the last change (a test,
  build, lint or the program; looking around with `ls`, `cat` or `git` doesn't count) and the report doesn't say
  `UNVERIFIED` (a command that wasn't found, or a test run that failed, doesn't count; with the finish check off, a
  failing last test run is named in that send-back). Every send-back ends with the report shape.

If the model writes `.arty/scratchpad.md` or `.arty/history.md` anyway, that is allowed without asking, and
`.arty/history.md` only at its end: a change that alters an earlier entry is refused. Everything else under `.arty` is
still asked about every time, and plan mode still changes nothing. The history is meant to be kept in git.

A project's `ARTY.md` or `AGENTS.md` comes after the rules and wins where they differ. To change them:

| Where | Setting |
|---|---|
| `settings.json` in the config folder (or a trusted project's `.arty/settings.json`) | `"builtInRules": true` (the default), `"tell-only"` (told, not checked; the history is still kept) or `false` (not told, no history) |
| One run | `arty --rules on`, `--rules tell-only` or `--rules off` |
| This session | `/rules on`, `/rules tell-only`, `/rules off`; `/rules` alone shows the state |

A project's settings can turn the rules down but not back on; the command line wins for that run. The welcome lines
show the state.
