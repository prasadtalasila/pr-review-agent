#!/bin/bash
# Entrypoint for docker/Dockerfile: make the mounted checkout importable, then
# run whatever was asked of the container.
#
# The image is built with `poetry install --no-root`, because at build time
# there is no package to install -- src/ arrives with the bind mount. So the
# one thing that has to happen after the mount exists, and cannot happen
# before it, is linking the checkout into /opt/venv. That is this file.
set -euo pipefail

# --no-deps because the dependency set is the image's, resolved from
# poetry.lock at build time; this step is only meant to add the `.pth` link
# and the `pr-review-agent` console script. Without it the command the
# quickstart and DEVELOPER.md both use is simply absent, and `python -c
# "import pr_review_agent"` works only from the repository root.
#
# Idempotent by measurement rather than by a guard: re-running it on an
# already-linked checkout is a sub-second no-op, and a guard on "is the
# script present" would skip the re-link after the entry points change.
if [ -f /workspace/pyproject.toml ]; then
    pip install --quiet --no-deps --editable /workspace
else
    echo "entrypoint: WARNING -- /workspace holds no pyproject.toml." >&2
    echo "entrypoint: set PRA_WORKSPACE in docker/.env to this repository's root." >&2
fi

exec "$@"
