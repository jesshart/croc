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
from dataclasses import dataclass, field
from typing import Iterable

import yaml

from croc.check import (
    Doc,
    DocId,
    DocPath,
    ID_RE,
    STRONG_REF,
    WEAK_REF,
    TreeError,
    build_index,
    check,
    load_tree,
    parse_frontmatter,
)


# Markdown path-ref pattern: matches `[text](relative/path.md[#anchor])`.
# Used for both migration (`--migrate-refs`) and the `refs` diagnostic.
# Intentionally narrow — only .md targets; ignores reference-style links,
# autolinks, images, and links with extra URL syntax (titles, query strings).
#
# The extension match is case-insensitive (`.md`, `.MD`, `.Md`, `.mD`) so we
# catch any case variant at detection. The resolver then rejects non-lowercase
# variants with a targeted case-sensitivity diagnostic — letting `.MD` pass
# silently would be the exact failure mode croc exists to prevent.
MD_PATH_REF = re.compile(
    r"\[(?P<text>[^\]]*)\]\((?P<path>[^)#\s]+\.[Mm][Dd])(?P<anchor>#[^)]+)?\)"
)


def _case_mismatch_ext(rel_path: str) -> bool:
    """True iff the path ends with a case variant of `.md` other than `.md`."""
    return rel_path.endswith((".MD", ".Md", ".mD"))


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
    migrated_refs: list[str] = field(default_factory=list)
    # ^ raw path strings (as written in markdown) of refs migrated during
    # --migrate-refs. Surfaced in the action log so dry-run plans show
    # which body refs are about to change.


