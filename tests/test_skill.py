"""The packaged skill, and the three copies of it that must not drift.

The skill exists because the same text had started living in two places --
``REVIEW_INSTRUCTIONS`` and ``docs/reporting/review-prompt.md`` -- and the
constant's own comment said paraphrasing it would let them drift silently.
Moving it into one file only helps if something notices when a second copy
appears, so this module compares every derived artefact against the thing it
was derived from: the prompt against the reference, the schema asset against
``FINDINGS_SCHEMA``, and the worked example against what ``publisher.render``
actually produces for the findings beside it.

The scripts are loaded by path rather than imported. Their directory is
``review-report``, which is not an identifier, and naming it with an
underscore to make it importable would rename the skill.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from fnmatch import fnmatch
from pathlib import Path

import pytest

from pr_review_agent import skills
from pr_review_agent.engine.models import Finding
from pr_review_agent.engine.prompt import (
    FALSE_POSITIVES,
    FINDINGS_SCHEMA,
    REVIEW_INSTRUCTIONS,
)
from pr_review_agent.publisher import TRAILER, render

ROOT = Path(str(skills.root()))
ASSETS = ROOT / "assets"


def load_script(name: str):
    """One of the skill's scripts, as a module.

    Registered in ``sys.modules`` before it is executed because
    ``@dataclass`` resolves its own module by name while the class body
    runs, and a module that is not there yet makes that lookup return None.
    """
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def example_findings() -> tuple[Finding, ...]:
    """The worked example's findings, parsed by the script that renders them."""
    data = json.loads((ASSETS / "findings.example.json").read_text(encoding="utf-8"))
    return load_script("render_report.py").findings_from(data)


def test_prompt_reads_the_skill_reference() -> None:
    """The daemon's instructions are the skill's, not a paraphrase of them."""
    assert skills.reference("finding-contract.md") == REVIEW_INSTRUCTIONS


def test_prompt_reads_the_false_positive_reference() -> None:
    """Both readers are told what not to report, and told it from one file."""
    assert skills.reference("false-positives.md") == FALSE_POSITIVES


def test_schema_asset_matches_the_schema_the_engine_enforces() -> None:
    """A finding written against the asset is one ``--json-schema`` accepts."""
    asset = json.loads((ASSETS / "findings.schema.json").read_text(encoding="utf-8"))
    assert asset == FINDINGS_SCHEMA


def test_worked_example_is_what_the_renderer_produces() -> None:
    """The example is generated output, so it cannot teach a shape that is wrong."""
    rendered = render(
        "d61de1700000000000000000000000000000000a",
        example_findings(),
        pr_number=1765,
        round_number=3,
        commits=3,
        handle="claude",
    )
    assert (ASSETS / "report.example.md").read_text(encoding="utf-8") == rendered + "\n"


def test_skill_front_matter_names_the_directory() -> None:
    """Claude Code resolves a skill by its directory, and names it by front matter."""
    first_lines = (ROOT / "SKILL.md").read_text(encoding="utf-8").splitlines()[:3]
    assert first_lines[0] == "---"
    assert first_lines[1] == f"name: {skills.SKILL_NAME}"


@pytest.mark.parametrize(
    "name", ["finding-contract.md", "report-contract.md", "false-positives.md"]
)
def test_every_reference_the_skill_names_exists(name: str) -> None:
    """SKILL.md sends the reader to three files; all three have to be there."""
    assert name in (ROOT / "SKILL.md").read_text(encoding="utf-8")
    assert skills.reference(name)


def test_checker_passes_the_worked_example() -> None:
    check = load_script("check_report.py")
    assert check.violations((ASSETS / "report.example.md").read_text("utf-8")) == []


