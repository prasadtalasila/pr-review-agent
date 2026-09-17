"""What both command-line entry points do before they can do anything else.

``bootstrap`` and ``daemon`` each need the same two things: the GitHub token,
and a validated ``config.yaml``. Sharing the step keeps one answer to "what
does exit status 2 mean" rather than two that drift apart.

The token is read from the environment, never from ``config.yaml``, so it can
come from a systemd ``EnvironmentFile`` or a secret manager without ever
being a file the repository could swallow. It is never printed: a
:class:`StartupError` says what was missing, not what was sent.
"""

from __future__ import annotations

import os

from .config import Config, ConfigError

TOKEN_ENV = "GITHUB_TOKEN"


class StartupError(RuntimeError):
    """Raised when the token or the configuration file is unusable."""


def startup(config_path: str) -> tuple[Config, str]:
    """Return the validated configuration and the token.

    Raises :class:`StartupError` with an operator-readable message when
    either is missing; both are a non-zero exit rather than a default, since
    guessing at a repository or running without a token is worse than
    stopping.
    """
    token = os.environ.get(TOKEN_ENV)
    if not token:
        raise StartupError(f"{TOKEN_ENV} is not set")
    try:
        return Config.load(config_path), token
    except ConfigError as exc:
        raise StartupError(str(exc)) from exc
