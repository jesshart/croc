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


def test_crawl_disambiguation_note_fires_on_stem_collision(runner: CliRunner, tmp_path: pathlib.Path) -> None:
    """Same-stem siblings (Dockerfile + Dockerfile.ecs + ...) previously
    collapsed to a single `.md` output silently. The fix disambiguates
    and the CLI now surfaces how many files were affected."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "Dockerfile").write_text("")
    (src / "Dockerfile.ecs").write_text("")
    (src / "Dockerfile.fargate_worker").write_text("")

    out = tmp_path / "out"
    result = runner.invoke(app, ["crawl", str(src), "-o", str(out)])
    assert result.exit_code == 0
    assert "3 filename collision(s) disambiguated" in result.stderr
    # All three stubs exist on disk — no last-write-wins data loss.
    assert (out / "Dockerfile.md").exists()
    assert (out / "Dockerfile.ecs.md").exists()
    assert (out / "Dockerfile.fargate_worker.md").exists()


def test_crawl_no_disambiguation_note_when_stems_unique(
    runner: CliRunner, tmp_path: pathlib.Path, src_with_py: pathlib.Path
) -> None:
    """The note must not fire on non-colliding trees — regression guard
    against spuriously nagging every crawl run."""
    out = tmp_path / "out"
    result = runner.invoke(app, ["crawl", str(src_with_py), "-o", str(out)])
    assert result.exit_code == 0
    assert "disambiguated" not in result.stderr


# ---------------------------------------------------------------------------
# --include-untracked / --no-include-untracked global flag
#
# These exercise the CLI-level contract: the flag is global, the default
# narrows tree-walks to `git ls-files`, and `--include-untracked` widens
# to tracked + untracked-but-not-ignored (drafts).
#
# See thoughts/shared/plans/2026-04-22-tracked-files-filter.md.
# ---------------------------------------------------------------------------


def _init_repo_with_draft(root: pathlib.Path) -> None:
    """Set up a throwaway git repo with:
      - `thoughts/tracked.md` — tracked, plain markdown
      - `thoughts/draft.md`   — untracked, not ignored, plain markdown
    Caller uses this tree to observe filter behavior."""
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=root, check=True)
    (root / "thoughts").mkdir()
    (root / "thoughts" / "tracked.md").write_text("# tracked\n")
    subprocess.run(["git", "add", "thoughts/tracked.md"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)
    (root / "thoughts" / "draft.md").write_text("# draft\n")


def test_global_flag_registered_at_app_level() -> None:
    """The flag is declared on the app-level callback, not per-command.

    Asserts against Typer's registered callback params directly instead
    of scraping rendered --help output — Rich word-wraps flag names to
    terminal width under `CliRunner` (which captures stdout and defaults
    to 80 columns), so a substring scan is brittle across environments.
    The registered params are the source of truth; if they exist, the
    flag is discoverable by users.
    """
    import typer.main

    click_group = typer.main.get_command(app)
    opts_by_name = {p.name: (p.opts, getattr(p, "secondary_opts", [])) for p in click_group.params}

    assert "include_untracked" in opts_by_name, "global --include-untracked param missing"
    primary, secondary = opts_by_name["include_untracked"]
    assert "--include-untracked" in primary
    assert "--no-include-untracked" in secondary


def test_init_adopt_default_skips_drafts(runner: CliRunner, tmp_path: pathlib.Path, monkeypatch) -> None:
    """Default (tracked-only): `init --adopt` augments tracked files, not drafts."""
    _init_repo_with_draft(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init", "--adopt", "--dry-run", "thoughts"])
    assert result.exit_code == 0, result.output
    assert "tracked.md" in result.stdout
    assert "draft.md" not in result.stdout


def test_init_adopt_include_untracked_covers_drafts(runner: CliRunner, tmp_path: pathlib.Path, monkeypatch) -> None:
    """With --include-untracked, drafts are folded in."""
    _init_repo_with_draft(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        ["--include-untracked", "init", "--adopt", "--dry-run", "thoughts"],
    )
    assert result.exit_code == 0, result.output
    assert "tracked.md" in result.stdout
    assert "draft.md" in result.stdout


def test_refs_default_skips_drafts(runner: CliRunner, tmp_path: pathlib.Path, monkeypatch) -> None:
    """`refs` walks only tracked files by default."""
    _init_repo_with_draft(tmp_path)
    # Give each file a path-ref so `refs` produces output.
    (tmp_path / "thoughts" / "tracked.md").write_text("see [link](./tracked.md)\n")
    (tmp_path / "thoughts" / "draft.md").write_text("see [link](./tracked.md)\n")
    import subprocess

    subprocess.run(["git", "add", "thoughts/tracked.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "update"], cwd=tmp_path, check=True)

    monkeypatch.chdir(tmp_path)
    default = runner.invoke(app, ["refs", "thoughts"])
    assert default.exit_code == 0, default.output
    assert "tracked.md" in default.stdout
    assert "draft.md" not in default.stdout

    widened = runner.invoke(app, ["--include-untracked", "refs", "thoughts"])
    assert widened.exit_code == 0, widened.output
    assert "draft.md" in widened.stdout


def test_check_default_skips_draft_with_broken_frontmatter(
    runner: CliRunner, tmp_path: pathlib.Path, monkeypatch
) -> None:
    """A malformed draft doesn't crash `check` at the default — it's
    out-of-scope. With --include-untracked, the same file fails."""
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=tmp_path, check=True)
    (tmp_path / "thoughts").mkdir()
    good = tmp_path / "thoughts" / "good.md"
    good.write_text("---\nid: good\ntitle: t\nkind: leaf\nlinks: []\n---\nbody\n")
    subprocess.run(["git", "add", "thoughts/good.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    (tmp_path / "thoughts" / "draft.md").write_text("---\nunterminated")

    monkeypatch.chdir(tmp_path)
    default = runner.invoke(app, ["check", "thoughts"])
    assert default.exit_code == 0, default.output

    widened = runner.invoke(app, ["--include-untracked", "check", "thoughts"])
    assert widened.exit_code != 0


def test_outside_git_repo_flag_is_noop(runner: CliRunner, tmp_path: pathlib.Path, monkeypatch) -> None:
    """Outside a git repo, flag has no effect — all files walked either way."""
    (tmp_path / "thoughts").mkdir()
    (tmp_path / "thoughts" / "a.md").write_text("# a\n")
    (tmp_path / "thoughts" / "b.md").write_text("# b\n")

    monkeypatch.chdir(tmp_path)
    default = runner.invoke(app, ["init", "--adopt", "--dry-run", "thoughts"])
    widened = runner.invoke(app, ["--include-untracked", "init", "--adopt", "--dry-run", "thoughts"])
    assert default.exit_code == 0
    assert widened.exit_code == 0
    assert "a.md" in default.stdout and "b.md" in default.stdout
    assert "a.md" in widened.stdout and "b.md" in widened.stdout


def test_crawl_mode_note_reflects_default(runner: CliRunner, tmp_path: pathlib.Path, monkeypatch) -> None:
    """The stderr 'respecting .gitignore' note names which mode ran."""
    _init_repo_with_draft(tmp_path)
    monkeypatch.chdir(tmp_path)

    default = runner.invoke(app, ["crawl", "thoughts", "-o", "out-default", "--dry-run"])
    assert default.exit_code == 0, default.output
    assert "tracked files only" in default.stderr

    widened = runner.invoke(
        app,
        ["--include-untracked", "crawl", "thoughts", "-o", "out-wide", "--dry-run"],
    )
    assert widened.exit_code == 0, widened.output
    assert "including untracked drafts" in widened.stderr


# ---------------------------------------------------------------------------
# lurk — per-file line-count budget
#
# See thoughts/shared/plans/2026-04-22-lurk-max-lines.md.
# ---------------------------------------------------------------------------


def test_lurk_command_registered() -> None:
    """The `lurk` subcommand exists on the app. Introspect Typer's
    command registry directly — same rationale as the global-flag
    test above (Rich wraps help output; substring scans are brittle)."""
    import typer.main

    click_group = typer.main.get_command(app)
    assert "lurk" in click_group.commands


def test_lurk_clean_tree_exits_zero(runner: CliRunner, tmp_path: pathlib.Path) -> None:
    (tmp_path / "small.md").write_text("line\n" * 20)
    result = runner.invoke(app, ["lurk", str(tmp_path), "-n", "100"])
    assert result.exit_code == 0
    assert "lurk OK" in result.stdout


def test_lurk_reports_violator_on_stderr_and_exits_one(runner: CliRunner, tmp_path: pathlib.Path) -> None:
    """Violation lines on stderr, summary line on stderr, exit 1."""
    (tmp_path / "big.md").write_text("line\n" * 150)
    (tmp_path / "small.md").write_text("line\n" * 10)
    result = runner.invoke(app, ["lurk", str(tmp_path), "-n", "100"])
    assert result.exit_code == 1
    assert "big.md" in result.stderr
    assert "150 lines" in result.stderr
    assert "over by 50" in result.stderr
    # Summary pluralization for 1 file: no trailing "s"
    assert "1 file exceed" in result.stderr
    assert "small.md" not in result.stderr


def test_lurk_higher_limit_clears_violation(runner: CliRunner, tmp_path: pathlib.Path) -> None:
    """Same tree, bigger `-n` — no violation."""
    (tmp_path / "big.md").write_text("line\n" * 150)
    result = runner.invoke(app, ["lurk", str(tmp_path), "-n", "200"])
    assert result.exit_code == 0


def test_lurk_include_frontmatter_changes_count(runner: CliRunner, tmp_path: pathlib.Path) -> None:
    """A doc with a 30-line frontmatter `links:` block plus an 80-line
    body sits under 100 by default, over 100 with --include-frontmatter."""
    body = "content\n" * 80
    links = "\n".join(f"  - to: id{i}" for i in range(25))
    (tmp_path / "doc.md").write_text(f"---\nid: big\ntitle: t\nkind: leaf\nlinks:\n{links}\n---\n\n{body}")

    default = runner.invoke(app, ["lurk", str(tmp_path), "-n", "100"])
    assert default.exit_code == 0, default.output

    inclusive = runner.invoke(app, ["lurk", "--include-frontmatter", str(tmp_path), "-n", "100"])
    assert inclusive.exit_code == 1, inclusive.output
    assert "doc.md" in inclusive.stderr


def test_lurk_respects_include_untracked(runner: CliRunner, tmp_path: pathlib.Path, monkeypatch) -> None:
    """Untracked `draft.md` (200 lines) is skipped by default, flagged
    with --include-untracked."""
    _init_repo_with_draft(tmp_path)
    # Draft is 200 lines → would violate if included.
    (tmp_path / "thoughts" / "draft.md").write_text("line\n" * 200)
    # Keep tracked.md below threshold.
    (tmp_path / "thoughts" / "tracked.md").write_text("line\n" * 20)
    monkeypatch.chdir(tmp_path)

    default = runner.invoke(app, ["lurk", "thoughts", "-n", "100"])
    assert default.exit_code == 0, default.output
    assert "draft.md" not in default.stderr

    widened = runner.invoke(app, ["--include-untracked", "lurk", "thoughts", "-n", "100"])
    assert widened.exit_code == 1, widened.output
    assert "draft.md" in widened.stderr


# ---------------------------------------------------------------------------
# attack: CLI surface. Library-level matrix is in tests/test_attack.py.
# ---------------------------------------------------------------------------


def _init_repo_with_trace_fixture(root: pathlib.Path) -> None:
    """A git repo under `root` with:
      - `src/producer.py` containing `persist_parquet("revenue")`
      - `thoughts/revenue.md` as a managed doc stub
      - `thoughts/.croc.toml` with one `[[trace]]` pattern

    Files are committed so the default tracked-only filter includes them.
    """
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=root, check=True)
    (root / "src").mkdir()
    (root / "src" / "producer.py").write_text('persist_parquet("revenue")\n')
    (root / "thoughts").mkdir()
    (root / "thoughts" / "revenue.md").write_text(
        "---\nid: revenue\ntitle: Revenue\nkind: leaf\nlinks: []\n---\n\n# Revenue\n"
    )
    (root / "thoughts" / ".croc.toml").write_text(
        'version = "0.1"\n\n'
        "[[trace]]\n"
        'name = "persist_parquet"\n'
        "pattern = '''persist_parquet\\([\"']([^\"']+)[\"']\\)'''\n"
        'code_globs = ["src/**/*.py"]\n'
    )
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)


def test_attack_command_registered() -> None:
    import typer.main

    click_group = typer.main.get_command(app)
    assert "attack" in click_group.commands


def test_attack_clean_bind_exits_zero(runner: CliRunner, tmp_path: pathlib.Path) -> None:
    _init_repo_with_trace_fixture(tmp_path)
    # `.croc.toml` is at tmp_path; pass thoughts/ as the tree root.
    result = runner.invoke(app, ["attack", str(tmp_path / "thoughts")])
    assert result.exit_code == 0, result.output
    assert "ATTACK revenue.md" in result.stdout
    assert "src/producer.py" in result.stdout
    # Doc was actually rewritten
    text = (tmp_path / "thoughts" / "revenue.md").read_text()
    assert "tracks:" in text
    assert "src/producer.py" in text


def test_attack_dry_run_writes_nothing(runner: CliRunner, tmp_path: pathlib.Path) -> None:
    _init_repo_with_trace_fixture(tmp_path)
    before = (tmp_path / "thoughts" / "revenue.md").read_text()
    result = runner.invoke(app, ["attack", str(tmp_path / "thoughts"), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "would ATTACK" in result.stdout
    assert (tmp_path / "thoughts" / "revenue.md").read_text() == before


def test_attack_strict_traces_exits_nonzero_on_skip(runner: CliRunner, tmp_path: pathlib.Path) -> None:
    _init_repo_with_trace_fixture(tmp_path)
    # Add an unresolved capture in the source — no ghost.md in tree.
    (tmp_path / "src" / "producer.py").write_text('persist_parquet("revenue")\npersist_parquet("ghost")\n')
    result = runner.invoke(app, ["attack", str(tmp_path / "thoughts"), "--strict-traces"])
    assert result.exit_code == 1
    assert "SKIP-TRACE" in result.stderr
    assert "ghost" in result.stderr


def test_attack_no_config_fails(runner: CliRunner, tmp_path: pathlib.Path) -> None:
    # git repo but no .croc.toml → load_config returns a default empty
    # config; attack_tree raises OpError for no patterns.
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "thoughts").mkdir()
    (tmp_path / "thoughts" / "doc.md").write_text("# doc\n")
    result = runner.invoke(app, ["attack", str(tmp_path / "thoughts")])
    assert result.exit_code == 1
    assert "no [[trace]] patterns" in result.stderr


def test_attack_bad_regex_in_config_fails(runner: CliRunner, tmp_path: pathlib.Path) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "thoughts").mkdir()
    (tmp_path / "thoughts" / ".croc.toml").write_text(
        'version = "0.1"\n\n[[trace]]\nname = "bad"\npattern = \'[unterminated\'\ncode_globs = ["**/*.py"]\n'
    )
    result = runner.invoke(app, ["attack", str(tmp_path / "thoughts")])
    assert result.exit_code == 1
    assert "invalid regex" in result.stderr


# ---------------------------------------------------------------------------
# hunt: CLI surface. Library-level matrix is in tests/test_hunt.py.
# ---------------------------------------------------------------------------


def _init_repo_with_hunt_fixture(root: pathlib.Path) -> None:
    """Committed repo with:
    - `src/producer.py`, `src/reader.py`
    - `thoughts/revenue.md` bound to `src/producer.py`
    - `thoughts/.croc.toml` (for [hunt] config; no [[trace]] patterns needed)
    """
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=root, check=True)
    (root / "src").mkdir()
    (root / "src" / "producer.py").write_text("# producer\n")
    (root / "src" / "reader.py").write_text("# reader\n")
    (root / "thoughts").mkdir()
    (root / "thoughts" / "revenue.md").write_text(
        "---\nid: revenue\ntitle: Revenue\nkind: leaf\nlinks: []\ntracks:\n- src/producer.py\n---\n\n# Revenue\n"
    )
    (root / "thoughts" / ".croc.toml").write_text('version = "0.1"\n')
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)


def test_hunt_command_registered() -> None:
    import typer.main

    click_group = typer.main.get_command(app)
    assert "hunt" in click_group.commands


def test_hunt_clean_tree_exits_zero(runner: CliRunner, tmp_path: pathlib.Path) -> None:
    _init_repo_with_hunt_fixture(tmp_path)
    result = runner.invoke(app, ["hunt", str(tmp_path / "thoughts")])
    assert result.exit_code == 0, result.output
    assert "hunt OK" in result.stdout


def test_hunt_staged_source_alerts(runner: CliRunner, tmp_path: pathlib.Path) -> None:
    import subprocess

    _init_repo_with_hunt_fixture(tmp_path)
    (tmp_path / "src" / "producer.py").write_text("# changed\n")
    subprocess.run(["git", "add", "src/producer.py"], cwd=tmp_path, check=True)
    result = runner.invoke(app, ["hunt", str(tmp_path / "thoughts")])
    assert result.exit_code == 1
    assert "revenue.md" in result.stderr
    assert "src/producer.py" in result.stderr
    assert "1 alert (strict mode)" in result.stderr


def test_hunt_base_flag(runner: CliRunner, tmp_path: pathlib.Path) -> None:
    """--base REF switches from --cached to commit-range diff."""
    import subprocess

    _init_repo_with_hunt_fixture(tmp_path)
    # Commit a change to the tracked source.
    (tmp_path / "src" / "producer.py").write_text("# changed\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "update producer"],
        cwd=tmp_path,
        check=True,
    )
    result = runner.invoke(app, ["hunt", str(tmp_path / "thoughts"), "--base", "HEAD~1"])
    assert result.exit_code == 1
    assert "revenue.md" in result.stderr


def test_hunt_forgiving_suppresses_when_doc_also_staged(runner: CliRunner, tmp_path: pathlib.Path) -> None:
    import subprocess

    _init_repo_with_hunt_fixture(tmp_path)
    (tmp_path / "src" / "producer.py").write_text("# changed\n")
    (tmp_path / "thoughts" / "revenue.md").write_text(
        "---\nid: revenue\ntitle: Revenue\nkind: leaf\nlinks: []\n"
        "tracks:\n- src/producer.py\n---\n\n# Revenue\n\nrefreshed\n"
    )
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    # Default strict still alerts.
    strict_result = runner.invoke(app, ["hunt", str(tmp_path / "thoughts")])
    assert strict_result.exit_code == 1
    # Forgiving suppresses.
    forgiving_result = runner.invoke(app, ["hunt", str(tmp_path / "thoughts"), "--forgiving"])
    assert forgiving_result.exit_code == 0, forgiving_result.output
    assert "hunt OK" in forgiving_result.stdout


def test_hunt_config_strict_false_overridable(runner: CliRunner, tmp_path: pathlib.Path) -> None:
    """`hunt.strict = false` in `.croc.toml` flips the default; `--strict`
    forces strict back on at the CLI."""
    import subprocess

    _init_repo_with_hunt_fixture(tmp_path)
    (tmp_path / "thoughts" / ".croc.toml").write_text('version = "0.1"\n\n[hunt]\nstrict = false\n')
    # Stage source + doc change both → forgiving should NOT alert.
    (tmp_path / "src" / "producer.py").write_text("# changed\n")
    (tmp_path / "thoughts" / "revenue.md").write_text(
        "---\nid: revenue\ntitle: Revenue\nkind: leaf\nlinks: []\n"
        "tracks:\n- src/producer.py\n---\n\n# Revenue\n\nrefreshed\n"
    )
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    # Default reads config → forgiving → no alert.
    default_result = runner.invoke(app, ["hunt", str(tmp_path / "thoughts")])
    assert default_result.exit_code == 0, default_result.output
    # --strict overrides config → alert.
    strict_result = runner.invoke(app, ["hunt", str(tmp_path / "thoughts"), "--strict"])
    assert strict_result.exit_code == 1


def test_hunt_outside_git_repo_fails(runner: CliRunner, tmp_path: pathlib.Path) -> None:
    # Plain tmp_path, no git.
    (tmp_path / "thoughts").mkdir()
    (tmp_path / "thoughts" / "doc.md").write_text("# doc\n")
    result = runner.invoke(app, ["hunt", str(tmp_path / "thoughts")])
    assert result.exit_code == 1
    assert "requires a git repo" in result.stderr
