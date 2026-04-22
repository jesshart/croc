"""Lurk — enforce a per-file line-count budget on a markdown tree.

croc's editorial take: small docs + id-based refs outperform one
big doc. `lurk` makes that opinion machine-checkable. It counts
lines per `.md` file and reports any that exceed a threshold.

YAML frontmatter is excluded by default so a doc isn't penalized
for being well-linked; pass `include_frontmatter=True` (CLI:
`--include-frontmatter`) for a literal whole-file count.

Works on any markdown tree — no croc frontmatter required. Walk
and filter mirror `scan_path_refs` so `--include-untracked` carries
through from the CLI layer unchanged.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass

from croc.check import DocPath


@dataclass
class LurkViolation:
    path: DocPath
    line_count: int
    limit: int


def count_content_lines(text: str, *, include_frontmatter: bool) -> int:
    """Line count for `text`, optionally stripping YAML frontmatter.

    Uses `str.splitlines()` — the stdlib idiom for POSIX line
    counting, tolerant of `\\n` / `\\r\\n` / no-trailing-newline.
    Frontmatter detection is best-effort and parser-free: if the
    file starts with `---\\n` and contains a closing `---\\n`, the
    block between them is removed. Malformed frontmatter (missing
    closing fence) falls through to a literal count — lurk refuses
    to error on unadopted trees.
    """
    if include_frontmatter:
        return len(text.splitlines())
    return len(_strip_frontmatter(text).splitlines())


def _strip_frontmatter(text: str) -> str:
    """Drop a `---\\n...\\n---\\n` block at the top of `text`, if present."""
    if not text.startswith("---\n"):
        return text
    parts = text.split("---\n", 2)
    if len(parts) < 3:
        # Opening fence found but no closing fence. Treat whole file as body.
        return text
    return parts[2]


def lurk_tree(
    root: pathlib.Path,
    *,
    max_lines: int,
    include_frontmatter: bool = False,
    git_files: set[pathlib.Path] | None = None,
) -> list[LurkViolation]:
    """Walk `root` for `.md` files and return violations.

    A violation is any file whose (optionally frontmatter-stripped)
    line count exceeds `max_lines`. The result is sorted by path for
    deterministic output.

    `git_files`: same semantics as the rest of croc. `None` means
    "walk everything"; a set filters to those resolved paths.
    """
    root = root.resolve()
    violations: list[LurkViolation] = []
    for p in sorted(root.rglob("*.md")):
        if git_files is not None and p.resolve() not in git_files:
            continue
        try:
            text = p.read_text()
        except OSError:
            continue
        count = count_content_lines(text, include_frontmatter=include_frontmatter)
        if count > max_lines:
            violations.append(
                LurkViolation(
                    path=DocPath(str(p.relative_to(root))),
                    line_count=count,
                    limit=max_lines,
                )
            )
    return violations
