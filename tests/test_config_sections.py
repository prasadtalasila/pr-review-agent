"""The remaining sections: sizes, exclusions, engine, workspace, worker, publish.

Each is optional or required for a stated reason, and each rejects a key its
dataclass cannot hold -- a silently ignored key is a setting the operator
believes is in force.
"""

import pytest
from config_harness import BUDGET, ENGINE, VALID

from pr_review_agent.config import (
    DEFAULT_EXCLUDED_PATHS,
    MAX_WORKERS,
    Config,
    ConfigError,
)
from pr_review_agent.queue import DEFAULT_LEASE


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


def test_the_built_in_exclusions_are_in_force_when_neither_key_is_set():
    """Lockfiles, vendored, generated, minified -- BUDGET.md layer 2's list."""
    budget = Config.from_mapping(VALID).budget
    assert budget.default_exclusions is True
    assert budget.excluded_paths == ()
    assert budget.effective_excluded_paths == DEFAULT_EXCLUDED_PATHS


def test_excluded_paths_adds_to_the_built_ins():
    """A config file names what is special about its repository, not the
    sixty patterns every repository shares."""
    data = {**VALID, "budget": {**BUDGET, "excluded_paths": ["*.snap"]}}
    budget = Config.from_mapping(data).budget
    assert budget.excluded_paths == ("*.snap",)
    assert budget.effective_excluded_paths == (*DEFAULT_EXCLUDED_PATHS, "*.snap")


def test_default_exclusions_off_leaves_only_the_operators_own():
    """A repository that genuinely reviews its lockfiles is a real repository."""
    own = {
        **VALID,
        "budget": {**BUDGET, "default_exclusions": False, "excluded_paths": ["*.snap"]},
    }
    assert Config.from_mapping(own).budget.effective_excluded_paths == ("*.snap",)
    nothing = {**VALID, "budget": {**BUDGET, "default_exclusions": False}}
    assert Config.from_mapping(nothing).budget.effective_excluded_paths == ()


@pytest.mark.parametrize("value", ["yes", 1, None])
def test_default_exclusions_must_be_a_boolean(value):
    data = {**VALID, "budget": {**BUDGET, "default_exclusions": value}}
    with pytest.raises(ConfigError, match="default_exclusions"):
        Config.from_mapping(data)


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


# -- which git the checkout runs -----------------------------------------


def test_workspace_git_defaults_to_the_plain_name():
    assert Config.from_mapping(VALID).workspace.git == "git"


def test_workspace_git_accepts_an_absolute_path():
    data = dict(VALID, workspace={"git": "/opt/git/bin/git"})
    assert Config.from_mapping(data).workspace.git == "/opt/git/bin/git"


@pytest.mark.parametrize("value", ["", "   ", 7, None])
def test_a_non_path_workspace_git_is_refused(value):
    data = dict(VALID, workspace={"git": value})
    with pytest.raises(ConfigError, match="workspace.git"):
        Config.from_mapping(data)
