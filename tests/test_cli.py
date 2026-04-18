"""CLI-surface tests for summary rendering and --strict-refs.

The library-level tests cover adoption / molt / migration semantics;
these tests cover the user-facing output contract: separated summary
counts, end-of-output skip re-echo, and the strict-refs exit code.
"""

from __future__ import annotations

import pathlib

import pytest
from typer.testing import CliRunner

from main import app


@pytest.fixture
def runner() -> CliRunner:
    # This Click/Typer version exposes Result.stdout and Result.stderr
    # as separate attributes by default — no `mix_stderr` knob needed.
    return CliRunner()


@pytest.fixture
def tree_with_unresolvable_ref(tmp_path: pathlib.Path) -> pathlib.Path:
    """A tree that will produce one SKIP-REF during adopt: a ref to a
    nonexistent file. Adopt succeeds (SKIP-REF is non-fatal); the ref is
    left in the body, surfaced as a skip note."""
    (tmp_path / "src.md").write_text("# Src\n\nLink: [ghost](missing.md).\n")
    return tmp_path


def test_summary_separates_writes_from_skips(runner: CliRunner, tree_with_unresolvable_ref: pathlib.Path) -> None:
    result = runner.invoke(app, ["init", "--adopt", str(tree_with_unresolvable_ref)])
    assert result.exit_code == 0
    # Summary counts: 2 writes (CREATE .croc.toml + SCAFFOLD src.md) plus
    # 1 skip (SKIP-REF missing.md). The contract is "writes and skips are
    # tallied separately" — the exact number matters less than the split.
    assert "init OK (2 actions" in result.stdout
    assert "1 skipped ref" in result.stdout


def test_skips_reechoed_on_stderr_after_summary(runner: CliRunner, tree_with_unresolvable_ref: pathlib.Path) -> None:
    """The end-of-output block on stderr is the visibility fix:
    skips are the last thing the user sees even after a wall of actions."""
    result = runner.invoke(app, ["init", "--adopt", str(tree_with_unresolvable_ref)])
    assert result.exit_code == 0
    # Stderr carries the "Unresolved refs" header + the skip lines
    assert "Unresolved ref" in result.stderr
    assert "missing.md" in result.stderr


def test_default_exit_zero_even_with_skips(runner: CliRunner, tree_with_unresolvable_ref: pathlib.Path) -> None:
    """Backwards compatible: plain `init --adopt` still exits 0 when a
    tree has unresolvable refs — same as pre-`--strict-refs`."""
    result = runner.invoke(app, ["init", "--adopt", str(tree_with_unresolvable_ref)])
    assert result.exit_code == 0


def test_strict_refs_flag_exits_nonzero_when_skips_present(
    runner: CliRunner, tree_with_unresolvable_ref: pathlib.Path
) -> None:
    result = runner.invoke(
        app,
        ["init", "--adopt", "--strict-refs", str(tree_with_unresolvable_ref)],
    )
    assert result.exit_code == 1
    # But the summary still prints so the user sees what happened
    assert "init OK" in result.stdout


def test_strict_refs_flag_exits_zero_when_no_skips(runner: CliRunner, tmp_path: pathlib.Path) -> None:
    """--strict-refs is not punitive on clean trees: when every ref
    resolved or no refs existed, exit code is still 0."""
    (tmp_path / "target.md").write_text("# Target")
    (tmp_path / "src.md").write_text("# Src\n\n[t](target.md)\n")
    result = runner.invoke(app, ["init", "--adopt", "--strict-refs", str(tmp_path)])
    assert result.exit_code == 0


def test_molt_strict_refs_flag_exits_nonzero_on_dangling_weak_refs(runner: CliRunner, tmp_path: pathlib.Path) -> None:
    """The molt analog of the init flag: a tree with a weak ref to a
    missing target produces a SKIP-MOLT-REF note; --strict-refs makes
    that a non-zero exit for CI use."""
    (tmp_path / "src.md").write_text(
        "---\n"
        "id: src\n"
        "title: Src\n"
        "kind: leaf\n"
        "links:\n"
        "- to: ghost\n"
        "  strength: weak\n"
        "---\n\n"
        "Aspirational: [[see:ghost|future]].\n"
    )
    result = runner.invoke(app, ["molt", "--strict-refs", str(tmp_path)])
    assert result.exit_code == 1
    assert "SKIP-MOLT-REF" in result.stdout or "SKIP-MOLT-REF" in result.stderr


def test_clean_tree_has_no_unresolved_ref_block(runner: CliRunner, tmp_path: pathlib.Path) -> None:
    """Trees with no skip notes produce no end-of-output yellow block."""
    (tmp_path / "target.md").write_text("# Target")
    (tmp_path / "src.md").write_text("# Src\n\n[t](target.md)\n")
    result = runner.invoke(app, ["init", "--adopt", str(tmp_path)])
    assert result.exit_code == 0
    assert "Unresolved ref" not in result.stderr
