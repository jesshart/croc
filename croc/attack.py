"""Scan source files for declared regex patterns and bind matches to
docs by writing `tracks:` entries in frontmatter.

Pattern shape: each `[[trace]]` in `.croc.toml` declares a regex with
exactly one capture group. Each match in any file under `code_globs`
yields `(capture_identifier, source_path)` — the capture is resolved
to a `.md` file by filename stem, and the source path is added to
that doc's `tracks:` list. Paths are stored relative to the git repo
root so `hunt` can mechanically line them up against
`git diff --name-only`.

Behavior matrix for capture resolution:

- 0 doc matches → SKIP-TRACE note, no write.
- 1 doc match   → bind: append source path to that doc's `tracks:`.
- N>1 matches   → SKIP-TRACE note (ambiguous), no write.

Idempotent: `attack` re-derives the full `tracks:` list from a fresh
scan. Stale entries (from patterns the user removed or code that was
refactored) are dropped. Docs that end up with no tracks have the
field removed entirely.
"""

from __future__ import annotations

import pathlib
import subprocess
from collections import defaultdict
from dataclasses import dataclass

import yaml

from croc.check import DocPath
from croc.config import CrocConfig
from croc.crawl import resolve_file_filter
from croc.ops import OpError, _commit, _dump_yaml


@dataclass
class _AttackPlanEntry:
    doc_abs: pathlib.Path
    new_content: str
    new_tracks: list[str]  # repo-root-relative source paths, sorted


def attack_tree(
    tree_root: pathlib.Path,
    config: CrocConfig,
    *,
    dry_run: bool = False,
    include_untracked: bool = False,
    git_files: set[pathlib.Path] | None = None,
) -> list[str]:
    """Scan code per `config.traces`, bind captures to docs, rewrite
    `tracks:` frontmatter. Returns action log.

    Raises `OpError` when the tree is not inside a git repo (paths
    can't be anchored) or `config.traces` is empty (nothing to do).

    `git_files` overrides the computed filter — useful for tests that
    want explicit control. In normal operation, leave it `None` and
    let `attack_tree` derive the filter from the git repo root so it
    covers both the tree and the code directories at once. (The usual
    `_file_filter_for` can't be used here: it scopes to the tree root,
    but code lives above it.)
    """
    tree_root = tree_root.resolve()
    if not tree_root.is_dir():
        raise OpError(f"{tree_root}: not a directory")
    if not config.traces:
        raise OpError(f"no [[trace]] patterns configured in {tree_root / '.croc.toml'}")

    repo_root = _git_repo_root(tree_root)
    if repo_root is None:
        raise OpError(f"{tree_root}: attack requires a git repo (paths anchored to `git rev-parse --show-toplevel`)")

    if git_files is None:
        git_files = resolve_file_filter(repo_root, include_untracked=include_untracked)

    # Stem → list of absolute doc paths. Multiple hits on the same stem
    # is the ambiguity case.
    stem_index: dict[str, list[pathlib.Path]] = defaultdict(list)
    for p in sorted(tree_root.rglob("*.md")):
        if git_files is not None and p.resolve() not in git_files:
            continue
        stem_index[p.stem].append(p.resolve())

    # Walk code, collect bindings. `bindings[doc_abs]` accumulates the
    # set of source paths across every pattern — so two patterns hitting
    # the same doc from different files both contribute, same patterns
    # hitting the same doc from the same file dedupes.
    bindings: dict[pathlib.Path, set[str]] = defaultdict(set)
    skip_notes: list[str] = []
    tree_rel_to_repo = tree_root.relative_to(repo_root)
    for pattern in config.traces:
        for src_abs in _iter_matched_code_files(repo_root, pattern.code_globs, git_files):
            try:
                text = src_abs.read_text()
            except OSError:
                continue
            src_rel = src_abs.relative_to(repo_root).as_posix()
            for m in pattern.pattern.finditer(text):
                captured = m.group(1)
                matches = stem_index.get(captured, [])
                if not matches:
                    skip_notes.append(
                        f"SKIP-TRACE {pattern.name} in {src_rel}: "
                        f"capture {captured!r} has no matching `<stem>.md` under {tree_rel_to_repo}"
                    )
                elif len(matches) > 1:
                    where = ", ".join(str(m.relative_to(tree_root)) for m in matches)
                    skip_notes.append(
                        f"SKIP-TRACE {pattern.name} in {src_rel}: "
                        f"capture {captured!r} ambiguous — matched {len(matches)} docs: {where}"
                    )
                else:
                    bindings[matches[0]].add(src_rel)

    # Build plan entries. A doc with bindings either keeps its current
    # tracks (no-op) or gets a rewrite. A doc *without* bindings that
    # currently carries a `tracks:` field gets that field cleared —
    # so refactoring away a pattern drops stale entries.
    plan: list[_AttackPlanEntry] = []
    all_docs: set[pathlib.Path] = set()
    for docs in stem_index.values():
        all_docs.update(docs)
    for doc_abs in sorted(all_docs):
        sources = bindings.get(doc_abs, set())
        entry = _plan_doc_rewrite(doc_abs, sources)
        if entry is not None:
            plan.append(entry)

    actions: list[str] = list(skip_notes)
    for entry in plan:
        rel = entry.doc_abs.relative_to(tree_root)
        if entry.new_tracks:
            actions.append(f"ATTACK {rel} (tracks: {', '.join(entry.new_tracks)})")
        else:
            actions.append(f"ATTACK {rel} (cleared tracks)")

    if dry_run:
        return actions

    # Commit with _commit's snapshot-rollback shape. Keys must be
    # DocPath (a str NewType) relative to tree_root.
    commit_plan: dict[DocPath, str] = {
        DocPath(e.doc_abs.relative_to(tree_root).as_posix()): e.new_content for e in plan
    }
    if commit_plan:
        _commit(tree_root, commit_plan)

    return actions