@pytest.mark.parametrize(
    ("mutate", "rule"),
    [
        (lambda t: t.replace("## Review: PR #1765", "## Review of PR 1765"), "header"),
        (lambda t: t.replace("## Nits", "## Other"), "sections"),
        (
            lambda t: t.replace("9. **The generators", "1. **The generators"),
            "numbering",
        ),
        (
            lambda t: t.replace("`BrandMark` uses", "- `BrandMark` uses"),
            "nits-are-prose",
        ),
        (lambda t: t.replace(TRAILER, ""), "trailer"),
    ],
)
def test_checker_names_the_rule_that_was_broken(mutate, rule: str) -> None:
    """Each failure quotes the identifier ``report-contract.md`` explains."""
    check = load_script("check_report.py")
    text = mutate((ASSETS / "report.example.md").read_text(encoding="utf-8"))
    assert [v for v in check.violations(text) if v.startswith(f"{rule}:")]


def test_empty_report_is_accepted() -> None:
    check = load_script("check_report.py")
    body = render("a" * 40, (), pr_number=7, round_number=2, commits=1, handle="claude")
    assert check.violations(body) == []


def test_render_script_reproduces_the_example(tmp_path: Path) -> None:
    """The script is a wrapper over ``publisher.render``, not a second renderer."""
    script = load_script("render_report.py")
    out = tmp_path / "report.md"
    script.main(
        [
            str(ASSETS / "findings.example.json"),
            "--pr",
            "1765",
            "--head-sha",
            "d61de1700000000000000000000000000000000a",
            "--round",
            "3",
            "--commits",
            "3",
            "--high-water",
            "9",
            "-o",
            str(out),
        ]
    )
    assert out.read_text(encoding="utf-8") == (ASSETS / "report.example.md").read_text(
        encoding="utf-8"
    )


#: Run a script in a child interpreter where the only importable
#: ``pr_review_agent`` is the one ``skill install`` copied beside it.
#:
#: That is the state a first-time user is most likely to be in -- ``skill
#: install`` puts files in a skills directory and installs nothing into the
#: interpreter that then runs them -- and it is not reachable by editing
#: ``sys.path`` from inside the suite, because the suite has the package and
#: CI installs it into site-packages. A meta-path finder that resolves the
#: name normally and then rejects any origin outside the vendored directory
#: is, so the same test means the same thing here and on CI.
WITHOUT_THE_PACKAGE = """
import importlib.machinery, os, runpy, sys

vendor = os.path.realpath(
    os.path.join(os.path.dirname(os.path.abspath(sys.argv[1])), "_vendor")
)


class OnlyVendored:
    '''Refuse `pr_review_agent` unless it is the copy beside the script.'''

    def find_spec(self, name, path=None, target=None):
        if name != "pr_review_agent" and not name.startswith("pr_review_agent."):
            return None
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        origin = getattr(spec, "origin", None) or ""
        if spec is not None and os.path.realpath(origin).startswith(vendor):
            return spec
        raise ModuleNotFoundError("No module named " + repr(name), name=name)


for name in [n for n in sys.modules if n.startswith("pr_review_agent")]:
    del sys.modules[name]
sys.meta_path.insert(0, OnlyVendored())
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
"""


