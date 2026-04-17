"""croc CLI — Typer entrypoint."""

from __future__ import annotations

import json
import pathlib
import sys

import typer

from croc.check import TreeError, build_index, check, load_tree, scan_symlinks
from croc.ops import OpError, move_file, rename_id

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
) -> None:
    """Relocate a file on disk. IDs stay valid — no references rewritten."""
    try:
        final_dst = move_file(root, src, dst)
    except OpError as e:
        typer.echo(f"move FAILED: {e}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"moved: {src} -> {final_dst.relative_to(root.resolve())}")


@app.command("rename")
def rename_cmd(
    old_id: str = typer.Argument(..., help="Current id."),
    new_id: str = typer.Argument(..., help="New id."),
    root: pathlib.Path = typer.Option(
        pathlib.Path("."), "--root", "-r", help="Tree root."
    ),
) -> None:
    """Rename an id. Every strong and weak reference is rewritten atomically."""
    try:
        changed = rename_id(root, old_id, new_id)
    except OpError as e:
        typer.echo(f"rename FAILED: {e}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"renamed: {old_id} -> {new_id} ({len(changed)} files updated)")
    for path in changed:
        typer.echo(f"  {path}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
