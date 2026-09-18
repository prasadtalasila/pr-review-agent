"""Review standards, read from the base ref rather than the pull request.

The engine is generic and the standards are per-repository, so the standards
have to come from the repository under review. They must not come from the
*pull request*: a diff that can rewrite the reviewer's instructions has
talked its way past every other control in the system.

They are read at the merge base, which is already on the ``Checkout`` and
already fetched, so this costs no second network round trip -- a linked
worktree shares the mirror's object database, so ``git show`` reaches a
commit that is not checked out.

**What this trusts, stated plainly:** anyone who can merge to the base branch
can change what the reviewer is told to do. That is a smaller set than
"anyone who can open a pull request", which is the set that would be trusted
if the head were read instead. It is not an empty one.
"""

from __future__ import annotations

import logging

from ..workspace import Checkout
from ..workspace.gitcmd import GitCommandError, run_git

logger = logging.getLogger(__name__)

#: Everything the standards may contribute to one prompt. A repository that
#: exceeds it is not refused -- the review still runs on what fits, because
#: an over-long standards file is a documentation problem and not a reason to
#: stop reviewing.
MAX_STANDARDS_BYTES = 64 * 1024


async def read_standards(checkout: Checkout, paths: tuple[str, ...]) -> str:
    """Concatenate ``paths`` as they stand at the checkout's merge base.

    A path that does not exist there is skipped: a repository need not carry
    every file the operator configured, and an absent one is not a failure.
    """
    sections: list[str] = []
    budget = MAX_STANDARDS_BYTES
    for path in paths:
        text = await _show(checkout, path)
        if text is None:
            continue
        encoded = text.encode("utf-8")
        if len(encoded) > budget:
            logger.warning(
                "%s at %s is %d bytes and does not fit the remaining %d; skipped",
                path,
                checkout.merge_base[:12],
                len(encoded),
                budget,
            )
            continue
        budget -= len(encoded)
        sections.append(f"### {path}\n\n{text.strip()}")
    return "\n\n".join(sections)


async def _show(checkout: Checkout, path: str) -> str | None:
    """One file at the merge base, or ``None`` if it is not there."""
    try:
        return await run_git(
            "-C",
            str(checkout.path),
            "show",
            f"{checkout.merge_base}:{path}",
        )
    except GitCommandError:
        logger.debug("no %s at %s", path, checkout.merge_base[:12])
        return None
