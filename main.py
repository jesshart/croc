"""croc CLI — Typer entrypoint."""

from __future__ import annotations

import json
import pathlib

import typer

from croc.attack import attack_tree
from croc.check import TreeError, build_index, check, load_tree, scan_symlinks
from croc.config import ConfigError, load_config
from croc.crawl import _plan_crawl_with_stats, apply_plan, resolve_file_filter
from croc.hunt import hunt_tree
from croc.lurk import lurk_tree
from croc.ops import (
    OpError,
    adopt_tree,
    init_tree,
    molt_tree,
    move_file,
    rename_id,
    scan_path_refs,
)

app = typer.Typer(
    name="croc",
    help="A Rust-inspired CLI for reliably managing project documentation.",
    no_args_is_help=True,
)


@app.callback()
def _root(
    ctx: typer.Context,
    include_untracked: bool = typer.Option(
        False,
        "--include-untracked/--no-include-untracked",
        help=(
            "Include untracked (but not gitignored) files in the tree "
            "walk. Default is tracked-only — commands like `check`, "
            "`refs`, `init --adopt`, `molt`, `rename`, `move`, and "
            "`crawl` skip in-progress drafts. Pass --include-untracked "
            "to fold them in. Outside a git repo, every file is walked "
            "regardless. Flag name mirrors `git stash --include-untracked`."
        ),
    ),
) -> None:
    ctx.ensure_object(dict)
    ctx.obj["include_untracked"] = include_untracked


def _file_filter_for(
    ctx: typer.Context,
    root: pathlib.Path,
) -> set[pathlib.Path] | None:
    """Resolve the git-backed file filter for this invocation, using
    the flag stashed on `ctx.obj` by the root callback. Returns `None`
    when `root` is not in a git repo — callers pass `None` straight
    through to the ops layer, which treats it as 'walk everything.'
    """
    include_untracked = bool((ctx.obj or {}).get("include_untracked", False))
    return resolve_file_filter(root, include_untracked=include_untracked)


def _is_skip_action(action: str) -> bool:
    """Skip-class actions report a problem but do not write anything.

    SKIP (malformed frontmatter), SKIP-REF (unresolvable body path-ref),
    SKIP-MOLT-REF (weak ref to missing target) all share this property.
    They deserve separate accounting in the summary and need visual
    emphasis so they don't scroll past a user's attention.
    """
    return action.startswith("SKIP")


def _render_actions(
    actions: list[str],
    *,
    dry_run: bool,
    verb_label: str,
) -> int:
    """Print actions with SKIP lines highlighted, separate writes from
    skips in the summary, and re-echo any skips after the summary so
    they survive a wall of successful actions.

    Returns the number of skip-class notes so callers can honor
    `--strict-refs` with a non-zero exit.
    """
    skips = [a for a in actions if _is_skip_action(a)]
    writes = [a for a in actions if not _is_skip_action(a)]

    prefix = "would " if dry_run else ""
    for a in actions:
        if _is_skip_action(a):
            typer.secho(f"{prefix}{a}", fg=typer.colors.YELLOW)
        else:
            typer.echo(f"{prefix}{a}")

    n_writes = len(writes)
    n_skips = len(skips)
    wp = "" if n_writes == 1 else "s"
    sp = "" if n_skips == 1 else "s"

    if dry_run:
        summary = f"(dry-run: {n_writes} action{wp}"
        if n_skips:
            summary += f", {n_skips} skipped ref{sp}"
        summary += "; nothing written)"
    else:
        summary = f"{verb_label} OK ({n_writes} action{wp}"
        if n_skips:
            summary += f", {n_skips} skipped ref{sp}"
        summary += ")"
    typer.echo(summary)

    # Re-echo skips on stderr in yellow so they're the last thing the user
    # sees. stderr keeps pipes clean; the yellow + header breaks the wall
    # of AUGMENT/MOLT/etc. that would otherwise bury them.
    if skips:
        typer.secho("", err=True)
        typer.secho(
            f"Unresolved ref{sp} ({n_skips}) — left for manual cleanup:",
            err=True,
            fg=typer.colors.YELLOW,
            bold=True,
        )
        for s in skips:
            typer.secho(f"  {s}", err=True, fg=typer.colors.YELLOW)

    return n_skips


