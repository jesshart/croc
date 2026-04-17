"""Borrow checker for a thoughts/ markdown tree.

Enforces five rules, analogous to Rust's compile-time guarantees:

  1. Ownership       — every .md file has a unique `id` in frontmatter.
  2. Schema          — frontmatter matches the declared shape.
  3. No dangling ref — every [[id:X]] in body text resolves.
  4. Lifetime bound  — strong links in frontmatter point to files that exist.
  5. Identity stable — ids declared in frontmatter match body references.

Weak links ([[see:X]]) are tolerated even when X is absent — that is the
whole point of `Weak<T>`: a reference that does not pin its target.

I/O (and its errors) live in `load_tree` and `parse_frontmatter`. The
`check` function is pure over already-parsed docs. Every way input can
be malformed becomes a `TreeError` (fatal, parse-time) or an `E-*`
diagnostic (non-fatal, check-time). Nothing throws a raw traceback.
"""

from __future__ import annotations

import pathlib
import re
from dataclasses import dataclass
from typing import NewType

import yaml

# Newtype pattern: paths and ids are not the same thing, and the type system
# should refuse to confuse them.
DocId = NewType("DocId", str)
DocPath = NewType("DocPath", str)

# One canonical ID grammar, used by both the parser (for frontmatter `id`)
# and the body-reference regexes. If the two disagree, a legal id in
# frontmatter can silently fail to match its own references.
ID_CHARS = r"[A-Za-z0-9_.-]+"
ID_RE = re.compile(rf"^{ID_CHARS}$")

# Body-reference dialect. Supports:
#   [[id:X]]
#   [[id:X#anchor]]
#   [[id:X|display text]]
#   [[id:X#anchor|display text]]
# Capturing groups: (1) id, (2) anchor or None, (3) display or None.
# The checker still depends only on group(1); the extra groups exist so
# `molt` can reconstruct faithful plain-markdown output without re-
# parsing the ref.
STRONG_REF = re.compile(
    rf"\[\[id:({ID_CHARS})(?:#([^|\]]+))?(?:\|([^\]]+))?\]\]"
)
WEAK_REF = re.compile(
    rf"\[\[see:({ID_CHARS})(?:#([^|\]]+))?(?:\|([^\]]+))?\]\]"
)


@dataclass
class Doc:
    path: DocPath
    id: DocId
    frontmatter: dict
    body: str


class TreeError(Exception):
    """Raised when the tree cannot even be loaded (e.g. missing frontmatter)."""


