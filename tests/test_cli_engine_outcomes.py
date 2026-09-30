"""What the adapter makes of what the CLI printed, including a usage limit.

Unreadable output raises rather than reporting a clean review -- a parse
failure that became "no findings" would be indistinguishable from a review
that found nothing -- and a usage limit carries whatever the envelope
measured.
"""

import asyncio
import logging

import pytest
from cli_engine_harness import (
    ASSESSMENT,
    USAGE,
    Recorder,
    engine,
    envelope,
    request,
)

from pr_review_agent.budget import UsageConfidence
from pr_review_agent.engine import (
    EngineProtocolError,
    EngineTimeout,
    EngineUnavailable,
    Outcome,
    Severity,
    UsageLimited,
)
from pr_review_agent.findings import Assessment, Recommendation, Risk

# -- parsing the envelope --


async def test_a_successful_run_yields_findings(tmp_path, run):
    finding = {
        "path": "src/x.py",
        "line": 12,
        "severity": "major",
        "title": "The retry loop never terminates on a persistent failure.",
        "body": "unbounded loop",
    }
    run(
        Recorder(
            envelope(
                structured_output={"assessment": ASSESSMENT, "findings": [finding]}
            )
        )
    )
    result = await engine().review(request(tmp_path))
    assert result.outcome is Outcome.COMPLETED
    assert result.findings[0].severity is Severity.MAJOR
    assert result.findings[0].line == 12


async def test_usage_counts_every_token_the_run_consumed(tmp_path, run):
    """Cache reads included: cheaper than fresh input, not free."""
    run(Recorder(envelope()))
    result = await engine().review(request(tmp_path))
    assert result.usage.tokens == 1_260
    assert result.usage.confidence is UsageConfidence.EXACT
    assert result.usage.engine == "claude"
    assert result.usage.model == "claude-sonnet-5"


@pytest.mark.parametrize(
    ("overrides", "outcome"),
    [
        ({"subtype": "error_max_turns"}, Outcome.TRUNCATED),
        ({"subtype": "error_max_structured_output_retries"}, Outcome.FAILED),
        ({"subtype": "error_during_execution"}, Outcome.FAILED),
        ({"structured_output": None}, Outcome.FAILED),
    ],
)
async def test_a_run_that_did_not_finish_publishes_nothing(
    tmp_path, run, overrides, outcome
):
    run(Recorder(envelope(**overrides)))
    result = await engine().review(request(tmp_path))
    assert result.outcome is outcome
    assert result.findings == ()


async def test_a_failed_run_still_reports_what_it_spent(tmp_path, run):
    """The money is gone whatever the run concluded, so it must settle."""
    run(Recorder(envelope(subtype="error_during_execution")))
    result = await engine().review(request(tmp_path))
    assert result.usage.tokens == 1_260
    assert result.usage.confidence is UsageConfidence.EXACT


@pytest.mark.parametrize("stdout", ["not json at all", '{"type": "assistant"}', ""])
async def test_unreadable_output_raises_rather_than_reviewing_nothing(
    tmp_path, run, stdout
):
    run(Recorder(stdout))
    with pytest.raises(EngineProtocolError):
        await engine().review(request(tmp_path))


async def test_findings_that_do_not_fit_the_schema_raise(tmp_path, run):
    run(
        Recorder(
            envelope(
                structured_output={
                    "assessment": ASSESSMENT,
                    "findings": [{"path": "x"}],
                }
            )
        )
    )
    with pytest.raises(EngineProtocolError, match="do not fit the schema"):
        await engine().review(request(tmp_path))


async def test_a_successful_run_carries_its_assessment(tmp_path, run):
    run(Recorder(envelope()))
    result = await engine().review(request(tmp_path))
    assert result.assessment == Assessment(
        effort=2,
        risk=Risk.LOW,
        recommendation=Recommendation.SAFE_TO_MERGE,
        priority_files=("src/x.py",),
    )


@pytest.mark.parametrize(
    "assessment",
    [
        None,
        {**ASSESSMENT, "effort": 6},
        {**ASSESSMENT, "risk": "severe"},
        {**ASSESSMENT, "recommendation": "approve"},
        {**ASSESSMENT, "priority_files": ["a", "b", "c", "d", "e", "f"]},
        {k: v for k, v in ASSESSMENT.items() if k != "priority_files"},
    ],
)
async def test_a_missing_or_malformed_assessment_raises(tmp_path, run, assessment):
    """Mandatory, not optional: a review without one is not posted without one."""
    structured = {"findings": []}
    if assessment is not None:
        structured["assessment"] = assessment
    run(Recorder(envelope(structured_output=structured)))
    with pytest.raises(EngineProtocolError, match="assessment that does not fit"):
        await engine().review(request(tmp_path))


async def test_a_nonzero_exit_raises(tmp_path, run):
    run(Recorder("", returncode=2))
    with pytest.raises(EngineProtocolError):
        await engine().review(request(tmp_path))


# -- the wall clock --


async def test_a_run_that_outlives_its_clock_is_killed(tmp_path, run):
    recorder = run(Recorder(envelope(), hang=True, returncode=None))
    with pytest.raises(EngineTimeout):
        await engine(timeout_seconds=0.01).review(request(tmp_path))
    assert recorder.stopped


