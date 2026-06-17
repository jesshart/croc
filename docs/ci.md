---
icon: lucide/check-check
---

# CI integration

The whole value of croc is mechanical enforcement. Wire it into the same gates as your linters and type checker.

## As a pre-commit hook

Add to `.pre-commit-config.yaml`:

```yaml
repos:
  - repo: local
    hooks:
      - id: croc-check
        name: croc check
        entry: uv run croc check path/to/docs/
        language: system
        pass_filenames: false
        files: ^path/to/docs/
```

Or as a plain `.git/hooks/pre-commit`:

```bash
#!/bin/sh
uv run croc check path/to/docs/ || exit 1
```

## Pre-commit hook for `croc hunt`

Catch code changes that outpace their documentation. Add alongside `croc-check`:

```yaml
repos:
  - repo: local
    hooks:
      - id: croc-hunt
        name: croc hunt
        entry: uv run croc hunt path/to/docs/
        language: system
        pass_filenames: false
```

[`croc hunt`](commands.md#croc-hunt) defaults to diffing against `--cached` (staged changes) which is what you want here; use `--base main` in CI pipelines instead.

The `entry` above passes the doc tree (`path/to/docs/`); `hunt` and `attack` discover `.croc.toml` by walking up from there to the git repo root, so the trace/hunt config can sit at the repo root next to `pyproject.toml`. A repo with several doc trees keeps one repo-root file using `[trees."<path>"]` tables.

## In GitHub Actions

```yaml
- name: croc check
  run: |
    uv sync
    uv run croc check path/to/docs/
```

For a PR gate that fires hunt against the merge base:

```yaml
- name: croc hunt
  run: |
    uv sync
    uv run croc hunt path/to/docs/ --base ${{ github.base_ref }}
```

## Brownfield migration with `croc refs`

When adopting croc onto an existing markdown tree, run [`croc refs --unresolved`](commands.md#croc-refs) in CI first — it walks every `[text](path.md)` link and reports broken ones. Exits 1 on any unresolved ref. Drop it in before `init --adopt --migrate-refs` runs in your migration PR so the broken refs surface in review rather than disappearing into the croc dialect.

## Recommended combo for a "production" tree

| Stage         | Command                                  | Catches                                     |
| ------------- | ---------------------------------------- | ------------------------------------------- |
| pre-commit    | `croc check thoughts/`                   | Schema, dangling refs, identity violations  |
| pre-commit    | `croc hunt thoughts/`                    | Code that drifted from its bound doc        |
| pre-commit    | `croc lurk thoughts/`                    | Files that grew past the 100-line guardrail |
| PR / CI       | `croc check thoughts/`                   | Same, defense in depth                      |
| PR / CI       | `croc hunt thoughts/ --base main`        | Range diff, not just staged                 |
