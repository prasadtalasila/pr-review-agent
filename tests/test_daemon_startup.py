"""What is decided before the first cycle, and what a SIGHUP may change.

The workers, the engine and whose budget arithmetic governs the pool are
settled at startup; a reload may move the limits and the kill switch and
must not move the repository or the role.
"""

import logging
from dataclasses import replace

import httpx
import pytest
from daemon_harness import (
    CONFIG,
    NOW,
    OTHER,
    config_yaml,
    daemon_with_config,
    make_daemon,
    with_budget,
)

from pr_review_agent._startup import StartupError
from pr_review_agent.config import WorkerConfig
from pr_review_agent.daemon import build_engine, build_workers, resolve_budget
from pr_review_agent.engine.claude import ClaudeCliEngine
from pr_review_agent.poller.client import GitHubClient
from pr_review_agent.poller.endpoints import RepoEndpoints
from pr_review_agent.store import BudgetPolicy, SqliteStore
from pr_review_agent.workspace import Workspace

# -- wiring: how many workers, and who they are --------------------------


def make_workers(tmp_path, count):
    daemon = make_daemon(tmp_path, lambda _request: httpx.Response(304))
    daemon.config = replace(CONFIG, worker=WorkerConfig(count=count))
    return build_workers(
        daemon,
        workspace=Workspace("o/r", tmp_path / "cache"),
        engine=build_engine(CONFIG),
        client=GitHubClient(token="t"),
        endpoints=RepoEndpoints("o", "r"),
    )


def test_one_worker_is_built_by_default(tmp_path):
    assert len(make_workers(tmp_path, 1)) == 1


def test_the_configured_number_of_workers_is_built(tmp_path):
    assert len(make_workers(tmp_path, 3)) == 3


def test_every_worker_owns_its_claims_distinctly(tmp_path):
    """The lease guard is on the owner, so two workers sharing one is a bug."""
    owners = [worker.owner for worker in make_workers(tmp_path, 4)]
    assert len(set(owners)) == 4


def test_workers_share_the_queue_and_the_governor(tmp_path):
    """One ledger, one queue: separate governors would each see their own."""
    workers = make_workers(tmp_path, 3)
    assert len({id(worker.governor) for worker in workers}) == 1
    assert len({id(worker.queue) for worker in workers}) == 1


def test_workers_share_one_publisher(tmp_path):
    """A second publisher would be a second thing for SIGHUP to find."""
    workers = make_workers(tmp_path, 3)
    assert len({id(worker.publisher) for worker in workers}) == 1


def test_every_worker_can_record_a_run(tmp_path):
    """One SQLite file, so a RunStore each is a view rather than a copy."""
    workers = make_workers(tmp_path, 3)
    assert all(worker.runs is not None for worker in workers)


# -- which engine the daemon runs ----------------------------------------


def test_the_configured_engine_is_what_the_workers_run():
    """The seam has a real caller now: this is what makes the agent spend."""
    engine = build_engine(CONFIG)
    assert isinstance(engine, ClaudeCliEngine)
    assert engine.model == "claude-sonnet-5"
    assert engine.expected_version == "2.1.274"
    assert engine.timeout_seconds == 900


def test_the_engine_carries_the_configured_binary_and_standards():
    config = replace(
        CONFIG,
        engine=replace(
            CONFIG.engine, binary="/opt/claude", standards_paths=("AGENTS.md",)
        ),
    )
    engine = build_engine(config)
    assert engine.binary == "/opt/claude"
    assert engine.standards_paths == ("AGENTS.md",)


def test_a_relative_engine_binary_warns(caplog):
    """A writable directory early on PATH defeats every argv control there is."""
    with caplog.at_level(logging.WARNING):
        build_engine(CONFIG)
    assert "resolves through PATH" in caplog.text


def test_an_absolute_engine_binary_does_not_warn(caplog, tmp_path):
    # tmp_path rather than a literal: "/opt/claude" is not an absolute path
    # on Windows, where the suite also runs.
    config = replace(
        CONFIG, engine=replace(CONFIG.engine, binary=str(tmp_path / "claude"))
    )
    with caplog.at_level(logging.WARNING):
        build_engine(config)
    assert "resolves through PATH" not in caplog.text


