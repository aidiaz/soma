"""The tier 3 path list must exist in exactly one place.

It used to exist in three: a grep pattern in `tier-gate.yml`, a table in
`docs/WORKFLOW.md`, and prose in `CLAUDE.md`. Three copies of a security
boundary drift, and they drift in the direction that hurts — a path added to
the documents but not the pattern produces a gate that is written down and does
not fire, so nobody looks for it.

`.github/tier3-paths.txt` is now the only copy. These tests fail when anything
else claims to know the list.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / ".github" / "tier3-paths.txt"
WORKFLOW_DOC = ROOT / "docs" / "WORKFLOW.md"
GATE = ROOT / ".github" / "workflows" / "tier-gate.yml"
CLAUDE_MD = ROOT / "CLAUDE.md"


def tier3_paths() -> list[str]:
    """The list, parsed the same way the workflow parses it."""
    return [
        line.strip()
        for line in SOURCE.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def documented_paths() -> list[str]:
    """The inline-code spans in the tier 3 row of the table in WORKFLOW.md."""
    for line in WORKFLOW_DOC.read_text().splitlines():
        if line.startswith("| **3**"):
            return re.findall(r"`([^`]+)`", line)
    pytest.fail("no tier 3 row found in docs/WORKFLOW.md")


def test_the_source_is_not_empty():
    # A parser bug that silently returned nothing would make every other
    # assertion here vacuously true, and the gate match nothing at all.
    assert len(tier3_paths()) >= 5


def test_the_document_lists_exactly_the_source():
    source, documented = tier3_paths(), documented_paths()
    assert set(documented) == set(source), (
        "docs/WORKFLOW.md has drifted from .github/tier3-paths.txt.\n"
        f"  only in the document: {sorted(set(documented) - set(source))}\n"
        f"  only in the source:   {sorted(set(source) - set(documented))}"
    )


def test_the_workflow_reads_the_file_rather_than_restating_it():
    gate = GATE.read_text()
    assert "tier3-paths.txt" in gate, "tier-gate.yml no longer reads the source file"
    # A reintroduced literal list is the specific regression this guards.
    for path in tier3_paths():
        if path.endswith("/"):
            continue
        escaped = path.replace(".", r"\.")
        assert escaped not in gate, (
            f"tier-gate.yml appears to hardcode {path!r} again. "
            "Build the pattern from .github/tier3-paths.txt instead."
        )


# Ordinary prose legitimately names two or three of these files together —
# "so `Dockerfile`, `compose.pi.yaml` and the deploy doc are unexercised" is a
# sentence, not a copy of the list. The restatement this guards against had
# nine entries on one line, so the threshold sits between the two rather than
# at the first hit.
RESTATEMENT_THRESHOLD = 4


def test_claude_md_does_not_keep_its_own_copy():
    text = CLAUDE_MD.read_text()
    assert "tier3-paths.txt" in text, "CLAUDE.md should point at the source file"
    for line in text.splitlines():
        hits = [p for p in tier3_paths() if f"`{p}`" in line]
        assert len(hits) < RESTATEMENT_THRESHOLD, (
            f"CLAUDE.md looks like it restates the tier 3 list ({len(hits)} entries "
            f"on one line). Point at .github/tier3-paths.txt instead: {line.strip()!r}"
        )


def classify(changed: list[str]) -> list[str]:
    """Mirror of the workflow's prefix match, so the semantics are tested."""
    paths = tier3_paths()
    return [c for c in changed if any(c.startswith(p) for p in paths)]


@pytest.mark.parametrize(
    "path",
    [
        "src/soma/serve/oauth.py",
        "src/soma/config.py",
        ".github/workflows/ci.yml",
        "deploy/RASPBERRY_PI.md",
        "compose.pi.yaml",
        "Dockerfile",
        ".env.example",
        "scripts/agent_token.sh",
        "CLAUDE.md",
        "docs/WORKFLOW.md",
    ],
)
def test_tier3_paths_are_caught(path):
    assert classify([path]) == [path]


@pytest.mark.parametrize(
    "path",
    [
        "src/soma/metrics.py",
        "src/soma/serve/queries.py",
        "tests/test_metrics.py",
        "README.md",
        "docs/SOMETHING_ELSE.md",
        "scripts/smoke.py",
    ],
)
def test_ordinary_paths_are_not_caught(path):
    # scripts/smoke.py matters most here: `scripts/agent_token.sh` is on the
    # list, and a prefix rule written as `scripts/` would swallow the whole
    # directory and make every change to it need a line-by-line read.
    assert classify([path]) == []
