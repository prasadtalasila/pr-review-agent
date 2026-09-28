"""What a config file must hold to load at all, and what ships as an example.

A typo is a rejection rather than a default, and the two shipped examples
are parsed by the same loader the daemon uses: an example that no longer
loads is a quickstart that fails on the operator's first command.
"""

from dataclasses import fields
from datetime import datetime, timezone
from pathlib import Path
from typing import get_type_hints

import pytest
import yaml
from config_harness import BUDGET, BUDGET_YAML, ENGINE, GITHUB, VALID

from pr_review_agent._startup import PLACEHOLDER_REPO, StartupError, load_config
from pr_review_agent.config import Config, ConfigError
from pr_review_agent.triggers.models import Actor


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
    assert config.github.repo == PLACEHOLDER_REPO
    assert not config.triggers.allowlist.user_ids


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


# -- the shipped examples, which documentation has already got wrong once --

EXAMPLES = Path(__file__).resolve().parent.parent


def test_the_minimal_example_loads():
    """It is the file the quickstart tells people to copy.

    CONFIG.md once showed a "minimal file" with no budget section, which
    would not have loaded at all. Parsing the real file is what stops the
    documentation and the loader drifting apart again.
    """
    config = Config.load(EXAMPLES / "config.minimal.example.yaml")
    assert config.github.repo == PLACEHOLDER_REPO
    assert config.budget.enabled is True


@pytest.mark.parametrize("name", ["config.minimal.example.yaml", "config.example.yaml"])
def test_a_shipped_example_names_no_real_account(name):
    """A shipped id is an account somebody else's deployment would trust."""
    config = Config.load(EXAMPLES / name)
    assert config.github.repo == PLACEHOLDER_REPO
    assert config.triggers.allowlist.user_ids == frozenset()


@pytest.mark.parametrize("name", ["config.minimal.example.yaml", "config.example.yaml"])
def test_a_shipped_example_is_parseable_but_not_runnable(name):
    """The loader accepts the placeholder; every command that acts refuses it."""
    with pytest.raises(StartupError, match="placeholder"):
        load_config(str(EXAMPLES / name))


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


#: Section name -> the dataclass that holds it, read off ``Config`` itself so
#: this cannot be the thing that drifts.
SECTIONS = get_type_hints(Config)


def _field_names(schema: type) -> set[str]:
    return {field.name for field in fields(schema)}


def test_the_comprehensive_example_shows_every_key_the_loader_accepts():
    """A key the loader takes but the example omits is undiscoverable.

    Derived from the dataclasses rather than spelled out beside them, which
    is the drift this file is meant to catch rather than join: a field added
    to a section fails here until the template shows it. A field whose
    default is to be *absent* -- ``per_contributor_pct`` means no cap at all
    -- is shown commented out, which counts.
    """
    text = (EXAMPLES / "config.example.yaml").read_text()
    data = yaml.safe_load(text)
    assert set(data) == set(SECTIONS)
    for name, schema in SECTIONS.items():
        shown = set(data[name])
        names = _field_names(schema)
        assert shown <= names, f"{name!r} advertises keys the loader rejects"
        for missing in sorted(names - shown):
            assert f"# {missing}:" in text, f"{name}.{missing} is undiscoverable"


def test_every_section_rejects_a_key_its_dataclass_cannot_hold():
    """Unknown-key rejection is per section, not only at the top level."""
    for name in SECTIONS:
        data = yaml.safe_load((EXAMPLES / "config.example.yaml").read_text())
        data[name]["not_a_field"] = 1
        with pytest.raises(ConfigError, match=f"unknown keys in '{name}'"):
            Config.from_mapping(data)


def test_the_comprehensive_example_states_the_real_defaults():
    """Its optional values are advertised as the defaults, so they must be."""
    shown = Config.load(EXAMPLES / "config.example.yaml")
    defaults = Config.load(EXAMPLES / "config.minimal.example.yaml")
    assert shown.triggers.handle == defaults.triggers.handle
    assert shown.budget.enabled == defaults.budget.enabled
    assert shown.budget.authority == defaults.budget.authority
    assert shown.budget.comply == defaults.budget.comply
    assert shown.budget.reviewer_share_pct == defaults.budget.reviewer_share_pct
    assert shown.budget.max_changed_files == defaults.budget.max_changed_files
    assert shown.budget.max_changed_lines == defaults.budget.max_changed_lines
    assert shown.budget.excluded_paths == defaults.budget.excluded_paths
    assert shown.store.path == defaults.store.path
    assert shown.workspace.cache_dir == defaults.workspace.cache_dir
    assert shown.workspace.git == defaults.workspace.git
    assert shown.worker.count == defaults.worker.count
    assert shown.publish.dry_run == defaults.publish.dry_run
    assert shown.engine.binary == defaults.engine.binary