@app.command("check")
def check_cmd(
    ctx: typer.Context,
    root: pathlib.Path = typer.Argument(
        pathlib.Path("thoughts"),
        help="Root of the documentation tree.",
    ),
) -> None:
    """Run the borrow checker against a doc tree."""
    git_files = _file_filter_for(ctx, root)
    try:
        docs = load_tree(root, git_files=git_files)
    except TreeError as e:
        typer.echo(f"borrow check FAILED:\n  {e}", err=True)
        raise typer.Exit(code=2) from e

    for w in scan_symlinks(root, git_files=git_files):
        typer.echo(w, err=True)

    if not docs:
        typer.echo(f"warning: no .md files found under {root}", err=True)

    errors = check(docs)
    if errors:
        typer.echo("borrow check FAILED:\n", err=True)
        for e in errors:
            typer.echo(f"  {e}", err=True)
        raise typer.Exit(code=1)
    typer.echo("borrow check OK")


@app.command("index")
def index_cmd(
    ctx: typer.Context,
    root: pathlib.Path = typer.Argument(
        pathlib.Path("thoughts"),
        help="Root of the documentation tree.",
    ),
) -> None:
    """Print the derived id → path index as JSON."""
    git_files = _file_filter_for(ctx, root)
    try:
        docs = load_tree(root, git_files=git_files)
    except TreeError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=2) from e
    typer.echo(json.dumps(build_index(docs), indent=2, sort_keys=True))