def parse_frontmatter(path: pathlib.Path, raw: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from a markdown file.

    Owns every way the parse can fail, so `load_tree` stays linear.
    Raises TreeError on any malformed input.
    """
    if not raw.startswith("---\n"):
        raise TreeError(f"{path}: missing frontmatter")
    parts = raw.split("---\n", 2)
    if len(parts) < 3:
        raise TreeError(f"{path}: unterminated frontmatter (missing closing `---`)")
    _, fm_text, body = parts

    try:
        fm = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError as e:
        raise TreeError(f"{path}: invalid YAML frontmatter: {e}") from e

    if not isinstance(fm, dict):
        raise TreeError(
            f"{path}: frontmatter must be a mapping, got {type(fm).__name__}"
        )
    if "id" not in fm:
        raise TreeError(f"{path}: no `id` in frontmatter")
    # NewType("DocId", str) must not lie about the runtime type. YAML will
    # parse `id: 12345` as int; force authors to quote ids they want treated
    # as stable tokens.
    if not isinstance(fm["id"], str):
        raise TreeError(
            f"{path}: `id` must be a string, got {type(fm['id']).__name__} "
            f"(quote numeric-looking ids in YAML: `id: \"{fm['id']}\"`)"
        )
    if not ID_RE.fullmatch(fm["id"]):
        raise TreeError(
            f"{path}: `id` {fm['id']!r} contains illegal characters "
            f"(allowed: letters, digits, `_`, `.`, `-`)"
        )
    return fm, body


def load_tree(root: pathlib.Path) -> list[Doc]:
    if not root.exists():
        raise TreeError(f"{root}: does not exist")
    if not root.is_dir():
        raise TreeError(f"{root}: not a directory")
    docs: list[Doc] = []
    for p in sorted(root.rglob("*.md")):
        fm, body = parse_frontmatter(p, p.read_text())
        docs.append(
            Doc(
                path=DocPath(str(p.relative_to(root))),
                id=DocId(fm["id"]),
                frontmatter=fm,
                body=body,
            )
        )
    return docs


def scan_symlinks(root: pathlib.Path) -> list[str]:
    """Return warnings for symlinks under `root` that rglob will not follow.

    Symlinks are never traversed (cycle risk), so a symlinked subtree is
    silently skipped. Surface that instead of hiding it.
    """
    if not root.is_dir():
        return []
    warnings: list[str] = []
    for p in sorted(root.rglob("*")):
        if p.is_symlink():
            warnings.append(
                f"warning: symlink {p.relative_to(root)} not traversed "
                f"(symlinks are never followed to avoid cycles)"
            )
    return warnings


def build_index(docs: list[Doc]) -> dict[DocId, DocPath]:
    return {d.id: d.path for d in docs}


def _normalized_links(d: Doc) -> tuple[list[dict], list[str]]:
    """Validate and normalize a doc's frontmatter links.

    Returns (valid_links, schema_errors). Rules 4 and 5 iterate over
    valid_links only — no KeyErrors, no per-character cascades if the
    author wrote `links: "oops"` instead of a list.
    """
    raw = d.frontmatter.get("links", [])
    errors: list[str] = []
    if not isinstance(raw, list):
        errors.append(f"E-SCHEMA {d.path}: `links` must be a list")
        return [], errors
    valid: list[dict] = []
    for link in raw:
        if not isinstance(link, dict):
            errors.append(f"E-SCHEMA {d.path}: link must be a mapping")
            continue
        if "to" not in link:
            errors.append(f"E-SCHEMA {d.path}: link missing `to` field")
            continue
        valid.append(link)
    return valid, errors


def check(docs: list[Doc]) -> list[str]:
    """Run the borrow checker over already-parsed docs.

    Returns a list of human-readable diagnostics. Empty list means the
    tree is sound.
    """
    errors: list[str] = []

    # Rule 1: ownership — ids unique across the tree.
    seen: dict[DocId, DocPath] = {}
    for d in docs:
        if d.id in seen:
            errors.append(
                f"E-OWNERSHIP: id '{d.id}' declared by both "
                f"{seen[d.id]} and {d.path}"
            )
        seen[d.id] = d.path

    index = build_index(docs)

    for d in docs:
        # Rule 2: schema — required top-level fields present.
        for field in ("title", "kind", "links"):
            if field not in d.frontmatter:
                errors.append(f"E-SCHEMA {d.path}: missing `{field}`")

        # Normalize links once; invalid links are reported as schema errors
        # and excluded from rules 4/5.
        links, link_errors = _normalized_links(d)
        errors.extend(link_errors)

        # Rule 3: no dangling strong refs in body.
        for m in STRONG_REF.finditer(d.body):
            target = DocId(m.group(1))
            if target not in index:
                errors.append(
                    f"E-DANGLING {d.path}: strong link [[id:{target}]] "
                    f"points to a nonexistent doc"
                )

        # Rule 4: lifetime — strong links in frontmatter must resolve.
        for link in links:
            target = DocId(link["to"])
            strength = link.get("strength", "strong")
            if strength == "strong" and target not in index:
                errors.append(
                    f"E-LIFETIME {d.path}: strong link to '{target}' "
                    f"outlives its target (deleted without retargeting)"
                )

        # Rule 5: identity — declared strong links match body's strong refs.
        declared_strong = {
            DocId(link["to"])
            for link in links
            if link.get("strength", "strong") == "strong"
        }
        body_strong = {DocId(m.group(1)) for m in STRONG_REF.finditer(d.body)}
        if declared_strong != body_strong:
            missing = body_strong - declared_strong
            extra = declared_strong - body_strong
            if missing:
                errors.append(
                    f"E-IDENTITY {d.path}: body references {sorted(missing)} "
                    f"but they are not declared in frontmatter `links`"
                )
            if extra:
                errors.append(
                    f"E-IDENTITY {d.path}: frontmatter declares strong links "
                    f"to {sorted(extra)} but body does not reference them"
                )

    return errors
