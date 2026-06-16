"""Shared git helpers.

Extracted from `croc.attack` / `croc.hunt` once `croc.config` became a
third caller of `git_repo_root` (the doc-tree config is discovered by
walking up to the git repo root).
"""

from __future__ import annotations

import pathlib
import subprocess


def git_repo_root(start: pathlib.Path) -> pathlib.Path | None:
    """Return `git rev-parse --show-toplevel` as a resolved path, or
    `None` when `start` isn't inside a git repo (or git is unavailable)."""
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
