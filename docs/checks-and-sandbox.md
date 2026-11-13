# Checks after edits, project map and sandbox

[Back to the README](../README.md)

After every `write_file`, `edit_file` or `multi_edit`, ArtyCode checks the file and tells the model what broke: Python
syntax and JSON in-process, `ruff` or `flake8` when the project configures one (`ruff.toml`, `[tool.ruff]` in
`pyproject.toml`, `.flake8`, or a `[flake8]` section in `setup.cfg` or `tox.ini`), `node --check` for JavaScript, and
`bash -n` plus `shellcheck` for shell scripts. Only programs found outside the project folder are run, never ones the
project ships, and each gets 15 seconds. flake8 is skipped when its config has a `[flake8:local-plugins]` section,
because that loads Python code from the project. Turn it off with `"lintAfterEdits": false` in `settings.json`.

At the start (and after `/clear` or a summary) the model gets a short map of the project: the files with their main
classes and functions, the most used first, tests last, about 3,600 characters at most. It is built with the standard
library and cached in the config folder's `repomap/`, so only changed files are read again. Turn it off with
`"repoMap": false`.

`--sandbox`, or `"sandbox": true` in `settings.json`, runs every command in a sandbox as well as asking first as
usual:

- Linux: bubblewrap (`bwrap`, install it with your package manager). The system is read-only, the project folder is
  writable except the parts that make programs run later (`.git/config`, `.git/hooks`, `.git/info` and the `.arty`
  folder stay read-only, so change git settings or hooks outside the sandbox), `/tmp` is private, and secrets in your home folder (`~/.ssh`, `~/.gnupg`, cloud and `gh` logins,
  `.netrc`, `.git-credentials`, ArtyCode's own config folder with your API key, shell histories, cargo, gradle, maven
  and git credential files, browser profiles, keystores and key files at the top of your home folder, and others)
  look empty. Your
  runtime folder (`/run/user/<uid>`, with the session bus and keyring and agent sockets) is empty too, the SSH agent
  socket is hidden wherever it lives, and environment variables that look secret (names with KEY, TOKEN, SECRET,
  PASSWORD, AUTH and the like) or point at the session bus or an agent are left out. The rest of your home folder
  stays readable so toolchains keep working.
  A project with no `.git` or `.arty` yet has nothing to protect: a sandboxed command can run `git init` or make
  `.arty` and write hooks or settings into them, and git run outside the sandbox (your shell prompt, a hook) would
  then run what was planted. Keeping a name that doesn't exist yet read-only would need bubblewrap to make a
  placeholder for it in your real project folder, which ArtyCode doesn't do. In a folder where a sandboxed command
  made a new repository, look at `.git/config` and `.git/hooks` before you run git there yourself.
- Windows: an AppContainer and a Job Object. The command can change only the project folder (it starts in a
  `Project:` drive there), can't read your user folder, and everything it started ends with it. A project folder that
  holds your whole user folder is refused. Programs installed only for you (under `AppData`) can't be run inside it.
- No network unless you also set `"sandboxNetwork": true` in your own `settings.json`.

It is off by default. When it can't run (no `bwrap`, or a container that blocks it) ArtyCode says so and runs commands
without it; `"sandbox": "required"` refuses commands instead. A trusted project's `.arty/settings.json` can switch
the sandbox on or make it required, never off, and can't turn the network on. Helpers' commands and the ones you type
after `!` run in the sandbox too; hooks, MCP servers, the headless browser for `screenshot` and the checks after
edits don't.
