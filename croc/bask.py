"""Flatten a markdown tree into a single directory.

`bask` walks a markdown tree, copies every `.md` file into a single
output directory, and encodes the original path into the filename via
`__` (dunder) joiners. Non-md files are ignored.

Markdown path-refs (`[text](path.md)`) inside bodies are rewritten to
point at the flattened siblings by default — the export is usable in
path-aware tools out of the box. Pass `rewrite_refs=False`
(`--no-rewrite-refs` on the CLI) to keep bodies byte-for-byte.

Croc id-refs (`[[id:X]]`) are not touched — they survive the
flattening unchanged because ids are stable.

Intended for one-way export — the artifact feeds tools that don't
traverse directories. There is no inverse operation.
"""

from __future__ import annotations

import pathlib
import re

from croc.check import in_any_span, scannable_spans
from croc.ops import MD_PATH_REF, _case_mismatch_ext


class BaskError(Exception):
    """Raised when the input cannot be flattened (embedded `__` in a
    path segment, or two source paths flatten to the same output)."""


def flatten_name(rel: pathlib.Path) -> str:
    """Join the parts of a tree-relative path with `__`.

    `thoughts/modules/a.md` (relative) → `thoughts__modules__a.md`.
    The leading directory IS included so the output is unambiguous —
    callers compute the relative path against the *parent* of the
    bask root, not the root itself. (See `plan_bask`.)
    """
    return "__".join(rel.parts)


def plan_bask(
    root: pathlib.Path,
    output: pathlib.Path,
    *,
    git_files: set[pathlib.Path] | None = None,
    rewrite_refs: bool = True,
) -> tuple[list[tuple[pathlib.Path, str]], list[str]]:
    """Return (plan, skip_notes) — no side effects.

    `plan` is a list of (out_path, content) pairs ready for
    `apply_plan`. `skip_notes` is a list of `SKIP-REF …` strings for
    any path-refs that could not be rewritten (target outside root,
    target not in the tree, non-lowercase `.md` extension). Library
    callers can ignore the second item; the CLI surfaces it through
    `_render_actions` so users see what didn't get rewritten.

    Args:
        root: Markdown tree root to flatten.
        output: Output directory. Created on apply if missing.
        git_files: When provided, only `.md` files in the set are
            considered (respects `.gitignore`). `None` means walk
            everything under `root`.
        rewrite_refs: When True (the default), markdown path-refs
            (`[text](path.md)`) in each file's body are rewritten to
            point at the flattened siblings. When False, bodies pass
            through verbatim.

    Output filenames are computed against `root`'s *parent*, so the
    root's own name is the leading segment in every output filename.
    For `bask thoughts/`, every output file starts with `thoughts__`.

    Raises:
        BaskError: when a directory or filename in the input tree
            already contains the `__` joiner sequence (would produce
            ambiguous output that can't be inverted by inspection),
            or when two source paths flatten to the same output
            filename. Both surface as a single error listing every
            offender so the user fixes them in one pass.
    """
    root = root.resolve()
    if not root.is_dir():
        raise BaskError(f"{root}: not a directory")

    sources = _walk_md(root, git_files)

    # Embedded-dunder rejection. Surface every offender at once so the
    # user fixes them in one pass.
    embedded: list[pathlib.Path] = []
    for src in sources:
        rel = src.relative_to(root.parent)
        if any("__" in part for part in rel.parts):
            embedded.append(rel)
    if embedded:
        listing = "\n  ".join(str(p) for p in embedded)
        raise BaskError(
            "input contains `__` (the bask joiner) inside a path segment; "
            "rename the offenders before running bask:\n  " + listing
        )

    # Build the source→flatname map (used both for collision detection
    # and as the path-ref rewrite target lookup).
    src_to_flatname: dict[pathlib.Path, str] = {}
    flatname_to_sources: dict[str, list[pathlib.Path]] = {}
    for src in sources:
        rel = src.relative_to(root.parent)
        name = flatten_name(rel)
        src_to_flatname[src] = name
        flatname_to_sources.setdefault(name, []).append(src)

    collisions = [(name, srcs) for name, srcs in flatname_to_sources.items() if len(srcs) > 1]
    if collisions:
        lines: list[str] = []
        for name, srcs in collisions:
            rels = ", ".join(str(s.relative_to(root.parent)) for s in srcs)
            lines.append(f"{name} ← {rels}")
        listing = "\n  ".join(lines)
        raise BaskError("output filename collisions detected:\n  " + listing)

    plan: list[tuple[pathlib.Path, str]] = []
    skip_notes: list[str] = []
    for src in sources:
        body = src.read_text()
        if rewrite_refs:
            new_body, notes = _rewrite_path_refs(body, src, root, src_to_flatname)
            skip_notes.extend(notes)
        else:
            new_body = body
        plan.append((output / src_to_flatname[src], new_body))

    return plan, skip_notes


