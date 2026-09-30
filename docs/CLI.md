# CLI reference

Every command is `pr-review-agent <noun> <verb>`. The nouns are grouped by the
order you meet them: `config` writes and checks the file, `host` proves the
machine can reach GitHub, `daemon` runs the loop, `service` hands that loop
to systemd instead of to your terminal, and `skill` installs the review and
description skills for a person working in Claude Code.

```bash
pr-review-agent --version
pr-review-agent --help
```

Installing the package puts `pr-review-agent` on the path. `python -m
pr_review_agent` is the same entry point and keeps working, so a unit written
against it needs no change.

## 🗺 The whole surface

| Command | What it does |
| --- | --- |
| `config generate` | Write a config template. |
| `config validate` | Load a config file and report what the loader made of it. |
| `host check` | Run the pre-flight checks; exit 1 if any fails. |
| `daemon start` | Poll, classify, review and publish until stopped. |
| `service install` | Write the systemd user unit and the directories it names. |
| `skill install` | Copy the review and description skills into a Claude Code skills directory. |

`--config` defaults to `config.yaml` in the directory the command was started
in — which is also where a relative `store.path` puts `state.db`. See
[CONFIG.md](CONFIG.md).

## 🧱 First-time setup

```bash
pr-review-agent config generate            # writes ./config.yaml
$EDITOR config.yaml                        # repo, allowlist, budget
pr-review-agent config validate
GITHUB_TOKEN=... pr-review-agent host check
GITHUB_TOKEN=... pr-review-agent daemon start
```

To run it unattended, `service install` replaces the last step.

## 📝 `config generate`

Writes a template. Refuses to overwrite, because a `config.yaml` names real
accounts and there is no second copy of it.

| Option | Default | Meaning |
| --- | --- | --- |
| `--output PATH` | `config.yaml` | Where to write the template. |
| `--full` | off | The commented template showing every key and its default. |
| `--force` | off | Overwrite an existing file. |

`--full` is the one worth reading: it carries the reasoning for each key,
including the [budget](BUDGET.md) settings that decide what may be spent.

## ✅ `config validate`

Loads the file and prints what the loader made of it — the resolved paths, the
allowlist, the windows the budget works out to. Needs no `GITHUB_TOKEN`: it
answers a question about the file, not about GitHub.

| Option | Default | Meaning |
| --- | --- | --- |
| `--config TEXT` | `config.yaml` | Path to the config file. |

## 🩺 `host check`

The pre-flight checks: the token works, the repository is reachable, `git` and
the review engine are installed and the versions are the expected ones. Exits
`1` if any check fails, so it is usable in a script.

| Option | Default | Meaning |
| --- | --- | --- |
| `--config TEXT` | `config.yaml` | Path to the config file. |

Run it on a host that has never run the daemon before, and after an upgrade.

## ▶️ `daemon start`

The loop itself: poll, classify, enqueue, review, publish. Runs until `SIGINT`
or `SIGTERM`; `SIGHUP` re-reads the `budget` and `publish` sections without a
restart — see [Reload](CONFIG.md#-reload).

| Option | Default | Meaning |
| --- | --- | --- |
| `--config TEXT` | `config.yaml` | Path to the config file. |
| `--log-level [debug\|info\|warning\|error\|critical]` | `info` | Overrides `PR_REVIEW_AGENT_LOG_LEVEL` and `logging.level`. |
| `--log-format [auto\|text\|json]` | `auto` | Overrides `PR_REVIEW_AGENT_LOG_FORMAT` and `logging.format`. `auto` is text on a terminal and JSON anywhere else. |

Logs go to stderr; stdout is left to the `config` and `host` verbs. The
absolute `store.path` and `workspace.cache_dir` it settled on are logged at
`INFO` on startup — worth checking, because a relative path means whatever the
working directory made of it.

## ⚙️ `service install`

Writes a systemd **user** unit, the directories it names, and an empty
`token.env` at mode `0600`. It never writes `config.yaml` — `config generate`
stays the only writer of that — and never overwrites an existing `token.env`,
not even under `--force`, because by the second install it holds a real
credential.

| Option | Default | Meaning |
| --- | --- | --- |
| `--force` | off | Overwrite an existing unit file. |
| `--instance NAME` | none | Install the **template** unit for one repository of several sharing a budget. |

Without `--instance` you get `pr-review-agent.service`, one repository, one
token, one store.

### Several repositories

`--instance` installs `pr-review-agent@.service` instead: one unit file that
serves every instance, with systemd's `%i` expanding to the instance name for
the config path, the `EnvironmentFile` and the syslog identifier.

```bash
pr-review-agent service install --instance web
pr-review-agent service install --instance api
systemctl --user enable --now pr-review-agent@web pr-review-agent@api
```

The name becomes a directory and half a unit name, so it must start with a
letter or digit and hold only letters, digits, `_`, `.` and `-`.

Installing the second instance rewrites the shared unit byte for byte, so it
does not need `--force`; anything that would genuinely change the file still
does. Each instance gets its own `config.yaml`, its own `token.env` and its own
checkout cache — see [TOKENS.md](TOKENS.md) for why the tokens must not be
shared, and [SERVICE.md](SERVICE.md#-several-repositories-one-budget) for the
three rules the configs have to agree on.

## 🧠 `skill install`

Copies the two packaged skills into a Claude Code skills directory: the
**review skill**, `review-report` — the format contract, the false-positive
list and three scripts — so that a person writing a review by hand gets the
same report a daemon run would have posted, and the **description skill**,
`pr-description`, which does the same for what `@claude describe` posts
([Description skill](reporting/pr-description.md)).

| Option | Default | Meaning |
| --- | --- | --- |
| `--dir PATH` | `~/.claude/skills` | Where to install. Use `<repo>/.claude/skills` for one project. |
| `--force` | off | Replace an existing install. Refuses if the directory is not one this wrote. |

```bash
pr-review-agent skill install
```

The daemon does not need this verb and is not affected by it. Its reviewer
runs `claude` with `--setting-sources ""`, `--restricted` and
`--disable-slash-commands`, so it discovers no skill, plugin or settings file
at all — and it is handed the same text through the prompt instead. See
[Report template](reporting/review-report.md) and
[ENGINE.md](ENGINE.md).

## 🚪 Exit status

| Code | Meaning |
| --- | --- |
| `0` | Success, or a clean shutdown. |
| `1` | A check failed (`host check`). |
| `2` | Usage error — a mistyped command or option, from Click. |
| `3` | Unusable config, missing token, or a refusal to overwrite a file. |

`3` is deliberately not `2`: a mistyped command and a missing credential are
different problems, and a complier waiting for its budget authority exits `3`
on purpose so that `Restart=on-failure` brings it back. See
[BUDGET.md](BUDGET.md#-several-repositories-one-allowance).

## 🌍 Environment

| Variable | Read by | Meaning |
| --- | --- | --- |
| `GITHUB_TOKEN` | `host check`, `daemon start` | The GitHub credential. Never read from `config.yaml`. See [TOKENS.md](TOKENS.md). |
| `PR_REVIEW_AGENT_LOG_LEVEL` | `daemon start` | Log level, below `--log-level` and above `logging.level`. |
| `PR_REVIEW_AGENT_LOG_FORMAT` | `daemon start` | Record shape, below `--log-format` and above `logging.format`. |
