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
STRONG_REF = re.compile(rf"\[\[id:({ID_CHARS})(?:#([^|\]]+))?(?:\|([^\]]+))?\]\]")
WEAK_REF = re.compile(rf"\[\[see:({ID_CHARS})(?:#([^|\]]+))?(?:\|([^\]]+))?\]\]")


# ---------------------------------------------------------------------------
# Non-scannable regions (fenced code, inline code, escape sequences)
# ---------------------------------------------------------------------------
#
# A doc that teaches croc's syntax needs to mention `[[id:X]]` and
# `(path.md)` literally, without those sequences being scanned as live
# references. The ref regexes above have no markdown-structural
# awareness, so we pre-compute the character spans where refs should
# NOT be anchored. Every call site (check, adopt-migrate, rename, molt,
# scan_path_refs) gates its `finditer` / `sub` on `in_any_span` — a
# masked match is returned as-is (rewrite sites) or skipped (read sites).
#
# Three sources of masking:
#
#   1. Fenced code blocks — triple-backtick ``` or triple-tilde ~~~
#      opener; a matching-or-longer run of the same char (on its own
#      line) closes. Unterminated fences extend to end-of-body
#      (CommonMark behavior).
#   2. Inline code — a backtick run of length N closes on the next
#      backtick run of the same length. Unterminated runs fall through
#      as literal text (also CommonMark).
#   3. Escape sequences — `\[`, `\]`, `\(`, `\)` mask two chars each,
#      so the ref regex can't anchor on an escaped bracket/paren.

_FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})", re.MULTILINE)
_ESCAPE_RE = re.compile(r"\\[\[\]()]")


def _fenced_spans(body: str) -> list[tuple[int, int]]:
    """Spans covering every character inside a fenced code block.

    Span starts at the opening fence line's first column and ends at
    the end of the closing fence line (inclusive of its trailing
    newline, if any). Unterminated fences extend to end-of-body.
    """
    spans: list[tuple[int, int]] = []
    pos = 0
    n = len(body)
    while pos < n:
        m = _FENCE_RE.search(body, pos)
        if not m:
            break
        fence = m.group(1)
        opener_char = fence[0]
        opener_len = len(fence)
        fence_start = m.start()
        # Advance past the opener line.
        opener_nl = body.find("\n", m.end())
        if opener_nl == -1:
            # Opener on last line, no body or closer possible. Unterminated.
            spans.append((fence_start, n))
            break
        scan_pos = opener_nl + 1
        close_end: int | None = None
        while scan_pos < n:
            nl = body.find("\n", scan_pos)
            line_end = nl if nl != -1 else n
            line = body[scan_pos:line_end]
            stripped = line.lstrip(" \t")
            if stripped and stripped[0] == opener_char:
                run = 0
                while run < len(stripped) and stripped[run] == opener_char:
                    run += 1
                rest = stripped[run:]
                if run >= opener_len and rest.strip() == "":
                    close_end = line_end + (1 if nl != -1 else 0)
                    break
            scan_pos = line_end + 1
        if close_end is None:
            spans.append((fence_start, n))
            break
        spans.append((fence_start, close_end))
        pos = close_end
    return spans