def _plan_doc_rewrite(doc_abs: pathlib.Path, sources: set[str]) -> _AttackPlanEntry | None:
    """Compute the new content for `doc_abs` with `tracks:` set to the
    sorted `sources`. Return `None` if the doc already has exactly
    these tracks (no write needed).

    Works on any markdown file with frontmatter; scaffolds a frontmatter
    block if the file has none, so a bare stub can still be bound.
    """
    raw = doc_abs.read_text()
    fm, body, had_frontmatter = _split_frontmatter(raw)

    new_tracks = sorted(sources)
    raw_current = fm.get("tracks")
    current: list = list(raw_current) if isinstance(raw_current, list) else []
    if current == new_tracks and had_frontmatter:
        return None
    # Doc without frontmatter and no new tracks: nothing to do (don't
    # scaffold a frontmatter block just to write an empty field).
    if not new_tracks and not had_frontmatter:
        return None

    if new_tracks:
        fm["tracks"] = new_tracks
    elif "tracks" in fm:
        del fm["tracks"]

    if not fm:
        # All keys stripped and the file never had real frontmatter —
        # emit body only. Matches how _molt_frontmatter handles the
        # empty-after-strip case.
        new_content = body
    else:
        fm_yaml = _dump_yaml(fm)
        # Ensure a single blank line between the closing `---` and the body.
        normalized_body = body if body.startswith("\n") or not body else "\n" + body
        new_content = f"---\n{fm_yaml}---{normalized_body}"

    if new_content == raw:
        return None

    return _AttackPlanEntry(doc_abs=doc_abs, new_content=new_content, new_tracks=new_tracks)


def _split_frontmatter(raw: str) -> tuple[dict, str, bool]:
    """Return (frontmatter_mapping, body, had_frontmatter).

    Malformed frontmatter (opening fence but no closing) raises OpError;
    attack shouldn't silently rewrite a file it can't parse.
    """
    if not raw.startswith("---\n"):
        return {}, raw, False
    parts = raw.split("---\n", 2)
    if len(parts) < 3:
        raise OpError("unterminated frontmatter (opening --- without closing ---)")
    _, fm_text, body = parts
    try:
        fm = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError as e:
        raise OpError(f"invalid YAML frontmatter ({e})") from e
    if not isinstance(fm, dict):
        raise OpError("frontmatter is not a mapping")
    return fm, body, True


def _git_repo_root(start: pathlib.Path) -> pathlib.Path | None:
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


def _iter_matched_code_files(
    repo_root: pathlib.Path,
    code_globs: tuple[str, ...],
    git_files: set[pathlib.Path] | None,
) -> list[pathlib.Path]:
    """Enumerate files under `repo_root` matching any of `code_globs`.

    Uses `pathlib.Path.glob` (not `rglob`) so `**` is explicit in the
    user's globs. Results deduplicated across patterns and sorted for
    determinism. Filtered through `git_files` when present.
    """
    seen: set[pathlib.Path] = set()
    for glob in code_globs:
        for p in repo_root.glob(glob):
            if not p.is_file():
                continue
            absp = p.resolve()
            if git_files is not None and absp not in git_files:
                continue
            seen.add(absp)
    return sorted(seen)
