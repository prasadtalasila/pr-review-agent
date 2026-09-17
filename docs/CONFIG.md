# Configuration reference

One file, `config.yaml`, next to the daemon. Copy `config.example.yaml` and
edit. `config.yaml` is gitignored: it names real accounts and will later sit
beside the agent's credentials.

## 🧾 The rule the loader follows

**Unknown keys are rejected, not ignored.** A typo in a safety setting must
fail at startup, not silently fall back to a default that spends tokens. That
applies to unknown top-level sections and to unknown keys inside a section.

Only the sections backed by implemented components are accepted today. The
`budget`, `publish` and `engine` sections described in [BUDGET.md](BUDGET.md)
will be added with the components that read them — adding them earlier would
mean accepting settings that do nothing, which is the failure this rule exists
to prevent.

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

## 📄 A minimal file

```yaml
github:
  repo: INTO-CPS-Association/DTaaS
  agent_user_id: null

triggers:
  handle: claude
  allowlist:
    - 114395272
```

## 🔁 Reload

`SIGHUP` reload is specified for the budget settings and the two kill switches
(`budget.enabled`, `publish.dry_run`) so that stopping the agent never requires
a restart. It is not implemented yet — today `Config.load` runs once at
startup.