async def test_a_missing_binary_says_so(tmp_path, monkeypatch):
    async def missing(*_argv, **_kwargs):
        raise OSError("No such file or directory")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", missing)
    with pytest.raises(EngineUnavailable):
        await engine(binary="claude-not-installed").review(request(tmp_path))


# -- the version pin --


async def test_a_version_mismatch_warns_and_proceeds(tmp_path, run, caplog):
    run(Recorder(envelope()))
    with caplog.at_level(logging.WARNING):
        result = await engine(expected_version="9.9.9").review(request(tmp_path))
    assert result.outcome is Outcome.COMPLETED
    assert "written against" in caplog.text


# -- the account's own limit, which must not be retried --


async def test_a_usage_limit_on_stderr_raises_usage_limited(tmp_path, run):
    """Refused before doing any work: knowable, and knowably zero."""
    run(Recorder("", stderr="Claude usage limit reached", returncode=1))
    with pytest.raises(UsageLimited) as raised:
        await engine().review(request(tmp_path))

    assert raised.value.usage is None


async def test_a_usage_limit_in_the_envelope_carries_its_usage(tmp_path, run):
    """Hit mid-run: the envelope still measured what it spent."""
    run(Recorder(envelope(subtype="error_during_execution", error="rate_limit_error")))
    with pytest.raises(UsageLimited) as raised:
        await engine().review(request(tmp_path))

    assert raised.value.usage is not None
    assert raised.value.usage.tokens == sum(USAGE.values())
    assert raised.value.usage.confidence is UsageConfidence.EXACT


MARKED_FINDING = {
    "path": "a.py",
    "line": 1,
    "severity": "nit",
    "title": "usage limit reached",
    "body": (
        "The handler catches `rate_limit_error` and retries forever, which is "
        "how you get a mail saying you exceeded your account's quota."
    ),
}


async def test_a_marker_quoted_by_a_finding_is_not_a_usage_limit(tmp_path, run):
    """The review's own prose is attacker-influenced; it must not trip the breaker."""
    run(
        Recorder(
            envelope(
                structured_output={
                    "assessment": ASSESSMENT,
                    "findings": [MARKED_FINDING],
                }
            )
        )
    )
    result = await engine().review(request(tmp_path))

    assert result.outcome is Outcome.COMPLETED
    assert len(result.findings) == 1


async def test_a_marker_in_a_clean_envelopes_result_is_not_a_usage_limit(tmp_path, run):
    """`is_error` false and no `error_` subtype means the CLI is not complaining."""
    run(Recorder(envelope(result="usage limit reached")))
    result = await engine().review(request(tmp_path))

    assert result.outcome is Outcome.COMPLETED


async def test_an_errored_envelope_still_reports_its_usage_limit(tmp_path, run):
    """`is_error` alone is enough; the marker need not be in the subtype."""
    run(
        Recorder(
            envelope(
                subtype="success",
                is_error=True,
                result="Claude usage limit reached",
                structured_output={
                    "assessment": ASSESSMENT,
                    "findings": [MARKED_FINDING],
                },
            )
        )
    )
    with pytest.raises(UsageLimited):
        await engine().review(request(tmp_path))


async def test_an_unrelated_failure_is_still_a_protocol_error(tmp_path, run):
    """The breaker refuses work, so a false trip costs more than a retry."""
    run(Recorder("", stderr="segmentation fault", returncode=139))
    with pytest.raises(EngineProtocolError):
        await engine().review(request(tmp_path))


# -- a nonzero exit, which is how the CLI reports an errored envelope --

#: What `claude -p --output-format json` really prints when a run fails: the
#: envelope goes to stdout, the exit is 1, and stderr says nothing useful.
API_ERROR = {"subtype": "success", "is_error": True, "api_error_status": 404}


async def test_a_nonzero_exit_names_the_envelopes_complaint(tmp_path, run):
    """stderr is empty on a real failure; the reason is on stdout."""
    run(Recorder(envelope(**API_ERROR, result="model not found"), returncode=1))
    with pytest.raises(EngineProtocolError, match="model not found") as raised:
        await engine().review(request(tmp_path))

    assert raised.value.status == 404
    assert raised.value.usage is not None
    assert raised.value.usage.tokens == sum(USAGE.values())
    assert raised.value.usage.confidence is UsageConfidence.EXACT


async def test_a_nonzero_exit_without_an_envelope_measured_nothing(tmp_path, run):
    """No envelope, no count: the worker falls back to the reservation."""
    run(Recorder("", stderr="segmentation fault", returncode=139))
    with pytest.raises(EngineProtocolError) as raised:
        await engine().review(request(tmp_path))

    assert raised.value.usage is None


async def test_a_usage_limit_in_an_exited_envelope_is_usage_limited(tmp_path, run):
    """Not a retry: the exit code must not hide the envelope from the detector."""
    run(
        Recorder(
            envelope(**API_ERROR, result="Claude usage limit reached"), returncode=1
        )
    )
    with pytest.raises(UsageLimited) as raised:
        await engine().review(request(tmp_path))

    assert raised.value.usage is not None
    assert raised.value.usage.tokens == sum(USAGE.values())


async def test_an_exited_clean_envelopes_result_is_not_quoted(tmp_path, run):
    """Only an envelope that says it errored has an error field to read."""
    run(Recorder(envelope(result="usage limit reached"), returncode=1))
    with pytest.raises(EngineProtocolError) as raised:
        await engine().review(request(tmp_path))

    assert "usage limit" not in str(raised.value)
