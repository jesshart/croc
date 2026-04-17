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
from dataclasses import dataclass
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
    root: pathlib.Path,
    src: pathlib.Path,
    dst: pathlib.Path,
    dry_run: bool = False,
) -> pathlib.Path:
    """Relocate a file on disk.

    croc's ID-based links mean no references need rewriting — the ID
    travels with the file. The tree's invariants are preserved by
    construction, so no post-check is needed (and doing one would just
    re-validate what the design already guarantees).

    Still runs the pre-check: refusing to pile a move on a broken tree.
    `dry_run=True` performs every check but skips the actual move.
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

    if dry_run:
        return dst

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
    root: pathlib.Path,
    old_id: str,
    new_id: str,
    dry_run: bool = False,
) -> list[DocPath]:
    """Rename a doc's id. Rewrites every referrer atomically.

    Returns the list of paths that were (or would be) modified.
    `dry_run=True` runs validate-plan-simulate but skips commit.
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

    if not dry_run:
        _commit(root, plan)

    return sorted(plan.keys())


# ---------------------------------------------------------------------------
# Initialization / adoption
# ---------------------------------------------------------------------------

_DEFAULT_CROC_TOML = """\
# croc tree root marker.
version = "0.1"
"""


def init_tree(root: pathlib.Path, dry_run: bool = False) -> list[str]:
    """Create `.croc.toml` at `root`. Returns action log.

    Idempotent: returns empty list (no-op) if the marker already exists,
    so `init --adopt` can run on a partially-initialized tree.
    """
    root = root.resolve()
    marker = root / ".croc.toml"

    if marker.exists():
        return []

    action = f"CREATE {marker.relative_to(root) if marker.is_relative_to(root) else marker}"
    if dry_run:
        return [action]

    root.mkdir(parents=True, exist_ok=True)
    marker.write_text(_DEFAULT_CROC_TOML)
    return [action]


@dataclass
class _AdoptEntry:
    path: pathlib.Path
    new_content: str
    proposed_id: str
    verb: str  # "SCAFFOLD" or "AUGMENT (added: ...)"
    is_new_id: bool  # True if the id is newly derived; False if it was already on disk


def adopt_tree(root: pathlib.Path, dry_run: bool = False) -> list[str]:
    """Bring every `.md` under `root` into the managed croc schema.

    Three outcomes per file:

    - **SCAFFOLD** — file has no frontmatter. Prepend a new block with a
      derived id (see `_propose_id`), a title, a kind, and empty `links`.
    - **AUGMENT** — file has frontmatter but is missing one or more of the
      required fields (`id`, `title`, `kind`, `links`). Fill in what's
      missing; preserve every existing key verbatim (including foreign
      fields like `type`, `mirrors`, `created`, ...).
    - **SKIP** — file has frontmatter we can't safely touch (unterminated,
      invalid YAML, existing `id` doesn't match the grammar). Report a
      note so the author can fix it by hand.

    Fully-managed files (all four required fields present + id valid) are
    left alone. Ids are checked for collision across proposed ids and
    against any ids already on disk; collisions abort with a clear list.
    """
    root = root.resolve()
    if not root.is_dir():
        raise OpError(f"{root}: not a directory")

    # Phase 1: scan and classify. Mutates existing_ids + skip_notes.
    existing_ids: dict[str, pathlib.Path] = {}
    plan: list[_AdoptEntry] = []
    skip_notes: list[str] = []

    for p in sorted(root.rglob("*.md")):
        entry = _classify_for_adopt(p, root, existing_ids, skip_notes)
        if entry is not None:
            plan.append(entry)

    # Phase 2: collision detection. Only newly-derived ids can collide;
    # pre-existing ids are the ground truth on disk.
    collisions: list[str] = []
    seen_proposed: dict[str, pathlib.Path] = {}
    for entry in plan:
        if not entry.is_new_id:
            continue
        pid = entry.proposed_id
        if pid in existing_ids:
            collisions.append(
                f"{entry.path.relative_to(root)}: proposed id {pid!r} is already "
                f"used by {existing_ids[pid].relative_to(root)}"
            )
        elif pid in seen_proposed:
            collisions.append(
                f"{entry.path.relative_to(root)} and "
                f"{seen_proposed[pid].relative_to(root)} both propose id {pid!r}"
            )
        else:
            seen_proposed[pid] = entry.path

    if collisions:
        raise OpError(
            "id collisions detected; resolve by renaming files or editing ids:\n  "
            + "\n  ".join(collisions)
        )

    actions = list(skip_notes)
    for entry in plan:
        actions.append(
            f"{entry.verb} {entry.path.relative_to(root)} (id: {entry.proposed_id})"
        )

    if dry_run:
        return actions

    # Phase 3: commit. Atomic per-file. Body content is preserved verbatim
    # in both scaffold and augment paths, so the "rollback" for a bad write
    # is to re-run the command.
    for entry in plan:
        _atomic_write(entry.path, entry.new_content)

    return actions