def _walk_md(
    root: pathlib.Path,
    git_files: set[pathlib.Path] | None,
) -> list[pathlib.Path]:
    """Sorted absolute paths of `.md` files under `root`, filtered."""
    out: list[pathlib.Path] = []
    for p in sorted(root.rglob("*.md")):
        if git_files is not None and p.resolve() not in git_files:
            continue
        out.append(p.resolve())
    return out


def _rewrite_path_refs(
    body: str,
    source_abs: pathlib.Path,
    root: pathlib.Path,
    src_to_flatname: dict[pathlib.Path, str],
) -> tuple[str, list[str]]:
    """Rewrite `[text](path.md)` refs to bare flattened filenames.

    Returns (new_body, skip_notes). `skip_notes` items are
    `SKIP-REF <source>: <reason>` strings — same shape as
    `_migrate_refs_in_body`'s notes so `_render_actions` highlights
    them consistently.

    Skips refs inside fenced code, inline code, and `\\[`-escaped
    brackets via `scannable_spans` / `in_any_span`. Anchor fragments
    (`#section`) are preserved on rewrite. Refs to targets outside
    the bask root, refs that don't resolve to a tracked `.md` in
    `src_to_flatname`, and non-lowercase `.md` extensions all
    surface as `SKIP-REF` notes — the original ref is left in place.
    Croc id-refs (`[[id:X]]`) are not matched by `MD_PATH_REF` and
    pass through untouched by construction.
    """
    spans = scannable_spans(body)
    source_rel = source_abs.relative_to(root)
    notes: list[str] = []

    def replace(m: re.Match[str]) -> str:
        if in_any_span(m.start(), spans):
            return m.group(0)
        text = m.group("text")
        rel_path = m.group("path")
        anchor = m.group("anchor") or ""

        if _case_mismatch_ext(rel_path):
            notes.append(
                f"SKIP-REF {source_rel}: path ref {rel_path!r} uses non-lowercase "
                f"`.md` extension (croc recognizes `.md` only)"
            )
            return m.group(0)

        try:
            target_abs = (source_abs.parent / rel_path).resolve()
        except OSError as e:
            notes.append(f"SKIP-REF {source_rel}: path ref {rel_path!r} could not be resolved ({e})")
            return m.group(0)

        try:
            target_rel = target_abs.relative_to(root)
        except ValueError:
            notes.append(f"SKIP-REF {source_rel}: path ref {rel_path!r} escapes tree root (resolved to: {target_abs})")
            return m.group(0)

        if target_abs not in src_to_flatname:
            notes.append(
                f"SKIP-REF {source_rel}: path ref {rel_path!r} does not resolve to any "
                f"`.md` in the bask set (tried: {target_rel})"
            )
            return m.group(0)

        flat = src_to_flatname[target_abs]
        rewrite = f"[{text}]({flat}{anchor})"
        return rewrite

    return MD_PATH_REF.sub(replace, body), notes
