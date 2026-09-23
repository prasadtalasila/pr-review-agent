"""Detect an agent mention in the *prose* of a comment body.

A mention only counts when it is something the commenter wrote as an
instruction. Text inside fenced code, indented code, inline code spans or
blockquotes is markup or quotation, not an instruction, so it is blanked
before the search. Without this, a diff containing ``@claude`` in a code
sample, or a reply quoting an earlier mention, would re-trigger a review.

The reverse operation lives here too. ``neutralise`` rewrites a body so that
``has_mention`` cannot fire on it, which is how the publisher keeps the
agent's own review comment from summoning another review. Detector and
sanitiser sit in one module on purpose: they are two halves of one rule, and
a change to either that is not matched in the other is a loop that spends
real tokens.
"""

from __future__ import annotations

import re

# CommonMark: a fence is 3+ backticks or tildes, indented at most 3 spaces.
_FENCE_RE = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,})(?P<info>.*)$")
# A blockquote marker, likewise indentable by up to 3 spaces.
_BLOCKQUOTE_RE = re.compile(r"^ {0,3}>")
# An indented code block: 4+ spaces or a tab.
_INDENTED_CODE_RE = re.compile(r"^(?: {4}|\t)")
# A code span is bounded by a matching run of backticks.
_INLINE_CODE_RE = re.compile(r"(?P<ticks>`+)(?s:.)*?(?P=ticks)")

#: What ``neutralise`` writes instead of ``@``. GitHub renders the entity as
#: the character, so a reader sees ``@claude`` and the raw body a later poll
#: reads back does not contain one.
_AT_ENTITY = "&#64;"


def _blank(text: str) -> str:
    """``text`` as the same number of spaces.

    Blanking has to preserve length, not only line structure: ``neutralise``
    locates a mention in the stripped text and edits that same offset in the
    original, so the two strings must index alike.
    """
    return " " * len(text)


def _blank_fenced_blocks(lines: list[str]) -> list[str]:
    """Blank every line of a fenced code block, fences included.

    An unclosed fence runs to the end of the document, per CommonMark.
    """
    out: list[str] = []
    open_fence: tuple[str, int] | None = None
    for line in lines:
        match = _FENCE_RE.match(line)
        if open_fence is None:
            if match:
                fence = match.group("fence")
                open_fence = (fence[0], len(fence))
            out.append(_blank(line) if match else line)
            continue
        char, length = open_fence
        if match and match.group("fence")[0] == char:
            closes = (
                len(match.group("fence")) >= length and not match.group("info").strip()
            )
            open_fence = None if closes else open_fence
        out.append(_blank(line))
    return out


def strip_non_prose(body: str) -> str:
    """Return ``body`` with code and quoted text blanked out.

    Every character of the result sits at the offset it sat at in ``body``,
    which is what lets ``neutralise`` edit the original in place. That is why
    the split is on ``"\\n"`` rather than ``str.splitlines``: the latter also
    breaks on ``\\r``, ``\\x0b`` and ``\\u2028``, and rejoining with ``"\\n"``
    would then shift every following offset. GitHub returns comment bodies
    with CRLF line endings, so that is the common case and not an exotic one.

    Fences are handled first: a ``>`` or a backtick inside a code block is
    literal text, not a blockquote marker or a code span.
    """
    lines = _blank_fenced_blocks(body.split("\n"))
    kept = [
        _blank(line)
        if _BLOCKQUOTE_RE.match(line) or _INDENTED_CODE_RE.match(line)
        else line
        for line in lines
    ]
    return _INLINE_CODE_RE.sub(lambda m: _blank(m.group()), "\n".join(kept))


def _mention_re(handle: str) -> re.Pattern[str]:
    """Build a word-bounded pattern for ``@handle``.

    The lookbehind rejects an address such as ``user@claude.ai``; the
    lookahead rejects a different account such as ``@claude-ci``.
    """
    return re.compile(
        rf"(?<![A-Za-z0-9_/.@-])@{re.escape(handle)}(?![A-Za-z0-9_-])",
        re.IGNORECASE,
    )


def has_mention(body: str, handle: str = "claude") -> bool:
    """True when ``@handle`` appears in the prose of ``body``."""
    return bool(_mention_re(handle).search(strip_non_prose(body)))


def neutralise(body: str, handle: str) -> str:
    """``body`` rewritten so that ``has_mention(body, handle)`` is false.

    Only the ``@`` of a mention the detector would actually find is replaced.
    A blanket substitution would reach into fenced code, where GitHub does
    not render entities and the reader would see ``&#64;claude`` in what is
    meant to be a code sample -- and the detector ignores fenced code anyway,
    so there is nothing there to neutralise.

    Wrapping the handle in backticks was the cheaper alternative and is not
    safe: the body is engine output over an untrusted tree, so it can carry
    an unmatched backtick that pairs with the opening one this would add,
    leaving the handle in prose after all.
    """
    stripped = strip_non_prose(body)
    out = list(body)
    for match in _mention_re(handle).finditer(stripped):
        out[match.start()] = _AT_ENTITY
    return "".join(out)
