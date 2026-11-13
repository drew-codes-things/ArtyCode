# Helpers

[Back to the README](../README.md)

Besides the built-in `explore` helper, you can write your own: a Markdown file with a short header and the helper's
instructions.

```
~/.config/arty-cli/helpers/reviewer.md     (yours, every project; %APPDATA%\arty-cli on Windows)
my-project/.arty/helpers/reviewer.md        (this project, once trusted; wins over yours with the same name)
```

```markdown
---
name: reviewer
description: Reviews the uncommitted changes for real bugs
tools: read_file, search, list_files, run_command
---
Run git diff, read each changed file around the change, and list only real bugs with file and line.
```

The model is told each helper's name and description and hands it a job with the `helper` tool; the helper works in its
own fresh conversation with only the tools listed (any of the built-in tools and `web_search`; without a `tools` line
it gets the look-only set `explore` has) and reports back. Up to four helpers, `explore` included, run at once. A
helper that only looks runs as in plan mode; one given `edit_file`, `write_file` or another changing tool asks for
permission just as the main agent does, and in plan mode it still can't change anything. `/helpers` lists them, and a
helper file with an unknown tool or no instructions is reported and skipped.
