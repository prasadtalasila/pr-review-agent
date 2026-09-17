"""Config loading: reject anything that could silently weaken a safety rule."""

from datetime import datetime, timezone

import pytest

from pr_review_agent.config import Config, ConfigError
from pr_review_agent.triggers.models import Actor

VALID = {
    "github": {"repo": "INTO-CPS-Association/DTaaS", "agent_user_id": 42},
    "triggers": {"handle": "claude", "allowlist": [114395272]},
}


def test_valid_config_parses():
    config = Config.from_mapping(VALID)
    assert config.github.owner == "INTO-CPS-Association"
    assert config.github.name == "DTaaS"
    assert config.triggers.allowlist.allows(Actor(114395272, "8ohamed"))


def test_handle_defaults_to_claude():
    data = {"github": {"repo": "a/b"}, "triggers": {"allowlist": []}}
    assert Config.from_mapping(data).triggers.handle == "claude"


def test_handle_accepts_leading_at():
    data = {
        "github": {"repo": "a/b"},
        "triggers": {"allowlist": [], "handle": "@aider"},
    }
    assert Config.from_mapping(data).triggers.handle == "aider"


def test_agent_user_id_is_optional():
    data = {"github": {"repo": "a/b"}, "triggers": {"allowlist": []}}
    assert Config.from_mapping(data).github.agent_user_id is None


@pytest.mark.parametrize(
    "repo", ["DTaaS", "a/b/c", "", "/DTaaS", "INTO-CPS-Association/", 42, None]
)
def test_invalid_repo_rejected(repo):
    with pytest.raises(ConfigError):
        Config.from_mapping({"github": {"repo": repo}, "triggers": {"allowlist": []}})


def test_login_in_allowlist_fails_loudly():
    # Would otherwise never match, silently disabling every trigger.
    data = {"github": {"repo": "a/b"}, "triggers": {"allowlist": ["8ohamed"]}}
    with pytest.raises(ConfigError, match="allowlist"):
        Config.from_mapping(data)


@pytest.mark.parametrize(
    "data",
    [
        {"github": {"repo": "a/b"}},
        {"triggers": {"allowlist": []}},
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
    data = {"github": {"repo": "a/b"}, "triggers": {"allowlist": 114395272}}
    with pytest.raises(ConfigError, match="must be a list"):
        Config.from_mapping(data)


def test_load_reads_yaml_file(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "github:\n  repo: INTO-CPS-Association/DTaaS\n"
        "triggers:\n  allowlist:\n    - 114395272\n",
        encoding="utf-8",
    )
    assert Config.load(path).github.name == "DTaaS"


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
    assert config.github.repo == "INTO-CPS-Association/DTaaS"
    assert config.triggers.allowlist.allows(Actor(114395272, "8ohamed"))


def test_classifier_is_built_from_config():
    classifier = Config.from_mapping(VALID).classifier(
        since=datetime(2026, 1, 1, tzinfo=timezone.utc)
    )
    assert classifier.agent_user_id == 42
    assert classifier.handle == "claude"
    assert classifier.allowlist.allows(Actor(114395272, "8ohamed"))
