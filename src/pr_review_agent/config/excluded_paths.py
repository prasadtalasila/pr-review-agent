"""The built-in path exclusions: what is not worth reading in any repository.

BUDGET.md layer 2's four categories -- lockfiles, vendored trees, generated
code, minified bundles -- where reviewing a line is close to worthless while
the line still counts against a size cap and is still shown to the engine.

They live in the package rather than in the configuration file so that an
operator names only what is special about their repository:
``budget.excluded_paths`` *adds* to this list, and ``budget.default_exclusions:
false`` drops it for the repository that genuinely reviews its lockfiles. Like
every key in the ``budget`` section the result reloads on ``SIGHUP``, so an
operator who finds the agent blind to something can fix it without a restart.

Every entry is a plain glob, matched at any depth via ``**/``. None may begin
with ``:``: ``workspace.exclusions`` supplies the ``:(exclude,glob)`` magic in
front of each one, and ``tests/test_exclusions.py`` pins that for the list as
a whole.
"""

from __future__ import annotations

LOCKFILES: tuple[str, ...] = (
    "**/package-lock.json",
    "**/yarn.lock",
    "**/pnpm-lock.yaml",
    "**/poetry.lock",
    "**/Cargo.lock",
    "**/Gemfile.lock",
    "**/composer.lock",
    "**/go.sum",
)

VENDORED: tuple[str, ...] = (
    "**/vendor/**",
    "**/node_modules/**",
    "**/third_party/**",
)

#: The first entry is this project's own catch-all. The generator groups
#: below it are copied from pr-agent's ``settings/generated_code_ignore.toml``
#: at commit ``10bbd9a`` (MIT licence, https://github.com/qodo-ai/pr-agent),
#: unchanged, so that the attribution is honest and an upstream diff is easy
#: to re-apply. ``**/*.generated.*`` already covers pr-agent's
#: ``**/*.generated.ts``; the broader local pattern is kept and the upstream
#: one is not duplicated.
GENERATED: tuple[str, ...] = (
    "**/*.generated.*",
    # Protocol Buffers
    "**/*.pb.go",
    "**/*.pb.cc",
    "**/*_pb2.py",
    "**/*.pb.swift",
    "**/*.pb.rb",
    "**/*.pb.php",
    "**/*.pb.h",
    # OpenAPI / Swagger stubs
    "**/__generated__/**",
    "**/openapi_client/**",
    "**/openapi_server/**",
    "**/swagger.json",
    "**/swagger.yaml",
    # GraphQL codegen
    "**/*.graphql.ts",
    "**/*.graphql.js",
    # RPC / gRPC generators
    "**/*_grpc.py",
    "**/*Grpc.java",
    "**/*Grpc.cs",
    "**/*_grpc.ts",
    "**/*_grpc.js",
    # Go code generators
    "**/*_gen.go",
    "**/*generated.go",
)

MINIFIED: tuple[str, ...] = (
    "**/*.min.js",
    "**/*.min.css",
    "**/*.map",
)

#: The effective exclusions when ``budget.default_exclusions`` is true, before
#: the operator's own ``budget.excluded_paths`` are appended.
DEFAULT_EXCLUDED_PATHS: tuple[str, ...] = LOCKFILES + VENDORED + GENERATED + MINIFIED
