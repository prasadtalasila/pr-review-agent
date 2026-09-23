"""Config loading: reject anything that could silently weaken a safety rule."""

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from pr_review_agent.config import (
    DEFAULT_EXCLUDED_PATHS,
    MAX_WORKERS,
    Config,
    ConfigError,
)
from pr_review_agent.queue import DEFAULT_LEASE
from pr_review_agent.triggers.models import Actor

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


def test_valid_config_parses():
    config = Config.from_mapping(VALID)
    assert config.github.owner == "prasadtalasila"
    assert config.github.name == "pr-review-agent"
    assert config.triggers.allowlist.allows(Actor(114395272, "8ohamed"))


def test_handle_defaults_to_claude():
    data = {
        "github": GITHUB,
        "triggers": {"allowlist": []},
        "budget": BUDGET,
        "engine": ENGINE,
    }
    assert Config.from_mapping(data).triggers.handle == "claude"


def test_handle_accepts_leading_at():
    data = {
        "github": GITHUB,
        "triggers": {"allowlist": [], "handle": "@aider"},
        "budget": BUDGET,
        "engine": ENGINE,
    }
    assert Config.from_mapping(data).triggers.handle == "aider"


def test_a_config_still_carrying_agent_user_id_is_refused_by_name():
    """The upgrade an operator meets, and the only breaking change here.

    The key named the account the agent posts as, so the classifier could
    reject its own events. It is gone -- the loop is closed in
    `publisher.render` instead -- and a file that still sets it is refused
    rather than silently ignored, because the fix is to delete one line and
    an ignored key is how an operator comes to believe it still does
    something.
    """
    data = {**VALID, "github": {"repo": "a/b", "agent_user_id": 42}}
    with pytest.raises(ConfigError, match="agent_user_id"):
        Config.from_mapping(data)


@pytest.mark.parametrize(
    "repo",
    ["pr-review-agent", "a/b/c", "", "/pr-review-agent", "prasadtalasila/", 42, None],
)
def test_invalid_repo_rejected(repo):
    with pytest.raises(ConfigError):
        Config.from_mapping(
            {"github": {"repo": repo}, "triggers": {"allowlist": []}, "budget": BUDGET}
        )


def test_login_in_allowlist_fails_loudly():
    # Would otherwise never match, silently disabling every trigger.
    data = {
        "github": GITHUB,
        "triggers": {"allowlist": ["8ohamed"]},
        "budget": BUDGET,
    }
    with pytest.raises(ConfigError, match="allowlist"):
        Config.from_mapping(data)


@pytest.mark.parametrize(
    "data",
    [
        {"github": GITHUB},
        {"triggers": {"allowlist": []}},
        # budget is required too: no limits means no reviews, not no bounds.
        {"github": GITHUB, "triggers": {"allowlist": []}},
        {},
    ],
)
def test_missing_section_rejected(data):
    with pytest.raises(ConfigError, match="missing required section"):
        Config.from_mapping(data)


def test_unknown_top_level_section_rejected():
    data = {**VALID, "budgets": {"enabled": True}}
    with pytest.raises(ConfigError, match="unknown top-level"):
        Config.from_mapping(data)


@pytest.mark.parametrize(
    "section, bad",
    [
        ("github", {"repo": "a/b", "agent_id": 1}),
        ("triggers", {"allowlist": [], "handel": "x"}),
    ],
)
def test_typo_in_key_rejected(section, bad):
    # "handel" must not silently fall back to the default handle.
    data = {**VALID, section: bad}
    with pytest.raises(ConfigError, match="unknown keys"):
        Config.from_mapping(data)


def test_allowlist_must_be_a_list():
    data = {
        "github": GITHUB,
        "triggers": {"allowlist": 114395272},
        "budget": BUDGET,
    }
    with pytest.raises(ConfigError, match="must be a list"):
        Config.from_mapping(data)


