"""Detect an agent mention in the *prose* of a comment body.

A mention only counts when it is something the commenter wrote as an
instruction. Text inside fenced code, indented code, inline code spans or
blockquotes is markup or quotation, not an instruction, so it is blanked
before the search. Without this, a diff containing ``@claude`` in a code
sample, or a reply quoting an earlier mention, would re-trigger a review.
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
            out.append("" if match else line)
            continue
        char, length = open_fence
        if match and match.group("fence")[0] == char:
            closes = (
                len(match.group("fence")) >= length and not match.group("info").strip()
            )
            open_fence = None if closes else open_fence
        out.append("")
    return out


def strip_non_prose(body: str) -> str:
    """Return ``body`` with code and quoted text blanked out.

    Line structure is preserved so that reported positions stay meaningful.
    Fences are handled first: a ``>`` or a backtick inside a code block is
    literal text, not a blockquote marker or a code span.
    """
    lines = _blank_fenced_blocks(body.splitlines())
    kept = [
        "" if _BLOCKQUOTE_RE.match(line) or _INDENTED_CODE_RE.match(line) else line
        for line in lines
    ]
    return _INLINE_CODE_RE.sub(" ", "\n".join(kept))


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
