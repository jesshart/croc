---
icon: lucide/wrench
---

# Contributing

## Layout

```
croc/
├── croc/
│   ├── __init__.py
│   ├── bask.py        # flatten a markdown tree into a single dir
│   ├── check.py       # borrow checker; pure over list[Doc]
│   ├── crawl.py       # scaffold plain-markdown trees from source
│   └── ops.py         # transformations: move, rename, init, adopt, molt
├── main.py            # Typer CLI — thin wrapper around ops
├── tests/
│   ├── conftest.py    # shared fixtures (tmp_path trees)
│   ├── test_bask.py   # flatten, ref rewriting, CLI surface
│   ├── test_check.py  # parser + five rules
│   ├── test_cli.py    # Typer CLI surface + exit codes
│   ├── test_crawl.py  # plan/build, filters, adopt/molt cycle
│   └── test_ops.py    # move, rename, init, adopt, molt, dry-run
├── docs/              # this site (Zensical)
├── examples/thoughts/                # canonical sample tree
├── examples/thoughts-from-code/      # crawl fixture (src + adopted output)
└── pyproject.toml
```

## Design principles

**Separation of concerns.** `check.py` is verification (pure, no I/O). `ops.py` is transformation (parse → check → plan → simulate → commit). Each raises a typed exception — `TreeError` for parse failures, `OpError` for precondition failures — so the CLI can map cleanly to exit codes.

**Validate-then-commit.** No operation writes to disk until its plan has been simulated and re-checked in memory. If validation fails, nothing is half-committed; if the physical commit fails mid-sequence, a snapshot-based rollback restores the already-written files.

**Newtype discipline.** `DocId` and `DocPath` are distinct `NewType` aliases over `str`. The parser enforces the id grammar at the boundary, so runtime values match their declared types.

**`--dry-run` is universal.** Every mutating operation accepts `dry_run=True` and skips only the final commit step. The simulation machinery is the same either way, so dry-run and real runs exercise identical code paths.

## Adding a new command

1. Add the implementation to `croc/ops.py`. Raise `OpError` on precondition failures. Follow load → check → plan → simulate → commit.
2. Wire the Typer command in `main.py`. Keep it thin — CLI only formats output and maps exceptions to exit codes.
3. Add tests in `tests/test_ops.py`. Use the `tmp_path` + `write_doc` / `sample_tree` fixtures.
4. Add a `--dry-run` flag if the command writes.

The [Commands page](commands.md) is regenerated from `main.py` via `typer utils docs`, so help strings you write *are* the documentation. Treat them accordingly.

## Running tests

```bash
uv sync --group dev
uv run pytest                # ~190 tests, ~0.3s
uv run pytest -v             # verbose
uv run pytest -k rename      # filter by name
```

Or via the Makefile:

```bash
make install      # sync + install pre-commit hook
make test         # pytest
make check        # lint + test + smoke
```

The test suite encodes the guarantees as regressions. Notable cases:

- `test_failed_rename_leaves_tree_unchanged` — fingerprints the tree, runs four failing renames back-to-back, asserts no file changed. Captures the validate-then-commit property.
- `test_post_adopt_check_passes` — adopts a fresh unmanaged tree, then runs `check`. Proves `init --adopt` produces a sound tree out of the gate.
- `test_dry_run_writes_nothing` (×3) — fingerprint-before / dry-run / fingerprint-after, applied uniformly across move, rename, and adopt.

## Running the docs site locally

```bash
make docs-serve       # regenerates docs/commands.md, then `zensical serve`
```

The site builds at `localhost:8000` and live-reloads on file changes. `docs/commands.md` is gitignored and regenerated from the Typer CLI source on each run.

## Known limitations

- **YAML round-trip formatting.** `rename` re-serializes frontmatter; inline flow style `{ to: X, strength: Y }` may render as `{to: X, strength: Y}`. Cosmetic — swap `yaml.dump` for `ruamel.yaml` if formatting preservation matters.
- **No `.crocignore`.** Trees with vendored READMEs or generated files need `init --adopt` pointed at a subdirectory.
- **Symlinked subtrees are not traversed.** `scan_symlinks` emits warnings; the user decides whether to follow.

## Releasing

Releases are cut through GitHub Releases — **not** a manual publish command. Publishing a release fires `.github/workflows/publish.yml`, which runs `uv build && uv publish --trusted-publishing always` inside the `pypi` environment via OIDC. There are no stored PyPI tokens to rotate.

The flow:

1. Land changes on `main`. Ensure CI is green (`style` + `test` workflows).
2. Bump `version` in `pyproject.toml` and `__version__` in `croc/__init__.py`. Keep them in sync. Pre-1.0: minor bump for new features, patch bump for fixes.
3. Promote the `## Unreleased` section in `CHANGELOG.md` to `## X.Y.Z — YYYY-MM-DD`. Add a fresh empty `## Unreleased` above it.
4. Commit as `chore(release): vX.Y.Z`. Duplicate the CHANGELOG section into the commit body so the commit stands alone.
5. Push: `git push origin main`.
6. Tag with the full release notes in the annotation. Write the new CHANGELOG entry's body to a temp file, then: `git tag -a vX.Y.Z --cleanup=verbatim -F notes.md && git push origin vX.Y.Z`.
7. Create the GitHub Release from the tag: `gh release create vX.Y.Z --notes-from-tag --title vX.Y.Z` (or use the web UI). Publication triggers the publish workflow.

!!! note "Why steps 6 and 7 look like this"

    `gh release create --notes-from-tag` copies the tag annotation verbatim into the release body, so the annotation *is* the release notes — `-m vX.Y.Z` produces an effectively empty release body (releases v0.4.0 through v0.6.0 shipped this way; their bodies are just the literal string "vX.Y.Z"). `-F notes.md` fills the annotation from a real file. `--cleanup=verbatim` is required to keep `##` markdown headers — without it, git strips every line starting with `#` as a comment, so your section structure disappears before it ever reaches GitHub.

The `make pypi` target is a **manual fallback only**, not the canonical path. It runs `uv build && uv publish` locally and requires a PyPI token in the environment; it bypasses CI's clean build and the version/tag/changelog discipline above. Avoid it unless Trusted Publishing is down.
