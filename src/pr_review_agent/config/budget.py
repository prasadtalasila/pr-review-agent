"""The ``budget`` section: every key that decides what may be spent.

Separate from the arithmetic in :mod:`pr_review_agent.budget`, which reads
what an operator declared here and turns it into windows and a ladder.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import timedelta
from decimal import Decimal

from ._sections import ConfigError
from .excluded_paths import DEFAULT_EXCLUDED_PATHS

#: The rolling windows every limit in this section is measured over, and the
#: one duration the per-pull-request cap is counted over. They live in the
#: config section rather than beside the arithmetic in
#: :mod:`pr_review_agent.budget` because two modules now measure against
#: them -- the governor's windows and the pacer's daily cap -- and the
#: governor imports the pacer, so a constant owned by either one would have
#: to be copied into the other to reach it. Copied is how they drift.
#:
#: ``DAILY`` is a flat seventh of the weekly limit rather than
#: ``weekly_remaining / days_left``: a rolling window never resets, so "days
#: remaining" has no value, and anchoring one would mean inventing a reset
#: day the plan does not publish. "At most a seventh of the week in any day"
#: needs no anchor and is stricter -- an agent idle since Monday cannot burn
#: four days' allowance on Friday.
SESSION = timedelta(hours=5)
WEEKLY = timedelta(days=7)
DAILY = timedelta(days=1)

#: BUDGET.md's human-headroom default: the agent may use this percentage of
#: each plan window, never the whole allowance.
DEFAULT_REVIEWER_SHARE_PCT = 40

#: BUDGET.md layer 2's diff-size caps. Unlike the plan token counts these do
#: have defaults: a plan's allowance is unpublished, so any default would be
#: a fabricated ceiling, whereas a diff-size cap is an ordinary engineering
#: choice. `tests/test_config.py` pins both by value.
DEFAULT_MAX_CHANGED_FILES = 100
DEFAULT_MAX_CHANGED_LINES = 5000

#: How long one pull request waits between reviews, and how long it waits
#: when a human asked directly. A contributor pushing fixups produces a
#: trigger per push once a maintainer is mentioning the agent alongside
#: them, and every one of those reviews is paid for in full -- so the
#: default is on rather than off. A mention waits less because it is a
#: person asking rather than a repository event, and silence is a worse
#: answer to a person.
#:
#: Zero disables either one. That is spelled rather than implied: an
#: operator who wants the previous behaviour should not have to discover
#: that some large number approximates it.
DEFAULT_MIN_REVIEW_INTERVAL_SECONDS = 900
DEFAULT_MENTION_MIN_REVIEW_INTERVAL_SECONDS = 300

#: A token count written with a thousand or million suffix: ``88k``,
#: ``1.5m``. Only those two, because a count this file holds is between
#: tens of thousands and a few million, and a suffix nobody expects is a
#: misread ceiling.
_SUFFIXED = re.compile(r"(\d+(?:\.\d+)?)([km])", re.IGNORECASE)
_MULTIPLIER = {"k": 1_000, "m": 1_000_000}


def _tokens(data: dict, key: str) -> int:
    """Read a required positive token count from the ``budget`` section.

    An integer (``88000``, or ``88_000``, which YAML already reads as one),
    or a string with a ``k``/``m`` suffix (``88k``, ``1.5m``). A suffixed
    value must come out whole: ``1.2345k`` is refused rather than rounded,
    because rounding a spending ceiling is choosing one.

    ``bool`` is excluded explicitly because it is a subclass of ``int``, so
    ``budget.weekly_tokens: true`` would otherwise validate as ``1`` -- a
    spending ceiling of one token, arrived at silently.
    """
    value = data.get(key)
    if isinstance(value, str) and (match := _SUFFIXED.fullmatch(value.strip())):
        number, suffix = match.groups()
        scaled = Decimal(number) * _MULTIPLIER[suffix.lower()]
        value = int(scaled) if scaled == scaled.to_integral_value() else None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(
            f"budget.{key} must be a positive number of tokens, "
            "such as 88000, 88_000 or 88k"
        )
    return value


def _excluded_paths(data: dict) -> tuple[str, ...]:
    """Read the operator's own excluded path patterns, added to the built-ins.

    A pattern may not begin with ``:``. ``exclusions.py`` builds a
    ``:(exclude,glob)`` prefix in front of each one, and a pattern free to
    open magic of its own -- ``:(attr:...)``, or a bare ``:`` re-anchoring
    the path -- is not something an operator can predict from reading their
    own configuration file. It cannot reach outside the argument it sits in;
    it can make that argument mean something else.
    """
    value = data.get("excluded_paths")
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError("budget.excluded_paths must be a list of path patterns")
    for pattern in value:
        if not isinstance(pattern, str) or not pattern.strip():
            raise ConfigError(
                f"budget.excluded_paths entries must be non-empty patterns, "
                f"got {pattern!r}"
            )
        if pattern.startswith(":"):
            raise ConfigError(
                f"budget.excluded_paths entry {pattern!r} may not begin with ':': "
                "the pathspec magic is supplied by the agent"
            )
    return tuple(value)


def _flag(data: dict, key: str) -> bool:
    """Read an optional switch that defaults to on."""
    value = data.get(key, True)
    if not isinstance(value, bool):
        raise ConfigError(f"budget.{key} must be true or false")
    return value


def _cap(data: dict, key: str, default: int) -> int:
    """Read an optional positive diff-size cap from the ``budget`` section."""
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"budget.{key} must be a positive integer")
    return value


def _interval(data: dict, key: str, default: int) -> int:
    """Read an optional pacing interval, in seconds. Zero disables it."""
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError(
            f"budget.{key} must be a whole number of seconds, or 0 to disable"
        )
    return value


def _commits(data: dict, key: str) -> int:
    """Read an optional commit-count threshold. Zero disables it."""
    value = data.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError(f"budget.{key} must be a whole number of commits, or 0")
    return value


def _reviews_cap(data: dict) -> int | None:
    """Read the optional per-pull-request daily review cap."""
    value = data.get("max_reviews_per_pull_request")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(
            "budget.max_reviews_per_pull_request must be a positive integer, "
            "or absent for no cap"
        )
    return value


#: What an authority publishes and a complier adopts: the pool arithmetic, and
#: only that. Every one of these decides how much of the *shared* allowance is
#: available, so two daemons disagreeing about one of them disagree about the
#: same pool.
#:
#: Everything else in ``budget`` stays local, for two different reasons.
#: ``enabled`` is the emergency brake, and a brake that could only be pulled
#: fleet-wide could not stop one misbehaving repository. The diff-size caps and
#: the exclusion keys describe a *repository* -- what is worth reading in it --
#: rather than the pool, and a vendored tree in one repository says nothing
#: about another.
SHARED_FIELDS = (
    "session_tokens",
    "weekly_tokens",
    "max_run_tokens",
    "reviewer_share_pct",
    "per_contributor_pct",
)


@dataclass(frozen=True)
class BudgetConfig:
    """The spending rails: what the plan is assumed to allow, and our share.

    ``session_tokens`` and ``weekly_tokens`` are the operator's estimate of
    the *plan's* limits, because a subscription publishes no quota. The
    agent's own ceiling is that times ``reviewer_share_pct``, which is what
    keeps a runaway agent from locking a maintainer out of interactive Claude
    Code.

    ``enabled: false`` **stops reviewing**; it does not stop checking. The
    two readings of a "kill switch" differ by catastrophe -- one is an
    emergency brake, the other is unbounded spend -- so the governor refuses
    every claim while it is false.

    ``authority`` and ``comply`` decide *whose* numbers govern when several
    daemons share one store, and so one pool. Exactly one configuration sets
    ``authority: true`` and publishes the fields in :data:`SHARED_FIELDS`;
    the rest adopt them. Without this each process would police the shared
    pool using its own file, and two files that disagree do not split the
    budget -- they hand it to whichever is most permissive.
    """

    # A configuration section is a flat list of keys. Splitting it to satisfy
    # the attribute count would scatter the spending rails across two types,
    # which is exactly what keeping them in one section is for.
    # pylint: disable=too-many-instance-attributes

    session_tokens: int
    weekly_tokens: int
    max_run_tokens: int
    enabled: bool = True
    #: Publishes :data:`SHARED_FIELDS` to the store for the others to adopt.
    #: Defaults true, which is what makes a lone daemon govern its own store
    #: with no extra key. A fleet sets it false on all but one, and getting
    #: that wrong is loud rather than silent: a second authority on the same
    #: store refuses to start.
    authority: bool = True
    #: Adopts what the authority published. Consulted only when ``authority``
    #: is false, so an authority needs no second key to say it governs itself.
    comply: bool = True
    reviewer_share_pct: int = DEFAULT_REVIEWER_SHARE_PCT
    max_changed_files: int = DEFAULT_MAX_CHANGED_FILES
    max_changed_lines: int = DEFAULT_MAX_CHANGED_LINES
    #: The built-in list in :mod:`.excluded_paths` -- lockfiles, vendored
    #: trees, generated code, minified bundles -- is in force unless this is
    #: false. Off is for the repository that genuinely reviews its lockfiles.
    default_exclusions: bool = True
    #: The operator's own patterns, *added* to the built-ins rather than
    #: replacing them, so a config file names only what is special about its
    #: repository. :attr:`effective_excluded_paths` is what the workspace is
    #: handed.
    excluded_paths: tuple[str, ...] = ()
    #: Optional, and off by default: on a one-person allowlist any cap below
    #: 100 % would block the only account that can trigger anything.
    per_contributor_pct: int | None = None
    #: The pacer, below the windows: how long one pull request waits between
    #: reviews. It bounds a *rate* where the windows bound a *total*, which
    #: is why neither replaces the other -- the windows cannot tell one pull
    #: request consuming the day from thirty sharing it.
    min_review_interval_seconds: int = DEFAULT_MIN_REVIEW_INTERVAL_SECONDS
    mention_min_review_interval_seconds: int = (
        DEFAULT_MENTION_MIN_REVIEW_INTERVAL_SECONDS
    )
    #: Optional, and off by default, for the reason ``per_contributor_pct``
    #: is: it is the fairness knob for pull requests rather than a spending
    #: ceiling, and a repository with one active pull request at a time
    #: gains nothing from it. Counted over a trailing 24 hours, the same
    #: duration as the daily window.
    max_reviews_per_pull_request: int | None = None
    #: Incremental review (roadmap C2) is on whenever a previous completed
    #: round exists; these only make a round full *below* a threshold --
    #: fewer new commits than this, or a previous round more recent than
    #: this many seconds. Zero disables each, and is the default. Note that
    #: the pacer already defers a trigger sooner than
    #: ``min_review_interval_seconds``, so a seconds threshold at or below
    #: that interval never fires. See docs/BUDGET.md.
    incremental_min_commits: int = 0
    incremental_min_seconds: int = 0

    @property
    def effective_excluded_paths(self) -> tuple[str, ...]:
        """The one list the size gate and the engine's diff are both cut by.

        Composed here rather than at the call site so that a reload, an
        adopted policy and a dry-run summary cannot each combine the two
        keys differently.
        """
        if self.default_exclusions:
            return DEFAULT_EXCLUDED_PATHS + self.excluded_paths
        return self.excluded_paths

    @property
    def session_limit(self) -> int:
        """The agent's share of the plan's rolling five-hour window."""
        return self._share(self.session_tokens)

    @property
    def weekly_limit(self) -> int:
        """The agent's share of the plan's rolling weekly window."""
        return self._share(self.weekly_tokens)

    @property
    def daily_limit(self) -> int:
        """A flat seventh of the weekly limit, over a trailing 24 hours.

        A rolling weekly window never resets, so the ``weekly_remaining /
        days_remaining`` pacing BUDGET.md describes has no divisor to use.
        A seventh needs no week anchor and is stricter: an agent idle since
        Monday cannot burn four days' allowance on Friday.
        """
        return self._share(self.weekly_tokens) // 7

    @property
    def per_contributor_limit(self) -> int | None:
        """One contributor's share of the agent's weekly allowance.

        ``None`` when the key is unset, which is how "no per-contributor
        cap" is spelled: the window is simply not measured.
        """
        if self.per_contributor_pct is None:
            return None
        return self.weekly_limit * self.per_contributor_pct // 100

    def review_interval(self, *, mention: bool) -> timedelta:
        """How long this pull request waits before it may be reviewed again.

        A mention has its own, shorter interval rather than an exemption. An
        exemption would put the whole control behind one word a contributor
        can type, which is the shape of a limit that does not limit.
        """
        seconds = (
            self.mention_min_review_interval_seconds
            if mention
            else self.min_review_interval_seconds
        )
        return timedelta(seconds=seconds)

    def shared(self) -> dict[str, int | None]:
        """The pool arithmetic, as an authority publishes it."""
        return {name: getattr(self, name) for name in SHARED_FIELDS}

    def adopt(self, shared: dict[str, int | None]) -> BudgetConfig:
        """This file's local settings under the authority's pool arithmetic.

        Needs no re-validation. Both cross-field invariants ``parse`` enforces
        -- a run fitting inside the daily allowance, and inside one
        contributor's -- are arithmetic over :data:`SHARED_FIELDS` alone, so
        they were already checked against these values in the authority's own
        file before it published them.
        """
        return replace(self, **shared)

    def _share(self, plan_tokens: int) -> int:
        return plan_tokens * self.reviewer_share_pct // 100

    @classmethod
    def parse(cls, data: dict) -> BudgetConfig:
        """Validate the ``budget`` section."""
        share = data.get("reviewer_share_pct", DEFAULT_REVIEWER_SHARE_PCT)
        if isinstance(share, bool) or not isinstance(share, int):
            raise ConfigError("budget.reviewer_share_pct must be a whole percentage")
        if not 1 <= share <= 100:
            # Zero would make every limit zero and utilisation an undefined
            # 0/0; an operator who wants the agent stopped has `enabled`.
            raise ConfigError("budget.reviewer_share_pct must be between 1 and 100")
        per_contributor = data.get("per_contributor_pct")
        if per_contributor is not None and (
            isinstance(per_contributor, bool)
            or not isinstance(per_contributor, int)
            or not 1 <= per_contributor <= 100
        ):
            raise ConfigError(
                "budget.per_contributor_pct must be a whole percentage "
                "between 1 and 100, or absent for no cap"
            )
        config = cls(
            session_tokens=_tokens(data, "session_tokens"),
            weekly_tokens=_tokens(data, "weekly_tokens"),
            max_run_tokens=_tokens(data, "max_run_tokens"),
            enabled=_flag(data, "enabled"),
            authority=_flag(data, "authority"),
            comply=_flag(data, "comply"),
            reviewer_share_pct=share,
            max_changed_files=_cap(
                data, "max_changed_files", DEFAULT_MAX_CHANGED_FILES
            ),
            max_changed_lines=_cap(
                data, "max_changed_lines", DEFAULT_MAX_CHANGED_LINES
            ),
            per_contributor_pct=per_contributor,
            default_exclusions=_flag(data, "default_exclusions"),
            excluded_paths=_excluded_paths(data),
            min_review_interval_seconds=_interval(
                data, "min_review_interval_seconds", DEFAULT_MIN_REVIEW_INTERVAL_SECONDS
            ),
            mention_min_review_interval_seconds=_interval(
                data,
                "mention_min_review_interval_seconds",
                DEFAULT_MENTION_MIN_REVIEW_INTERVAL_SECONDS,
            ),
            max_reviews_per_pull_request=_reviews_cap(data),
            incremental_min_commits=_commits(data, "incremental_min_commits"),
            incremental_min_seconds=_interval(data, "incremental_min_seconds", 0),
        )
        if (
            config.mention_min_review_interval_seconds
            > config.min_review_interval_seconds
        ):
            # Not arithmetic that breaks anything -- it simply means the
            # repository's own events are paced more loosely than the people
            # asking about them, which is the opposite of what the two keys
            # are for and is far likelier to be a transposition.
            raise ConfigError(
                "budget.mention_min_review_interval_seconds "
                f"({config.mention_min_review_interval_seconds}) exceeds "
                f"budget.min_review_interval_seconds "
                f"({config.min_review_interval_seconds}): a mention would "
                "wait longer than an ordinary trigger"
            )
        if (
            config.per_contributor_limit is not None
            and config.per_contributor_limit < config.max_run_tokens
        ):
            # The same trap as the daily check below, one window further: a
            # cap this low admits nobody, including the only contributor on
            # a one-person allowlist.
            raise ConfigError(
                f"budget.max_run_tokens ({config.max_run_tokens}) exceeds one "
                f"contributor's allowance ({config.per_contributor_limit}); "
                "no run could ever be admitted"
            )
        if config.daily_limit < config.max_run_tokens:
            # The daily window is the tightest of the three, so a run that
            # cannot fit inside it can never be admitted at all -- a config
            # that reviews nothing, arrived at by arithmetic nobody did.
            raise ConfigError(
                f"budget.max_run_tokens ({config.max_run_tokens}) exceeds the daily "
                f"allowance ({config.daily_limit}); no run could ever be admitted"
            )
        return config