def test_load_reads_yaml_file(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "github:\n  repo: prasadtalasila/pr-review-agent\n"
        "triggers:\n  allowlist:\n    - 114395272\n" + BUDGET_YAML,
        encoding="utf-8",
    )
    assert Config.load(path).github.name == "pr-review-agent"


def test_load_missing_file_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="cannot read config"):
        Config.load(tmp_path / "nope.yaml")


def test_load_invalid_yaml_is_a_config_error(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("github: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid YAML"):
        Config.load(path)


def test_shipped_example_config_is_valid():
    # The example must stay loadable; it is what an operator copies.
    from pathlib import Path

    example = Path(__file__).parent.parent / "config.example.yaml"
    config = Config.load(example)
    assert config.github.repo == "prasadtalasila/pr-review-agent"
    assert config.triggers.allowlist.allows(Actor(114395272, "8ohamed"))


def test_store_path_defaults_when_the_section_is_absent():
    assert Config.from_mapping(VALID).store.path == "state.db"


def test_store_path_is_read_from_the_section():
    data = {**VALID, "store": {"path": "/var/lib/agent/state.db"}}
    assert Config.from_mapping(data).store.path == "/var/lib/agent/state.db"


@pytest.mark.parametrize("path", ["   ", "", None, 7])
def test_an_unusable_store_path_is_rejected(path):
    data = {**VALID, "store": {"path": path}}
    with pytest.raises(ConfigError, match="store.path"):
        Config.from_mapping(data)


def test_unknown_key_in_store_is_rejected():
    data = {**VALID, "store": {"paht": "state.db"}}
    with pytest.raises(ConfigError, match="unknown keys in 'store'"):
        Config.from_mapping(data)


def test_classifier_is_built_from_config():
    classifier = Config.from_mapping(VALID).classifier(
        since=datetime(2026, 1, 1, tzinfo=timezone.utc)
    )
    assert classifier.handle == "claude"
    assert classifier.allowlist.allows(Actor(114395272, "8ohamed"))


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


@pytest.mark.parametrize("value", [0, -1, "88000", None, 1.5, True])
def test_an_unusable_token_limit_is_rejected(value):
    # True is in this list on purpose: bool subclasses int, so without an
    # explicit check "weekly_tokens: true" would parse as a one-token ceiling.
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


def test_unknown_key_in_budget_is_rejected():
    data = {**VALID, "budget": {**BUDGET, "reviewer_share": 40}}
    with pytest.raises(ConfigError, match="unknown keys in 'budget'"):
        Config.from_mapping(data)


def test_the_kill_switch_parses_off():
    data = {**VALID, "budget": {**BUDGET, "enabled": False}}
    assert Config.from_mapping(data).budget.enabled is False


def test_size_caps_default_to_the_pinned_values():
    """Asserted by value, not against the constants they come from.

    These are layer 2's diff-size caps, so widening one has to be a visible
    diff rather than a changed default nobody reviewed.
    """
    budget = Config.from_mapping(VALID).budget
    assert budget.max_changed_files == 100
    assert budget.max_changed_lines == 5000


def test_size_caps_can_be_tightened():
    data = {
        **VALID,
        "budget": {**BUDGET, "max_changed_files": 10, "max_changed_lines": 200},
    }
    budget = Config.from_mapping(data).budget
    assert budget.max_changed_files == 10
    assert budget.max_changed_lines == 200


def test_excluded_paths_defaults_to_the_four_categories():
    """Lockfiles, vendored, generated, minified -- BUDGET.md layer 2's list."""
    budget = Config.from_mapping(VALID).budget
    assert budget.excluded_paths == DEFAULT_EXCLUDED_PATHS
    assert "**/vendor/**" in budget.excluded_paths


def test_excluded_paths_can_be_replaced_outright():
    """Including with nothing: a repository may want its lockfiles read."""
    data = {**VALID, "budget": {**BUDGET, "excluded_paths": ["*.generated.ts"]}}
    assert Config.from_mapping(data).budget.excluded_paths == ("*.generated.ts",)
    empty = {**VALID, "budget": {**BUDGET, "excluded_paths": []}}
    assert Config.from_mapping(empty).budget.excluded_paths == ()


@pytest.mark.parametrize("value", ["", "   ", 5, None, True])
def test_an_unusable_exclusion_pattern_is_rejected(value):
    data = {**VALID, "budget": {**BUDGET, "excluded_paths": [value]}}
    with pytest.raises(ConfigError, match="excluded_paths"):
        Config.from_mapping(data)


def test_a_pattern_may_not_open_its_own_pathspec_magic():
    """The ``:(exclude,glob)`` prefix is the agent's to supply.

    A pattern free to start with ``:`` could mean something an operator
    cannot predict from reading their own configuration file -- ``:(attr:)``,
    or a bare ``:`` re-anchoring the path.
    """
    data = {**VALID, "budget": {**BUDGET, "excluded_paths": [":(attr:binary)"]}}
    with pytest.raises(ConfigError, match="may not begin with"):
        Config.from_mapping(data)


def test_excluded_paths_must_be_a_list():
    data = {**VALID, "budget": {**BUDGET, "excluded_paths": "**/vendor/**"}}
    with pytest.raises(ConfigError, match="excluded_paths"):
        Config.from_mapping(data)


@pytest.mark.parametrize("value", [0, -1, True, "500", 2.5, None])
def test_a_cap_that_is_not_a_positive_integer_is_rejected(value):
    # True is here for the same reason as in the token limits: bool
    # subclasses int, so "max_changed_lines: true" would be a cap of one.
    data = {**VALID, "budget": {**BUDGET, "max_changed_lines": value}}
    with pytest.raises(ConfigError, match="max_changed_lines"):
        Config.from_mapping(data)


# -- engine: which coding agent reviews -----------------------------------


def test_engine_section_is_required():
    """The worker calls it, so a file that names no engine cannot run."""
    data = {k: v for k, v in VALID.items() if k != "engine"}
    with pytest.raises(ConfigError, match="missing required section: 'engine'"):
        Config.from_mapping(data)


def test_engine_keys_have_no_defaults_that_choose_a_cost():
    """There is no model an operator could be assumed to have chosen."""
    assert Config.from_mapping(VALID).engine.model == "claude-sonnet-5"


def test_engine_section_is_read():
    data = {**VALID, "engine": {**ENGINE, "standards_paths": ["AGENTS.md"]}}
    engine = Config.from_mapping(data).engine
    assert engine is not None
    assert engine.model == "claude-sonnet-5"
    assert engine.binary == "claude"
    assert engine.timeout_seconds == 900.0
    assert engine.standards_paths == ("AGENTS.md",)


@pytest.mark.parametrize("key", ["model", "expected_version"])
def test_a_key_that_decides_a_cost_has_no_default(key):
    data = {**VALID, "engine": {k: v for k, v in ENGINE.items() if k != key}}
    with pytest.raises(ConfigError, match=f"engine.{key}"):
        Config.from_mapping(data)


@pytest.mark.parametrize("value", [0, -1, "soon", True, None])
def test_the_wall_clock_must_be_a_positive_number(value):
    data = {**VALID, "engine": {**ENGINE, "timeout_seconds": value}}
    with pytest.raises(ConfigError, match="engine.timeout_seconds"):
        Config.from_mapping(data)


@pytest.mark.parametrize("over", [0, 1])
def test_a_wall_clock_at_or_above_the_lease_is_refused(over):
    """A run that can outlive its lease loses it to a second worker.

    ``queue.py`` calls ``DEFAULT_LEASE`` "comfortably above the per-run
    wall-clock ceiling", and leases carry an expiry rather than a heartbeat
    because of it. This is that assumption, enforced rather than asserted.
    """
    data = {
        **VALID,
        "engine": {**ENGINE, "timeout_seconds": DEFAULT_LEASE.total_seconds() + over},
    }
    with pytest.raises(ConfigError, match="below the queue lease"):
        Config.from_mapping(data)


def test_a_wall_clock_below_the_lease_loads():
    """Pinned against the lease itself, so changing it cannot orphan the check."""
    seconds = DEFAULT_LEASE.total_seconds() - 1
    data = {**VALID, "engine": {**ENGINE, "timeout_seconds": seconds}}
    assert Config.from_mapping(data).engine.timeout_seconds == seconds


@pytest.mark.parametrize("value", ["AGENTS.md", [""], [3], {}])
def test_standards_paths_must_be_a_list_of_paths(value):
    data = {**VALID, "engine": {**ENGINE, "standards_paths": value}}
    with pytest.raises(ConfigError, match="engine.standards_paths"):
        Config.from_mapping(data)


def test_unknown_key_in_engine_is_rejected():
    data = {**VALID, "engine": {**ENGINE, "mdoel": "sonnet"}}
    with pytest.raises(ConfigError, match="unknown keys in 'engine'"):
        Config.from_mapping(data)


# -- workspace: a path and nothing else ----------------------------------


def test_workspace_section_is_optional():
    assert Config.from_mapping(VALID).workspace.cache_dir == ".cache/repos"


def test_workspace_cache_dir_is_read():
    data = {**VALID, "workspace": {"cache_dir": "/srv/agent/repos"}}
    assert Config.from_mapping(data).workspace.cache_dir == "/srv/agent/repos"


def test_unknown_key_in_workspace_is_rejected():
    data = {**VALID, "workspace": {"cachedir": "/srv"}}
    with pytest.raises(ConfigError, match="unknown keys in 'workspace'"):
        Config.from_mapping(data)


@pytest.mark.parametrize("value", ["", "   ", 42, None])
def test_an_unusable_cache_dir_is_rejected(value):
    data = {**VALID, "workspace": {"cache_dir": value}}
    with pytest.raises(ConfigError, match="workspace.cache_dir"):
        Config.from_mapping(data)


# -- worker: how many reviews may run at once ----------------------------
#
# CLAUDE.md section 5: worker.count multiplies the reservation floor, so the
# default and the cap are spending bounds and are pinned here.


def test_worker_section_is_optional_and_defaults_to_one():
    assert Config.from_mapping(VALID).worker.count == 1


def test_worker_count_is_read():
    data = {**VALID, "worker": {"count": 3}}
    assert Config.from_mapping(data).worker.count == 3


def test_worker_count_is_capped():
    """Every concurrent run reserves max_run_tokens up front."""
    data = {**VALID, "worker": {"count": MAX_WORKERS + 1}}
    with pytest.raises(ConfigError, match="worker.count"):
        Config.from_mapping(data)


@pytest.mark.parametrize("value", [0, -1, "2", 1.5, None, True])
def test_an_unusable_worker_count_is_rejected(value):
    data = {**VALID, "worker": {"count": value}}
    with pytest.raises(ConfigError, match="worker.count"):
        Config.from_mapping(data)


def test_unknown_key_in_worker_is_rejected():
    data = {**VALID, "worker": {"workers": 2}}
    with pytest.raises(ConfigError, match="unknown keys in 'worker'"):
        Config.from_mapping(data)


# -- publish: the second operator brake ----------------------------------
#
# `dry_run` runs the whole pipeline and posts nothing. Unlike the budget
# token counts it has a safe default -- posting is the point of the agent --
# so the section is optional.


def test_publish_section_is_optional_and_posts_by_default():
    assert Config.from_mapping(VALID).publish.dry_run is False


def test_dry_run_is_read():
    data = {**VALID, "publish": {"dry_run": True}}
    assert Config.from_mapping(data).publish.dry_run is True


@pytest.mark.parametrize("value", ["true", 1, None, []])
def test_an_unusable_dry_run_is_rejected(value):
    """`dry_run: "no"` is truthy in Python and would post for real."""
    data = {**VALID, "publish": {"dry_run": value}}
    with pytest.raises(ConfigError, match="publish.dry_run"):
        Config.from_mapping(data)


def test_unknown_key_in_publish_is_rejected():
    data = {**VALID, "publish": {"dryrun": True}}
    with pytest.raises(ConfigError, match="unknown keys in 'publish'"):
        Config.from_mapping(data)


# -- the shipped examples, which documentation has already got wrong once --

EXAMPLES = Path(__file__).resolve().parent.parent


def test_the_minimal_example_loads():
    """It is the file the quickstart tells people to copy.

    CONFIG.md once showed a "minimal file" with no budget section, which
    would not have loaded at all. Parsing the real file is what stops the
    documentation and the loader drifting apart again.
    """
    config = Config.load(EXAMPLES / "config.minimal.example.yaml")
    assert config.github.repo == "prasadtalasila/pr-review-agent"
    assert config.budget.enabled is True


def test_the_minimal_example_carries_only_required_keys():
    """Minimal has to mean minimal: every key in it must be load-bearing."""
    data = yaml.safe_load((EXAMPLES / "config.minimal.example.yaml").read_text())
    assert set(data) == {"github", "triggers", "budget", "engine"}
    assert set(data["github"]) == {"repo"}
    assert set(data["triggers"]) == {"allowlist"}
    assert set(data["budget"]) == {
        "session_tokens",
        "weekly_tokens",
        "max_run_tokens",
    }
    assert set(data["engine"]) == {"model", "expected_version", "timeout_seconds"}


def test_the_minimal_example_carries_no_comments():
    """It is copied verbatim to config.yaml; the reasoning lives in CONFIG.md.

    A comment here becomes a comment in somebody's real configuration, where
    it ages without anyone reviewing it.
    """
    text = (EXAMPLES / "config.minimal.example.yaml").read_text()
    assert "#" not in text


def test_the_comprehensive_example_loads():
    config = Config.load(EXAMPLES / "config.example.yaml")
    assert config.store.path == "state.db"
    assert config.workspace.cache_dir == ".cache/repos"


def test_the_comprehensive_example_shows_every_key_the_loader_accepts():
    """A key the loader takes but the example omits is undiscoverable."""
    data = yaml.safe_load((EXAMPLES / "config.example.yaml").read_text())
    assert set(data) == {
        "github",
        "triggers",
        "budget",
        "store",
        "workspace",
        "engine",
        "worker",
        "publish",
        "logging",
    }
    assert set(data["logging"]) == {"level", "format"}
    assert set(data["github"]) == {"repo"}
    assert set(data["triggers"]) == {"allowlist", "handle"}
    assert set(data["budget"]) == {
        "enabled",
        "session_tokens",
        "weekly_tokens",
        "max_run_tokens",
        "reviewer_share_pct",
        "max_changed_files",
        "max_changed_lines",
        "excluded_paths",
    }
    assert set(data["store"]) == {"path"}
    assert set(data["publish"]) == {"dry_run"}
    assert set(data["workspace"]) == {"cache_dir"}
    assert set(data["engine"]) == {
        "binary",
        "model",
        "timeout_seconds",
        "standards_paths",
        "expected_version",
    }


def test_the_comprehensive_example_states_the_real_defaults():
    """Its optional values are advertised as the defaults, so they must be."""
    shown = Config.load(EXAMPLES / "config.example.yaml")
    defaults = Config.load(EXAMPLES / "config.minimal.example.yaml")
    assert shown.triggers.handle == defaults.triggers.handle
    assert shown.budget.enabled == defaults.budget.enabled
    assert shown.budget.reviewer_share_pct == defaults.budget.reviewer_share_pct
    assert shown.budget.max_changed_files == defaults.budget.max_changed_files
    assert shown.budget.max_changed_lines == defaults.budget.max_changed_lines
    assert shown.budget.excluded_paths == defaults.budget.excluded_paths
    assert shown.store.path == defaults.store.path
    assert shown.workspace.cache_dir == defaults.workspace.cache_dir
    assert shown.worker.count == defaults.worker.count
    assert shown.publish.dry_run == defaults.publish.dry_run
    assert shown.engine.binary == defaults.engine.binary
