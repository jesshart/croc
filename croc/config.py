"""`.croc.toml` parser and schema validator.

The file holds two kinds of content:

1. The `version` marker — written by `init` / `init --adopt`. Indicates
   the tree is currently under croc management. Inert: nothing branches
   on its value, and `molt` no longer strips it (the file is the user's
   to keep or remove).
2. Foreign config — `[[trace]]` patterns consumed by `croc attack` /
   `croc hunt`, a `[hunt]` table for default strictness, and anything
   else users may add.

Discovery walks up from the doc-tree `root` to the git repo root, so the
repo-scoped scan config can live at the project root next to
`pyproject.toml`. A single file can describe multiple doc trees via
`[trees."<path>"]` tables, keyed by tree path relative to the file's
directory; a per-tree table overrides the top-level defaults for that
tree. See `_find_croc_toml` and `load_config`.

Reading is via stdlib `tomllib` (Python 3.11+).
"""

from __future__ import annotations

import pathlib
import re
import tomllib
from dataclasses import dataclass, field
from typing import Any, cast

from croc.gitutil import git_repo_root


class ConfigError(Exception):
    """`.croc.toml` is missing or malformed."""


@dataclass(frozen=True)
class TracePattern:
    name: str
    pattern: re.Pattern[str]
    code_globs: tuple[str, ...]


@dataclass(frozen=True)
class HuntConfig:
    strict: bool = True


@dataclass(frozen=True)
class CrocConfig:
    version: str | None
    traces: tuple[TracePattern, ...] = ()
    hunt: HuntConfig = field(default_factory=HuntConfig)


@dataclass(frozen=True)
class _TreeOverride:
    """A per-tree `[trees."<path>"]` table, parsed. `None` fields mean
    "not specified — inherit the top-level default"."""

    version: str | None
    traces: tuple[TracePattern, ...] | None
    hunt: HuntConfig | None


def _find_croc_toml(tree_root: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path] | None:
    """Locate the `.croc.toml` governing `tree_root`.

    Walks parents from `tree_root` up to and including the git repo root,
    returning the first `.croc.toml` found as `(file_path, config_dir)`.
    Because the walk starts at `tree_root`, a tree-local file (the legacy
    location) always wins over one higher up. Outside a git repo only the
    tree-local file is considered. Returns `None` when no file is found.

    `config_dir` anchors the per-tree key (`tree_root` relative to it).
    """
    tree_root = tree_root.resolve()
    ceiling = git_repo_root(tree_root)
    cur = tree_root
    while True:
        candidate = cur / ".croc.toml"
        if candidate.exists():
            return candidate, cur
        if ceiling is None or cur == ceiling or cur.parent == cur:
            return None
        cur = cur.parent


def _tree_key(tree_root: pathlib.Path, config_dir: pathlib.Path) -> str | None:
    """The `[trees."<key>"]` lookup key for `tree_root` under `config_dir`.

    The tree path relative to the config file's directory, posix-style
    (e.g. `"thoughts"`, `"docs/internal"`). `"."` when the file sits at
    the tree root. `None` when `tree_root` is not under `config_dir`.
    """
    try:
        rel = tree_root.resolve().relative_to(config_dir.resolve())
    except ValueError:
        return None
    return rel.as_posix()


def _parse_version(version: Any, where: str) -> str | None:
    if version is not None and not isinstance(version, str):
        raise ConfigError(f"{where}: `version` must be a string")
    return version