def adopt_tree(
    root: pathlib.Path,
    dry_run: bool = False,
    migrate_refs: bool = True,
) -> list[str]:
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

    With `migrate_refs=True` (the default), body text in every plan
    entry — including already-managed files that happen to contain
    path-refs — is walked for markdown-style refs (`[text](rel/path.md)`)
    and rewritten to the croc dialect (`[[id:X#anchor|text]]`). Managed
    files with no path-refs stay out of the plan entirely; re-running
    adopt is cheap and idempotent. Unresolvable refs are left in place
    and surfaced as `SKIP-REF` notes; fix the target or add it to the
    tree, then re-run.

    Pass `migrate_refs=False` to adopt only frontmatter shape and leave
    body content untouched.
    """
    root = root.resolve()
    if not root.is_dir():
        raise OpError(f"{root}: not a directory")

    # Phase 1: scan and classify. Mutates existing_ids + skip_notes.
    existing_ids: dict[str, pathlib.Path] = {}
    plan: list[_AdoptEntry] = []
    skip_notes: list[str] = []

    for p in sorted(root.rglob("*.md")):
        entry = _classify_for_adopt(
            p, root, existing_ids, skip_notes, migrate_refs=migrate_refs
        )
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

    # Phase 2.5 (optional): migrate markdown path-refs in plan entries' bodies
    # to the croc dialect. Must run after collision detection (we need the
    # full post-adopt path→id map) and before actions are rendered.
    if migrate_refs:
        _migrate_refs_in_plan(root, existing_ids, plan, skip_notes)
        # Prune MIGRATE entries that turned out to have nothing to migrate
        # (e.g. managed file whose only path-refs were unresolvable — the
        # SKIP-REF notes already carry that information; no write needed).
        plan = [
            e for e in plan
            if e.verb != "MIGRATE" or e.migrated_refs
        ]

    actions = list(skip_notes)
    for entry in plan:
        actions.append(_format_adopt_action(entry, root))

    if dry_run:
        return actions

    # Phase 3: commit. Atomic per-file. Body content is preserved verbatim
    # in both scaffold and augment paths, so the "rollback" for a bad write
    # is to re-run the command.
    for entry in plan:
        _atomic_write(entry.path, entry.new_content)

    return actions


def _format_adopt_action(entry: _AdoptEntry, root: pathlib.Path) -> str:
    """Render one entry's action line for the CLI action log.

    Combines verb + path + id and, if any refs migrated, appends a count
    and up to the first three source paths with `+N more` truncation.
    One line per entry reflects one write per file — readers can trust
    the line count as the write count.
    """
    parts: list[str] = [f"id: {entry.proposed_id}"]
    if entry.migrated_refs:
        seen: list[str] = []
        for r in entry.migrated_refs:
            if r not in seen:
                seen.append(r)
        n = len(seen)
        plural = "" if n == 1 else "s"
        shown = seen[:3]
        suffix = ", ".join(shown)
        if n > 3:
            suffix += f", +{n - 3} more"
        parts.append(f"migrated {n} ref{plural}: {suffix}")
    return (
        f"{entry.verb} {entry.path.relative_to(root)} ({'; '.join(parts)})"
    )


def _migrate_refs_in_plan(
    root: pathlib.Path,
    existing_ids: dict[str, pathlib.Path],
    plan: list[_AdoptEntry],
    skip_notes: list[str],
) -> None:
    """Rewrite markdown path-refs in each plan entry's body to croc dialect.

    Mutates `plan` entries in-place (updating `new_content` and the
    `migrated_refs` record) and appends `SKIP-REF` notes for any
    unresolvable refs.
    """
    # Build the complete post-adopt path → id map. Managed files contribute
    # their existing id; plan entries contribute their proposed id. Keys
    # are resolved absolute paths so relative-path resolution from any
    # source file lands on a canonical key.
    path_to_id: dict[pathlib.Path, str] = {}
    for id_, p in existing_ids.items():
        path_to_id[p.resolve()] = id_
    for entry in plan:
        path_to_id[entry.path.resolve()] = entry.proposed_id

    for entry in plan:
        parts = entry.new_content.split("---\n", 2)
        if len(parts) < 3:
            continue  # shouldn't happen for plan entries we construct
        _, fm_text, body = parts

        new_body, unresolved, migrated = _migrate_refs_in_body(
            body, entry.path, root, path_to_id
        )
        entry.migrated_refs.extend(migrated)

        for note in unresolved:
            skip_notes.append(f"SKIP-REF {note}")

        # After migration, make sure every strong body ref is declared in
        # frontmatter `links` (Rule 5 — identity). This is correct for
        # both refs we migrated AND any pre-existing `[[id:X]]` refs in
        # the body, so scaffolds with hand-written croc refs also work.
        fm = yaml.safe_load(fm_text) or {}
        body_strong_ids = {
            m.group(1) for m in STRONG_REF.finditer(new_body)
        }
        declared = {
            link["to"]
            for link in fm.get("links", [])
            if isinstance(link, dict) and "to" in link
        }
        missing = body_strong_ids - declared

        body_changed = new_body != body
        links_changed = bool(missing)

        if not (body_changed or links_changed):
            continue

        if links_changed:
            new_links = list(fm.get("links", []))
            for mid in sorted(missing):
                new_links.append({"to": mid, "strength": "strong"})
            fm["links"] = new_links

        fm_yaml = yaml.dump(fm, sort_keys=False, default_flow_style=None)
        entry.new_content = f"---\n{fm_yaml}---\n{new_body}"


def _migrate_refs_in_body(
    body: str,
    source_abs_path: pathlib.Path,
    root: pathlib.Path,
    path_to_id: dict[pathlib.Path, str],
) -> tuple[str, list[str], list[str]]:
    """Rewrite markdown path-refs in `body`.

    Returns `(new_body, unresolved_notes, migrated_paths)`.
    `migrated_paths` is the raw path strings (as they appeared in the
    markdown source) that were successfully rewritten — usable for
    audit logs and diff-style reporting.
    """
    unresolved: list[str] = []
    migrated: list[str] = []
    source_rel = source_abs_path.relative_to(root)

    def replace(m: re.Match) -> str:
        text = m.group("text").strip()
        rel_path = m.group("path")
        anchor = m.group("anchor") or ""

        # Phase 0: case-sensitivity. croc only recognizes `.md`; any other
        # casing is silently-rotting-link territory, so reject with a
        # targeted message — and offer the normalized variant as a "did you
        # mean" hint when the lowercase target exists on disk.
        if _case_mismatch_ext(rel_path):
            normalized = rel_path[:-3] + ".md"
            hint = ""
            try:
                normalized_abs = (source_abs_path.parent / normalized).resolve()
                if normalized_abs in path_to_id:
                    hint = f"; did you mean {normalized!r}?"
            except OSError:
                pass
            unresolved.append(
                f"{source_rel}: path ref {rel_path!r} uses non-lowercase "
                f"`.md` extension (croc recognizes `.md` only){hint}"
            )
            return m.group(0)

        # Phase 1: resolve to an absolute, normalized path. resolve() with
        # default strict=False doesn't require the target to exist — it
        # just canonicalizes. OSError is rare (e.g. permission denied
        # traversing a parent).
        try:
            target_abs = (source_abs_path.parent / rel_path).resolve()
        except OSError as e:
            unresolved.append(
                f"{source_rel}: path ref {rel_path!r} could not be resolved "
                f"({e})"
            )
            return m.group(0)

        # Phase 2: must land under the tree root. If not, surface the
        # resolved absolute path so the author can see where it landed.
        try:
            target_rel = target_abs.relative_to(root)
        except ValueError:
            unresolved.append(
                f"{source_rel}: path ref {rel_path!r} escapes tree root "
                f"(resolved to: {target_abs})"
            )
            return m.group(0)

        # Phase 3: path is valid and under root, but no managed doc lives
        # there. Surface the tree-relative resolved path so case /
        # symlink / missing-file issues are diagnosable at a glance.
        if target_abs not in path_to_id:
            unresolved.append(
                f"{source_rel}: path ref {rel_path!r} does not resolve to "
                f"any doc in the tree (tried: {target_rel})"
            )
            return m.group(0)

        target_id = path_to_id[target_abs]
        migrated.append(rel_path)
        rewrite = f"[[id:{target_id}"
        if anchor:
            rewrite += anchor  # keeps leading `#`
        if text:
            rewrite += f"|{text}"
        rewrite += "]]"
        return rewrite

    return MD_PATH_REF.sub(replace, body), unresolved, migrated


# ---------------------------------------------------------------------------
# Diagnostic: find markdown path-refs in the tree
# ---------------------------------------------------------------------------


@dataclass
class PathRefReport:
    source: DocPath       # source file, relative to root
    raw_path: str         # path as written in the markdown link
    target: DocPath | None  # resolved target, relative to root (None if unresolved)
    resolved: bool        # whether the target exists as a file under root
    note: str | None = None  # reason for unresolved status, if explanatory


def scan_path_refs(root: pathlib.Path) -> list[PathRefReport]:
    """Walk the tree and report every markdown path-ref.

    Works on any markdown tree — no croc frontmatter required. Useful as
    a health check before `init --adopt --migrate-refs` to see how many
    refs would migrate cleanly vs. end up as `SKIP-REF` notes.
    """
    root = root.resolve()
    if not root.is_dir():
        raise OpError(f"{root}: not a directory")

    reports: list[PathRefReport] = []
    for p in sorted(root.rglob("*.md")):
        source_rel = DocPath(str(p.relative_to(root)))
        try:
            text = p.read_text()
        except OSError:
            continue
        for m in MD_PATH_REF.finditer(text):
            raw = m.group("path")
            target_rel: DocPath | None = None
            resolved = False
            note: str | None = None

            if _case_mismatch_ext(raw):
                note = (
                    "non-lowercase `.md` extension "
                    "(croc recognizes `.md` only)"
                )
            else:
                try:
                    target_abs = (p.parent / raw).resolve()
                    target_abs.relative_to(root)
                    if target_abs.exists() and target_abs.is_file():
                        target_rel = DocPath(str(target_abs.relative_to(root)))
                        resolved = True
                except (OSError, ValueError):
                    pass

            reports.append(
                PathRefReport(
                    source=source_rel,
                    raw_path=raw,
                    target=target_rel,
                    resolved=resolved,
                    note=note,
                )
            )
    return reports


def _classify_for_adopt(
    p: pathlib.Path,
    root: pathlib.Path,
    existing_ids: dict[str, pathlib.Path],
    skip_notes: list[str],
    migrate_refs: bool = True,
) -> _AdoptEntry | None:
    """Classify one .md file. Mutates `existing_ids` and `skip_notes`.

    Returns an `_AdoptEntry` if the file needs writing (SCAFFOLD, AUGMENT,
    or MIGRATE-only), or None if the file is already fully managed AND has
    no body path-refs to migrate (or is being skipped).

    MIGRATE-only entries are created for already-managed files that happen
    to contain markdown path-refs — fixing the "re-running adopt can't
    reach managed files" gap. Their `new_content` is the original raw text;
    the migration phase rewrites it if any refs resolve.
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

    # --- Case B: already fully managed ---
    required = ("id", "title", "kind", "links")
    if all(f in fm for f in required):
        existing_ids[fm["id"]] = p
        # If the body still has markdown path-refs, we need to migrate them
        # on this run — otherwise re-running `init --adopt` on a previously
        # adopted tree can't reach the files that need dialect fixes.
        if migrate_refs and MD_PATH_REF.search(body):
            return _AdoptEntry(
                path=p,
                new_content=raw,
                proposed_id=fm["id"],
                verb="MIGRATE",
                is_new_id=False,
            )
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


