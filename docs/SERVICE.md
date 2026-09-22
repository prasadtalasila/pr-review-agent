# Running as a service

`daemon start` is built to run unattended: it polls on an interval, it holds
leases across restarts, and it reloads its budget on `SIGHUP`. What it has
never had is a supported way to *stay* running. This page is that way — a
**systemd user unit**, shipped inside the package and installed by
`pr-review-agent service install`.

`DOCKER.md` rules the container out explicitly ("This is not a deployment
image"), so this is the deployment story.

## 🙋 Why a user unit and not a system one

The review engine spawns the `claude` binary, and `claude` finds its login
under `$HOME`. A **system** unit would therefore need a dedicated service
account, a second `claude login` performed as that account, and a
`ProtectHome=` setting carefully drilled through so the daemon can still
reach it. Every one of those is a step that can be got wrong silently: the
daemon starts, polls, classifies, and only fails when it first tries to
review.

A user unit runs as the human who already ran `claude login`. There is no
second credential, nothing to chown, and no root anywhere in the procedure.

The cost is one command, and it is not optional:

```bash
loginctl enable-linger $USER
```

Without lingering, a user manager starts at your first login and stops at
your last logout. With it, the manager — and so the daemon — starts at boot
and survives you logging out. If you forget this, the symptom is a daemon
that works perfectly until you close your SSH session.

If you need a system unit anyway — a shared host where no human account
should own the agent — the unit here is a reasonable starting point, but
`state.db` and the checkout cache must move to `StateDirectory=` and
`CacheDirectory=` under `/var/lib`, and the `claude` login problem above is
yours to solve.

## 🗂 What goes where

Config and state are deliberately not in one directory: `config.yaml` is
read-only to the service and reloaded on `SIGHUP`, while `state.db` is
written on every poll cycle. `/etc` is the wrong home for the second of
those even on a system install — and `ProtectSystem=strict`, which this unit
sets, mounts `/etc` read-only for the service, so a state database there
could not be opened at all.

| What | Path | Written by |
| :-- | :-- | :-- |
| the unit | `~/.config/systemd/user/pr-review-agent.service` | `service install` |
| `config.yaml` | `~/.config/pr-review-agent/config.yaml` | you, via `config generate` |
| `GITHUB_TOKEN` | `~/.config/pr-review-agent/token.env`, mode 0600 | you |
| `state.db` | `~/.local/state/pr-review-agent/state.db` | the daemon |
| checkout cache | `~/.cache/pr-review-agent/repos` | the daemon |
| logs | the journal | journald |

`XDG_CONFIG_HOME`, `XDG_STATE_HOME` and `XDG_CACHE_HOME` are honoured if
set; the paths above are what they default to.

!!! note "Why not `StateDirectory=` and friends"

    systemd would create these directories itself, but the base it resolves
    them against for a *user* unit changed in systemd 256 — state moved from
    `$XDG_DATA_HOME` to `$XDG_STATE_HOME`. `service install` resolves the
    paths once and writes them absolutely into the unit and into what it
    tells you to put in `config.yaml`, so the layout does not depend on the
    host's systemd version.

## 🚀 Install

Install the package somewhere permanent first. A throwaway venv is fine for
a trial, but `ExecStart=` will name whatever interpreter's `bin` directory
the console script sits in, so deleting it later breaks the unit.

```bash
pipx install pr-review-agent        # or a venv you intend to keep
pr-review-agent service install
```

That writes the unit, creates the three directories, and creates an empty
`token.env` at mode 0600. It does **not** write `config.yaml` — `config
generate` is the only thing that writes that file, and this keeps it so. It
does not run `systemctl` either; it prints the commands, because starting a
service is your decision and not an install step.

### 1. Write the config

```bash
pr-review-agent config generate --output ~/.config/pr-review-agent/config.yaml
```

Then edit it. Beyond the usual `github.repo`, `triggers.allowlist` and
`budget` keys — see the [configuration reference](CONFIG.md) — **two keys
must change for a service install**, because both default to paths relative
to the working directory, and a unit has no meaningful one:

```yaml
store:
  path: /home/YOU/.local/state/pr-review-agent/state.db

workspace:
  cache_dir: /home/YOU/.cache/pr-review-agent/repos
```

`service install` prints both lines with your paths already filled in.

A third key often needs changing too:

```yaml
engine:
  binary: /home/YOU/.local/bin/claude      # not just `claude`
```

The user manager's `PATH` is not your shell's. It does not read `.bashrc`,
`.zshrc` or `.profile`, so an `nvm` shim or an `~/.npm-global/bin` entry
that works interactively will not be found. `command -v claude` in your
shell gives the absolute path to paste here. Getting this wrong fails at the
first review, not at startup.

Then check it:

```bash
pr-review-agent config validate --config ~/.config/pr-review-agent/config.yaml
```

### 2. Supply the token

```bash
chmod 600 ~/.config/pr-review-agent/token.env      # service install already did
$EDITOR ~/.config/pr-review-agent/token.env
```

One line, no quotes, no `export`:

```bash
GITHUB_TOKEN=github_pat_...
```

A fine-grained PAT with read and write on pull requests for the one
repository in `config.yaml`. It is passed to the daemon by
`EnvironmentFile=` and never by `ExecStart=`, so it does not appear in
`ps`, in `systemctl show` or in `systemctl cat`.

`service install` never overwrites this file, not even under `--force`: by
the second install it holds a real credential.

### 3. Check the host, then start

```bash
pr-review-agent host check --config ~/.config/pr-review-agent/config.yaml

systemctl --user daemon-reload
systemctl --user enable --now pr-review-agent
loginctl enable-linger $USER

systemctl --user status pr-review-agent
journalctl --user -u pr-review-agent -f
```

`host check` is worth running by hand before the first start, because it
names which of the four things is unreachable — `api.github.com`,
`github.com`, `api.anthropic.com`, or a git too old for
`GIT_CONFIG_GLOBAL`. From inside the unit the same failure is just a
non-zero exit and a restart.

## 🎛 Operating it

```bash
systemctl --user reload pr-review-agent     # SIGHUP: budget and publish only
systemctl --user restart pr-review-agent    # everything else
systemctl --user stop pr-review-agent
```

**`reload` and `restart` are not interchangeable.** `ExecReload=` sends
`SIGHUP`, which re-reads `config.yaml` and applies only the `budget` and
`publish` sections — see [the daemon loop](DAEMON.md). That is the one way
to turn the kill switch on, or tighten a cap, without dropping the queue and
re-seeding the poll watermarks. A change to any other section needs a
restart, and the daemon says so in the log when it sees one.

To change anything in the `[Service]` section, do not edit the installed
file — the next `service install --force` would overwrite it:

```bash
systemctl --user edit pr-review-agent       # writes an override.conf
systemctl --user restart pr-review-agent
```

Logs:

```bash
journalctl --user -u pr-review-agent -f              # follow
journalctl --user -u pr-review-agent -S today        # since midnight
journalctl --user -u pr-review-agent -p warning      # budget refusals, crashes
```

The priority is the daemon's own level, not `SyslogLevel=`: under a unit it
writes each record behind a `<N>` prefix that journald reads and strips, so
`-p warning` returns the warnings and errors and nothing else. journald
cannot read an application's level by itself, which is why the daemon has to
say so. [LOGGING.md](LOGGING.md#-the-n-priority-prefix) has the detail.

## 🔧 What the unit says, and why

`systemctl --user cat pr-review-agent` shows the installed file. Five
choices in it are load-bearing and would not be obvious from the outside.

**`StandardOutput=` and `StandardError=` are not set.** This looks like an
omission and is not. The defaults are `journal` and `inherit`, which makes
both descriptors the *same* journal socket — the only arrangement in which
`JOURNAL_STREAM` names it unambiguously. Setting `StandardError=journal`
explicitly opens a second stream with a different inode, and
[systemd#6800](https://github.com/systemd/systemd/issues/6800) then leaves
`JOURNAL_STREAM` naming only one of the two. The daemon's journald
detection, and the priority prefix that depends on it, fail silently. Leave
both unset.

**There is no `LogsDirectory=`.** The daemon never opens a log file. It
writes one stream to stderr and journald owns the destination; a file copy,
if you want one, is rsyslog's job.

**`ProtectHome=` is absent, on purpose.** It would hide the state database,
the checkout cache and the `claude` login in a single directive. The
service would start and then fail at its first review.

**`ProtectSystem=strict`** makes `/usr`, `/boot`, `/efi` and `/etc`
read-only for the service; `$HOME`, where everything the daemon writes
lives, is untouched. On a host with unprivileged user namespaces disabled
this directive can stop a *user* unit from starting at all, with a
namespace error in `systemctl --user status`. If you hit that, comment it
out in an override and report it — the rest of the hardening does not
depend on it.

**`Environment=PR_REVIEW_AGENT_LOG_LEVEL=INFO` is the verbosity knob.** It
is what `systemctl --user edit` is meant to change, because changing
verbosity should never mean editing `ExecStart=`:

```bash
systemctl --user edit pr-review-agent     # Environment=PR_REVIEW_AGENT_LOG_LEVEL=DEBUG
systemctl --user restart pr-review-agent
```

A restart, not a reload: only the `budget` and `publish` sections are
re-read on `SIGHUP`. `--log-level` on `ExecStart=` would beat the
environment, and the shipped unit sets none, so this line is the effective
level. `logging.level` in `config.yaml` is the lowest layer of the three.
[LOGGING.md](LOGGING.md) has what each level shows.

**The format needs no knob under systemd.** `PR_REVIEW_AGENT_LOG_FORMAT`
exists and takes the same three layers, but the default `auto` already
resolves to JSON here, because stderr is the journal rather than a terminal
— and the daemon detects the journal socket and prefixes each record with
its `<N>` priority, so `journalctl -u pr-review-agent -p warning` returns
the warnings and nothing else. Set `PR_REVIEW_AGENT_LOG_FORMAT=text` only if
you would rather read the journal than query it:

```bash
journalctl --user -u pr-review-agent -f -o cat \
  | jq -R --unbuffered -r 'fromjson? // empty | "\(.time[11:19]) \(.msg)"'
```

## ⬆️ Upgrading

```bash
pipx upgrade pr-review-agent
systemctl --user restart pr-review-agent
```

Re-run `service install --force` only if the unit itself changed in the
release. It rewrites the unit and leaves `config.yaml`, `token.env`,
`state.db` and the checkout cache alone — but it also discards anything you
edited into the unit directly, which is why `systemctl --user edit` is the
right place for local changes.

If the venv or pipx path moved, the unit's `ExecStart=` is now stale and
`service install --force` is how it is refreshed.

## 🩺 Troubleshooting

| Symptom | Cause |
| :-- | :-- |
| dies when you log out | `loginctl enable-linger $USER` was not run |
| `Failed to connect to bus` over SSH | no user manager in that session; `XDG_RUNTIME_DIR` is unset — log in properly or use `machinectl shell` |
| starts, then exits 3 | unusable config or missing token; `journalctl --user -u pr-review-agent -n 20` names which |
| reviews fail but polling works | `engine.binary` is not an absolute path, or `claude` is not logged in as this user |
| `state.db` keeps reappearing empty | `store.path` is still the relative default, so it is being written to the manager's working directory |
| restart loop reporting success | an `ExecStart=` with no verb; a bare `pr-review-agent` exits 2 by design, precisely so this is visible |

Verify the unit parses before blaming it:

```bash
systemd-analyze --user verify ~/.config/systemd/user/pr-review-agent.service
```

## 🍎 macOS and Windows

There is no unit for either, and `service install` is a Linux procedure.
The daemon itself runs on both — `daemon.py` skips `SIGHUP` on Windows
rather than faking it — so a launchd plist or an NSSM service works; they
are just not shipped or tested here.

---

See also: [configuration reference](CONFIG.md), [the daemon loop](DAEMON.md),
[budget governor](BUDGET.md).
