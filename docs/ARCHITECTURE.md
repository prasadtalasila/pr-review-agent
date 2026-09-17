# Architecture

What runs, what each part is responsible for, and how much of it exists
today. For *why* the shape is this one, see [DESIGN.md](DESIGN.md).

## 🧩 The seven components

A single Python asyncio daemon with a SQLite (WAL) store, running on a private
host.

| # | Component | Responsibility | State |
| :-- | :-- | :-- | :-- |
| 1 | **Poller** | Outbound-only conditional GETs against three repo-wide GitHub REST endpoints. | implemented — [POLLER.md](POLLER.md) |
| 2 | **Classifier + allowlist** | Turn a polled payload into an accepted trigger or a reason code. | implemented — [TRIGGERS.md](TRIGGERS.md) |
| 3 | **Store** | The watermarks and ETags that must survive a restart. | partial — [STORAGE.md](STORAGE.md) |
| 4 | **Queue and lease** | Atomic conditional claim (SQLite has no `SKIP LOCKED`), a per-PR lease so reviews of one pull request never overlap, and a `head_sha` re-check immediately before posting so a review of an older commit can never land after a newer one. | not started |
| 5 | **Budget governor** | Five layers of spending control over one ledger. | not started — [BUDGET.md](BUDGET.md) |
| 6 | **Engine adapter** | A `ReviewEngine` protocol with `claude_sdk`, `claude_cli` and `generic_cli` implementations. | not started |
| 7 | **Publisher** | One line-anchored review, event `COMMENT`, preceded by an immediate 👀 reaction. | not started |
| 8 | **Retention sweep** | Purge review content once a pull request merges; keep the ledger. | not started |

The ordering is deliberate rather than convenient: **the budget governor lands
before the review worker**, so the spending rails exist before anything can
spend.

## 🔁 The path an event takes

```text
GitHub REST ──► Poller ──► payload mapping ──► Classifier ──► Trigger
  (3 endpoints)  (ETag,      (raw dict to        (allowlist,     │
                  interval)   dataclass)          mention,       │
                                                  watermark)     ▼
                                                            Queue + lease
                                                                 │
                                                    reserve ◄────┤
                                                    (governor)   ▼
                                                            Review engine
                                                                 │
                                                    settle  ◄────┤
                                                                 ▼
                                                             Publisher
```

Everything left of the queue exists today. Everything right of it does not.

The reservation is taken inside the *same* transaction as the queue claim —
that is the invariant the whole storage choice rests on, and it is spelled out
in [BUDGET.md](BUDGET.md#-reserve-then-settle).

## 📦 Package layout

```text
src/pr_review_agent/
├── _compat.py         # the one Python 3.10 shim (enum.StrEnum)
├── config.py          # config.yaml → frozen dataclasses
├── store.py           # SQLite: watermarks and ETags
├── triggers/
│   ├── models.py      # payload-shaped dataclasses; PayloadError
│   ├── allowlist.py   # numeric-user-id membership
│   ├── mention.py     # @claude in *prose* only
│   └── classifier.py  # PullRequest | Comment → Decision
└── poller/
    ├── endpoints.py   # the three repo-wide request paths
    ├── client.py      # async conditional GET, rate-limit handling
    ├── etag_store.py  # the ETagCache protocol + in-memory cache
    ├── interval.py    # adaptive poll delay
    ├── payloads.py    # raw GitHub dicts → trigger models
    └── poller.py      # one sweep across all three endpoints
```

## ⬇ Layering

Dependencies point one way only:

- `config` builds a `Classifier`; it knows nothing about HTTP.
- `poller/payloads.py` imports `triggers/models.py`, never the reverse.
- Nothing in `triggers/` imports `poller/`.

That is what keeps the trigger suite free of HTTP: it is pure functions over
fixtures, needs no network and spends no tokens.

`poller/payloads.py` is the seam where every quirk of the GitHub REST shape is
resolved — the three that shaped it are documented in
[POLLER.md](POLLER.md#-mapping-a-payload-to-a-pull-request).

## 🐍 The Python 3.10 shim

Supporting Python 3.10 costs exactly one shim, in `_compat.py`: `enum.StrEnum`
arrived in 3.11. The replacement is *not* the obvious `class StrEnum(str, Enum)`
— on 3.10 that inherits `Enum.__str__`, so `str(member)` yields
`"Endpoint.OPEN_PULLS"` instead of `"open_pulls"`, and any interpolated log
line or persisted dict key would change meaning with the interpreter version.
`_compat.py` delegates `__str__` and `__format__` to `str` to restore the 3.11
behaviour, and `tests/test_compat.py` pins that parity.

Those assertions are the reason the CI matrix includes 3.10: it is the only job
where the shim is imported at all.

## ⏱ The daemon loop

The client and poller are `asyncio`-native. That is not speculative: the queue,
the per-PR leases and the budget governor are all built on top of the client,
and a synchronous `get` would block the whole loop for the duration of a
rate-limit backoff. Migrating later would have meant touching every poller test
through `MockTransport`, so it was done before anything was built on top.

The loop that calls `poll_once()` on a schedule, and the wiring that feeds
classified triggers into the queue, are the remaining gap between the poller
and the queue phase.