# ---------------------------------------------------------------------------
# Molt — the inverse of adopt
# ---------------------------------------------------------------------------
#
# `molt_tree` sheds the croc dialect: rewrites `[[id:X]]` body refs back
# to plain-markdown `[text](path.md)` syntax, strips croc-specific
# frontmatter fields, and removes `.croc.toml`. Same transactional shape
# as `rename_id`.

# Croc-specific frontmatter fields. `init --adopt` writes these; `molt`
# strips them. Every other key (title, type, mirrors, created, custom
# stuff) is preserved verbatim in its original order.
_CROC_FRONTMATTER_FIELDS: tuple[str, ...] = ("id", "kind", "links")


def molt_tree(
    root: pathlib.Path, dry_run: bool = False
) -> list[str]:
    """Reverse adoption.

    Rewrites every `[[id:X]]` / `[[see:X]]` body ref back into plain
    `[text](path.md[#anchor])` markdown, strips `id`/`kind`/`links` from
    frontmatter (preserving every other key), removes empty-after-strip
    frontmatter blocks entirely, and deletes `.croc.toml`.

    The tree must pass `check` first — same pre-condition as `rename`.
    `dry_run=True` runs validation and planning but writes nothing.
    """
    root = root.resolve()
    docs = _assert_sound(root)  # pre-check; raises OpError if unsound
    index = build_index(docs)
    id_to_title: dict[DocId, str] = {
        d.id: str(d.frontmatter.get("title", d.id)) for d in docs
    }

    plan: dict[DocPath, str] = {}
    per_file_stats: dict[DocPath, tuple[int, list[str]]] = {}
    skip_notes: list[str] = []

    for d in docs:
        abs_source = root / d.path
        new_body, refs_rewritten, dangling_weak = _molt_body(
            d.body, abs_source, root, index, id_to_title
        )
        for ghost_id in dangling_weak:
            skip_notes.append(
                f"SKIP-MOLT-REF {d.path}: weak ref [[see:{ghost_id}]] "
                f"points to no managed doc; left as-is (no path to rewrite to)"
            )
        new_fm, stripped_fields = _molt_frontmatter(d.frontmatter)
        new_content = _render_molted(new_fm, new_body)
        current = abs_source.read_text()
        if new_content != current:
            plan[d.path] = new_content
            per_file_stats[d.path] = (refs_rewritten, stripped_fields)

    marker = root / ".croc.toml"
    remove_marker = marker.exists()

    _simulate_molt(plan)  # raises OpError if any planned output is malformed

    actions: list[str] = list(skip_notes)
    for rel_path in sorted(plan):
        rw, stripped = per_file_stats[rel_path]
        parts: list[str] = []
        if rw:
            parts.append(f"rewrote {rw} ref{'s' if rw != 1 else ''}")
        if stripped:
            parts.append(f"stripped {', '.join(stripped)}")
        actions.append(f"MOLT {rel_path} ({'; '.join(parts)})")
    if remove_marker:
        actions.append(f"REMOVE {marker.relative_to(root)}")

    if dry_run:
        return actions

    if plan:
        _commit(root, plan)
    if remove_marker:
        marker.unlink()
    return actions


