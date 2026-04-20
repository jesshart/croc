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


# ---------------------------------------------------------------------------
# crawl
# ---------------------------------------------------------------------------


@pytest.fixture
def src_with_py(tmp_path: pathlib.Path) -> pathlib.Path:
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text("a = 1\n")
    return src


def test_crawl_dry_run_writes_nothing(runner: CliRunner, tmp_path: pathlib.Path, src_with_py: pathlib.Path) -> None:
    out = tmp_path / "out"
    result = runner.invoke(app, ["crawl", str(src_with_py), "-o", str(out), "--dry-run"])
    assert result.exit_code == 0
    assert "would CREATE" in result.stdout
    assert not out.exists()


def test_crawl_adopt_flag_produces_checkable_tree(
    runner: CliRunner, tmp_path: pathlib.Path, src_with_py: pathlib.Path
) -> None:
    """The one-shot path: `crawl --adopt` → tree passes `check`."""
    out = tmp_path / "out"
    result = runner.invoke(app, ["crawl", str(src_with_py), "-o", str(out), "--adopt"])
    assert result.exit_code == 0

    check_result = runner.invoke(app, ["check", str(out)])
    assert check_result.exit_code == 0
    assert "borrow check OK" in check_result.stdout


def test_crawl_strict_refs_clean_exits_zero(
    runner: CliRunner, tmp_path: pathlib.Path, src_with_py: pathlib.Path
) -> None:
    """--strict-refs is not punitive: a clean crawl --adopt still exits 0."""
    out = tmp_path / "out"
    result = runner.invoke(app, ["crawl", str(src_with_py), "-o", str(out), "--adopt", "--strict-refs"])
    assert result.exit_code == 0


def test_crawl_strict_refs_exits_nonzero_on_unresolvable_ref(
    runner: CliRunner, tmp_path: pathlib.Path, src_with_py: pathlib.Path
) -> None:
    """Pre-seeded broken ref in the output dir survives crawl (existing file
    kept), then adopt hits it with SKIP-REF; --strict-refs turns that into
    a non-zero exit. Same pathway test_strict_refs_flag_exits_nonzero_when_skips_present
    exercises for `init --adopt`, here routed through the crawl command."""
    out = tmp_path / "out"
    out.mkdir()
    (out / "seed.md").write_text("# Seed\n\n[missing](nowhere.md)\n")
    result = runner.invoke(
        app,
        ["crawl", str(src_with_py), "-o", str(out), "--adopt", "--strict-refs"],
    )
    assert result.exit_code == 1
    assert "crawl OK" in result.stdout
    assert "SKIP-REF" in result.stderr or "SKIP-REF" in result.stdout


def test_crawl_adopt_dry_run_prints_advisory(
    runner: CliRunner, tmp_path: pathlib.Path, src_with_py: pathlib.Path
) -> None:
    """--adopt + --dry-run can't simulate adoption (no files on disk yet);
    we surface a note pointing to the follow-up command instead of silently
    skipping."""
    out = tmp_path / "out"
    result = runner.invoke(app, ["crawl", str(src_with_py), "-o", str(out), "--adopt", "--dry-run"])
    assert result.exit_code == 0
    assert "not previewed in --dry-run" in result.stderr


def test_crawl_existing_files_noted_on_stderr(
    runner: CliRunner, tmp_path: pathlib.Path, src_with_py: pathlib.Path
) -> None:
    """Re-running crawl without --force keeps existing files; the count
    lands on stderr so it's visible even after a wall of CREATE lines."""
    out = tmp_path / "out"
    first = runner.invoke(app, ["crawl", str(src_with_py), "-o", str(out)])
    assert first.exit_code == 0

    second = runner.invoke(app, ["crawl", str(src_with_py), "-o", str(out)])
    assert second.exit_code == 0
    assert "existing file(s) kept" in second.stderr
    assert "crawl OK (0 actions)" in second.stdout
