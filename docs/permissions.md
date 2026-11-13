# Permissions

[Back to the README](../README.md)

## Answering a question

Each question lists its answers under it, one per line with its key, then asks `Choose y, a or n:`. Answer **y**
(yes), **a** (yes, and don't ask again for this until you quit) or **n** (no). After **n** typed here, ArtyCode asks
`What should Arty do instead?`: what you write goes to the model with the refusal, so it changes course instead of
stopping, and Enter on its own just says no. "Always" lasts until you quit: for edits it covers every edit, for a
command the program and its subcommand words, which the question names (`Yes, and don't ask again for
\`python3 -m pytest\` commands until you quit`), so `python3 -m pytest -q`, `python3 -m pytest 2>&1 | tail -40` and
`python3 -m pytest | grep -E "FAILED|ERROR"` are covered too. A package runner is remembered together with its package (`npx eslint`, `pipx run black`, `uvx ruff check`), so it never covers a different package; one that picks the package through an option (`npx -p`, `npx --package`, `npx -y`, `pipx run --spec`, `uvx --from`) is only remembered exactly. A remembered or saved `gh` command never covers the same command opening a browser, writing a file, watching, naming a host other than github.com, or a `--jq`/`--template` filter that reaches environment variables, modules or inputs, so a yes to `gh pr view` is not a yes to `gh pr view --web`. A command whose words decide what it touches or runs
(`rm`, `mv`, `cp`, `curl`, `sudo`, `env`, `xargs`, `sed`, `find`, `python -c` and the like), one that starts another
program (`systemd-run`, `stdbuf`, `xvfb-run`, `strace`, `timeout`, `nice`, `uv run --with ...` and the like), one that
runs a script or a command through an option (`make`, `cmake`, `tar`, `zip`, `vim`, `emacs`, `less`, `man`, `tmux`
and the like), a PowerShell command that changes, fetches, stops or launches (`Remove-Item`, `Set-Content`,
`Tee-Object`, `Invoke-WebRequest`, `Start-Process`, `Invoke-Expression`, `Import-Module`, `Add-Type`, `Set-Alias` and
their aliases, in any letters and also behind a module name such as `Microsoft.PowerShell.Utility\`), one that
starts with a shell keyword (`if`, `then`, `do`), or one with `$`, backticks, `(`, `<`, a `#` comment or output written
to a file is only ever remembered exactly, as before. A prefix rule for such a program or keyword in the settings is not
followed as an allow rule (it would cover any command); as a deny rule it still forbids. In a trusted project there is also
**s** (save as a project rule): the answer is written to `.arty/settings.json` as an allow rule (below), for a command
as `run_command(python3 -m pytest:*)`, for that file, that site or that tool, so later sessions don't ask either. When
no rule fits, **s** lasts the session like **a**. When there's no terminal to ask in (piped input), anything that needs an OK is refused.

## Permission modes

`--permission-mode` (or `/mode` in the chat) sets how much is asked: `default`; `auto-edit` (edits inside the project
without asking, apart from `.arty` and `.git` folders); `plan` (look and plan only, nothing changed or run; `/plan` switches it; when the model has a plan it
presents it with the `present_plan` tool and you answer **y** to approve it, so plan mode ends and the work carries on
in `default`, **a** to approve it and carry on in `auto-edit`, or **n** to keep planning, with what should change); `allow-all` (nothing asked,
apart from a command ArtyCode can't read, such as one with `$` or a wildcard where the program's name goes; only for a
folder you can throw away). **Shift+Tab** moves on to the next mode, keeping what you have typed: default,
auto-edit, plan, then default again. allow-all is never reached with Shift+Tab, only with `/mode allow-all` or the flag.

## Look-only commands

`ls`, `cat`, `grep`, `git status`, `git diff` and the like run without a question when they stay inside the project,
also when joined by pipes or `&&` (`cat a.py | grep -n x`), as long as there is no `$`, backtick or file redirection.
Every path is read the way the shell would: `~`, `~+` and `~-`, wildcards (each match is followed through links), and
Windows backslash, drive and network paths. A wildcard that starts with a dot (`.*`, `.?`) asks, since some shells
match it to `..`, the folder above the project. Anything that reaches outside the project, or can't be read, asks first,
in plan mode and in helpers too; so does a file with more than one hard link, since another of its names may be outside.
Program names are matched letter for letter (`CAT` is not `cat`), except on Windows and for PowerShell commands. A command also asks when it uses an option that writes, runs a program or follows
links out of the project (`rg --pre`, `find -fprint`, `find -L`, `grep -R`, `ls -L`, `du -L`, `tree -l`, `tree -o`,
`file -C`, `diff -r`, `git log --output`, `git branch -D`), and look-only git asks when the git settings of the
repository it runs in (the project, or a repository inside it reached through `cwd`) name a program git would run (an
fsmonitor, an external diff, a textconv or filter driver, a gpg program).

On Windows, PowerShell also reads the curly quotes (U+2018 to U+201E) as quote marks, the en dash, em dash and
horizontal bar as a parameter's dash, and the non-breaking space and other Unicode spaces as spaces. A command with one
of these outside quotes, or with a curly quote that would end the quoted text it sits in, can't be read: it asks (in
allow-all too) and is only ever remembered exactly. A backslash is not an escape there either: `ca\t` or `.\cat` names a
program file in a folder (which auto-edit could have written), not `cat`, so it is never look-only. Each item of a
PowerShell list (`Get-Content a.py,..\secret.txt`) and the value of a `-Name:value` parameter behind any of the dashes is
checked as a path of its own.

The GitHub CLI's look-only commands run without a question too: `gh pr view`, `gh pr list`, `gh pr diff`,
`gh pr checks`, `gh pr status`, `gh issue view`, `gh issue list`, `gh issue status`, `gh run view` (with `--log` or
`--log-failed` as well), `gh run list`, `gh release view`, `gh release list`, `gh repo view`, `gh workflow view`,
`gh workflow list` and `gh status`. The program has to be written `gh` (not `GH`, `./gh` or a path), followed straight
by those words; any other gh command asks, and so does one gh doesn't know itself, since it could be an alias or an
extension. They ask when they open a browser (`--web`, `-w`), write files (`--output`, `-O`, `--dir`, `-D`), keep
watching (`--watch`), name a host other than github.com (`-R host/owner/repo`, `-R host:port/repo`, a URL or
`git@host:` address, also behind leading dashes), have a tab, line break, non-breaking space or other unusual space, have a wildcard or `~` outside quotes (the shell would swap it for file names first), or have a
word containing `env`, `import`, `include` or `input` (the jq words for environment variables, modules and
inputs, which `--jq` passes on). `-R owner/repo` (with a space) and github.com URLs are fine; a word with two or more
`/` that doesn't start with `github.com` asks, so a jq `//` does too. Like git, they ask when the repository's git
settings name a program, and deny rules still stop them.

## Permission rules

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
to the project), the command, the address or the list pattern. A command pattern ending in `:*`, such as
`run_command(npm test:*)`, is a prefix: it matches any command whose first words are those words. Deny wins over
everything, including `allow-all` and hooks, and is checked against each part of a chained command and against the
program each part runs, also behind a path (`/bin/rm`), a setting (`X=1 rm`), a shell keyword or a program that starts
another (`sudo`, `xargs`, `bash -c`). It is still a match on the command's words, not on what a program does:
`find -delete` or a script that removes files is not caught by `run_command(rm *)`, so a command deny rule stops the usual
ways of running that program, not every way of doing what it does. While any `run_command` deny rule is set, a command
ArtyCode can't read through (a `$` or backtick expansion, brackets or braces, `<`, `>` into a file, a `#` comment, or a
wildcard where a program's name goes) is refused, because `rm$IFS-rf` or `r$@m` would hide the denied program. A chained command
or pipeline (`|`, `&&`, `||`, `;`, a new line) is allowed only when every part is: a part matches an allow rule or a
prefix allowed with **a**, or, after a `|`, is only a look-only filter (`tail`, `head`, `grep`, `wc`, `sort`, `uniq`,
or `cat` with nothing after it) that reads nothing outside the project. Nothing is allowed this way with `$`,
backticks, `(`, `<`, or output sent to a file (`2>&1` and `>/dev/null` are fine). An allow rule never lets plan mode
change anything.
