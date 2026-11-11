import json

from arty_cli import hooks, skills

SKILL_TOOL = "skill"
MAX_ARGUMENT_PREVIEW_CHARS = 2_000


def argument_preview(arguments: dict) -> str:
    shown = json.dumps(arguments, indent=2, ensure_ascii=False)
    return shown if len(shown) <= MAX_ARGUMENT_PREVIEW_CHARS else shown[:MAX_ARGUMENT_PREVIEW_CHARS] + "\n..."


class Extras:
    def __init__(self, hooks=None, skills=None, hub=None):
        self.hooks = hooks
        self.skills = skills or {}
        self.hub = hub

    def definitions(self) -> list[dict]:
        found = [skills.DEFINITION] if self.skills else []
        return found + (self.hub.definitions() if self.hub else [])

    def handles(self, name: str) -> bool:
        if name == SKILL_TOOL:
            return bool(self.skills)
        return self.hub is not None and self.hub.tool(name) is not None

    def may_change_things(self, name: str) -> bool:
        return name != SKILL_TOOL and not self.hub.tool(name).read_only

    def check(self, name: str, arguments: dict, gate) -> str | None:
        if name == SKILL_TOOL:
            return None
        tool = self.hub.tool(name)
        return gate.check_server(tool.server, name, tool.read_only, argument_preview(arguments), arguments)

    def run(self, name: str, arguments: dict) -> str:
        if name == SKILL_TOOL:
            return skills.loaded(self.skills, str(arguments.get("name", "")))
        return self.hub.run(name, arguments)

    def hook(self, event: str, payload: dict, tool_name: str = "") -> hooks.Outcome:
        if self.hooks is None:
            return hooks.Outcome()
        return self.hooks.run(event, payload, tool_name)

    def without_stop_hooks(self) -> "Extras":
        kept = None
        if self.hooks is not None:
            kept = hooks.Hooks([hook for hook in self.hooks.listed() if hook.event != "Stop"], self.hooks.workspace)
            kept.session_id = self.hooks.session_id
        return Extras(hooks=kept)

    def close(self) -> None:
        if self.hub is not None:
            self.hub.close()


NONE = Extras()