def _molt_body(
    body: str,
    source_abs_path: pathlib.Path,
    root: pathlib.Path,
    index: dict[DocId, DocPath],
    id_to_title: dict[DocId, str],
) -> tuple[str, int, list[str]]:
    """Rewrite every `[[id:X]]` / `[[see:X]]` ref in `body` to plain
    markdown `[display](relative/path.md[#anchor])`.

    Returns (new_body, refs_rewritten, dangling_weak_ids).

    Bare refs (no `|display`) fall back to the target's frontmatter
    `title`; if the target has no title, the id itself is used. Paths
    are normalized to forward slashes so molted output is portable
    across OS.

    Weak refs to absent targets are intentionally tolerated by croc
    (that's the whole `Weak<T>` design). During molt they have no
    target path to rewrite to, so we leave the original `[[see:X]]`
    in place and record the id in the returned dangling list — molt
    surfaces these as SKIP-MOLT-REF notes rather than forging a path
    that never existed. Strong refs can't legally be dangling (the
    pre-check rejects those trees); we defensively leave those in
    place too rather than crash.
    """
    count = 0
    dangling_weak: list[str] = []
    source_dir = source_abs_path.parent

    def _replace(m: re.Match, *, strong: bool) -> str:
        nonlocal count
        target_id = DocId(m.group(1))
        if target_id not in index:
            if not strong:
                dangling_weak.append(target_id)
            return m.group(0)
        anchor = m.group(2) or ""
        display = m.group(3) or id_to_title.get(target_id, target_id)
        target_abs = root / pathlib.Path(index[target_id])
        rel = os.path.relpath(target_abs, start=source_dir).replace(
            os.sep, "/"
        )
        suffix = f"#{anchor}" if anchor else ""
        count += 1
        return f"[{display}]({rel}{suffix})"

    body = STRONG_REF.sub(lambda m: _replace(m, strong=True), body)
    body = WEAK_REF.sub(lambda m: _replace(m, strong=False), body)
    return body, count, dangling_weak


