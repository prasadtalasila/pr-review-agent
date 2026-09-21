# A development container for this repository: the local gate
# (pytest/ruff/pylint/pyright/poetry build) and a runnable agent, in one
# image, on a machine that need not have Python 3.13, git 2.32+, Poetry or
# Node on it.
#
# The checkout is **mounted, not baked in** -- /workspace is a bind mount of
# your working copy (docker-compose.yml), so the code the container runs is
# the code you are editing and a rebuild is only needed when the toolchain
# changes. What the image carries is the resolved dependency set: poetry.lock
# installed into /opt/venv, plus the `claude` CLI the review engine shells
# out to.
#
# Not exercised by CI -- nothing in .github/workflows builds this file -- so a
# break here is caught by hand or not at all. DOCKER.md's "Verify the
# container" is the check to re-run after changing this file or entrypoint.sh.
#
# Build (from the repository root -- the context is the root, not docker/):
#   docker build -t pr-review-agent-dev -f docker/Dockerfile \
#     --build-arg PRA_USER="$(id -un)" \
#     --build-arg PRA_UID="$(id -u)" --build-arg PRA_GID="$(id -g)" .
# Run (from docker/, so compose resolves its own relative defaults):
#   cd docker && cp .env.example .env   # then edit it
#   docker compose up -d && docker compose exec dev bash

# Pinned rather than floating on `node:26`, and Node rather than a `python:`
# base, because two of the three toolchains here are Node programs: the
# `claude` CLI the engine adapter runs, and pyright, which downloads its own
# Node when it cannot find one. Debian trixie underneath, whose python3 is
# 3.13 -- inside the supported 3.10-3.14 range, and one version below the
# 3.14 the CI matrix tops out at, so a 3.14-only break is still CI's to catch.
FROM node:26.6.0-trixie

ENV DEBIAN_FRONTEND=noninteractive

# The account this image runs as. Every part of it is a build arg with **no
# default**, for the reason docker/.env.example spells out where you set them:
# the account is a property of your machine, not of this project, and a
# default that is wrong for almost everyone still works -- it just writes
# files owned by the wrong uid into the checkout you mount, which you then
# cannot edit from outside the container. The guard refuses a bare
# `docker build` that forgets them rather than creating an account called "".
#
# uid 1000 is the one value to avoid: the Node base image has already given it
# to its own `node` account.
ARG PRA_USER
ARG PRA_UID
ARG PRA_GID
RUN : "${PRA_USER:?required: set PRA_USER in docker/.env, or pass --build-arg PRA_USER=<name>}" && \
    : "${PRA_UID:?required: set PRA_UID in docker/.env (your \`id -u\`), or pass --build-arg PRA_UID=<uid>}" && \
    : "${PRA_GID:?required: set PRA_GID in docker/.env (your \`id -g\`), or pass --build-arg PRA_GID=<gid>}"

# git is load-bearing rather than a convenience: the workspace layer shells
# out to it for the bare mirror and the per-run worktree, and it must be 2.32
# or later for GIT_CONFIG_GLOBAL -- below that the checkout's hardening is
# ignored without an error (DEVELOPER.md). Trixie ships 2.47.
# python3-venv is what answers PEP 668, which forbids a bare `pip install`
# here as it does on any modern Debian; python-is-python3 makes a bare
# `python` the same interpreter as the venv's.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        python3 \
        python3-venv \
        python-is-python3 \
        ca-certificates \
        less \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -g "${PRA_GID}" "${PRA_USER}" && \
    useradd -u "${PRA_UID}" -g "${PRA_GID}" -m -s /bin/bash "${PRA_USER}"

# Claude Code lands after the account exists: `npm install -g` writes into
# root-owned /usr/local, which is fine, but the first *run* wants a writable
# home for its config and cache and resolves that from $HOME at exec time.
#
# The default is the version `engine.expected_version` names in
# config.example.yaml, so the image and the shipped config agree out of the
# box -- a mismatch only warns, but a warning nobody can explain is its own
# cost. Bumping it is a deliberate edit here *and* there, which is the point
# of pinning: unpinned, whatever npm serves at build time lands in the image,
# and two builds of this file diverge silently.
ARG CLAUDE_CODE_VERSION=2.1.274
RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}"

# One venv, first on PATH, owned by the unprivileged account -- so `python`,
# `pip`, `poetry`, `pytest` and `pr-review-agent` are all the same
# environment, and the entrypoint's editable install needs no root.
#
# POETRY_VIRTUALENVS_CREATE=false is what reconciles this with poetry.toml's
# `in-project = true`: without it `poetry install` would build a *second*
# venv at /workspace/.venv, inside the bind mount, colliding with the .venv
# your host already has there. With it, Poetry installs into the active
# environment, and every `poetry run ...` in DEVELOPER.md works verbatim.
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="/opt/venv/bin:${PATH}"
ENV POETRY_VIRTUALENVS_CREATE=false
RUN python3 -m venv "${VIRTUAL_ENV}" && \
    "${VIRTUAL_ENV}/bin/pip" install --no-cache-dir --upgrade pip && \
    "${VIRTUAL_ENV}/bin/pip" install --no-cache-dir poetry && \
    chown -R "${PRA_USER}:${PRA_USER}" "${VIRTUAL_ENV}"

# ENV PATH above is not enough on its own, and this was measured rather than
# anticipated: Debian's /etc/profile *assigns* PATH rather than extending it,
# so any login shell -- `docker compose exec dev bash -l`, `su - <user>`, an
# editor's remote shell -- starts with the venv gone and `poetry: command not
# found` from a container that demonstrably has Poetry in it. A drop-in that
# /etc/profile sources at the end is what survives that.
RUN printf 'export VIRTUAL_ENV=%s\nexport PATH="%s/bin:$PATH"\n' \
        "${VIRTUAL_ENV}" "${VIRTUAL_ENV}" > /etc/profile.d/pr-review-agent-venv.sh

WORKDIR /workspace

# Dependencies only, in their own cached layer: these four files decide the
# resolved set, so editing source code does not re-resolve it. README.md rides
# along because pyproject.toml declares `readme = "README.md"` and Poetry
# validates that path even under --no-root.
#
# --no-root because the package itself is not here at build time -- it arrives
# with the bind mount, and entrypoint.sh installs it editable on first start.
COPY pyproject.toml poetry.lock poetry.toml README.md ./
USER ${PRA_USER}
RUN poetry install --no-root && rm -rf "${HOME}/.cache/pypoetry" "${HOME}/.cache/pip"

# Pyright fetches its own npm package on first use and would do it inside
# whichever `poetry run pyright` first ran -- i.e. at the worst moment, over
# the network, in the middle of the gate. Warming it here puts it in the
# image instead. It finds the Node installed above rather than downloading a
# second copy.
RUN pyright --version

USER root
# /usr/local/bin rather than the account's home: ENTRYPOINT bakes this path
# in, and a path under /home/<user> would make it depend on PRA_USER.
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
# 0755 explicitly, not `chmod +x`: COPY preserves the host file's mode, so a
# checkout made under a group-writable umask would put a group-writable PID 1
# in the image.
RUN chmod 0755 /usr/local/bin/entrypoint.sh

USER ${PRA_USER}
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["bash"]
