from dataclasses import dataclass


@dataclass(frozen=True)
class Choice:
    model: str
    about: str


class Choices:
    def __init__(self, listed: list[Choice]):
        self.listed = listed

    def pick(self, number: str) -> Choice | None:
        if not number.isdigit() or not 1 <= int(number) <= len(self.listed):
            return None
        return self.listed[int(number) - 1]

    def lines(self, model: str) -> list[str]:
        shown, width = [], len(str(len(self.listed)))
        for number, choice in enumerate(self.listed, 1):
            mark = "●" if choice.model == model else " "
            about = f"  {choice.about}" if choice.about and choice.about != choice.model else ""
            shown.append(f"{mark} {number:>{width}}. {choice.model}{about}")
        return shown


def listed_for(client, current_model: str = "") -> list[Choice]:
    found = [Choice(model, about) for model, about in client.available_models()]
    if current_model and all(choice.model != current_model for choice in found):
        found.insert(0, Choice(current_model, "current"))
    return found
