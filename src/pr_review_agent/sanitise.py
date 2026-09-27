"""Make engine prose inert before it is posted under the agent's account.

The publisher's comment body is model output written about a tree an
attacker can edit, posted by an account with a real token. Markdown that is
merely *read* is harmless; markdown that **acts** is not, and GitHub gives
ordinary comment text four ways to act:

- ``@someone`` notifies that account -- from the agent, not from whoever
  wrote the pull request. ``@org/team`` pings a team.
- ``#123``, ``GH-123`` and ``owner/repo#123`` create a cross-reference on
  another issue, again attributed to the agent.
- raw HTML renders. ``<sub>`` forges a second "Automated review" trailer,
  and an HTML comment hides text from a reader while leaving it in the body.
- length: GitHub rejects a body over 65 536 characters with a 422, *after*
  the tokens were spent.

The first three are handled here by escaping, the fourth by
:data:`MAX_BODY_CHARS` in the publisher. Escaping rather than stripping is
deliberate: a reader still sees ``@someone`` and ``#123``, because a review
that says "see #123" is saying something useful and a reader should be able
to read it. What they stop being is a link the agent pulled.

**Prose only.** Like :mod:`pr_review_agent.triggers.mention`, the rewrite
skips fenced code, indented code, code spans and blockquotes -- GitHub does
not mention, cross-reference or render HTML inside them either, and an
entity there would show up as ``&#64;`` in what is meant to be a code
sample. :func:`strip_non_prose` is shared, so the two modules cannot
disagree about where prose is.

Bare commit shas are deliberately **not** escaped. They auto-link to a
commit in the repository under review, which is where the review is posted:
a link, not a notification and not a cross-reference somewhere else.
"""

from __future__ import annotations

import re

from .triggers.mention import strip_non_prose

#: Every character that makes engine prose act rather than read, in one
#: alternation so that a single pass sees the *original* text: escaping
#: ``@`` as ``&#64;`` introduces a ``#`` that a second pass would escape
#: again, and ``&amp;#64;`` is what the reader would then see.
_UNSAFE_RE = re.compile(
    # An account mention. The lookbehind rejects an address such as
    # `user@example.com`, matching the detector in `triggers.mention`.
    r"(?P<at>(?<![A-Za-z0-9_/.@-])@(?=[A-Za-z0-9]))"
    # `GH-123`. Escaped at the hyphen, which is what makes it a reference.
    r"|(?P<gh>(?<![A-Za-z0-9_-])GH-(?=\d))"
    # `#123`, including the `owner/repo#123` and `GH-` forms, so no
    # lookbehind: a `#` before digits is a reference wherever it appears.
    r"|(?P<hash>#(?=\d))"
    # Raw HTML, tags and comments alike. One rule, because both begin here.
    r"|(?P<lt><)"
)

_ESCAPES = {"at": "&#64;", "gh": "GH&#45;", "hash": "&#35;", "lt": "&lt;"}


def sanitise(text: str) -> str:
    """``text`` with every acting construct in its prose escaped.

    The match is found in the stripped copy and applied to the original, so
    offsets have to agree -- which is exactly what ``strip_non_prose``
    guarantees and why it blanks rather than deletes.
    """
    stripped = strip_non_prose(text)
    out: list[str] = []
    last = 0
    for match in _UNSAFE_RE.finditer(stripped):
        out.append(text[last : match.start()])
        out.append(_ESCAPES[match.lastgroup])  # type: ignore[index]
        last = match.end()
    out.append(text[last:])
    return "".join(out)


def leaks(body: str, secrets: tuple[str, ...]) -> bool:
    """Whether ``body`` contains any of ``secrets`` verbatim.

    A last line of defence rather than a mitigation: nothing is *meant* to
    put the GitHub token in a review, but the engine reads the filesystem
    and the one thing worse than a bad review is a public comment carrying
    the credential that posted it. Short values are ignored -- an empty or
    one-character "secret" would match everything.
    """
    return any(len(secret) >= 8 and secret in body for secret in secrets)
