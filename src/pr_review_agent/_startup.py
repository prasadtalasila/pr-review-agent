"""What a command-line entry point does before it can do anything else.

``host check`` and ``daemon start`` each need the same two things: the GitHub
token, and a validated ``config.yaml``. Sharing the step keeps one answer to
"what does exit status 3 mean" rather than two that drift apart.

The two halves are separate functions because one command needs only one of
them. ``config validate`` asks whether a file is loadable, and a combined
step would have made it refuse to answer on a host that has no token --
demanding a credential to parse YAML. So the token check is its own call, and
the commands that need both make both.

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


def require_token() -> str:
    """The GitHub token, or :class:`StartupError` if the environment has none.

    A non-zero exit rather than a default: running without a token is worse
    than stopping, because every failure it causes surfaces later and
    further from the cause.
    """
    token = os.environ.get(TOKEN_ENV)
    if not token:
        raise StartupError(f"{TOKEN_ENV} is not set")
    return token


def load_config(config_path: str) -> Config:
    """The validated configuration, or :class:`StartupError` if unusable.

    ``ConfigError`` is re-raised as ``StartupError`` so a caller has one
    exception type to catch for "this host is not set up", whichever half
    of the setup is missing.
    """
    try:
        return Config.load(config_path)
    except ConfigError as exc:
        raise StartupError(str(exc)) from exc


def startup(config_path: str) -> tuple[Config, str]:
    """Both halves, token first.

    The order is load-bearing: a missing token is the cheaper and more
    common misconfiguration, and reporting it before parsing keeps the
    first error an operator sees the one they most likely caused.
    """
    token = require_token()
    return load_config(config_path), token
