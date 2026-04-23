"""Scaffold a plain-markdown doc tree from a source directory.

`crawl` mirrors a source tree — one `.md` stub per source file, one
`self.md` per directory — and emits *plain markdown* with only a
`mirrors:` breadcrumb in frontmatter. No `id`, `kind`, `links`, or
`title`.

This is deliberate: `crawl` sits at the "resting" state of croc's
adopt/molt cycle. Users run `croc init --adopt` when they want the
borrow checker, `molt` when they want to read/share, and `crawl`'s
output is shape-compatible with the post-molt state at both ends.
Re-adopting is idempotent; round-tripping preserves the `mirrors:`
field.

I/O (and its errors) live in `build_crawl` and `list_git_files`. The
`plan_crawl` function is pure over already-discovered filesystem
state — caller passes in an optional `git_files` set if gitignore
filtering is desired.
"""

from __future__ import annotations

import pathlib
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class CrawlResult:
    created: list[pathlib.Path] = field(default_factory=list)
    skipped: list[pathlib.Path] = field(default_factory=list)


def plan_crawl(
    src: pathlib.Path,
    output_root: pathlib.Path,
    file_types: list[str] | None = None,
    git_files: set[pathlib.Path] | None = None,
) -> list[tuple[pathlib.Path, str]]:
    """Return (path, content) pairs to write. No side effects.

    Args:
        src: Source directory to mirror.
        output_root: Root of the output doc tree.
        file_types: Extensions to include (e.g. `[".py", ".ts"]`) or
            `["all"]` for no filter. Defaults to `["all"]` — croc is
            not language-specific; `.gitignore` plus dot/pycache
            pruning is the principled discovery filter.
        git_files: Absolute paths of files git knows about. When
            provided, only files in this set are mirrored (respects
            `.gitignore`). When `None`, every file on disk is considered.

    Every directory under `src` — root included — yields a `self.md`
    whose body lists the directory's contents. Each matching source
    file yields a `<stem>.md` stub.

    Directories with no tracked content (when `git_files` is used) and
    dot-prefixed / `__`-prefixed directories are pruned entirely.
    """
    planned, _ = _plan_crawl_with_stats(src, output_root, file_types=file_types, git_files=git_files)
    return planned


def _plan_crawl_with_stats(
    src: pathlib.Path,
    output_root: pathlib.Path,
    file_types: list[str] | None = None,
    git_files: set[pathlib.Path] | None = None,
) -> tuple[list[tuple[pathlib.Path, str]], int]:
    """Same as `plan_crawl`, plus a count of files whose output name
    was disambiguated because a same-directory stem collision would
    have otherwise silently overwritten them. The CLI uses the count
    to surface the rename; library callers use `plan_crawl` and drop
    it. Kept private so we aren't committing the stats shape as part
    of the public surface."""
    if file_types is None:
        file_types = ["all"]

    planned: list[tuple[pathlib.Path, str]] = []
    n_disambiguated = 0
    src_name = src.name

    for dir_path, dirnames, filenames in src.walk():
        dirnames[:] = sorted(d for d in dirnames if not _skip_dir(d))

        if git_files is not None:
            dirnames[:] = [d for d in dirnames if _has_git_files_in_dir(dir_path / d, git_files)]

        rel = dir_path.relative_to(src)
        out_dir = output_root / rel

        matched = sorted(f for f in filenames if _include_file(dir_path, f, file_types, git_files))
        subdirs = dirnames  # already sorted/filtered
        contents = [f"{d}/" for d in subdirs] + matched

        # `mirrors:` tracks the source directory relative to `src`'s
        # parent, so `crawl src/` → `mirrors: src` at the root and
        # `mirrors: src/app` one level in. Survives `molt` untouched.
        mirrors_dir = src_name if str(rel) == "." else f"{src_name}/{rel.as_posix()}"
        dir_name = src_name if str(rel) == "." else dir_path.name
        planned.append((out_dir / "self.md", _directory_self_md(dir_name, contents, mirrors_dir)))

        # `Path.stem` strips only the final suffix, so siblings that
        # differ only in extension (e.g. `Dockerfile`, `Dockerfile.ecs`,
        # `Dockerfile.fargate_worker`; `Makefile` + `Makefile.local`)
        # would all collapse to the same `<stem>.md` output path and
        # silently overwrite each other at `apply_plan`. Detect those
        # groups here and fall back to the full source filename for
        # every member of a colliding group.
        stem_groups: dict[str, list[str]] = defaultdict(list)
        for f in matched:
            stem_groups[f"{pathlib.Path(f).stem}.md"].append(f)

        for f in matched:
            candidate = f"{pathlib.Path(f).stem}.md"
            if len(stem_groups[candidate]) == 1:
                out_name = candidate
            else:
                out_name = f"{f}.md"
                n_disambiguated += 1
            mirrors_file = f"{mirrors_dir}/{f}"
            planned.append((out_dir / out_name, _file_stub(f, mirrors_file)))

    return planned, n_disambiguated


