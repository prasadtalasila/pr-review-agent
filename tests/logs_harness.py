"""The logging suite's one fixture, and the source it greps.

Registered as a plugin in ``conftest.py`` like the other harnesses, so
``clean_logging`` resolves in both halves of a suite that outgrew one
module. It restores process-wide logger state, which is why it is a
fixture and not a helper: every test here reconfigures the root logger,
and a test that leaks its level makes the next one lie.
"""

import logging
from pathlib import Path

import pytest

from pr_review_agent import logs

#: The package source, for the tests that assert on where a call is made
#: rather than on what it prints.
SRC = Path(logs.__file__).parent


@pytest.fixture(name="clean_logging")
def _clean_logging():
    """Undo whatever ``configure`` did to the process-wide logger tree."""
    root = logging.getLogger()
    before = (list(root.handlers), root.level)
    names = (logs.PACKAGE_LOGGER, *logs.THIRD_PARTY_FLOORS)
    levels = {name: logging.getLogger(name).level for name in names}
    yield
    root.handlers, root.level = before
    for name, level in levels.items():
        logging.getLogger(name).setLevel(level)
