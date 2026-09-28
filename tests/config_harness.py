"""One valid mapping, and the YAML the budget tests vary.

Shared by the `test_config_*.py` family: every case there is this mapping
with one key changed, so the thing under test is the change rather than the
twenty lines of context around it.
"""

# Required, so every fixture below carries it. A config that names no
# spending limits must not load: every one of these numbers is a guess the
# operator has to make, and a default would be a ceiling nobody chose.
BUDGET = {
    "session_tokens": 88_000,
    "weekly_tokens": 1_500_000,
    "max_run_tokens": 60_000,
}

GITHUB = {"repo": "a/b"}

# Required since the worker started calling it: a daemon that claims work
# with no engine to run would reserve allowance and then fail every review.
ENGINE = {
    "model": "claude-sonnet-5",
    "expected_version": "2.1.274",
    "timeout_seconds": 900,
}

VALID = {
    "github": {"repo": "prasadtalasila/pr-review-agent"},
    "triggers": {"handle": "claude", "allowlist": [114395272]},
    "budget": BUDGET,
    "engine": ENGINE,
}

BUDGET_YAML = (
    "budget:\n"
    "  session_tokens: 88000\n"
    "  weekly_tokens: 1500000\n"
    "  max_run_tokens: 60000\n"
    "engine:\n"
    "  model: claude-sonnet-5\n"
    "  expected_version: '2.1.274'\n"
    "  timeout_seconds: 900\n"
)
