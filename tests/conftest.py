"""A local HTTPS remote, so the git tests speak the production transport.

``GIT_ALLOW_PROTOCOL=https`` refuses ``file://``, and that is the point: the
whitelist is a constant with no test-only widening, because widening it is
what an ``ext::`` submodule URL needs. Serving ``git http-backend`` behind a
loopback TLS socket keeps the suite offline while still exercising
``git-remote-https`` and real smart HTTP.

It also makes the wire observable. Every request is recorded, so "no
credential reaches git" is an assertion about what was actually sent rather
than a claim about the argv -- something a ``file://`` remote could not
express at all.

Loopback is not network access: nothing leaves the host.
"""

from __future__ import annotations

import datetime
import http.server
import ipaddress
import os
import pathlib
import ssl
import subprocess
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from pr_review_agent.workspace import Workspace

#: The pull request the fixture repository publishes.
PR_NUMBER = 7

#: Lines the fixture's vendored file adds, and the two reviewable lines
#: beside it in ``feature.py``. Named so a test can assert on the totals
#: without restating what the fixture happens to contain.
VENDORED_LINES = 60
REVIEWABLE_LINES = 2


def git(*args: str, cwd: pathlib.Path | None = None) -> str:
    """Plain git, for building fixtures.

    Deliberately *not* the hardened runner: this is the fake GitHub, and it
    has to be able to do things the agent must not, such as writing a
    ``refs/pull/N/head`` that no branch points at.
    """
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "Fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    }
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _backend() -> str:
    """``git http-backend``, found through git's own exec path."""
    exec_path = pathlib.Path(git("--exec-path"))
    for candidate in ("git-http-backend", "git-http-backend.exe"):
        if (exec_path / candidate).exists():
            return str(exec_path / candidate)
    raise RuntimeError(f"git-http-backend is not under {exec_path}")


def _write_cert(directory: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    """A self-signed certificate for 127.0.0.1, valid for a day."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    ca_path = directory / "ca.pem"
    key_path = directory / "key.pem"
    ca_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return ca_path, key_path


@dataclass
class GitRemote:
    """A loopback HTTPS remote serving one bare repository."""

    #: The server root, as a `Workspace` takes it: no repository path.
    base_url: str
    #: The full clone URL, for tests that drive git directly.
    url: str
    ca: pathlib.Path
    serve_root: pathlib.Path
    head_sha: str
    repo: str = "owner/name"
    base_ref: str = "main"
    requests: list[dict[str, str]] = field(default_factory=list)


def _make_handler(
    root: pathlib.Path, seen: list[dict[str, str]]
) -> type[http.server.BaseHTTPRequestHandler]:
    """A CGI shim over ``git http-backend`` that records what it was sent."""

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:
            """Silence the server; pytest output is not a web log."""

        def _cgi(self, body: bytes = b"") -> None:
            seen.append(dict(self.headers.items()))
            path, _, query = self.path.partition("?")
            env = {
                "GIT_PROJECT_ROOT": str(root),
                "GIT_HTTP_EXPORT_ALL": "1",
                "PATH_INFO": path,
                "QUERY_STRING": query,
                "REQUEST_METHOD": self.command,
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                "CONTENT_LENGTH": str(len(body)),
                "PATH": os.environ["PATH"],
            }
            protocol = self.headers.get("Git-Protocol")
            if protocol:
                env["HTTP_GIT_PROTOCOL"] = protocol
            completed = subprocess.run(
                [_backend()], input=body, env=env, capture_output=True, check=False
            )
            head, _, payload = completed.stdout.partition(b"\r\n\r\n")
            self.send_response(200)
            for line in head.split(b"\r\n"):
                if b":" in line:
                    key, value = line.split(b":", 1)
                    self.send_header(key.decode(), value.decode().strip())
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's API
            self._cgi()

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's API
            self._cgi(self.rfile.read(int(self.headers["Content-Length"])))

    return Handler


def _build_fixture_repo(root: pathlib.Path) -> tuple[pathlib.Path, str]:
    """A base branch, plus a head commit that lives on no branch at all.

    That second part is the whole point: a fork's pull request head is
    reachable only from ``refs/pull/N/head`` in the base repository, so
    building it that way models a fork exactly, offline.
    """
    build = root / "build"
    serve = root / "serve" / "owner" / "name.git"
    serve.parent.mkdir(parents=True)
    build.mkdir()

    git("init", "-q", "-b", "main", str(build))
    (build / "base.txt").write_text("base\n")
    git("add", "-A", cwd=build)
    git("commit", "-qm", "base", cwd=build)
    git("checkout", "-q", "-b", "pr", cwd=build)
    (build / "feature.py").write_text("def added():\n    return 1\n")
    # A vendored tree and a binary blob, so the size gate can be tested
    # against what exclusions actually leave behind. VENDORED_LINES is large
    # enough to swamp the two reviewable lines beside it, which is the shape
    # of the pull request layer 2 exists to stop being charged for.
    (build / "vendor").mkdir()
    (build / "vendor" / "lib.js").write_text("var x = 0;\n" * VENDORED_LINES)
    (build / "logo.bin").write_bytes(bytes(range(256)))
    git("add", "-A", cwd=build)
    git("commit", "-qm", "pull request head", cwd=build)
    head_sha = git("rev-parse", "HEAD", cwd=build)
    git("checkout", "-q", "main", cwd=build)
    git("branch", "-q", "-D", "pr", cwd=build)

    git("clone", "-q", "--bare", str(build), str(serve))
    git("update-ref", f"refs/pull/{PR_NUMBER}/head", head_sha, cwd=serve)
    return serve, head_sha


@pytest.fixture
def workspace(git_remote: GitRemote, tmp_path, monkeypatch) -> Workspace:
    """A workspace pointed at the double.

    ``GIT_SSL_CAINFO`` is how the fixture's certificate reaches git, and the
    runner passes that variable through for production reasons of its own: a
    TLS-inspecting proxy presents its own certificate too.
    """
    monkeypatch.setenv("GIT_SSL_CAINFO", str(git_remote.ca))
    return Workspace(
        repo=git_remote.repo,
        cache_dir=tmp_path / "cache",
        base_url=git_remote.base_url,
    )


@pytest.fixture
def git_remote(tmp_path_factory: pytest.TempPathFactory) -> Iterator[GitRemote]:
    """A fork-shaped pull request, served over https from loopback."""
    root = tmp_path_factory.mktemp("remote")
    serve, head_sha = _build_fixture_repo(root)

    ca, key = _write_cert(root)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(ca, key)
    seen: list[dict[str, str]] = []
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), _make_handler(root / "serve", seen)
    )
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    base_url = f"https://127.0.0.1:{server.server_address[1]}"
    try:
        yield GitRemote(
            base_url=base_url,
            url=f"{base_url}/owner/name.git",
            ca=ca,
            serve_root=serve,
            head_sha=head_sha,
            requests=seen,
        )
    finally:
        server.shutdown()
        server.server_close()
