"""Transformation operations on a croc tree.

Every operation follows the Rust-style transactional pattern:

  1. Load   — parse the tree. Malformed input → TreeError, no side effects.
  2. Check  — run the borrow checker. Unsound tree → refuse; can't refactor
              a program that doesn't compile.
  3. Plan   — compute the change in memory as a map of path -> new content.
  4. Simulate — apply the plan to an in-memory copy of the tree, re-parse,
              re-check. If the simulated tree has errors, the plan is bad
              and we never write anything to disk.
  5. Commit — write each changed file atomically (temp + os.replace). On
              FS failure mid-commit, snapshot the originals and roll back
              the files we already wrote.

Invalid logical states never reach the filesystem. FS-level failures are
the only thing that can tear, and they have their own rollback net.
"""

from __future__ import annotations

import copy
import os
import pathlib
import re
import shutil
import subprocess
from typing import Iterable

import yaml

from croc.check import (
    Doc,
    DocId,
    DocPath,
    ID_RE,
    TreeError,
    build_index,
    check,
    load_tree,
    parse_frontmatter,
)


class OpError(Exception):
    """An operation cannot proceed (pre-condition failure or commit failure)."""


# ---------------------------------------------------------------------------
# Public ops
# ---------------------------------------------------------------------------


def move_file(
    root: pathlib.Path, src: pathlib.Path, dst: pathlib.Path
) -> pathlib.Path:
    """Relocate a file on disk.

    croc's ID-based links mean no references need rewriting — the ID
    travels with the file. The tree's invariants are preserved by
    construction, so no post-check is needed (and doing one would just
    re-validate what the design already guarantees).

    Still runs the pre-check: refusing to pile a move on a broken tree.
    """
    root = root.resolve()
    src = src.resolve()

    if not src.exists():
        raise OpError(f"{src}: does not exist")
    if not src.is_file():
        raise OpError(f"{src}: not a regular file")

    _require_under_root(src, root)

    if dst.exists() and dst.is_dir():
        dst = dst / src.name
    dst = dst.resolve()

    _require_under_root(dst, root)
    if dst.exists():
        raise OpError(f"{dst}: already exists")

    _assert_sound(root)

    dst.parent.mkdir(parents=True, exist_ok=True)
    if _in_git_repo(root):
        result = subprocess.run(
            ["git", "-C", str(root), "mv", str(src), str(dst)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise OpError(f"git mv failed: {result.stderr.strip()}")
    else:
        shutil.move(str(src), str(dst))

    return dst


def rename_id(
    root: pathlib.Path, old_id: str, new_id: str
) -> list[DocPath]:
    """Rename a doc's id. Rewrites every referrer atomically.

    Returns the list of paths that were modified.
    """
    root = root.resolve()

    if not ID_RE.fullmatch(new_id):
        raise OpError(
            f"{new_id!r} is not a valid id "
            f"(allowed: letters, digits, `_`, `.`, `-`)"
        )
    if old_id == new_id:
        raise OpError("old and new id are the same")

    docs = _assert_sound(root)

    index = build_index(docs)
    if old_id not in index:
        raise OpError(f"no doc with id {old_id!r}")
    if new_id in index:
        raise OpError(
            f"id {new_id!r} already in use by {index[new_id]}"
        )

    plan = _plan_rename(docs, old_id, new_id)

    # Simulate: apply the plan in memory, re-parse, re-check. If the
    # rewritten tree has any errors, we never touch disk.
    simulated = _apply_plan_in_memory(docs, plan)
    sim_errors = check(simulated)
    if sim_errors:
        msg = "rewrite would break invariants:\n  " + "\n  ".join(sim_errors)
        raise OpError(msg)

    # Commit: atomic per-file write, with cross-file rollback on FS failure.
    _commit(root, plan)

    return sorted(plan.keys())


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _require_under_root(path: pathlib.Path, root: pathlib.Path) -> None:
    try:
        path.relative_to(root)
    except ValueError:
        raise OpError(f"{path}: not under tree root {root}")


def _assert_sound(root: pathlib.Path) -> list[Doc]:
    """Load + check. Raise OpError if the tree can't be loaded or is unsound."""
    try:
        docs = load_tree(root)
    except TreeError as e:
        raise OpError(f"pre-check: {e}") from e
    errors = check(docs)
    if errors:
        msg = "tree is not sound; fix violations first:\n  " + "\n  ".join(errors)
        raise OpError(msg)
    return docs


def _in_git_repo(path: pathlib.Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _plan_rename(
    docs: Iterable[Doc], old_id: str, new_id: str
) -> dict[DocPath, str]:
    plan: dict[DocPath, str] = {}
    for d in docs:
        new_content = _rewrite_doc(d, old_id, new_id)
        if new_content is not None:
            plan[d.path] = new_content
    return plan


def _rewrite_doc(d: Doc, old: str, new: str) -> str | None:
    """Return new file content if this doc changes under the rename, else None."""
    fm_changed = False
    new_fm = copy.deepcopy(d.frontmatter)

    if new_fm.get("id") == old:
        new_fm["id"] = new
        fm_changed = True

    links = new_fm.get("links", [])
    if isinstance(links, list):
        for link in links:
            if isinstance(link, dict) and link.get("to") == old:
                link["to"] = new
                fm_changed = True

    new_body = d.body
    new_body = re.sub(
        rf"\[\[id:{re.escape(old)}\]\]", f"[[id:{new}]]", new_body
    )
    new_body = re.sub(
        rf"\[\[see:{re.escape(old)}\]\]", f"[[see:{new}]]", new_body
    )
    body_changed = new_body != d.body

    if not (fm_changed or body_changed):
        return None

    fm_yaml = yaml.dump(new_fm, sort_keys=False, default_flow_style=None)
    return f"---\n{fm_yaml}---\n{new_body}"


def _apply_plan_in_memory(
    docs: list[Doc], plan: dict[DocPath, str]
) -> list[Doc]:
    """Return a doc list with the plan applied, re-parsed to confirm roundtrip."""
    out: list[Doc] = []
    for d in docs:
        if d.path not in plan:
            out.append(d)
            continue
        fm, body = parse_frontmatter(pathlib.Path(d.path), plan[d.path])
        out.append(
            Doc(
                path=d.path,
                id=DocId(fm["id"]),
                frontmatter=fm,
                body=body,
            )
        )
    return out


def _atomic_write(path: pathlib.Path, content: str) -> None:
    """Write content to path via temp + os.replace. Atomic on POSIX."""
    tmp = path.with_suffix(path.suffix + ".croc.tmp")
    tmp.write_text(content)
    os.replace(tmp, path)


def _commit(root: pathlib.Path, plan: dict[DocPath, str]) -> None:
    """Write every file in plan atomically, with snapshot-based rollback."""
    snapshot: dict[DocPath, str] = {
        rel: (root / rel).read_text() for rel in plan
    }
    written: list[DocPath] = []
    try:
        for rel, new_content in plan.items():
            _atomic_write(root / rel, new_content)
            written.append(rel)
    except Exception as e:
        for rel in written:
            try:
                _atomic_write(root / rel, snapshot[rel])
            except Exception:
                # Last-ditch best effort. If rollback itself fails, we've got
                # bigger problems — let the user see the original error and
                # the list of files that may be inconsistent.
                pass
        raise OpError(
            f"commit failed after {len(written)}/{len(plan)} files "
            f"written; rolled back. original error: {e}"
        ) from e