def build_crawl(
    src: pathlib.Path,
    output_root: pathlib.Path,
    *,
    force: bool,
    file_types: list[str] | None = None,
    git_files: set[pathlib.Path] | None = None,
) -> CrawlResult:
    """Convenience: plan + apply. Skip existing files unless `force=True`.

    Idempotent by default — running twice on the same tree yields an
    all-skipped second run. `force=True` overwrites.

    Callers who need the plan separately (e.g. to render a preview
    before committing) should call `plan_crawl` + `apply_plan` directly.
    """
    planned = plan_crawl(src, output_root, file_types=file_types, git_files=git_files)
    return apply_plan(planned, force=force)


def apply_plan(
    planned: list[tuple[pathlib.Path, str]],
    *,
    force: bool,
) -> CrawlResult:
    """Write a pre-computed plan to disk. Skip existing files unless
    `force=True`. Split from `build_crawl` so the CLI can plan once,
    render a preview, then commit that same plan — no second walk of
    the source tree."""
    result = CrawlResult()
    for out_path, content in planned:
        if out_path.exists() and not force:
            result.skipped.append(out_path)
            continue

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(content)
        result.created.append(out_path)

    return result


def list_git_files(directory: pathlib.Path) -> set[pathlib.Path] | None:
    """Files git knows about under `directory` — tracked and untracked-
    but-not-ignored. Returns a set of absolute resolved paths, or
    `None` if we're not inside a git repo (or `git` is unavailable).

    The union of `ls-files` and `ls-files --others --exclude-standard`
    gives us everything git would include in a snapshot, i.e. exactly
    the files a user considers part of the project. Use this when the
    caller wants "everything not ignored" — includes in-progress
    drafts that haven't been `git add`ed yet.
    """
    try:
        tracked = subprocess.run(
            ["git", "ls-files"],
            capture_output=True,
            text=True,
            cwd=directory,
        )
        if tracked.returncode != 0:
            return None

        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            capture_output=True,
            text=True,
            cwd=directory,
        )
    except FileNotFoundError:
        return None

    files: set[pathlib.Path] = set()
    for line in tracked.stdout.strip().splitlines():
        if line:
            files.add((directory / line).resolve())
    if untracked.returncode == 0:
        for line in untracked.stdout.strip().splitlines():
            if line:
                files.add((directory / line).resolve())
    return files


def list_tracked_only_files(directory: pathlib.Path) -> set[pathlib.Path] | None:
    """Files git is actively tracking under `directory`. Returns a set
    of absolute resolved paths, or `None` if we're not inside a git
    repo (or `git` is unavailable).

    Narrower than `list_git_files`: drafts (untracked-but-not-ignored
    files) are excluded. Use when the caller wants "files committed to
    the project," not "files the user is working on."
    """
    try:
        tracked = subprocess.run(
            ["git", "ls-files"],
            capture_output=True,
            text=True,
            cwd=directory,
        )
    except FileNotFoundError:
        return None
    if tracked.returncode != 0:
        return None

    files: set[pathlib.Path] = set()
    for line in tracked.stdout.strip().splitlines():
        if line:
            files.add((directory / line).resolve())
    return files


def resolve_file_filter(
    directory: pathlib.Path,
    *,
    include_untracked: bool,
) -> set[pathlib.Path] | None:
    """Pick the right git-backed file filter for this invocation.

    Returns `None` when `directory` is not in a git repo — callers
    should treat `None` as "no filter; walk everything." The two
    branches correspond to the CLI's `--include-untracked` /
    `--no-include-untracked` (default) flag pair.
    """
    if include_untracked:
        return list_git_files(directory)
    return list_tracked_only_files(directory)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _skip_dir(name: str) -> bool:
    # Hidden directories (`.git`, `.venv`, ...) and Python build artifacts
    # (`__pycache__`, `__pypackages__`) never contain documentation-worthy
    # source. Pruned unconditionally, before any gitignore filter.
    return name.startswith((".", "__"))


def _matches_file_types(filename: str, file_types: list[str]) -> bool:
    if "all" in file_types:
        return True
    return any(filename.endswith(ext) for ext in file_types)


def _include_file(
    dir_path: pathlib.Path,
    filename: str,
    file_types: list[str],
    git_files: set[pathlib.Path] | None,
) -> bool:
    if not _matches_file_types(filename, file_types):
        return False
    if git_files is None:
        return True
    return (dir_path / filename).resolve() in git_files


def _has_git_files_in_dir(dir_path: pathlib.Path, git_files: set[pathlib.Path]) -> bool:
    """True iff any tracked file lives under `dir_path`.

    `is_relative_to` (not a string `startswith`) avoids the classic
    sibling-prefix bug where `/a/foo` would falsely claim ownership
    of files under `/a/foobar`.
    """
    dir_resolved = dir_path.resolve()
    return any(f.is_relative_to(dir_resolved) for f in git_files)


def _directory_self_md(dir_name: str, contents: list[str], mirrors: str) -> str:
    listing = "\n".join(f"- `{c}`" for c in contents) if contents else "_(empty)_"
    return f"""---
mirrors: {mirrors}
---

# {dir_name}/

## Contents

{listing}
"""


def _file_stub(file_name: str, mirrors: str) -> str:
    return f"""---
mirrors: {mirrors}
---

# {file_name}

## Overview

_Document the purpose of this file._
"""