def _inline_code_spans(body: str, fenced: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Spans covering matching backtick-run pairs OUTSIDE fenced spans.

    A run of N backticks closes on the next run of exactly N backticks;
    longer or shorter runs in between count as literal content (standard
    CommonMark rule). Unterminated opens fall through: the backticks
    are treated as literal text, no span emitted.
    """
    spans: list[tuple[int, int]] = []
    regions: list[tuple[int, int]] = []
    cursor = 0
    for fs, fe in fenced:
        if cursor < fs:
            regions.append((cursor, fs))
        cursor = fe
    if cursor < len(body):
        regions.append((cursor, len(body)))

    for rstart, rend in regions:
        i = rstart
        while i < rend:
            if body[i] != "`":
                i += 1
                continue
            open_start = i
            run_len = 0
            while i + run_len < rend and body[i + run_len] == "`":
                run_len += 1
            open_end = i + run_len
            j = open_end
            close_end: int | None = None
            while j < rend:
                if body[j] != "`":
                    j += 1
                    continue
                k = j
                while k < rend and body[k] == "`":
                    k += 1
                if k - j == run_len:
                    close_end = k
                    break
                j = k
            if close_end is not None:
                spans.append((open_start, close_end))
                i = close_end
            else:
                # Unterminated: move past the opener run and keep scanning.
                i = open_end
    return spans


def _escape_spans(body: str) -> list[tuple[int, int]]:
    """Two-char spans covering `\\[`, `\\]`, `\\(`, and `\\)`.

    Masking the backslash+bracket pair is enough to prevent the ref
    regex from anchoring on the escaped bracket — and therefore to
    prevent matches like `[[id:X\\]]` from resolving as references.
    """
    return [(m.start(), m.end()) for m in _ESCAPE_RE.finditer(body)]


def _merge_sorted(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Sort by start, then fuse overlapping/touching spans.

    `in_any_span` relies on the output being non-overlapping for a
    clean linear scan, and callers that inspect spans directly get
    an unambiguous representation.
    """
    if not spans:
        return []
    spans = sorted(spans)
    merged: list[tuple[int, int]] = [spans[0]]
    for start, end in spans[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def scannable_spans(body: str) -> list[tuple[int, int]]:
    """Return the non-scannable character spans in `body`.

    The ref parser treats these regions as literal text: matches whose
    `start()` position falls inside any span are skipped (read sites)
    or left untouched (rewrite sites).

    Name is read from the caller's perspective: "these are the regions
    I must not scan." Output is sorted by start and non-overlapping.
    """
    fenced = _fenced_spans(body)
    inline = _inline_code_spans(body, fenced)
    escaped = _escape_spans(body)
    return _merge_sorted(fenced + inline + escaped)


def in_any_span(pos: int, spans: list[tuple[int, int]]) -> bool:
    """True iff `pos` lies within any `(start, end)` span (half-open).

    Linear scan — spans are small (typically 0-5 per doc) and sorted,
    so tree structures cost more overhead than they save.
    """
    for start, end in spans:
        if start <= pos < end:
            return True
        if pos < start:
            return False
    return False


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
        raise TreeError(f"{path}: frontmatter must be a mapping, got {type(fm).__name__}")
    if "id" not in fm:
        raise TreeError(f"{path}: no `id` in frontmatter")
    # NewType("DocId", str) must not lie about the runtime type. YAML will
    # parse `id: 12345` as int; force authors to quote ids they want treated
    # as stable tokens.
    if not isinstance(fm["id"], str):
        raise TreeError(
            f"{path}: `id` must be a string, got {type(fm['id']).__name__} "
            f'(quote numeric-looking ids in YAML: `id: "{fm["id"]}"`)'
        )
    if not ID_RE.fullmatch(fm["id"]):
        raise TreeError(
            f"{path}: `id` {fm['id']!r} contains illegal characters (allowed: letters, digits, `_`, `.`, `-`)"
        )
    return fm, body


def load_tree(
    root: pathlib.Path,
    *,
    git_files: set[pathlib.Path] | None = None,
) -> list[Doc]:
    """Load every `.md` under `root` as a `Doc`.

    When `git_files` is provided, only files whose resolved path lives
    in that set are loaded — the rest are silently skipped (not
    parsed, not errored). `None` (the default) means "walk everything";
    the CLI layer is responsible for deciding the set and passing it
    in. See `croc.crawl.resolve_file_filter`.
    """
    if not root.exists():
        raise TreeError(f"{root}: does not exist")
    if not root.is_dir():
        raise TreeError(f"{root}: not a directory")
    docs: list[Doc] = []
    for p in sorted(root.rglob("*.md")):
        if git_files is not None and p.resolve() not in git_files:
            continue
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


def scan_symlinks(
    root: pathlib.Path,
    *,
    git_files: set[pathlib.Path] | None = None,
) -> list[str]:
    """Return warnings for symlinks under `root` that rglob will not follow.

    Symlinks are never traversed (cycle risk), so a symlinked subtree is
    silently skipped. Surface that instead of hiding it.

    When `git_files` is provided, symlinks outside the filter set are
    ignored — a user who excluded them from their tree doesn't need
    them called out. Bonus side effect: the `.git/` subtree is skipped
    entirely, since none of its entries are in the filter set.
    """
    if not root.is_dir():
        return []
    warnings: list[str] = []
    for p in sorted(root.rglob("*")):
        if git_files is not None and p.resolve() not in git_files:
            continue
        if p.is_symlink():
            warnings.append(
                f"warning: symlink {p.relative_to(root)} not traversed (symlinks are never followed to avoid cycles)"
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
            errors.append(f"E-OWNERSHIP: id '{d.id}' declared by both {seen[d.id]} and {d.path}")
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

        # Compute masked regions once per doc; rules 3 and 5 both consult
        # it to skip refs that live inside fenced code, inline code, or
        # backslash-escaped brackets — those are documentation about
        # croc's syntax, not usage of it.
        spans = scannable_spans(d.body)

        # Rule 3: no dangling strong refs in body.
        for m in STRONG_REF.finditer(d.body):
            if in_any_span(m.start(), spans):
                continue
            target = DocId(m.group(1))
            if target not in index:
                errors.append(f"E-DANGLING {d.path}: strong link [[id:{target}]] points to a nonexistent doc")

        # Rule 4: lifetime — strong links in frontmatter must resolve.
        for link in links:
            target = DocId(link["to"])
            strength = link.get("strength", "strong")
            if strength == "strong" and target not in index:
                errors.append(
                    f"E-LIFETIME {d.path}: strong link to '{target}' outlives its target (deleted without retargeting)"
                )

        # Rule 5: identity — declared strong links match body's strong refs.
        declared_strong = {DocId(link["to"]) for link in links if link.get("strength", "strong") == "strong"}
        body_strong = {DocId(m.group(1)) for m in STRONG_REF.finditer(d.body) if not in_any_span(m.start(), spans)}
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