@app.command("move")
def move_cmd(
    ctx: typer.Context,
    src: pathlib.Path = typer.Argument(..., help="Source file."),
    dst: pathlib.Path = typer.Argument(..., help="Destination file or directory."),
    root: pathlib.Path = typer.Option(pathlib.Path("."), "--root", "-r", help="Tree root."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Run all checks; do not move."),
) -> None:
    """Relocate a file on disk. IDs stay valid — no references rewritten."""
    git_files = _file_filter_for(ctx, root)
    try:
        final_dst = move_file(root, src, dst, dry_run=dry_run, git_files=git_files)
    except OpError as e:
        typer.echo(f"move FAILED: {e}", err=True)
        raise typer.Exit(code=1) from e
    prefix = "would move" if dry_run else "moved"
    typer.echo(f"{prefix}: {src} -> {final_dst.relative_to(root.resolve())}")


@app.command("rename")
def rename_cmd(
    ctx: typer.Context,
    old_id: str = typer.Argument(..., help="Current id."),
    new_id: str = typer.Argument(..., help="New id."),
    root: pathlib.Path = typer.Option(pathlib.Path("."), "--root", "-r", help="Tree root."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Run validation + simulation; do not write."),
) -> None:
    """Rename an id. Every strong and weak reference is rewritten atomically."""
    git_files = _file_filter_for(ctx, root)
    try:
        changed = rename_id(root, old_id, new_id, dry_run=dry_run, git_files=git_files)
    except OpError as e:
        typer.echo(f"rename FAILED: {e}", err=True)
        raise typer.Exit(code=1) from e
    verb = "would rename" if dry_run else "renamed"
    typer.echo(f"{verb}: {old_id} -> {new_id} ({len(changed)} files)")
    for path in changed:
        typer.echo(f"  {path}")


@app.command("init")
def init_cmd(
    ctx: typer.Context,
    path: pathlib.Path = typer.Argument(pathlib.Path("."), help="Tree root to initialize."),
    adopt: bool = typer.Option(False, "--adopt", help="Scaffold frontmatter into every .md that lacks it."),
    migrate_refs: bool = typer.Option(
        True,
        "--migrate-refs/--no-migrate-refs",
        help=(
            "During --adopt, rewrite markdown path-refs to the croc "
            "[[id:X]] dialect. On by default; pass --no-migrate-refs to "
            "adopt only frontmatter shape and leave body content alone."
        ),
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview actions; do not write."),
    strict_refs: bool = typer.Option(
        False,
        "--strict-refs",
        help=(
            "Exit non-zero if any SKIP / SKIP-REF notes were emitted. "
            "Use in CI / pre-commit so unresolvable refs are not silently "
            "ignored."
        ),
    ),
) -> None:
    """Initialize a croc tree. Optionally scaffold missing frontmatter."""
    path = path.resolve()
    marker = path / ".croc.toml"

    if marker.exists() and not adopt:
        typer.echo(
            f"init FAILED: {marker} already exists; use --adopt to scaffold missing frontmatter",
            err=True,
        )
        raise typer.Exit(code=1)

    git_files = _file_filter_for(ctx, path)
    actions: list[str] = []
    try:
        actions += init_tree(path, dry_run=dry_run)
        if adopt:
            actions += adopt_tree(path, dry_run=dry_run, migrate_refs=migrate_refs, git_files=git_files)
    except OpError as e:
        typer.echo(f"init FAILED: {e}", err=True)
        raise typer.Exit(code=1) from e

    n_skips = _render_actions(actions, dry_run=dry_run, verb_label="init")
    if strict_refs and n_skips:
        raise typer.Exit(code=1)


@app.command("crawl")
def crawl_cmd(
    ctx: typer.Context,
    src: pathlib.Path = typer.Argument(..., help="Source directory to mirror."),
    output: pathlib.Path = typer.Option(
        None,
        "--output",
        "-o",
        help="Output directory. Defaults to ./thoughts/<src-name>/.",
    ),
    file_types: list[str] = typer.Option(
        ["all"],
        "--file-types",
        help=(
            "Narrow to specific extensions (repeat for multiple, e.g. "
            "--file-types .py --file-types .ts). Default mirrors every "
            "file git tracks; dot-dirs and __pycache__ are always pruned."
        ),
    ),
    adopt: bool = typer.Option(
        False,
        "--adopt",
        help="After crawling, run init + adopt on the output so the tree is croc-checkable.",
    ),
    migrate_refs: bool = typer.Option(
        True,
        "--migrate-refs/--no-migrate-refs",
        help="With --adopt, passed through to adoption. No effect otherwise.",
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite existing files."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview actions; do not write."),
    strict_refs: bool = typer.Option(
        False,
        "--strict-refs",
        help=("With --adopt: exit nonzero if the adopt phase emitted SKIP notes. Has no effect without --adopt."),
    ),
) -> None:
    """Scaffold a plain-markdown doc tree from a source directory.

    Emits plain markdown with only a `mirrors:` breadcrumb in
    frontmatter — no id, no links. That keeps crawl output shape-
    compatible with croc's post-molt state, so `adopt` / `molt`
    round-trips cleanly. Pass `--adopt` to fold in `init --adopt`
    and get a croc-checkable tree in one shot.
    """
    if not src.exists() or not src.is_dir():
        typer.echo(f"crawl FAILED: {src}: not a directory", err=True)
        raise typer.Exit(code=1)

    if output is None:
        output = pathlib.Path("thoughts") / src.name

    git_files = _file_filter_for(ctx, src)
    if git_files is not None:
        include_untracked = bool((ctx.obj or {}).get("include_untracked", False))
        mode = "including untracked drafts" if include_untracked else "tracked files only"
        typer.secho(
            f"respecting .gitignore (git repo detected; {mode})",
            err=True,
            fg=typer.colors.CYAN,
        )

    planned, n_disambiguated = _plan_crawl_with_stats(src, output, file_types=file_types, git_files=git_files)

    if not planned:
        typer.echo(f"crawl FAILED: no matching files found under {src}", err=True)
        raise typer.Exit(code=1)

    if n_disambiguated:
        # Silent disambiguation was the pre-fix data-loss bug's blast
        # radius; surface it so the user notices their output names
        # diverged from the default `<stem>.md` form.
        typer.secho(
            f"note: {n_disambiguated} filename collision(s) disambiguated (full filenames used)",
            err=True,
            fg=typer.colors.CYAN,
        )

    # Split the plan into would-create vs would-keep. Existing files
    # are summarized on stderr (not fed through _render_actions as
    # SKIP lines — they're intentional, not unresolvable).
    to_create: list[tuple[pathlib.Path, str]] = []
    n_existing = 0
    actions: list[str] = []
    cwd = pathlib.Path.cwd()
    for out_path, content in planned:
        try:
            display = out_path.relative_to(cwd)
        except ValueError:
            display = out_path
        if out_path.exists() and not force:
            n_existing += 1
            continue
        to_create.append((out_path, content))
        actions.append(f"CREATE {display}")

    if not dry_run and to_create:
        apply_plan(to_create, force=True)  # force=True because we filtered already

    if adopt:
        if dry_run:
            # Files aren't on disk yet; adopt needs them to analyze
            # frontmatter and collision-check ids. Surface the gap
            # instead of silently skipping.
            typer.secho(
                "note: --adopt actions not previewed in --dry-run; "
                f"run `croc init --adopt --dry-run {output}` after commit to see them",
                err=True,
                fg=typer.colors.CYAN,
            )
        else:
            try:
                actions += init_tree(output, dry_run=False)
                actions += adopt_tree(output, dry_run=False, migrate_refs=migrate_refs)
            except OpError as e:
                typer.echo(f"crawl adopt FAILED: {e}", err=True)
                raise typer.Exit(code=1) from e

    if n_existing:
        typer.secho(
            f"note: {n_existing} existing file(s) kept (pass --force to overwrite)",
            err=True,
            fg=typer.colors.YELLOW,
        )

    n_skips = _render_actions(actions, dry_run=dry_run, verb_label="crawl")
    if strict_refs and adopt and n_skips:
        raise typer.Exit(code=1)


@app.command("molt")
def molt_cmd(
    ctx: typer.Context,
    root: pathlib.Path = typer.Argument(pathlib.Path("."), help="Tree root."),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Preview actions; do not write.",
    ),
    strict_refs: bool = typer.Option(
        False,
        "--strict-refs",
        help=(
            "Exit non-zero if any SKIP-MOLT-REF notes were emitted. "
            "Use in CI so dangling weak refs do not silently survive "
            "a molt."
        ),
    ),
) -> None:
    """Reverse croc adoption.

    Rewrites id-based body refs back to plain markdown links, strips
    croc-specific frontmatter fields, and removes `.croc.toml`. The
    tree must pass `croc check` first. See README for the full syntax
    mapping.
    """
    git_files = _file_filter_for(ctx, root)
    try:
        actions = molt_tree(root, dry_run=dry_run, git_files=git_files)
    except OpError as e:
        typer.echo(f"molt FAILED: {e}", err=True)
        raise typer.Exit(code=1) from e

    n_skips = _render_actions(actions, dry_run=dry_run, verb_label="molt")
    if strict_refs and n_skips:
        raise typer.Exit(code=1)


@app.command("refs")
def refs_cmd(
    ctx: typer.Context,
    root: pathlib.Path = typer.Argument(pathlib.Path("."), help="Tree root."),
    unresolved_only: bool = typer.Option(
        False,
        "--unresolved",
        help="Report only path-refs that don't resolve to a doc in the tree.",
    ),
) -> None:
    """Report markdown-style path refs in the tree and whether they resolve.

    Works on any markdown tree, including pre-adoption ones. Useful as a
    health check before `init --adopt --migrate-refs`: anything printed
    as UNRESOLVED will become a SKIP-REF note during migration.
    """
    git_files = _file_filter_for(ctx, root)
    try:
        reports = scan_path_refs(root, git_files=git_files)
    except OpError as e:
        typer.echo(f"refs FAILED: {e}", err=True)
        raise typer.Exit(code=2) from e

    unresolved_count = 0
    for r in reports:
        if unresolved_only and r.resolved:
            continue
        status = "OK        " if r.resolved else "UNRESOLVED"
        target_display = r.target if r.target else r.raw_path
        note = f" (note: {r.note})" if r.note else ""
        typer.echo(f"{status} {r.source}: -> {target_display}{note}")
        if not r.resolved:
            unresolved_count += 1

    if unresolved_count:
        typer.echo(f"\n{unresolved_count} unresolved ref(s) across the tree", err=True)
        raise typer.Exit(code=1)


@app.command("lurk")
def lurk_cmd(
    ctx: typer.Context,
    root: pathlib.Path = typer.Argument(
        pathlib.Path("thoughts"),
        help="Root of the documentation tree.",
    ),
    max_lines: int = typer.Option(
        100,
        "-n",
        "--max-lines",
        help="Reject any `.md` file whose line count exceeds this. Default 100.",
    ),
    include_frontmatter: bool = typer.Option(
        False,
        "--include-frontmatter/--no-include-frontmatter",
        help=(
            "Count YAML frontmatter toward the line budget. Default "
            "excludes frontmatter so a doc isn't penalized for a rich "
            "`links:` block."
        ),
    ),
) -> None:
    """Report `.md` files exceeding a per-file line-count budget."""
    git_files = _file_filter_for(ctx, root)
    violations = lurk_tree(
        root,
        max_lines=max_lines,
        include_frontmatter=include_frontmatter,
        git_files=git_files,
    )
    for v in violations:
        over_by = v.line_count - v.limit
        typer.secho(
            f"{v.path}: {v.line_count} lines (over by {over_by})",
            err=True,
            fg=typer.colors.YELLOW,
        )
    n = len(violations)
    plural = "" if n == 1 else "s"
    if n:
        typer.echo(f"\n{n} file{plural} exceed {max_lines} lines", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"lurk OK (0 file{plural} exceed {max_lines} lines)")


@app.command("attack")
def attack_cmd(
    ctx: typer.Context,
    root: pathlib.Path = typer.Argument(
        pathlib.Path("thoughts"),
        help="Root of the documentation tree.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview actions; do not write."),
    strict_traces: bool = typer.Option(
        False,
        "--strict-traces",
        help=(
            "Exit non-zero if any SKIP-TRACE notes were emitted (unresolved or "
            "ambiguous captures). Use in CI so bad config / missing docs do not "
            "silently slip through."
        ),
    ),
) -> None:
    """Scan code per `.croc.toml` `[[trace]]` patterns; write `tracks:` to docs."""
    # Attack's file filter must cover the whole repo — code lives above
    # the tree root — so we pass `include_untracked` through and let
    # attack_tree derive the filter at the git repo root itself.
    include_untracked = bool((ctx.obj or {}).get("include_untracked", False))
    try:
        config = load_config(root)
        actions = attack_tree(root, config, dry_run=dry_run, include_untracked=include_untracked)
    except (ConfigError, OpError) as e:
        typer.echo(f"attack FAILED: {e}", err=True)
        raise typer.Exit(code=1) from e

    n_skips = _render_actions(actions, dry_run=dry_run, verb_label="attack")
    if strict_traces and n_skips:
        raise typer.Exit(code=1)


@app.command("hunt")
def hunt_cmd(
    ctx: typer.Context,
    root: pathlib.Path = typer.Argument(
        pathlib.Path("thoughts"),
        help="Root of the documentation tree.",
    ),
    base: str = typer.Option(
        None,
        "--base",
        help=(
            "Git ref to diff against (`<base>...HEAD`). Default is staged "
            "changes (`git diff --cached`). Use `--base main` for CI."
        ),
    ),
    forgiving: bool = typer.Option(
        None,
        "--forgiving/--strict",
        help=(
            "Override `.croc.toml` `hunt.strict`. `--strict` (default in "
            "config) alerts on any tracked source change. `--forgiving` "
            "suppresses the alert when the bound doc is also in the diff."
        ),
    ),
) -> None:
    """Alert when docs' bound source files changed without the docs.

    Intended as a pre-commit hook or CI gate. Exits 1 if any alerts fire.
    """
    git_files = _file_filter_for(ctx, root)
    try:
        config = load_config(root)
    except ConfigError as e:
        typer.echo(f"hunt FAILED: {e}", err=True)
        raise typer.Exit(code=1) from e

    # CLI flag (if given) overrides config default. Typer parses the
    # three-state flag as bool | None — None means "use the config".
    strict = config.hunt.strict if forgiving is None else (not forgiving)

    try:
        alerts = hunt_tree(root, base=base, strict=strict, git_files=git_files)
    except OpError as e:
        typer.echo(f"hunt FAILED: {e}", err=True)
        raise typer.Exit(code=1) from e

    for a in alerts:
        typer.secho(
            f"{a.doc_rel} tracks changed file {a.source_rel}",
            err=True,
            fg=typer.colors.YELLOW,
        )
    n = len(alerts)
    plural = "" if n == 1 else "s"
    if n:
        mode = "strict" if strict else "forgiving"
        typer.echo(f"\n{n} alert{plural} ({mode} mode)", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"hunt OK (0 alert{plural})")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
