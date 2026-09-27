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
import re
import sys
from fnmatch import fnmatch
from pathlib import Path

import pytest

from pr_review_agent import skills
from pr_review_agent.engine.models import Finding
from pr_review_agent.engine.prompt import FINDINGS_SCHEMA, REVIEW_INSTRUCTIONS
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
