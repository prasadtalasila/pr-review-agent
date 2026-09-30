"""The ``pr-description`` skill, and the copies of it that must not drift.

The same arrangement as ``review-report``, checked the same way: the
daemon's describe prompt against the contract, the schema asset against
``DESCRIPTION_SCHEMA``, the worked example against what the renderer
produces, and an installed copy rendering with the real package blocked.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from click.testing import CliRunner
from test_skill import run_blocked

from pr_review_agent import skills
from pr_review_agent.cli import cli
from pr_review_agent.engine.describe import DESCRIBE_INSTRUCTIONS, DESCRIPTION_SCHEMA

ROOT = Path(str(skills.root(skills.DESCRIBE_SKILL)))
ASSETS = ROOT / "assets"
#: The arguments ``SKILL.md`` says produce the worked example.
EXAMPLE_ARGS = (
    str(ASSETS / "description.example.json"),
    "--pr",
    "130",
    "--head-sha",
    "6a6d208e5c1c4a0b9d7f3e2a1b0c9d8e7f6a5b4c",
    "--commits",
    "4",
)


def load_script(name: str):
    """One of the skill's scripts, as a module; see ``test_skill.load_script``."""
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(f"describe_{path.stem}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def example() -> str:
    return (ASSETS / "description.example.md").read_text(encoding="utf-8")


def test_the_prompt_reads_the_skill_contract() -> None:
    contract = (ROOT / "references" / "description-contract.md").read_text("utf-8")
    assert contract.rstrip("\n") == DESCRIBE_INSTRUCTIONS


def test_the_schema_asset_is_the_schema_the_engine_enforces() -> None:
    asset = json.loads((ASSETS / "description.schema.json").read_text("utf-8"))
    assert asset == DESCRIPTION_SCHEMA


def test_the_front_matter_names_the_directory() -> None:
    lines = (ROOT / "SKILL.md").read_text(encoding="utf-8").splitlines()
    assert lines[:2] == ["---", f"name: {skills.DESCRIBE_SKILL}"]


def test_every_file_the_skill_names_exists() -> None:
    text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    for name in ("description-contract.md", "layout-contract.md"):
        assert f"references/{name}" in text and (ROOT / "references" / name).is_file()


def test_the_render_script_reproduces_the_example(tmp_path: Path) -> None:
    out = tmp_path / "description.md"
    load_script("render_description.py").main([*EXAMPLE_ARGS, "-o", str(out)])
    assert out.read_text(encoding="utf-8") == example()


def test_the_checker_passes_the_example_and_names_a_broken_rule() -> None:
    check = load_script("check_description.py")
    assert check.violations(example()) == []
    broken = example().replace("**Type:** Enhancement", "**Type:** Verdict")
    assert [v.split(":")[0] for v in check.violations(broken)] == ["type"]


def test_both_skills_ship_the_same_context_collector() -> None:
    review = Path(str(skills.root())) / "scripts" / "collect_context.py"
    assert (ROOT / "scripts" / "collect_context.py").read_bytes() == (
        review.read_bytes()
    )


def test_an_installed_skill_renders_without_the_package(tmp_path: Path) -> None:
    target = skills.install(tmp_path, skill=skills.DESCRIBE_SKILL)
    out = tmp_path / "description.md"
    rendered = run_blocked(
        target / "scripts" / "render_description.py", *EXAMPLE_ARGS, "-o", str(out)
    )
    assert rendered.returncode == 0, rendered.stderr
    assert out.read_text(encoding="utf-8") == example()
    checked = run_blocked(target / "scripts" / "check_description.py", str(out))
    assert checked.returncode == 0, checked.stderr


def test_skill_install_places_both_skills(tmp_path: Path) -> None:
    result = CliRunner().invoke(cli, ["skill", "install", "--dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    for name in skills.SKILLS:
        assert (tmp_path / name / "SKILL.md").is_file()
        assert f"Installed {name} to" in result.output
