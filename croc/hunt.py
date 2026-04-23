"""Detect docs whose bound source files changed vs. a git ref.

Inputs: (a) every doc's `tracks:` frontmatter list, read fresh from
disk; (b) a set of changed paths from `git diff --name-only`. Output:
one `HuntAlert` per (doc, changed tracked source) pair.

Strict vs forgiving:
- Strict (default): any tracked source in the diff → alert.
- Forgiving: tracked source in the diff AND the bound doc is NOT
  itself in the diff → alert. Docs updated alongside their code pass.

Both modes require a git repo. Paths in `tracks:` are repo-root
relative; git diff paths are repo-root relative; they line up without
translation.
"""

from __future__ import annotations

import pathlib
import subprocess
from dataclasses import dataclass

import yaml

from croc.ops import OpError


@dataclass(frozen=True)
class HuntAlert:
    doc_rel: str  # doc path relative to tree root
    source_rel: str  # source path relative to repo root, from `tracks:`


def hunt_tree(
    tree_root: pathlib.Path,
    *,
    base: str | None = None,
    strict: bool,
    git_files: set[pathlib.Path] | None = None,
) -> list[HuntAlert]:
    """Scan the tree for `tracks:` frontmatter and compare against a
    git diff. Return one alert per (doc, changed source) pair.

    `base=None` uses staged changes (`git diff --cached`). Setting
    `base` switches to `<base>...HEAD` — the PR-style triple-dot range.

    `strict=True` alerts on any changed source; `False` suppresses the
    alert when the bound doc is itself in the diff.

    Raises `OpError` when `tree_root` isn't a directory or isn't inside
    a git repo.
    """
    tree_root = tree_root.resolve()
    if not tree_root.is_dir():
        raise OpError(f"{tree_root}: not a directory")

    repo_root = _git_repo_root(tree_root)
    if repo_root is None:
        raise OpError(f"{tree_root}: hunt requires a git repo")

    changed = _git_changed_paths(repo_root, base=base)
    alerts: list[HuntAlert] = []

    for p in sorted(tree_root.rglob("*.md")):
        if git_files is not None and p.resolve() not in git_files:
            continue
        absp = p.resolve()
        try:
            raw = p.read_text()
        except OSError:
            continue
        tracks = _read_tracks(raw)
        if not tracks:
            continue
        doc_rel_tree = str(p.relative_to(tree_root))
        doc_rel_repo = str(absp.relative_to(repo_root))
        for src in tracks:
            if src not in changed:
                continue
            if not strict and doc_rel_repo in changed:
                continue
            alerts.append(HuntAlert(doc_rel=doc_rel_tree, source_rel=src))

    alerts.sort(key=lambda a: (a.doc_rel, a.source_rel))
    return alerts


def _read_tracks(raw: str) -> list[str]:
    """Extract `tracks:` from a markdown file's frontmatter.

    Tolerant of non-croc trees: returns `[]` on missing frontmatter,
    malformed YAML, or a non-list value. Hunt refuses to crash on a
    half-managed tree.
    """
    if not raw.startswith("---\n"):
        return []
    parts = raw.split("---\n", 2)
    if len(parts) < 3:
        return []
    try:
        fm = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return []
    if not isinstance(fm, dict):
        return []
    tracks = fm.get("tracks", [])
    if not isinstance(tracks, list):
        return []
    return [t for t in tracks if isinstance(t, str)]


def _git_repo_root(start: pathlib.Path) -> pathlib.Path | None:
    """Return `git rev-parse --show-toplevel` as a resolved path, or
    `None` when `start` isn't inside a git repo.

    Duplicated from `croc.attack` for now — deliberate module-local
    copy while both helpers live in isolation; extract to a shared
    module once a third caller emerges.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return pathlib.Path(result.stdout.strip()).resolve()


def _git_changed_paths(repo_root: pathlib.Path, *, base: str | None) -> set[str]:
    """Return the set of repo-root-relative paths in the diff.

    Default (`base is None`): staged vs. HEAD via `git diff --cached`.
    Matches the pre-commit hook use case.

    `base` set: `base...HEAD` triple-dot range. Matches CI / PR
    review use: everything that changed on this branch vs. its merge
    base with `base`.

    Raises `OpError` when `git diff` fails — e.g. `base` doesn't exist,
    or there is no HEAD yet (freshly-initialized repo).
    """
    if base is None:
        cmd = ["git", "diff", "--cached", "--name-only"]
    else:
        cmd = ["git", "diff", "--name-only", f"{base}...HEAD"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=repo_root, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        raise OpError(f"hunt: failed to compute git diff ({e})") from e
    if result.returncode != 0:
        raise OpError(f"hunt: git diff failed: {result.stderr.strip()}")
    return {line for line in result.stdout.strip().splitlines() if line}