def _parse_traces(traces_raw: Any, where: str) -> tuple[TracePattern, ...]:
    """Validate an array-of-tables of `[[trace]]` entries.

    `where` is the scope prefix used in error messages (the file path for
    top-level config, or `<path>: [trees."<key>"]` for a per-tree table).
    """
    if not isinstance(traces_raw, list):
        raise ConfigError(f"{where}: `trace` must be an array of tables")

    traces: list[TracePattern] = []
    for i, raw_entry in enumerate(traces_raw):
        loc = f"{where}: [[trace]] #{i + 1}"
        if not isinstance(raw_entry, dict):
            raise ConfigError(f"{loc}: must be a table")
        # Widen the narrowed dict type so ty doesn't infer `Never` for
        # successive .get() key literals.
        entry = cast(dict[str, Any], raw_entry)
        name = entry.get("name")
        pattern_str = entry.get("pattern")
        code_globs = entry.get("code_globs")
        if not isinstance(name, str) or not name:
            raise ConfigError(f"{loc}: `name` must be a non-empty string")
        if not isinstance(pattern_str, str) or not pattern_str:
            raise ConfigError(f"{loc}: `pattern` must be a non-empty string")
        if not isinstance(code_globs, list) or not code_globs:
            raise ConfigError(f"{loc}: `code_globs` must be a non-empty array of strings")
        if not all(isinstance(g, str) for g in code_globs):
            raise ConfigError(f"{loc}: `code_globs` must contain only strings")
        try:
            compiled = re.compile(pattern_str)
        except re.error as e:
            raise ConfigError(f"{loc}: invalid regex ({e})") from e
        if compiled.groups != 1:
            raise ConfigError(f"{loc}: `pattern` must have exactly one capture group (found {compiled.groups})")
        traces.append(TracePattern(name=name, pattern=compiled, code_globs=tuple(code_globs)))
    return tuple(traces)


def _parse_hunt(hunt_raw: Any, where: str) -> HuntConfig:
    if not isinstance(hunt_raw, dict):
        raise ConfigError(f"{where}: `[hunt]` must be a table")
    strict = hunt_raw.get("strict", True)
    if not isinstance(strict, bool):
        raise ConfigError(f"{where}: `hunt.strict` must be a boolean")
    return HuntConfig(strict=strict)


def _parse_trees(trees_raw: Any, path: pathlib.Path) -> dict[str, _TreeOverride]:
    """Validate the optional `[trees]` table-of-tables.

    Every entry is validated eagerly (not just the one matching the active
    tree) so a typo in any tree's config surfaces in CI, mirroring how
    top-level `[[trace]]` entries are all validated up front.
    """
    if not isinstance(trees_raw, dict):
        raise ConfigError(f"{path}: `trees` must be a table of tables")

    parsed: dict[str, _TreeOverride] = {}
    for key, table in cast(dict[str, Any], trees_raw).items():
        where = f'{path}: [trees."{key}"]'
        if not isinstance(table, dict):
            raise ConfigError(f"{where}: must be a table")
        tt = cast(dict[str, Any], table)
        parsed[key] = _TreeOverride(
            version=_parse_version(tt.get("version"), where) if "version" in tt else None,
            traces=_parse_traces(tt["trace"], where) if "trace" in tt else None,
            hunt=_parse_hunt(tt["hunt"], where) if "hunt" in tt else None,
        )
    return parsed


def load_config(tree_root: pathlib.Path) -> CrocConfig:
    """Discover and load the `.croc.toml` governing `tree_root`.

    Walks up from `tree_root` to the git repo root (see `_find_croc_toml`).
    Top-level `version` / `[[trace]]` / `[hunt]` are the defaults; a
    matching `[trees."<rel-path>"]` table overrides them for this tree
    (its `trace` *replaces* the top-level set, rather than concatenating).

    Raises `ConfigError` if the file is malformed, any `[[trace]]` entry
    is missing required fields, a regex fails to compile, or a regex
    doesn't have exactly one capture group.

    Returns a default `CrocConfig` (no patterns, strict hunt) when no
    file is found.
    """
    found = _find_croc_toml(tree_root)
    if found is None:
        return CrocConfig(version=None)
    path, config_dir = found

    try:
        raw = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: invalid TOML ({e})") from e

    top_version = _parse_version(raw.get("version"), str(path))
    top_traces = _parse_traces(raw.get("trace", []), str(path))
    top_hunt = _parse_hunt(raw.get("hunt", {}), str(path))

    trees = _parse_trees(raw.get("trees", {}), path)
    key = _tree_key(tree_root, config_dir)
    override = trees.get(key) if key is not None else None

    if override is None:
        return CrocConfig(version=top_version, traces=top_traces, hunt=top_hunt)
    return CrocConfig(
        version=override.version if override.version is not None else top_version,
        traces=override.traces if override.traces is not None else top_traces,
        hunt=override.hunt if override.hunt is not None else top_hunt,
    )