def _classify_for_adopt(
    p: pathlib.Path,
    root: pathlib.Path,
    existing_ids: dict[str, pathlib.Path],
    skip_notes: list[str],
) -> _AdoptEntry | None:
    """Classify one .md file. Mutates `existing_ids` and `skip_notes`.

    Returns an `_AdoptEntry` if the file needs writing (SCAFFOLD or AUGMENT),
    or None if the file is already managed or is being skipped.
    """
    raw = p.read_text()

    # --- Case A: no frontmatter → SCAFFOLD ---
    if not raw.startswith("---\n"):
        proposed_id = _propose_id(p, root)
        if not proposed_id or not ID_RE.fullmatch(proposed_id):
            raise OpError(
                f"{p.relative_to(root)}: cannot derive a valid id from path "
                f"(got {proposed_id!r}); rename the file or add frontmatter manually"
            )
        kind = "self" if p.name == "self.md" else "leaf"
        title = _propose_title(p, root)
        new_content = _scaffold_content(proposed_id, title, kind, raw)
        return _AdoptEntry(
            path=p,
            new_content=new_content,
            proposed_id=proposed_id,
            verb="SCAFFOLD",
            is_new_id=True,
        )

    # --- File has frontmatter. Parse leniently; don't require croc schema. ---
    parts = raw.split("---\n", 2)
    if len(parts) < 3:
        skip_notes.append(
            f"SKIP {p.relative_to(root)}: unterminated frontmatter"
        )
        return None
    _, fm_text, body = parts
    try:
        fm = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError as e:
        skip_notes.append(
            f"SKIP {p.relative_to(root)}: invalid YAML "
            f"({str(e).splitlines()[0]})"
        )
        return None
    if not isinstance(fm, dict):
        skip_notes.append(
            f"SKIP {p.relative_to(root)}: frontmatter must be a mapping"
        )
        return None

    # If an id is present, it must be a valid one; we refuse to silently fix it.
    if "id" in fm and (
        not isinstance(fm["id"], str) or not ID_RE.fullmatch(fm["id"])
    ):
        skip_notes.append(
            f"SKIP {p.relative_to(root)}: existing `id` is not a valid string; "
            f"fix manually then re-run"
        )
        return None

    # --- Case B: already fully managed → no write ---
    required = ("id", "title", "kind", "links")
    if all(f in fm for f in required):
        existing_ids[fm["id"]] = p
        return None

    # --- Case C: has frontmatter but is missing required fields → AUGMENT ---
    augmented = dict(fm)  # preserve all existing fields and their order
    added: list[str] = []
    is_new_id = False

    if "id" not in augmented:
        proposed_id = _propose_id(p, root)
        if not proposed_id or not ID_RE.fullmatch(proposed_id):
            raise OpError(
                f"{p.relative_to(root)}: cannot derive a valid id from path "
                f"(got {proposed_id!r}); add `id` manually"
            )
        augmented["id"] = proposed_id
        added.append("id")
        is_new_id = True
    if "title" not in augmented:
        augmented["title"] = _propose_title(p, root)
        added.append("title")
    if "kind" not in augmented:
        augmented["kind"] = "self" if p.name == "self.md" else "leaf"
        added.append("kind")
    if "links" not in augmented:
        augmented["links"] = []
        added.append("links")

    new_content = _render_augmented(augmented, body)

    # If the id was already on disk, it's a real claim — track it so other
    # files' proposed ids can collide against it.
    if not is_new_id:
        existing_ids[augmented["id"]] = p

    return _AdoptEntry(
        path=p,
        new_content=new_content,
        proposed_id=augmented["id"],
        verb=f"AUGMENT (added: {', '.join(added)})",
        is_new_id=is_new_id,
    )


