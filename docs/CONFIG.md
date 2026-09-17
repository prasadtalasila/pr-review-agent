# Configuration reference

One file, `config.yaml`, next to the daemon. Copy `config.example.yaml` and
edit. `config.yaml` is gitignored: it names real accounts and will later sit
beside the agent's credentials.

## 🧾 The rule the loader follows

**Unknown keys are rejected, not ignored.** A typo in a safety setting must
fail at startup, not silently fall back to a default that spends tokens. That
applies to unknown top-level sections and to unknown keys inside a section.

Only the sections backed by implemented components are accepted today. The
`publish` and `engine` sections described in [BUDGET.md](BUDGET.md) will be
added with the components that read them — adding them earlier would mean
accepting settings that do nothing, which is the failure this rule exists to
prevent. The same rule is why `budget` carries `max_run_tokens` but not
`max_turns`: the governor reserves against the first, and nothing yet reads
the second.

## 🔐 What is *not* in this file

Credentials. The GitHub token is read from the `GITHUB_TOKEN` environment
variable, so it can come from a systemd `EnvironmentFile`, a secret manager or
the shell without ever being a file the repository could swallow. `config.yaml`
is gitignored regardless, because it names real accounts.

## 📖 Sections

### `github`

| Key | Type | Required | Meaning |
| :-- | :-- | :-- | :-- |
| `repo` | `owner/name` | yes | The repository to poll. Reviews are posted here. |
| `agent_user_id` | integer or `null` | no | The numeric id of the account the agent posts as. Its own comments are then ignored, so a posted review can never re-trigger a review. |

`repo` must contain exactly one `/`, with both halves non-empty.

Until `agent_user_id` is set, the agent cannot recognise and skip its own
comments — the `self_author` / `self_commenter` rejections never fire. Fill it
in as soon as the reviewer account exists.

### `triggers`

| Key | Type | Required | Meaning |
| :-- | :-- | :-- | :-- |
| `allowlist` | list of integers | yes | Numeric GitHub user ids whose events may start a review. |
| `handle` | string | no (default `claude`) | The handle that summons a review. A leading `@` is stripped. |

**`allowlist` entries must be numeric user ids, never logins.** A login can be
renamed and the freed name registered by somebody else, which would hand
eligibility to a stranger. A login in this list raises `AllowlistConfigError`
at startup rather than never matching — so does a non-positive number, and so
does a string like `"--5"` that only looks numeric.

Look an id up with:

```bash
curl -s https://api.github.com/users/<login> | grep '"id"'
```

An empty allowlist is valid and allows nobody. It is the safe starting state.

`handle` is what makes the pipeline reusable for a different agent: set it to
`aider` and `@aider` becomes the trigger.

### `store`

| Key | Type | Required | Meaning |
| :-- | :-- | :-- | :-- |
| `path` | string | no (default `state.db`) | The SQLite file holding watermarks, ETags and the review queue. |

The whole section is optional, which is the one exception to "only the
sections backed by implemented components are accepted" being paired with a
required section. The exception is affordable because the default cannot spend
anything: a database that does not exist yet has its watermarks
[seeded to the moment the daemon started](DAEMON.md#-cold-start-is-the-spend-bound),
so a fresh file reviews nothing from the backlog. Unknown keys inside `store`
are still rejected.

A relative path is resolved against the working directory the daemon is
started in, so the daemon logs the absolute path it settled on at `INFO`.
Prefer an absolute path under a service account's data directory in
production — pointing at the wrong file costs the queue's memory of what has
already been reviewed.

## 📄 A minimal file

```yaml
github:
  repo: INTO-CPS-Association/DTaaS
  agent_user_id: null

triggers:
  handle: claude
  allowlist:
    - 114395272

store:
  path: state.db
```

## 🔁 Reload

`SIGHUP` re-reads `config.yaml` and adopts its `budget` section, so stopping
the agent never requires a restart.

**Only `budget` is hot-swapped.** A changed `github`, `triggers` or `store`
section is logged as needing a restart rather than half-applied: the daemon's
watermarks describe the repository it started against, and swapping that
mid-flight would make them meaningless.

**A broken file leaves the previous configuration in force**, logged at
`ERROR`. Crashing on a bad reload would turn the emergency brake into a way to
take the service down with a typo.

`publish.dry_run` is the other key specified as reloadable. It arrives with
the publisher; this mechanism takes it without change.