def test_no_fake_engine_reaches_a_running_daemon(tmp_path):
    """FakeEngine is a test double; a daemon running one would review nothing."""
    workers = make_workers(tmp_path, 2)
    assert all(isinstance(w.engine, ClaudeCliEngine) for w in workers)


def test_sighup_reloads_the_kill_switch(tmp_path):
    daemon, path = daemon_with_config(tmp_path, config_yaml(enabled="true"))
    assert daemon.governor.config.enabled is True

    path.write_text(config_yaml(enabled="false"), encoding="utf-8")
    daemon.reload_config()

    assert daemon.governor.config.enabled is False
    assert daemon.config.budget.enabled is False


def test_sighup_with_a_broken_file_keeps_the_previous_config(tmp_path, caplog):
    """A typo must not take the service down -- that is the brake, not a bomb."""
    daemon, path = daemon_with_config(tmp_path, config_yaml(enabled="true"))
    path.write_text("github: [unclosed\n", encoding="utf-8")

    daemon.reload_config()

    assert daemon.governor.config.enabled is True
    assert "keeping the previous configuration" in caplog.text


def test_sighup_reloads_the_quieter_brake(tmp_path):
    """`publish.dry_run` takes the mechanism `budget.enabled` built."""
    daemon, path = daemon_with_config(tmp_path, config_yaml(dry_run="false"))
    assert daemon.publisher.config.dry_run is False

    path.write_text(config_yaml(dry_run="true"), encoding="utf-8")
    daemon.reload_config()

    assert daemon.publisher.config.dry_run is True
    assert daemon.config.publish.dry_run is True


def test_sighup_with_a_broken_file_keeps_the_previous_dry_run(tmp_path):
    """A typo must not silently start posting what a dry run was hiding."""
    daemon, path = daemon_with_config(tmp_path, config_yaml(dry_run="true"))
    daemon.reload_config()
    assert daemon.publisher.config.dry_run is True

    path.write_text("github: [unclosed\n", encoding="utf-8")
    daemon.reload_config()

    assert daemon.publisher.config.dry_run is True


def test_sighup_does_not_swap_a_changed_repository(tmp_path, caplog):
    """Only budget is hot-swapped: the watermarks describe the old repo."""
    daemon, path = daemon_with_config(tmp_path, config_yaml())
    path.write_text(config_yaml(repo="other/repo"), encoding="utf-8")

    daemon.reload_config()

    assert daemon.config.github.repo == "o/r"
    assert "need a restart" in caplog.text


def test_an_authority_publishes_the_pool_arithmetic(tmp_path):
    with SqliteStore(tmp_path / "state.db") as store:
        comply = resolve_budget(store, with_budget(authority=True), now=NOW)
        published = store.budget_policy()

    assert comply is False
    assert published is not None
    assert published.authority_repo == "o/r"
    assert published.fields == CONFIG.budget.shared()


def test_a_lone_daemon_publishes_without_being_told_to(tmp_path):
    """Both keys default true, so a single deployment needs neither."""
    with SqliteStore(tmp_path / "state.db") as store:
        assert resolve_budget(store, CONFIG, now=NOW) is False
        assert store.budget_policy() is not None


def test_a_complier_refuses_to_start_before_any_authority(tmp_path):
    # Retryable by design: the unit restarts on failure, so a complier
    # started before its authority waits rather than needing a start order.
    with (
        SqliteStore(tmp_path / "state.db") as store,
        pytest.raises(StartupError, match="no authority has published"),
    ):
        resolve_budget(store, with_budget(authority=False), now=NOW)


def test_a_complier_starts_once_the_authority_has_published(tmp_path):
    with SqliteStore(tmp_path / "state.db") as store:
        resolve_budget(store, OTHER, now=NOW)

        assert resolve_budget(store, with_budget(authority=False), now=NOW) is True