def _propose_id(path: pathlib.Path, root: pathlib.Path) -> str:
    """Derive a proposed id from the file's full path under the tree root.

    Hierarchical by default, for two reasons:

    1. Filesystem paths are unique by OS invariant, so path-derived ids
       are unique by construction — no collision-fallback heuristic
       required, no order-dependent "who claimed the short id first."
    2. Matches Rust's module convention: `transforms::utils::alerts` is
       the canonical name; the short form only works when unambiguous.
       Code-adjacent doc trees (mirroring a repo) already disambiguate
       by path, not filename, and this makes croc honor that.

    Examples:
    - `foo.md` (root) → `foo`
    - `sub/foo.md` → `sub-foo`
    - `transforms/utils/__init__.md` → `transforms-utils-init`
    - `alerts/self.md` → `alerts` (directory-index convention)
    - `self.md` (root) → `root`
    """
    rel = path.relative_to(root)
    if path.name == "self.md":
        rel_parent = rel.parent
        if str(rel_parent) == ".":
            return "root"
        return _slugify(
            str(rel_parent).replace(os.sep, "/").replace("/", "-")
        )
    # Other files: slugify the full relative path minus the extension.
    without_ext = rel.with_suffix("")
    return _slugify(
        str(without_ext).replace(os.sep, "/").replace("/", "-")
    )


def _slugify(name: str) -> str:
    """Lowercase alphanumeric-and-hyphen slug."""
    s = name.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def _title_from_stem(stem: str) -> str:
    """Filename stem → Title Case display string."""
    words = re.sub(r"[^A-Za-z0-9]+", " ", stem).split()
    return " ".join(w.capitalize() for w in words) or stem


def _propose_title(path: pathlib.Path, root: pathlib.Path) -> str:
    """Human-readable title. For `self.md`, uses the directory name so an
    index file reads as `Alerts` instead of the useless `Self`."""
    if path.name == "self.md":
        rel_parent = path.parent.relative_to(root)
        if str(rel_parent) == ".":
            return "Root"
        return _title_from_stem(rel_parent.name)
    return _title_from_stem(path.stem)


def _scaffold_content(id_: str, title: str, kind: str, original_body: str) -> str:
    """Produce a full markdown file with scaffolded frontmatter + original body."""
    fm = {"id": id_, "title": title, "kind": kind, "links": []}
    fm_yaml = yaml.dump(fm, sort_keys=False, default_flow_style=None)
    body = original_body
    if body and not body.startswith("\n"):
        body = "\n" + body
    return f"---\n{fm_yaml}---\n{body}"


def _render_augmented(fm: dict, body: str) -> str:
    """Render a file with augmented frontmatter; body passed through verbatim.

    Key order is preserved (CPython 3.7+ dicts are ordered), so existing
    fields keep their position and new fields append at the end.
    """
    fm_yaml = yaml.dump(fm, sort_keys=False, default_flow_style=None)
    return f"---\n{fm_yaml}---\n{body}"


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
