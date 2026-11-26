# Checking the setup and updating

[Back to the README](../README.md)

`arty doctor` prints a short checklist, one line each with a tick or a cross:

- the settings file is there and only you can read it (mode 600);
- Arty's brain answers, and at which address (home or from anywhere);
- the brain accepts this computer's key;
- the model list can be read, and which model this computer uses;
- the brain marks its coding models;
- the context window the brain reports (a cross below 16,384 tokens, where long files and long jobs are cut down and summarised often);
- web search works, through the desktop browser or through the brain;
- the sandbox tool is there (bubblewrap on Linux);
- a browser is found for screenshots;
- git is installed;
- the terminal's size and whether colour is on.

It never prints the key or any other secret. The first four are needed: it exits with 0 when they all pass and 1
otherwise. The rest are marked `(optional)` when they fail. It works with no brain and before logging in: those
lines get a cross and the checks of this computer still run. The web search line runs one real search.

`arty update` says which version you have and how it was installed, then:

- from a git checkout (`pip install -e .` in a clone): it fetches `origin`, fast-forwards the clone and reinstalls
  it, and says which commits it moved between. It does this only when the clone has no uncommitted changes and is
  on its default branch; otherwise it changes nothing and says why;
- any other install (the installer, pipx, pip): it prints the one command to run.

`arty update --check` only reports whether a newer version is out. Neither ever uses sudo.

When the chat starts, ArtyCode asks GitHub for the latest ArtyCode release
(`api.github.com/repos/drew-codes-things/ArtyCode/releases/latest`) and, when it is newer than the one you have,
shows one line: "Version X is available", with the release page. It asks at most once a day: the answer is kept in
`update-check.json` next to the settings. The request is one HTTPS call with a 5 second limit and carries nothing
but an `ArtyCode/<version>` User-Agent. Nothing is downloaded or installed. When the check fails (offline, rate
limited, an odd answer) nothing is shown. A single question (`-p`) never checks.

- `arty update --skip` hides the line for the newest version seen; a later version shows it again.
- `update_notice` set to `off` in the config file, or `ARTY_CLI_UPDATE_NOTICE=off`, stops the check.
