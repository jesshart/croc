"""croc CLI — Typer entrypoint."""

from __future__ import annotations

import json
import pathlib
import sys

import typer

from croc.check import TreeError, build_index, check, load_tree, scan_symlinks
from croc.ops import (
    OpError,
    adopt_tree,
    init_tree,
    move_file,
    rename_id,
    scan_path_refs,
)

app = typer.Typer(
    name="croc",
    help="A Rust-inspired CLI for reliably managing project documentation.",
    no_args_is_help=True,
)


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
        raise typer.Exit(code=2)

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
        raise typer.Exit(code=2)
    typer.echo(json.dumps(build_index(docs), indent=2, sort_keys=True))


@app.command("move")
def move_cmd(
    src: pathlib.Path = typer.Argument(..., help="Source file."),
    dst: pathlib.Path = typer.Argument(..., help="Destination file or directory."),
    root: pathlib.Path = typer.Option(
        pathlib.Path("."), "--root", "-r", help="Tree root."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Run all checks; do not move."
    ),
) -> None:
    """Relocate a file on disk. IDs stay valid — no references rewritten."""
    try:
        final_dst = move_file(root, src, dst, dry_run=dry_run)
    except OpError as e:
        typer.echo(f"move FAILED: {e}", err=True)
        raise typer.Exit(code=1)
    prefix = "would move" if dry_run else "moved"
    typer.echo(f"{prefix}: {src} -> {final_dst.relative_to(root.resolve())}")


@app.command("rename")
def rename_cmd(
    old_id: str = typer.Argument(..., help="Current id."),
    new_id: str = typer.Argument(..., help="New id."),
    root: pathlib.Path = typer.Option(
        pathlib.Path("."), "--root", "-r", help="Tree root."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Run validation + simulation; do not write."
    ),
) -> None:
    """Rename an id. Every strong and weak reference is rewritten atomically."""
    try:
        changed = rename_id(root, old_id, new_id, dry_run=dry_run)
    except OpError as e:
        typer.echo(f"rename FAILED: {e}", err=True)
        raise typer.Exit(code=1)
    verb = "would rename" if dry_run else "renamed"
    typer.echo(f"{verb}: {old_id} -> {new_id} ({len(changed)} files)")
    for path in changed:
        typer.echo(f"  {path}")


@app.command("init")
def init_cmd(
    path: pathlib.Path = typer.Argument(
        pathlib.Path("."), help="Tree root to initialize."
    ),
    adopt: bool = typer.Option(
        False, "--adopt", help="Scaffold frontmatter into every .md that lacks it."
    ),
    migrate_refs: bool = typer.Option(
        True, "--migrate-refs/--no-migrate-refs",
        help=(
            "During --adopt, rewrite markdown path-refs to the croc "
            "[[id:X]] dialect. On by default; pass --no-migrate-refs to "
            "adopt only frontmatter shape and leave body content alone."
        ),
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Preview actions; do not write."
    ),
) -> None:
    """Initialize a croc tree. Optionally scaffold missing frontmatter."""
    path = path.resolve()
    marker = path / ".croc.toml"

    if marker.exists() and not adopt:
        typer.echo(
            f"init FAILED: {marker} already exists; "
            f"use --adopt to scaffold missing frontmatter",
            err=True,
        )
        raise typer.Exit(code=1)

    actions: list[str] = []
    try:
        actions += init_tree(path, dry_run=dry_run)
        if adopt:
            actions += adopt_tree(
                path, dry_run=dry_run, migrate_refs=migrate_refs
            )
    except OpError as e:
        typer.echo(f"init FAILED: {e}", err=True)
        raise typer.Exit(code=1)

    for a in actions:
        prefix = "would " if dry_run else ""
        typer.echo(f"{prefix}{a}")

    n = len(actions)
    plural = "" if n == 1 else "s"
    if dry_run:
        typer.echo(f"(dry-run: {n} action{plural}; nothing written)")
    else:
        typer.echo(f"init OK ({n} action{plural})")


@app.command("refs")
def refs_cmd(
    root: pathlib.Path = typer.Argument(
        pathlib.Path("."), help="Tree root."
    ),
    unresolved_only: bool = typer.Option(
        False, "--unresolved",
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
        raise typer.Exit(code=2)

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
        typer.echo(
            f"\n{unresolved_count} unresolved ref(s) across the tree", err=True
        )
        raise typer.Exit(code=1)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
