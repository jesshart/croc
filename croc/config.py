"""`.croc.toml` parser and schema validator.

The file holds two kinds of content:

1. The `version` marker — written by `init` / `init --adopt`, stripped
   by `molt`. Indicates the tree is currently under croc management.
2. Foreign config — `[[trace]]` patterns consumed by `croc attack` /
   `croc hunt`, a `[hunt]` table for default strictness, and anything
   else users may add. Preserved through the adopt / molt lifecycle.

Reading is via stdlib `tomllib` (Python 3.11+); no writer is needed
here — molt's surgical strip lives in `ops.py`.
"""

from __future__ import annotations

import pathlib
import re
import tomllib
from dataclasses import dataclass, field
from typing import Any, cast


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


def load_config(root: pathlib.Path) -> CrocConfig:
    """Load and validate `.croc.toml` at `root`.

    Raises `ConfigError` if the file is malformed, any `[[trace]]` entry
    is missing required fields, a regex fails to compile, or a regex
    doesn't have exactly one capture group.

    Returns a default `CrocConfig` (no patterns, strict hunt) when the
    file is absent — callers that need the file present should check
    `root / ".croc.toml"` themselves.
    """
    path = root / ".croc.toml"
    if not path.exists():
        return CrocConfig(version=None)

    try:
        raw = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: invalid TOML ({e})") from e

    version = raw.get("version")
    if version is not None and not isinstance(version, str):
        raise ConfigError(f"{path}: `version` must be a string")

    traces_raw = raw.get("trace", [])
    if not isinstance(traces_raw, list):
        raise ConfigError(f"{path}: `trace` must be an array of tables")

    traces: list[TracePattern] = []
    for i, raw_entry in enumerate(traces_raw):
        where = f"{path}: [[trace]] #{i + 1}"
        if not isinstance(raw_entry, dict):
            raise ConfigError(f"{where}: must be a table")
        # Widen the narrowed dict type so ty doesn't infer `Never` for
        # successive .get() key literals.
        entry = cast(dict[str, Any], raw_entry)
        name = entry.get("name")
        pattern_str = entry.get("pattern")
        code_globs = entry.get("code_globs")
        if not isinstance(name, str) or not name:
            raise ConfigError(f"{where}: `name` must be a non-empty string")
        if not isinstance(pattern_str, str) or not pattern_str:
            raise ConfigError(f"{where}: `pattern` must be a non-empty string")
        if not isinstance(code_globs, list) or not code_globs:
            raise ConfigError(f"{where}: `code_globs` must be a non-empty array of strings")
        if not all(isinstance(g, str) for g in code_globs):
            raise ConfigError(f"{where}: `code_globs` must contain only strings")
        try:
            compiled = re.compile(pattern_str)
        except re.error as e:
            raise ConfigError(f"{where}: invalid regex ({e})") from e
        if compiled.groups != 1:
            raise ConfigError(f"{where}: `pattern` must have exactly one capture group (found {compiled.groups})")
        traces.append(TracePattern(name=name, pattern=compiled, code_globs=tuple(code_globs)))

    hunt_raw = raw.get("hunt", {})
    if not isinstance(hunt_raw, dict):
        raise ConfigError(f"{path}: `[hunt]` must be a table")
    strict = hunt_raw.get("strict", True)
    if not isinstance(strict, bool):
        raise ConfigError(f"{path}: `hunt.strict` must be a boolean")

    return CrocConfig(version=version, traces=tuple(traces), hunt=HuntConfig(strict=strict))
