"""Every number that decides a cost, and the refusal when one is unusable.

There is no default anywhere in this file that chooses to spend: a missing
limit is an error, because a limit that defaulted would be a ceiling nobody
set.
"""

from datetime import timedelta

import pytest
import yaml
from config_harness import BUDGET, VALID

from pr_review_agent.config import DEFAULT_EXCLUDED_PATHS, Config, ConfigError

# -- budget: every key here is a spending bound (CLAUDE.md §5) -----------


def test_budget_defaults_to_the_documented_share():
    budget = Config.from_mapping(VALID).budget
    assert budget.enabled is True
    assert budget.reviewer_share_pct == 40
    assert budget.session_limit == 88_000 * 40 // 100
    assert budget.weekly_limit == 1_500_000 * 40 // 100
    assert budget.daily_limit == (1_500_000 * 40 // 100) // 7


@pytest.mark.parametrize("key", ["session_tokens", "weekly_tokens", "max_run_tokens"])
def test_a_missing_token_limit_is_rejected(key):
    # No defaults: an invented spending ceiling is worse than being asked.
    data = {**VALID, "budget": {k: v for k, v in BUDGET.items() if k != key}}
    with pytest.raises(ConfigError, match=key):
        Config.from_mapping(data)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1_500_000", 1_500_000),
        ("1500k", 1_500_000),
        ("1.5m", 1_500_000),
        ("1.5M", 1_500_000),
        ("'1.5m'", 1_500_000),
    ],
)
def test_a_token_limit_may_be_written_readably(text, expected):
    # Through the YAML loader, because underscores are its doing, not ours.
    config = Config.from_mapping(
        {**VALID, "budget": {**BUDGET, **yaml.safe_load(f"weekly_tokens: {text}")}}
    )
    assert config.budget.weekly_tokens == expected


@pytest.mark.parametrize(
    "value",
    [0, -1, "88000", None, 1.5, True, "0k", "1.2345k", "88g", "k", "-5k", "1e6"],
)
def test_an_unusable_token_limit_is_rejected(value):
    # True is in this list on purpose: bool subclasses int, so without an
    # explicit check "weekly_tokens: true" would parse as a one-token ceiling.
    # "1.2345k" is 1234.5 tokens: refused, because rounding a ceiling is
    # choosing one.
    data = {**VALID, "budget": {**BUDGET, "weekly_tokens": value}}
    with pytest.raises(ConfigError, match="weekly_tokens"):
        Config.from_mapping(data)


@pytest.mark.parametrize("value", [0, -1, 101, "40", None, True])
def test_an_unusable_share_is_rejected(value):
    data = {**VALID, "budget": {**BUDGET, "reviewer_share_pct": value}}
    with pytest.raises(ConfigError, match="reviewer_share_pct"):
        Config.from_mapping(data)


def test_enabled_must_be_a_boolean():
    data = {**VALID, "budget": {**BUDGET, "enabled": "false"}}
    with pytest.raises(ConfigError, match="budget.enabled"):
        Config.from_mapping(data)


def test_a_run_larger_than_the_daily_allowance_is_rejected():
    """A config that could never admit anything fails loudly at startup.

    The daily window is the tightest of the three, so a run that cannot fit
    inside it can never be admitted -- an agent that reviews nothing, arrived
    at by arithmetic nobody did by hand.
    """
    data = {**VALID, "budget": {**BUDGET, "max_run_tokens": 1_000_000}}
    with pytest.raises(ConfigError, match="no run could ever be admitted"):
        Config.from_mapping(data)


def test_the_per_contributor_cap_is_off_by_default():
    """Unset means no cap, so an existing deployment is unaffected."""
    budget = Config.from_mapping(VALID).budget
    assert budget.per_contributor_pct is None
    assert budget.per_contributor_limit is None


def test_the_per_contributor_cap_is_a_share_of_the_weekly_allowance():
    data = {**VALID, "budget": {**BUDGET, "per_contributor_pct": 50}}
    budget = Config.from_mapping(data).budget
    assert budget.per_contributor_limit == budget.weekly_limit * 50 // 100


@pytest.mark.parametrize("value", [0, -1, 101, "40", 1.5, True])
def test_an_unusable_per_contributor_cap_is_rejected(value):
    # None is absent from this list on purpose: it is how the key is unset.
    data = {**VALID, "budget": {**BUDGET, "per_contributor_pct": value}}
    with pytest.raises(ConfigError, match="per_contributor_pct"):
        Config.from_mapping(data)


def test_a_run_larger_than_a_contributor_allowance_is_rejected():
    """The same arithmetic trap as the daily window, one window further.

    1 % of the weekly share is below ``max_run_tokens``, so with the cap set
    that low no contributor could ever be admitted -- including the only one
    on a one-person allowlist.
    """
    data = {**VALID, "budget": {**BUDGET, "per_contributor_pct": 1}}
    with pytest.raises(ConfigError, match="no run could ever be admitted"):
        Config.from_mapping(data)


def test_the_pacer_ships_on_with_a_shorter_wait_for_a_mention():
    """A default an operator does not have to discover to be protected by."""
    budget = Config.from_mapping(VALID).budget
    assert budget.min_review_interval_seconds == 900
    assert budget.mention_min_review_interval_seconds == 300
    assert budget.review_interval(mention=True) == timedelta(seconds=300)
    assert budget.review_interval(mention=False) == timedelta(seconds=900)


@pytest.mark.parametrize("key", ["min_review", "mention_min_review"])
@pytest.mark.parametrize("value", [-1, "900", 1.5, True])
def test_an_unusable_review_interval_is_rejected(key, value):
    name = f"{key}_interval_seconds"
    data = {**VALID, "budget": {**BUDGET, name: value}}
    with pytest.raises(ConfigError, match=name):
        Config.from_mapping(data)


