# Architecture

What runs, what each part is responsible for, and how much of it exists
today. For *why* the shape is this one, see [DESIGN.md](DESIGN.md).

## 🧩 The components

A single Python asyncio daemon with a SQLite (WAL) store, running on a private
host.

| # | Component | Responsibility | State |
| :-- | :-- | :-- | :-- |
| 0 | **Daemon loop** | Poll on the adaptive interval, classify what changed, enqueue what is accepted, advance the watermarks. Stops at `enqueue`. | implemented — [DAEMON.md](DAEMON.md) |
| 1 | **Poller** | Outbound-only conditional GETs against three repo-wide GitHub REST endpoints. | implemented — [POLLER.md](POLLER.md) |
| 2 | **Classifier + allowlist** | Turn a polled payload into an accepted trigger or a reason code. | implemented — [TRIGGERS.md](TRIGGERS.md) |
| 3 | **Store** | The watermarks, ETags and queue rows that must survive a restart, and the schema migrations that get them there. | implemented — [STORAGE.md](STORAGE.md) |
| 4 | **Queue and lease** | Atomic conditional claim (SQLite has no `SKIP LOCKED`) and a per-PR lease so reviews of one pull request never overlap. The `head_sha` re-check before posting belongs to the publisher, which is where the live head can be read. | implemented — [QUEUE.md](QUEUE.md) |
| 5 | **Budget governor** | Layers 4 and 5 of spending control over one ledger; layers 2 and 3 land with the engine. | implemented — [BUDGET.md](BUDGET.md) |
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

Everything down to the queue exists today, and the [daemon
loop](DAEMON.md) is what drives it on a schedule. Everything below the queue
does not exist: the queue fills and nothing drains it, which is the intended
state until the governor lands.

The reservation is taken inside the *same* transaction as the queue claim —
that is the invariant the whole storage choice rests on, and it is spelled out
in [BUDGET.md](BUDGET.md#-reserve-then-settle). `budget.py` imports `queue.py`
and never the reverse: the seam is an optional `admit` predicate on `claim()`,
so the queue stays unaware of the governor.

## 📦 Package layout

```text
src/pr_review_agent/
├── _compat.py         # the one Python 3.10 shim (enum.StrEnum)
├── _startup.py        # token + config, shared by both entry points
├── bootstrap.py       # pre-flight egress checks for a new host
├── config.py          # config.yaml → frozen dataclasses
├── daemon.py          # the poll-classify-enqueue loop, and its entry point
├── queue.py           # claim protocol and per-pull-request leases
├── store.py           # SQLite: schema, watermarks, ETags, queue table
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
classified triggers into the queue, are in `daemon.py` — see
[DAEMON.md](DAEMON.md) for the ordering rules it has to keep and the
cold-start bound that stops a fresh database paying for the backlog.