def run_blocked(script: Path, *args: str) -> subprocess.CompletedProcess:
    """``script`` in a child interpreter that cannot import the real package."""
    return subprocess.run(
        [sys.executable, "-c", WITHOUT_THE_PACKAGE, str(script), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def run_without_the_package(script: str, *args: str) -> subprocess.CompletedProcess:
    """A script in the *source* skill, which has no vendored copy beside it."""
    return run_blocked(ROOT / "scripts" / script, *args)


@pytest.mark.parametrize("script", ["render_report.py", "check_report.py"])
def test_a_missing_package_is_explained_rather_than_raised(script: str) -> None:
    """A traceback here reads as a broken install, which is the wrong diagnosis."""
    completed = run_without_the_package(script)
    assert completed.returncode != 0
    assert "Traceback" not in completed.stderr
    assert script in completed.stderr
    assert "pip install pr-review-agent" in completed.stderr


def test_collecting_context_needs_nothing_installed() -> None:
    """The one script SKILL.md may call stdlib-only, pinned so the claim stays true."""
    completed = run_without_the_package("collect_context.py", "--help")
    assert completed.returncode == 0
    assert "--base" in completed.stdout


def test_an_installed_skill_renders_without_the_package(tmp_path: Path) -> None:
    """The whole point of vendoring: a skill directory that works on its own.

    Rendering the worked example rather than asserting a file list, because
    a list only says the copy happened. Byte-equality with the example says
    the copy is complete *and* produces what the daemon produces -- one
    assertion covering both halves of "one source, two deliveries".
    """
    target = skills.install(tmp_path)
    out = tmp_path / "report.md"
    completed = run_blocked(
        target / "scripts" / "render_report.py",
        str(ASSETS / "findings.example.json"),
        "--pr",
        "1765",
        "--head-sha",
        "d61de1700000000000000000000000000000000a",
        "--round",
        "3",
        "--commits",
        "3",
        "--high-water",
        "9",
        "-o",
        str(out),
    )
    assert completed.returncode == 0, completed.stderr
    assert out.read_text(encoding="utf-8") == (ASSETS / "report.example.md").read_text(
        encoding="utf-8"
    )


def test_an_installed_checker_works_without_the_package(tmp_path: Path) -> None:
    """``check_report.py`` reads its rules out of ``report``, so it vendors too."""
    target = skills.install(tmp_path)
    completed = run_blocked(
        target / "scripts" / "check_report.py", str(ASSETS / "report.example.md")
    )
    assert completed.returncode == 0, completed.stderr


def test_the_installed_package_wins_over_the_vendored_copy(tmp_path: Path) -> None:
    """Appended, not inserted: upgrading the package upgrades an old install."""
    target = skills.install(tmp_path)
    vendored = target.joinpath(*skills.VENDOR_DIR) / skills.PACKAGE / "report.py"
    vendored.write_text("raise AssertionError('the vendored copy was preferred')\n")
    completed = subprocess.run(
        [
            sys.executable,
            str(target / "scripts" / "render_report.py"),
            str(ASSETS / "findings.example.json"),
            "--pr",
            "1765",
            "--head-sha",
            "d61de1700000000000000000000000000000000a",
            "--round",
            "3",
            "--commits",
            "3",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).resolve().parent.parent / "src"),
        },
    )
    assert completed.returncode == 0, completed.stderr


def test_the_vendored_closure_stays_poor(tmp_path: Path) -> None:
    """Every vendored module is a byte copy, so there is no second renderer."""
    target = skills.install(tmp_path)
    package = Path(str(skills.root())).parent.parent
    for name in skills.VENDORED:
        copied = target.joinpath(*skills.VENDOR_DIR, skills.PACKAGE, *name.split("/"))
        assert copied.read_bytes() == (package / name).read_bytes(), name


def test_install_copies_the_whole_skill(tmp_path: Path) -> None:
    target = skills.install(tmp_path)
    assert (target / "SKILL.md").is_file()
    assert (target / "references" / "finding-contract.md").is_file()
    assert (target / "scripts" / "check_report.py").is_file()
    assert (target / "assets" / "findings.schema.json").is_file()


def test_install_refuses_to_overwrite(tmp_path: Path) -> None:
    skills.install(tmp_path)
    with pytest.raises(FileExistsError):
        skills.install(tmp_path)


def test_force_replaces_only_a_directory_this_wrote(tmp_path: Path) -> None:
    """A name collision is not a licence to delete somebody else's directory."""
    (tmp_path / skills.SKILL_NAME).mkdir(parents=True)
    with pytest.raises(FileExistsError):
        skills.install(tmp_path, force=True)
    skills.install(tmp_path / "fresh")
    assert skills.install(tmp_path / "fresh", force=True).is_dir()


def test_every_data_file_is_matched_by_a_packaging_include() -> None:
    """A wheel without these is a daemon that will not import.

    `pyproject.toml` needs an explicit `include` for anything that is not a
    `.py` file, and for thirteen releases this project shipped a wheel whose
    config templates were missing for exactly that reason. The check is
    static rather than a build, so it costs nothing to keep.
    """
    pyproject = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    patterns = re.findall(r'\{ path = "([^"]+)", format = "wheel" \}', pyproject)
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or path.suffix == ".py":
            continue
        if "__pycache__" in path.parts:
            continue
        relative = f"src/pr_review_agent/skills/{path.relative_to(ROOT.parent)}"
        assert any(fnmatch(relative, p) for p in patterns), relative