def test_a_mention_may_not_wait_longer_than_an_ordinary_trigger():
    """Far likelier to be a transposition than a policy anybody wanted."""
    data = {
        **VALID,
        "budget": {
            **BUDGET,
            "min_review_interval_seconds": 60,
            "mention_min_review_interval_seconds": 120,
        },
    }
    with pytest.raises(ConfigError, match="wait longer"):
        Config.from_mapping(data)


def test_the_per_pull_request_review_cap_is_off_by_default():
    assert Config.from_mapping(VALID).budget.max_reviews_per_pull_request is None


@pytest.mark.parametrize("value", [0, -1, "5", 1.5, True])
def test_an_unusable_per_pull_request_cap_is_rejected(value):
    data = {**VALID, "budget": {**BUDGET, "max_reviews_per_pull_request": value}}
    with pytest.raises(ConfigError, match="max_reviews_per_pull_request"):
        Config.from_mapping(data)


def test_superseded_reviews_are_posted_by_default():
    """The tokens are spent before the head is re-read; discarding shows nobody."""
    assert Config.from_mapping(VALID).publish.post_superseded is True


def test_an_unusable_post_superseded_is_rejected():
    data = {**VALID, "publish": {"post_superseded": "no"}}
    with pytest.raises(ConfigError, match="post_superseded"):
        Config.from_mapping(data)


def test_posts_are_tried_ten_times_by_default():
    """The findings are paid for; the cost of one more attempt is a request."""
    assert Config.from_mapping(VALID).publish.max_publish_attempts == 10


def test_max_publish_attempts_is_read_from_the_file():
    data = {**VALID, "publish": {"max_publish_attempts": 3}}
    assert Config.from_mapping(data).publish.max_publish_attempts == 3


@pytest.mark.parametrize("given", [0, -1, "many", 2.5, True])
def test_an_unusable_max_publish_attempts_is_rejected(given):
    """`true` included: a bool is an int in Python, and would mean a limit of one."""
    data = {**VALID, "publish": {"max_publish_attempts": given}}
    with pytest.raises(ConfigError, match="max_publish_attempts"):
        Config.from_mapping(data)


def test_unknown_key_in_budget_is_rejected():
    data = {**VALID, "budget": {**BUDGET, "reviewer_share": 40}}
    with pytest.raises(ConfigError, match="unknown keys in 'budget'"):
        Config.from_mapping(data)


def test_the_kill_switch_parses_off():
    data = {**VALID, "budget": {**BUDGET, "enabled": False}}
    assert Config.from_mapping(data).budget.enabled is False


def test_a_lone_daemon_governs_its_own_store_by_default():
    """A single deployment needs neither key, which is why both default true.

    ``comply`` is consulted only when ``authority`` is false, so the pair
    means "publish my own limits" until a fleet says otherwise.
    """
    budget = Config.from_mapping(VALID).budget
    assert budget.authority is True
    assert budget.comply is True


@pytest.mark.parametrize("key", ["authority", "comply"])
def test_the_policy_flags_reject_a_non_boolean(key):
    data = {**VALID, "budget": {**BUDGET, key: "yes"}}
    with pytest.raises(ConfigError, match=f"budget.{key} must be true or false"):
        Config.from_mapping(data)


def test_adopting_a_policy_takes_the_pool_and_leaves_the_rest():
    """The split that makes a shared budget safe to run.

    The pool arithmetic has to come from one place or the most permissive
    file wins; everything else describes a repository rather than the
    allowance, and ``enabled`` is the brake that must stay pullable per repo.
    """
    local = Config.from_mapping(
        {
            **VALID,
            "budget": {
                **BUDGET,
                "enabled": False,
                "max_changed_files": 7,
                "default_exclusions": False,
                "excluded_paths": ["**/local.lock"],
            },
        }
    ).budget
    authority = Config.from_mapping(
        {**VALID, "budget": {**BUDGET, "weekly_tokens": 9_000_000}}
    ).budget

    adopted = local.adopt(authority.shared())

    assert adopted.weekly_tokens == 9_000_000
    assert adopted.enabled is False
    assert adopted.max_changed_files == 7
    assert adopted.default_exclusions is False
    assert adopted.effective_excluded_paths == ("**/local.lock",)


def test_the_built_in_exclusions_are_pinned_by_value():
    """Issue #122's acceptance: a change to the built-ins is a diff to this
    test, not a changed default nobody reviewed. The generator groups are
    pr-agent's ``generated_code_ignore.toml`` at ``10bbd9a`` (MIT)."""
    assert DEFAULT_EXCLUDED_PATHS == (
        "**/package-lock.json",
        "**/yarn.lock",
        "**/pnpm-lock.yaml",
        "**/poetry.lock",
        "**/Cargo.lock",
        "**/Gemfile.lock",
        "**/composer.lock",
        "**/go.sum",
        "**/vendor/**",
        "**/node_modules/**",
        "**/third_party/**",
        "**/*.generated.*",
        "**/*.pb.go",
        "**/*.pb.cc",
        "**/*_pb2.py",
        "**/*.pb.swift",
        "**/*.pb.rb",
        "**/*.pb.php",
        "**/*.pb.h",
        "**/__generated__/**",
        "**/openapi_client/**",
        "**/openapi_server/**",
        "**/swagger.json",
        "**/swagger.yaml",
        "**/*.graphql.ts",
        "**/*.graphql.js",
        "**/*_grpc.py",
        "**/*Grpc.java",
        "**/*Grpc.cs",
        "**/*_grpc.ts",
        "**/*_grpc.js",
        "**/*_gen.go",
        "**/*generated.go",
        "**/*.min.js",
        "**/*.min.css",
        "**/*.map",
    )