def _molt_frontmatter(fm: dict) -> tuple[dict | None, list[str]]:
    """Strip croc-specific fields from `fm`.

    Returns (new_fm, stripped_field_names). `new_fm is None` when the
    resulting mapping would be empty — callers then omit the frontmatter
    block entirely, so the molted file reads as untouched plain markdown.
    """
    stripped = [f for f in _CROC_FRONTMATTER_FIELDS if f in fm]
    new_fm = {k: v for k, v in fm.items() if k not in _CROC_FRONTMATTER_FIELDS}
    if not new_fm:
        return None, stripped
    return new_fm, stripped


def _render_molted(fm: dict | None, body: str) -> str:
    """Recombine frontmatter + body for a molted file.

    When `fm is None`, the frontmatter block is omitted entirely. The
    leading newline that `parse_frontmatter` preserved from the original
    file's blank-line separator is trimmed, so the molted file doesn't
    open with an extraneous `\\n`.
    """
    if fm is None:
        return body.lstrip("\n")
    fm_yaml = yaml.dump(fm, sort_keys=False, default_flow_style=None)
    return f"---\n{fm_yaml}---\n{body}"


def _simulate_molt(plan: dict[DocPath, str]) -> None:
    """Verify every planned molted file is syntactically sound.

    Checks: balanced frontmatter delimiters if any, YAML parseable if
    a frontmatter block remains. Raises OpError on violation — molt
    refuses to write a file it can't verify.
    """
    for rel_path, content in plan.items():
        if not content.startswith("---\n"):
            continue
        parts = content.split("---\n", 2)
        if len(parts) < 3:
            raise OpError(
                f"molt simulation: {rel_path} would have unterminated "
                f"frontmatter"
            )
        try:
            yaml.safe_load(parts[1])
        except yaml.YAMLError as e:
            raise OpError(
                f"molt simulation: {rel_path} would have invalid YAML "
                f"frontmatter ({e})"
            )
