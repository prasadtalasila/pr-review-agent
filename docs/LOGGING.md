# Logging design

**The whole design is built.** This page records it and the reasoning behind
each choice; the table below is what landed where.

| Part | State |
| :-- | :-- |
| The level — `--log-level`, `PR_REVIEW_AGENT_LOG_LEVEL`, `logging.level` | **built** (`logs.py`), see [Configuration](#-configuration) |
| The level applied to `pr_review_agent` and never to root, with a floor per third-party logger | **built**, pinned by `tests/test_logs.py` |
| All six events, each at its agreed level | **built**, see [the table](#-global-level-not-per-logger) |
| The two call-site demotions | **built** |
| `logging.format` — `auto`, `text`, `json` | **built** (`logs.py`), see [Configuration](#-configuration) |
| The JSON record, with the six events' contextual values as fields | **built**, see [the record](#-the-json-record) |
| The `<N>` journald prefix and `_on_journal` | **built**, pinned by `tests/test_logs.py` |

Tracked by [#51](https://github.com/prasadtalasila/pr-review-agent/issues/51)
(the umbrella), [#52](https://github.com/prasadtalasila/pr-review-agent/issues/52)
(level, **done**) and [#53](https://github.com/prasadtalasila/pr-review-agent/issues/53)
(destination and format, **done**). The separate issue for the two missing
log records is **done** too: both are emitted, as events 3 and 6 below.

## 🎯 What the operator asked for

Six things visible while the daemon runs, and nothing else. All six are now
emitted; the level each carries is [below](#-global-level-not-per-logger):

- the poll cycle querying for pull requests;
- the pull request number, and why it was or was not taken;
- the start of a review;
- the review's outcome;
- the review being posted;
- the remaining token budget.

## 🧭 The shape of the decision

**One stream, one format, and the supervisor owns the destination.** The
daemon writes to stderr. It does not open files, it does not hold a list of
sinks, and `config.yaml` names no destinations at all. Duplicating the stream
to a second place is systemd's job, or rsyslog's, or a log collector's.

This is the same conclusion the most directly comparable daemon reached.
`dockerd` is a long-lived systemd service with a JSON config file, and
`/etc/docker/daemon.json` carries exactly two logging keys for the daemon's
own output — `log-level` and `log-format`. There is no option, flag or key
anywhere for `dockerd` to write its own log to a file; it writes to stderr
and lets journald own the destination. (`log-driver` and `log-opts` in the
same file configure *container* logs, which is a different concern that
happens to live in the same file.) `containerd` is the same: `[debug] level`
and `format`, no path.

The alternative — a list of endpoints in `config.yaml` — was considered and
rejected. Its decisive problem is that partial failure has no good answer.
If the config names three sinks and one path is unwritable, refusing to start
means a typo in a log path stops pull request reviews, and starting anyway
means the operator believes they have a log they do not have. Every
multi-sink design picks one of those and both are wrong. A single stderr
stream cannot be misconfigured into partial existence.

The rest of the case against it, briefly: runtime failures (disk full,
permissions, rotation racing the handler) become the daemon's rather than the
platform's; sinks invite per-sink levels, which is strictly more machinery
than the per-logger map that was also rejected; `SIGHUP` reload would have to
reopen file handles mid-review, and logging is exactly the section an operator
expects to be reloadable; two copies with different retention give the spend
question two answers that diverge after a rotation; and every extra sink is
another place the attacker-influenced text of **F4** lands, at its own
permissions. rsyslog and Vector already do fan-out with buffering, retry and
backpressure. An in-process sink list is a worse version of software that is
already installed.

## ⚙️ Configuration

Two scalars, and no destinations:

```yaml
logging:
  level: INFO      # DEBUG | INFO | WARNING | ERROR | CRITICAL
  format: auto     # auto | text | json
```

Precedence is **flag > environment > config file**, per clig.dev's
configuration order, and the same three layers for each of the two:

```bash
pr-review-agent daemon start --log-level DEBUG --log-format json   # highest
PR_REVIEW_AGENT_LOG_LEVEL=DEBUG                     # what a systemd unit uses
PR_REVIEW_AGENT_LOG_FORMAT=json
# then config.yaml, then the INFO and auto defaults
```

The environment layer is the one that matters for deployment: `GITHUB_TOKEN`
already arrives that way, so every unit already has an `Environment=` block,
and the level lands beside it without touching `ExecStart`.

`auto` resolves to text when stderr is a terminal and JSON otherwise.

`dockerd` resolves the same conflict differently — it refuses to start if an
option is set both by flag and in `daemon.json`, *"regardless of their
value"*. That works for two layers and would not work here, because the
environment layer exists precisely so a unit can override the file.

### The level is applied to `pr_review_agent`, never to root

`basicConfig` configured the **root** logger, so a naive `--log-level DEBUG`
would have switched on `httpx` and `httpcore` — and `httpcore` prints
fourteen records per request at DEBUG, the response header list among them.
The level therefore moves the `pr_review_agent` logger, and three
third-party loggers follow it only as far down as each one stays quiet:

| `level` | `pr_review_agent` | `httpx` | `httpcore` | `asyncio` |
| :-- | :-- | :-- | :-- | :-- |
| `DEBUG` | `DEBUG` | `WARNING` | `INFO` | `DEBUG` |
| `INFO` | `INFO` | `WARNING` | `INFO` | `INFO` |
| `WARNING` | `WARNING` | `WARNING` | `WARNING` | `WARNING` |
| `ERROR` | `ERROR` | `ERROR` | `ERROR` | `ERROR` |
| `CRITICAL` | `CRITICAL` | `CRITICAL` | `CRITICAL` | `CRITICAL` |

A floor each rather than one shared pin, and the floors differ because the
libraries do. The rule is **the lowest level at which that library is
quiet**, measured against the pinned versions with one request each:

| Library | What it emits | Floor |
| :-- | :-- | :-- |
| `httpx` | Nothing at DEBUG; one line per request at INFO, and the poller makes several every cycle for as long as the daemon runs | WARNING |
| `httpcore` | Nothing at INFO; fourteen records per request at DEBUG including the response header list, and it is the half of the pair the credential travels through | INFO |
| `asyncio` | One record for the whole process, nothing per request, no credential near it | DEBUG — it follows |

Two consequences worth stating. At the default INFO **all three are
silent**, which is the noise this design replaces: the root-logger
`basicConfig` put `httpx`'s per-request line into the log on every poll. And
`--log-level ERROR` silences their warnings too, which a shared pin at
WARNING would not have done — a pin and a floor only agree below WARNING.

What a transport prints about itself is a library-version detail rather than
a contract, which is the argument for holding `httpcore` above DEBUG
independently of what any given release happens to log there. With a single
global knob that matters: `LOG_LEVEL=DEBUG` is the first thing an operator
reaches for during an incident. Per **CLAUDE.md** §5 the whole table gets a
test, not just attention — `tests/test_logs.py`.

## 📊 Global level, not per-logger

An earlier draft of this design argued that no global level could produce the
six events, and that a per-logger map was therefore necessary. That rested on
an unstated assumption — that the level of each call site is fixed. It is
not. Selection lives in the levels assigned *at the call sites*, and then a
global INFO is exactly the operator's view.

The six events carry the levels below. The split is **per poll cycle**
against **per review**: events 1 and 2 fire for every pull request and every
comment on every cycle, so they sit at DEBUG where an operator turns them on
to ask *why wasn't this reviewed*. Events 3 to 6 fire once per review, so
all four are visible at the default INFO: an operator watching a healthy
daemon sees each review start, end, get posted, and what it left in the
budget, and nothing else.

| # | Event | Record | Where | Level |
| :-- | :-- | :-- | :-- | :-- |
| 1 | Poll cycle | `cycle seen=N enqueued=N` | `daemon.py` | **DEBUG** |
| 2 | The PR, and why it was or was not taken | `trigger decision kind=… repo=… pr=… reason=…` | `triggers/classifier.py` | **DEBUG** |
| 3 | Start of a review | `reviewing repo#N as … (mode=…)` | `worker.py` | **INFO** |
| 4 | The review's outcome | `reviewed …: …, N findings, N tokens` | `worker.py` | **INFO** |
| 5 | The review being posted | `published … as comment N` | `publisher.py` | **INFO** |
| 6 | Remaining token budget | `budget after …: N tokens left in the … window, mode=…` | `worker.py` | **INFO** |

`tests/test_logs.py` pins all six by reading the levels out of the four
modules, so the table above and the code cannot drift apart.

Everything else, with one rule added:

| Level | Records |
| :-- | :-- |
| **ERROR** | **every caught exception**: the engine failing to start, a review failing, a publish or acknowledgement failing, a permanently abandoned trigger, the account's usage limit, a worktree that would not tear down, a broken `SIGHUP` reload, a failed poll cycle, a crashed worker |
| **WARNING** | states the daemon reasoned its way into rather than caught: a lapsed lease, a reservation with nothing to settle, every budget refusal, a `SIGHUP` naming a section that needs a restart — plus the startup banner, which is loud because it is the line where the agent starts costing money |
| **INFO** | startup (state database, workspace cache, worker count); cold-start watermark; `SIGHUP` reload; resumed and superseded runs; publisher's stale-head discard; the one-line `publish.dry_run` summary |
| **DEBUG** | `engine/cli.py` argv and prompt digest *(demoted)*; `publisher.py` dry-run review body *(demoted, keeping the one-line INFO summary)* |

The ERROR row is a rule rather than a list, and
`tests/test_logs.py::test_a_caught_exception_is_never_logged_below_error`
enforces it over every module: a `logger.debug`, `logger.info` or
`logger.warning` lexically inside an `except` block fails the suite. A
handler that reports its exception at WARNING is invisible to
`journalctl -p err` and to anything alerting on severity, which is exactly
the audience for *the engine would not start*.

There is one exemption, and it carries its reason in the test: `standards.py`
asks git for an optional file at the merge base and reads `GitCommandError`
as *not there*. That exception is control flow, and a configured path that
does not exist is documented as skipped, so it stays at DEBUG.

Besides the six, two records were demoted. They are the only INFO records
that did not belong in an operator's view: the engine adapter's argv and
prompt digest fires once per review and is also the **F4** log-hygiene
surface, and the dry-run branch dumped an entire rendered review body into
the log on every review. The dry-run branch keeps a one-line INFO summary,
because an operator does need to know the brake is on.

This is the vertical cut #51 asks for, expressed in code rather than in
configuration — which is where the level policy already lives in this
codebase. `triggers/classifier.py` is the clearest case: it used to split its
own decisions between INFO and DEBUG by reason, and now puts the whole record
at DEBUG, because a decision fires for every pull request and every comment
on every cycle. [Triggers](TRIGGERS.md) documents the reason codes; the level
is no longer per-reason.

The cost is real and is accepted: if a component becomes noisy, muting it
needs a code change and a restart. That has happened once — the v0.12.0 run
produced roughly a hundred INFO decisions per cycle — and it was fixed in
code, by the `pr_not_open` filter, not by a level knob. Per **CLAUDE.md** §2,
per-logger configuration is configurability nobody has yet needed.

What recovers the per-component view is the JSON `logger` field, which allows
the slice to be taken at query time instead:

```bash
jq 'select(.logger == "pr_review_agent.budget")' agent.jsonl
```

## 🧱 The JSON record

Modelled on `dockerd`'s own JSON output, which is `time`, `level`, `msg` and
a discriminator field:

```json
{"time":"2026-09-22T07:31:02.114938+00:00","level":"info","logger":"pr_review_agent.worker","msg":"reviewing prasadtalasila/pr-review-agent#123","repo":"prasadtalasila/pr-review-agent","pr":123}
{"time":"2026-09-22T07:34:41.802115+00:00","level":"info","logger":"pr_review_agent.worker","msg":"reviewed …","repo":"…","pr":123,"findings":4,"tokens":18211,"remaining":141789,"tightest":"weekly"}
{"time":"2026-09-22T07:34:43.011907+00:00","level":"warning","logger":"pr_review_agent.budget","msg":"budget at 80% of the weekly window: auto-review paused","reason":"mention_only"}
```

`logger` is this project's equivalent of `dockerd`'s `component`.

The contextual keys — `repo`, `pr`, `reason`, `findings`, `tokens`,
`remaining`, `tightest` — are passed as `extra=` at roughly five call sites,
which are the six events. They are *content*, not routing markers: every one
of them is a value already interpolated into the message text, and the
classifier's `kind`, `repo`, `pr` and `reason` are real fields rather than an
interpolated string. No `event` tag is added to any call site, because
`logger` already carries the component.

## 🔍 How the daemon knows journald is listening

When systemd starts a unit with `StandardOutput=journal`, the service manager
does not hand the child a pipe or a file. It asks journald for a stream, and
journald returns an `AF_UNIX` socket connection, which systemd installs as the
child's file descriptor 1 — and as file descriptor 2 as well, because
`StandardError=` defaults to `inherit`, which duplicates the stdout
descriptor. It then sets in the child's environment:

```
JOURNAL_STREAM=8:15803
```

— the decimal device and inode numbers of that exact socket. The documented
use is to compare them against the descriptor:

```bash
sudo systemd-run --unit=js-demo --wait /usr/bin/python3 -c \
  'import os; s = os.fstat(2); print(f"env={os.environ.get(\"JOURNAL_STREAM\")} fd2={s.st_dev}:{s.st_ino}")'
journalctl -u js-demo -o cat
# env=8:15803 fd2=8:15803
```

Outside systemd the variable is absent and the comparison is never reached.

### Why the comparison, and not the variable's presence

The environment is inherited by children. The engine adapter spawns `claude`
and the workspace spawns `git`, both with a pipe for stderr, and both
inheriting `JOURNAL_STREAM`. A presence-only check is wrong in exactly those
cases, in the direction that corrupts output. It is also stale if a process
redirects its own stderr.

The cheaper tests do not work either. `isatty(2)` is false for the journal
*and* for a file, a pipe and a container runtime. `S_ISSOCK` is true for any
socket and does not prove the socket is the journal.

### Standard output goes to the journal too

By default it already does, and it is the more fundamental of the two:
`StandardOutput=` defaults to `journal`, and `StandardError=inherit`
duplicates that descriptor. Both file descriptors are therefore the same
socket, `fstat(1)` and `fstat(2)` return the same pair, and `JOURNAL_STREAM`
matches either — so checking stderr is correct in the default configuration.

The project's own convention is unaffected: stdout stays reserved for
`click.echo` in the `config` and `host` verbs, and the daemon logs to stderr.
Under a unit both land in the journal regardless; the separation matters only
when a human redirects one of them.

**The one case operators must avoid** is setting `StandardOutput=journal` and
`StandardError=journal` *explicitly*. That makes systemd open two separate
streams with different inodes, and `JOURNAL_STREAM` then names only one of
them — an ambiguity systemd has had open since
[systemd#6800](https://github.com/systemd/systemd/issues/6800). The stderr
comparison may then fail, the prefix is skipped, and priorities are lost while
the output stays valid. The remedy is to leave `StandardError=` unset.

## 🏷 The `<N>` priority prefix

journald assigns every line a service writes to stdout or stderr the priority
from `SyslogLevel=`, which defaults to `info`. It does not read the
application's own level, and it cannot distinguish stdout from stderr — a
limitation open since [systemd#5019](https://github.com/systemd/systemd/issues/5019),
whose proposed `_SOURCE=stdout|stderr` field was never merged.

The consequence today is a live defect: every record this daemon emits,
including budget exhaustion warnings and worker crashes, is stored at
`PRIORITY=6`. `journalctl -u pr-review-agent -p warning` returns nothing,
ever, and no priority-based filtering or alerting can work.

`SyslogLevelPrefix=` defaults to yes, and a line beginning `<N>` is *"passed
on to syslog with the specified log level set but the prefix removed"*. So
the daemon writes `<4>{"level":"warning",…}` and journald stores
`{"level":"warning",…}` with `PRIORITY=4`. The prefix never reaches the
stored message, which is why it does not corrupt the JSON.

| Python | syslog | `N` |
| :-- | :-- | :-- |
| `CRITICAL` | crit | 2 |
| `ERROR` | err | 3 |
| `WARNING` | warning | 4 |
| `INFO` | info | 6 |
| `DEBUG` | debug | 7 |

Use the single digit 0–7. It sets the level only; the facility remains
whatever `SyslogFacility=` says, which defaults to `daemon`.

### Format and prefix are two separate decisions

They are resolved by different tests, and conflating them is the mistake this
section exists to prevent.

| stderr actually goes to | `isatty()` | `JOURNAL_STREAM` matches | prefix | `auto` format |
| :-- | :-- | :-- | :-- | :-- |
| journald, under a systemd unit | false | **yes** | **`<N>`** | json |
| a terminal, run by hand | true | no | none | text |
| a file — `2>agent.jsonl` | false | no | none | json |
| a pipe — `… \| jq` | false | no | none | json |
| a container runtime | false | no | none | json |
| rsyslog, arriving through journald | false | yes | `<N>`, stripped before rsyslog sees it | json |

`isatty()` is false in five of those six rows and they do not share an
answer, so it cannot drive the prefix. `JOURNAL_STREAM` splits exactly the
rows that need splitting, and the terminal needs no detection of its own — it
is simply one of the things that are not journald.

The failure modes are asymmetric, which is why this is detected rather than
configured. Prefixing always puts `<6>{"time":…}` into every file, pipe and
terminal, making each line invalid JSON, silently, discovered only when
something tries to parse it. Prefixing never leaves the production
deployment with no priorities at all.

### Why there is no `level_prefix` setting

A boolean would offer four states, two of which are broken and neither of
which detection can produce. Its only function would be to let an operator
select a broken state, and the corrupt-JSON one fails silently. Neither
`dockerd` nor `containerd` exposes such a knob.

The one case detection genuinely gets wrong is
`pr-review-agent daemon start | systemd-cat`, where journald is the consumer
but `JOURNAL_STREAM` is set by the service manager rather than by
`systemd-cat`. That is an ad-hoc shell invocation, not a unit. If it ever
matters, it should be added as a fourth value of the existing `format` key
(`auto | text | json | journal`) rather than as a boolean — an enum value
leaves the broken combinations with no spelling.

### `<N>` is systemd's prefix, not the syslog wire format

It looks identical to the `<PRI>` of RFC 5424, but it is not the same thing
and nothing downstream sees it. rsyslog receives these lines *through*
journald, which has already parsed the prefix, set `PRIORITY` and removed it
— so the file written in the next section contains no `<4>` anywhere.

Speaking syslog directly is the road not taken:
`logging.handlers.SysLogHandler` writes to `/dev/log` and computes a real
`<PRI>` as `facility * 8 + severity`. That is a second endpoint with its own
socket, framing and failure modes.

## 🛠 How it is wired

`logs.py` holds all of it: `JsonFormatter`, which promotes `extra=` fields
onto the object; `JournalPriority`, which prefixes a rendered record; and
`_on_journal`, which compares `JOURNAL_STREAM` against `os.fstat(2)`. They
are joined in `configure`, once, when the daemon starts:

```python
handler = logging.StreamHandler(sys.stderr)
handler.setFormatter(_formatter(fmt, sys.stderr))   # shape, then prefix

root = logging.getLogger()
root.addHandler(handler)
root.setLevel(logging.WARNING)
logging.getLogger(PACKAGE_LOGGER).setLevel(level)
for name in THIRD_PARTY_FLOORS:                     # httpx, httpcore, asyncio
    logging.getLogger(name).setLevel(_third_party_level(name, level))
```

Both format decisions are resolved there, at handler construction, and
neither is re-evaluated per record.

The contextual keys the six events carry — `repo`, `pr`, `reason`, `kind`,
`findings`, `tokens`, `remaining`, `tightest` — are passed as `extra=`
*beside* the interpolated message, not instead of it. Text mode is therefore
the line it always was, and JSON mode carries both a legible `msg` and the
fields the queries below select on.

JSON also settles a multi-line problem. journald applies the prefix per
line, so a traceback logged with `exc_info=True` — which the worker does in
three places and the daemon in one — would otherwise split into several
journal entries, only the first carrying the right priority. Inside the
`"exc"` string it is one line with one priority.

## 🧩 The systemd unit

```ini
[Unit]
Description=pr-review-agent
After=network-online.target

[Service]
Type=exec
ExecStart=/usr/local/bin/pr-review-agent daemon start --config /etc/pr-review-agent/config.yaml
Environment=PR_REVIEW_AGENT_LOG_LEVEL=INFO
EnvironmentFile=/etc/pr-review-agent/token.env      # GITHUB_TOKEN
SyslogIdentifier=pr-review-agent
# StandardOutput and StandardError are deliberately unset: the defaults are
# journal and inherit, which makes both descriptors the same socket. Setting
# StandardError= explicitly opens a second stream and breaks JOURNAL_STREAM
# detection -- see systemd#6800.
LogsDirectory=pr-review-agent
NoNewPrivileges=yes
ProtectSystem=strict
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

`SyslogIdentifier=` is what gives rsyslog a stable name to match; without it
the identifier is derived from the executable.

```bash
systemctl edit pr-review-agent          # change the level, no ExecStart edit
systemctl restart pr-review-agent
journalctl -u pr-review-agent -p warning -S today
```

## 🗂 A file copy, without a file sink

journald keeps its copy and rsyslog writes the file. The application is not
involved and `config.yaml` says nothing about it.

Check which input module the distribution already uses before adding a
second — `imjournal` on RHEL and Fedora, `imuxsock` plus
`ForwardToSyslog=yes` in `/etc/systemd/journald.conf` on Debian and Ubuntu:

```bash
grep -rn "imjournal\|imuxsock" /etc/rsyslog.conf /etc/rsyslog.d/
```

```
# /etc/rsyslog.d/30-pr-review-agent.conf

# Raise the cap first: rsyslog truncates at 8 KiB by default, and a truncated
# JSON line is an unparseable line. Global scope, before any rule.
global(maxMessageSize="64k")

# Pass the message through verbatim. The default templates prepend a syslog
# header, which would make every line invalid JSON.
template(name="jsonline" type="string" string="%msg:::drop-last-lf%\n")

if $programname == "pr-review-agent" then {
    action(type="omfile"
           file="/var/log/pr-review-agent/agent.jsonl"
           template="jsonline"
           fileCreateMode="0640"
           fileOwner="root"
           fileGroup="adm")
    stop
}
```

`stop` keeps the lines out of `/var/log/syslog` as well.

Lines may carry a leading space, because rsyslog's `msg` property
conventionally has one. Leave it: JSON parsers skip leading whitespace. The
widely copied `%msg:2:$%` template slices from the second character to remove
it, which silently eats the opening brace when the space is *not* there.

```bash
sudo mkdir -p /var/log/pr-review-agent
sudo rsyslogd -N1                       # configuration check
sudo systemctl restart rsyslog
sudo head -2 /var/log/pr-review-agent/agent.jsonl | jq .
```

Rotation belongs to logrotate, not to the daemon:

```
/var/log/pr-review-agent/agent.jsonl {
    daily
    rotate 14
    compress
    missingok
    notifempty
    create 0640 root adm
    sharedscripts
    postrotate
        /usr/lib/rsyslog/rsyslog-rotate
    endscript
}
```

## 🔎 Querying

```bash
AGENT=/var/log/pr-review-agent/agent.jsonl

# One pull request's entire story, in order
jq -r 'select(.pr == 123) | "\(.time[11:19]) \(.msg)"' $AGENT

# Why was this not reviewed
jq -r 'select(.reason) | [.pr, .reason] | @tsv' $AGENT

# Budget drawdown over time
jq -r 'select(.remaining) | [.time, .tightest, .remaining] | @tsv' $AGENT

# Everything one component said
jq 'select(.logger == "pr_review_agent.budget")' $AGENT

# Reviews posted today
jq -r 'select(.msg | startswith("published")) | [.time, .repo, .pr] | @tsv' $AGENT
```

Live, off the journal:

```bash
journalctl -u pr-review-agent -f -o cat \
  | jq -R --unbuffered -r 'fromjson? // empty
      | "\(.time[11:19]) \(.level[0:1]) \(.pr // "-") \(.msg)"'
```

Two details in that pipeline are not optional. `-o cat` prints only the
message, but `journalctl -u` also carries systemd's own plain-text lines
(`Started pr-review-agent.`), so `-R` with `fromjson? // empty` is what skips
them instead of aborting on the first one. And `--unbuffered` is required for
any `-f` tail, or output appears in 4 KiB bursts. Neither is needed against
the rsyslog file, which contains only matched lines.

Because the priority prefix makes journald record real priorities, the coarse
cut needs no `jq` at all:

```bash
journalctl -u pr-review-agent -p warning -S today
```

## 🍎 macOS and Windows

Nothing platform-specific is required, because the detection *is* the
platform test. `JOURNAL_STREAM` cannot be set on either, so `_on_journal`
returns false and the daemon emits plain JSON lines on stderr. `os.environ`
and `os.fstat` are portable, and no import exists that is unavailable off
Linux.

This matches the posture `daemon.py:355-375` already takes for signals —
Windows has no `SIGHUP`, *"so that one is skipped rather than faked"*.
Platform integration is additive; its absence degrades a feature rather than
breaking the program. It matters because `python-ci.yml` spot-checks macOS
and Windows at 3.12.

Point the supervisor at a file and every query above works unaltered:

```xml
<!-- launchd -->
<key>StandardErrorPath</key>
<string>/usr/local/var/log/pr-review-agent/agent.jsonl</string>
```

Apple's unified log is the journald analogue but needs pyobjc or ctypes, and
launchd will not route stdout there regardless. On Windows, NSSM and WinSW
both redirect stderr to a file; the Event Log analogue,
`logging.handlers.NTEventLogHandler`, needs pywin32 and is a second endpoint.

## 🧪 What the tests must pin

- Precedence: flag beats environment beats config file.
- The five-by-four level table above, end to end. The load-bearing rows:
  `DEBUG` leaves `httpx` at WARNING and `httpcore` at INFO, so neither the
  per-request line nor the transport's fourteen-record trace is reachable by
  asking the agent for DEBUG; `ERROR` and `CRITICAL` do lower all three.
- Each of the six events is emitted at its agreed level, read back out of
  the four modules that emit them, so the table and the code cannot drift.
- At INFO events 3 to 6 are visible and events 1, 2 and the engine adapter's
  argv line are not.
- The reason codes in [Triggers](TRIGGERS.md) still describe reality, and
  every decision is DEBUG.

And, with #53:

- `auto` resolves to text on a terminal and to JSON on anything else, both
  branches; a named format is not second-guessed by either.
- `_on_journal` is true when `JOURNAL_STREAM` matches a real `os.fstat` and
  false when it does not or is unset. Built from a temporary file's own
  device and inode, this runs unmodified on all three CI platforms, because
  `st_dev` and `st_ino` are populated on Windows too.
- A warning record starts `<4>` when journald is detected, starts `{` when it
  is not, and round-trips through `json.loads` in both cases; every level
  maps to its syslog digit.
- A real classifier decision, rendered as JSON, carries `reason`, `pr`,
  `repo` and `kind` as fields — the acceptance criterion of #53 — and the
  keys this page's queries name are attached where each event is emitted.

## ✅ What #53 changed

Nothing about which events are emitted or at what level. Three things about
their shape and their priority:

**`logging.format`.** Accepted by the loader, with `--log-format` and
`PR_REVIEW_AGENT_LOG_FORMAT` above it. An unrecognised name is refused at
startup rather than falling back, like the level: a format that quietly
became something else is a stream the collector downstream cannot parse.

**The JSON record.** `JsonFormatter` and the promoted `extra=` fields, and
with them the per-component slice at query time — `jq 'select(.logger == …)'`.
The contextual keys the six events carry were already interpolated into the
message text, so this was a change of shape rather than of content.

**The `<N>` priority prefix.** Before it, every record the daemon emitted was
stored by journald at `PRIORITY=6` — budget refusals and worker crashes
included — so `journalctl -u pr-review-agent -p warning` returned nothing,
ever. That was a live defect rather than a missing luxury, and it is fixed:
the priority now follows the application's own level.

### Already built, recorded here because earlier drafts listed them

The two events that once had no record at all now have one, and both landed
with the level work:

**Start of review** is in `worker.py`, immediately after
`await self.publisher.acknowledge(claim.trigger)` — after the lease is
confirmed held, before anything slow. Not in the engine adapter, whose line
fires only once the pull request facts and the checkout have both succeeded;
a cold clone is the slow part, so a run that stalled there was
indistinguishable from one that never started.

**Remaining budget** is read from `Governor.headroom(now)` once per review,
*after* the settle rather than on the `reviewed …` record as the design
originally proposed. Until the settle the ledger still holds the run's
reservation at `max_run_tokens`, so the number would understate what is left
by whatever the run did not spend.

Neither addition calls a review engine, removes a cap, or widens what
triggers a review.
