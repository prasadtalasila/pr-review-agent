# Tokens and credentials

The agent posts under a real GitHub account and spends a real metered LLM
allowance. Both are credentials, and neither is in `config.yaml`.

## 🔑 The GitHub token

Read from the `GITHUB_TOKEN` environment variable, and only from there.

```bash
GITHUB_TOKEN=github_pat_... pr-review-agent daemon start
```

**Never from `config.yaml`.** The config file names repositories and accounts,
is meant to be readable, and is the thing you would paste into an issue when
asking for help. A credential in it would leak the first time anyone did that.
`config validate` therefore needs no token at all — it answers a question about
the file, not about GitHub.

**What it needs:** a fine-grained personal access token scoped to the one
repository the config names, with exactly three permissions:

| Permission | Access | Why |
| --- | --- | --- |
| **Pull requests** | Read and write | Read lists the open pull requests and the inline review comments; write puts the 👀 on an inline review comment that mentioned the agent. |
| **Issues** | Read and write | A pull request's conversation comments are *issue* comments in the REST API. Read is the `@claude` feed; write posts the review or description, the notices, and the 👀 on a conversation comment or the pull request. |
| **Metadata** | Read only | Mandatory on every fine-grained token, and what `host check` reads the repository with. |

Nothing else. In particular **Contents is not needed**: the checkout fetches
the repository over anonymous `https` and no credential ever reaches `git`
(see [WORKSPACE.md](WORKSPACE.md)), so a token that can write code is a blast
radius the agent has no use for.

A classic PAT with `repo` also works, and is coarser than all of the above put
together. Prefer the fine-grained one.

Scope it to that repository. A token that can reach more than the agent is
configured for is a token whose blast radius is larger than the agent's.

### What `host check` can and cannot tell you

`pr-review-agent host check` reads `GET /repos/{owner}/{name}` and reports on
`permissions.push`. Read its answer with the limit in mind: **`push` is
*contents: write***, which is what a classic `repo` token carries and what the
list above deliberately leaves out. So a correctly scoped fine-grained token
reports `push: false`, and the check says so as a caution naming the three
permissions rather than as a failure.

That is the honest limit of a read-only pre-flight. GitHub publishes no
per-resource permission breakdown on that response, and the only way to be
certain a token may comment is to post a comment — which is not something a
pre-flight check may do to somebody's pull request. What the check does still
catch outright is a token that cannot read the repository at all.

**What happens to it.** It is passed as a `Bearer` header and nothing else. It
is never logged, never printed by the pre-flight checks — those report what
happened, not what was sent — and under systemd it reaches the process through
`EnvironmentFile=` rather than `ExecStart=`, so it does not appear in `ps`, in
`systemctl show` or in `systemctl cat`.

## 📄 The token file under systemd

`service install` creates an empty `token.env` at mode `0600` and never
overwrites it again — not even under `--force`, because by the second install it
holds a real credential.

```bash
$EDITOR ~/.config/pr-review-agent/token.env
```

One line. No quotes, no `export`:

```bash
GITHUB_TOKEN=github_pat_...
```

## 👥 One token per repository

When several daemons share a budget — one process per repository, all pointing
at one `store.path` — **each keeps its own token in its own file**:

| | Single | Several repositories |
| --- | --- | --- |
| token file | `~/.config/pr-review-agent/token.env` | `~/.config/pr-review-agent/<instance>/token.env` |
| unit | `pr-review-agent.service` | `pr-review-agent@<instance>.service` |

This is the whole reason the design is a process per repository rather than one
process with a list. Isolation is the operating system's to enforce: a daemon
that never holds another repository's token cannot post with it, however it
misbehaves. Merging the tokens into one credential would give that back — one
compromised or buggy instance could then write anywhere the shared token
reaches.

Two rules follow, and neither is enforced by code:

- **Do not point two units at one `token.env`.** The files are per-instance and
  mode `0600`; keep them that way.
- **Do not reuse one PAT across instances.** It defeats the isolation even when
  the files are separate.

The queue is scoped to its repository so that a daemon only ever claims its own
work — see [QUEUE.md](QUEUE.md#-a-queue-belongs-to-one-repository) — but that is
a correctness guard, not a substitute for separate credentials.

## 🧍 Who may spend the allowance

A token decides what the agent *can* do. The **allowlist** decides whose
requests it will act on, and it is a separate control:

```yaml
triggers:
  allowlist:
    - 1234567          # numeric GitHub user id, never a login
```

Allowlisting is on the numeric user id, never the login. A login can be renamed
and the freed name registered by a stranger; a user id cannot be transferred.

Each repository carries its own allowlist, in its own `config.yaml`. Sharing a
budget does not share trust.

Pull request bodies, comment bodies and diffs are untrusted input. Nothing in
them can widen what the agent is allowed to do.

## 🤖 The LLM credential

The review engine is the `claude` CLI, and it authenticates itself — the agent
holds no Anthropic credential and sends none. `host check` probes
`api.anthropic.com` only to prove the route exists: **no API key is sent and
none is needed**, so any HTTP status passes and only a transport error fails.

The engine runs with a scrubbed environment except for `ANTHROPIC_*`, which is
passed through if you have set it. `--bare` was rejected for the engine
invocation precisely because it would force `ANTHROPIC_API_KEY` authentication
and silently settle the billing question in favour of per-token API charges
rather than the CLI's own subscription.

What the agent *does* own is the spending, which is why the
[budget governor](BUDGET.md) sits in front of every review and why several
repositories sharing one plan must also share one
[store and one authority](BUDGET.md#-several-repositories-one-allowance).

## 🔁 Rotating a token

1. Issue the new PAT with the same repository scope.
2. Edit the `token.env` for that instance — or export the new value if you run
   by hand.
3. `systemctl --user restart pr-review-agent@<instance>` (or
   `pr-review-agent.service`). The token is read at startup, so `SIGHUP` does
   not pick up a new one.
4. Revoke the old PAT.

A restart is safe at any point: an in-flight review loses its lease and is
retried, and the watermarks and the ledger are on disk.

## 🚨 If a token leaks

Revoke it at GitHub first — that is the only step that stops it being used.
Then issue a replacement and restart the instance. Check the account's recent
activity for anything the agent did not do: every review the agent posts is
recorded in `runs`, so the database is the list to compare against.

Shell history is worth a thought here. `GITHUB_TOKEN=... pr-review-agent ...` on
one line is convenient and lands the credential in `~/.zsh_history` or
`~/.bash_history` in plain text. Prefer the `token.env` file, or a leading space
where your shell is configured to keep those out of history.
