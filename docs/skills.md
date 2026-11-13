# Skills

[Back to the README](../README.md)

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