def test_a_second_authority_refuses_to_start(tmp_path):
    """Two authorities are two opinions about one allowance."""
    with SqliteStore(tmp_path / "state.db") as store:
        resolve_budget(store, with_budget(OTHER, authority=True), now=NOW)

        with pytest.raises(StartupError, match="already the budget authority"):
            resolve_budget(store, with_budget(authority=True), now=NOW)


def test_an_authority_republishes_on_restart(tmp_path):
    """Its file is the declared truth; the row is only ever a copy of it."""
    with SqliteStore(tmp_path / "state.db") as store:
        resolve_budget(store, with_budget(authority=True), now=NOW)

        resolve_budget(
            store, with_budget(authority=True, weekly_tokens=3_000_000), now=NOW
        )

        published = store.budget_policy()
        assert published is not None
        assert published.fields["weekly_tokens"] == 3_000_000


def test_declining_to_comply_beside_an_authority_warns(tmp_path, caplog):
    """The one remaining way to overspend a shared pool, so it is said loudly."""
    with SqliteStore(tmp_path / "state.db") as store:
        resolve_budget(store, with_budget(OTHER, authority=True), now=NOW)

        declined = with_budget(authority=False, comply=False)
        assert resolve_budget(store, declined, now=NOW) is False

    assert "budget.comply is false" in caplog.text


def test_sighup_republishes_an_authoritys_policy(tmp_path):
    """An authority's reload is how a shared limit changes for everyone."""
    daemon, path = daemon_with_config(tmp_path, config_yaml(authority="true"))
    path.write_text(config_yaml(authority="true", weekly="3000000"), encoding="utf-8")

    daemon.reload_config()

    published = daemon.store.budget_policy()
    assert published is not None
    assert published.fields["weekly_tokens"] == 3_000_000


def test_sighup_does_not_claim_a_compliers_limits_changed(tmp_path, caplog):
    """A complier's own token counts are inert, so the log must not imply
    a reload applied them."""
    daemon, path = daemon_with_config(tmp_path, config_yaml(authority="false"))
    daemon.config = with_budget(daemon.config, authority=False, comply=True)
    path.write_text(config_yaml(authority="false", weekly="3000000"), encoding="utf-8")

    with caplog.at_level(logging.INFO):
        daemon.reload_config()

    assert "the limits in force remain the authority's" in caplog.text


def test_sighup_does_not_promote_a_complier_to_authority(tmp_path, caplog):
    """`resolve_budget`'s two-authorities refusal runs at startup only.

    A complier that republished here would overwrite a live authority's
    policy with its own numbers, which is the silent disagreement about one
    allowance the whole authority model exists to prevent.
    """
    daemon, path = daemon_with_config(tmp_path, config_yaml(authority="false"))
    daemon.config = with_budget(daemon.config, authority=False, comply=True)
    daemon.store.publish_budget_policy(
        BudgetPolicy("other/repo", daemon.config.budget.shared()), now=NOW
    )

    path.write_text(config_yaml(authority="true"), encoding="utf-8")
    daemon.reload_config()

    assert daemon.config.budget.authority is False
    published = daemon.store.budget_policy()
    assert published is not None
    assert published.authority_repo == "other/repo"
    assert "need a restart" in caplog.text


def test_sighup_does_not_demote_an_authority(tmp_path, caplog):
    """The mirror: the governor's compliance is fixed at construction, so an
    authority demoted here would keep governing with its own file while the
    log claimed the authority's limits were in force."""
    daemon, path = daemon_with_config(tmp_path, config_yaml(authority="true"))
    assert daemon.config.budget.authority is True

    path.write_text(config_yaml(authority="false"), encoding="utf-8")
    daemon.reload_config()

    assert daemon.config.budget.authority is True
    assert "need a restart" in caplog.text


def test_sighup_still_reloads_the_limits_when_the_role_is_pinned(tmp_path):
    """Pinning the role must not cost the reload its actual purpose."""
    daemon, path = daemon_with_config(
        tmp_path, config_yaml(authority="true", weekly="1500000")
    )

    path.write_text(config_yaml(authority="false", weekly="1200000"), encoding="utf-8")
    daemon.reload_config()

    assert daemon.config.budget.authority is True
    assert daemon.governor.config.weekly_tokens == 1_200_000
