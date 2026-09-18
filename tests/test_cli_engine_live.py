"""The one test that spends money, and never runs unless asked.

Everything else in the engine suite stubs the subprocess, which proves the
adapter's own logic and proves nothing about the envelope the real CLI
prints. That gap is what this file covers: an output-format change is the
known cost of the subprocess boundary, and this is how it gets noticed.

Deselected by default through ``addopts`` in _pyproject.toml_, so it cannot
run in CI or by accident::

    poetry run pytest -m live tests/test_cli_engine_live.py
"""

import shutil

import pytest

from pr_review_agent.budget import Mode, UsageConfidence
from pr_review_agent.engine import ClaudeCliEngine, Outcome, ReviewRequest
from pr_review_agent.triggers.models import Trigger, TriggerKind
from pr_review_agent.workspace import Checkout, PullRequestFacts

pytestmark = pytest.mark.live

DIFF = """\
--- a/adder.py
+++ b/adder.py
@@ -0,0 +1,2 @@
+def add(a, b):
+    return a - b
"""


@pytest.mark.skipif(shutil.which("claude") is None, reason="claude is not installed")
async def test_a_real_run_returns_findings_and_a_token_count(tmp_path):
    """A wrong subtraction in a two-line diff, reviewed for real."""
    (tmp_path / "adder.py").write_text("def add(a, b):\n    return a - b\n")
    request = ReviewRequest(
        checkout=Checkout(
            path=tmp_path, head_sha="0" * 40, merge_base="1" * 40, diff=DIFF
        ),
        facts=PullRequestFacts(
            number=1,
            head_sha="0" * 40,
            base_ref="main",
            additions=2,
            deletions=0,
            changed_files=1,
        ),
        trigger=Trigger(
            kind=TriggerKind.PR_OPENED,
            repo="o/r",
            pr_number=1,
            head_sha="0" * 40,
            actor_id=1,
            dedupe_key="o/r#1@" + "0" * 40,
        ),
        mode=Mode.FULL,
    )
    engine = ClaudeCliEngine(
        model="claude-sonnet-5", expected_version="2.1.274", timeout_seconds=300.0
    )
    result = await engine.review(request)

    assert result.outcome is Outcome.COMPLETED
    assert result.usage.confidence is UsageConfidence.EXACT
    assert result.usage.tokens > 0
    assert result.findings
