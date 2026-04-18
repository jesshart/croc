"""croc CLI — Typer entrypoint."""

from __future__ import annotations

import json
import pathlib

import typer

from croc.check import TreeError, build_index, check, load_tree, scan_symlinks
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
    root: pathlib.Path = typer.Argument(
        pathlib.Path("thoughts"),
        help="Root of the documentation tree.",
    ),
) -> None:
    """Run the borrow checker against a doc tree."""
    try:
        docs = load_tree(root)
    except TreeError as e:
        typer.echo(f"borrow check FAILED:\n  {e}", err=True)
        raise typer.Exit(code=2) from e

    for w in scan_symlinks(root):
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
    root: pathlib.Path = typer.Argument(
        pathlib.Path("thoughts"),
        help="Root of the documentation tree.",
    ),
) -> None:
    """Print the derived id → path index as JSON."""
    try:
        docs = load_tree(root)
    except TreeError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=2) from e
    typer.echo(json.dumps(build_index(docs), indent=2, sort_keys=True))


@app.command("move")
def move_cmd(
    src: pathlib.Path = typer.Argument(..., help="Source file."),
    dst: pathlib.Path = typer.Argument(..., help="Destination file or directory."),
    root: pathlib.Path = typer.Option(pathlib.Path("."), "--root", "-r", help="Tree root."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Run all checks; do not move."),
) -> None:
    """Relocate a file on disk. IDs stay valid — no references rewritten."""
    try:
        final_dst = move_file(root, src, dst, dry_run=dry_run)
    except OpError as e:
        typer.echo(f"move FAILED: {e}", err=True)
        raise typer.Exit(code=1) from e
    prefix = "would move" if dry_run else "moved"
    typer.echo(f"{prefix}: {src} -> {final_dst.relative_to(root.resolve())}")


@app.command("rename")
def rename_cmd(
    old_id: str = typer.Argument(..., help="Current id."),
    new_id: str = typer.Argument(..., help="New id."),
    root: pathlib.Path = typer.Option(pathlib.Path("."), "--root", "-r", help="Tree root."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Run validation + simulation; do not write."),
) -> None:
    """Rename an id. Every strong and weak reference is rewritten atomically."""
    try:
        changed = rename_id(root, old_id, new_id, dry_run=dry_run)
    except OpError as e:
        typer.echo(f"rename FAILED: {e}", err=True)
        raise typer.Exit(code=1) from e
    verb = "would rename" if dry_run else "renamed"
    typer.echo(f"{verb}: {old_id} -> {new_id} ({len(changed)} files)")
    for path in changed:
        typer.echo(f"  {path}")


@app.command("init")
def init_cmd(
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

    actions: list[str] = []
    try:
        actions += init_tree(path, dry_run=dry_run)
        if adopt:
            actions += adopt_tree(path, dry_run=dry_run, migrate_refs=migrate_refs)
    except OpError as e:
        typer.echo(f"init FAILED: {e}", err=True)
        raise typer.Exit(code=1) from e

    n_skips = _render_actions(actions, dry_run=dry_run, verb_label="init")
    if strict_refs and n_skips:
        raise typer.Exit(code=1)


@app.command("molt")
def molt_cmd(
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
    try:
        actions = molt_tree(root, dry_run=dry_run)
    except OpError as e:
        typer.echo(f"molt FAILED: {e}", err=True)
        raise typer.Exit(code=1) from e

    n_skips = _render_actions(actions, dry_run=dry_run, verb_label="molt")
    if strict_refs and n_skips:
        raise typer.Exit(code=1)


@app.command("refs")
def refs_cmd(
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
    try:
        reports = scan_path_refs(root)
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


def main() -> None:
    app()


if __name__ == "__main__":
    main()
