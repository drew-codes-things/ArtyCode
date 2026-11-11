from arty_cli import brain, settings


class ProviderError(Exception):
    pass


ERRORS = (brain.BrainError, ProviderError)


def make(config: settings.Settings) -> brain.Brain:
    if not config.is_ready():
        raise ProviderError("Arty isn't set up yet. Run `arty-cli login` and choose an API key or a browser login.")
    return brain.Brain(config)
