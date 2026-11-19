"""Expected administrator operation failures."""


class ActionFailed(ValueError):
    """An operation was rejected without performing the requested change."""


class LoginFailed(ValueError):
    """The supplied credentials or session limits did not permit login."""
