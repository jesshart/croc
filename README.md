# croc

A Rust-inspired, Typer-powered CLI for reliably managing project documentation.

`croc` treats a markdown doc tree the way Rust treats memory: **ids are owners, links are borrows, `croc check` is the borrow checker, and `croc rename` is an atomic refactor.** Move a file and every reference keeps working. Introduce a dangling link and the commit is refused.

---

## The problem

A `thoughts/` tree grows branching paths, nested directories with `self.md` files, and files that reference other files by path. When a file moves, every referrer has to be updated. The usual options all fail:

- **By hand:** grep for every reference and hope you didn't miss one.
- **With `sed`:** risk rewriting substring matches inside prose or code blocks.
- **With Obsidian/Notion:** silent link rot whenever someone edits outside the editor.

None of these prevent the *broken intermediate state* — the window where `main` has dangling refs and no one has noticed. `croc check` closes that window by refusing the commit; `croc rename` makes the refactor an atomic transaction.

## The idea

Replace path-based references with **stable ids**. A reference like `[[id:registry-pattern]]` resolves through a derived index of every `id` in the tree. When a file moves, the id travels with it; every link still works. When an id changes, one command rewrites every referrer atomically.

### Tree-as-memory

| Rust concept       | croc concept                                         |
| ------------------ | ---------------------------------------------------- |
| Ownership          | Each `.md` has a unique `id` in frontmatter          |
| Move semantics     | `mv` relocates bytes; id travels with the file       |
| `&T` (borrow)      | `[[id:X]]` — strong link                             |
| `Weak<T>`          | `[[see:X]]` — soft citation, may dangle              |
| Lifetimes          | Strong links may not outlive their target            |
| Newtype pattern    | `DocId` and `DocPath` are distinct types             |
| Borrow checker     | `croc check` refuses trees with broken invariants    |
| Validate-then-commit | Rewrites simulated in memory before any disk write |

## Quick start

Requires Python `>=3.13` and [`uv`](https://docs.astral.sh/uv/).

```bash
# Install
uv sync

# Check the included example
uv run croc check examples/thoughts

# Print the derived id → path index
uv run croc index examples/thoughts

# Adopt croc on a repo with plain markdown (preview first)
uv run croc init --adopt --dry-run path/to/docs/
uv run croc init --adopt            path/to/docs/

# Rename an id; every referrer updates atomically
uv run croc rename old-id new-id --root path/to/docs/

# Move a file; id-based links mean zero references need rewriting
uv run croc move path/to/docs/a.md path/to/docs/subdir/ --root path/to/docs/
```

## Commands

### `croc check <root>`

Runs the borrow checker. Exit codes:

- `0` — tree is sound
- `1` — tree has violations (printed to stderr)
- `2` — tree cannot be loaded (malformed frontmatter, missing root)

### `croc index <root>`

Prints the derived `id → path` map as JSON. The index is never stored — it's a regenerable view over the tree, so it cannot drift.

### `croc move <src> <dst> [--root R] [--dry-run]`

Relocates a file. Because ids are stable, zero references are rewritten. Runs a pre-check so you don't pile a move on a broken tree. Uses `git mv` when in a git repo, falls back to `shutil.move`.

### `croc rename <old-id> <new-id> [--root R] [--dry-run]`

Rewrites every strong and weak reference in the tree, plus the owner's `id` field. Transactional:

1. **Pre-check** the tree is sound.
2. **Plan** the rewrite in memory.
3. **Simulate** the plan: apply in memory, re-parse, re-check.
4. **Commit** atomically per-file (temp + `os.replace`); snapshot-based rollback on FS failure.

If any step fails, nothing is written.

### `croc init [path] [--adopt] [--dry-run]`

Creates a `.croc.toml` marker at `path`. With `--adopt`, brings every `.md` into the managed schema in one of three ways:

- **SCAFFOLD** — no frontmatter. Prepend a fresh block.
- **AUGMENT** — has frontmatter but missing required fields. Fill in `id`/`title`/`kind`/`links` while preserving every existing key and its order (foreign fields like `type`, `mirrors`, `created`, ... survive untouched).
- **SKIP** — has frontmatter we can't safely modify (unterminated, invalid YAML, malformed existing `id`). The author fixes by hand.

Proposed ids are **hierarchical** — slugified relative path, not just the filename — so code-adjacent trees with lots of repeated stems (`__init__.md`, per-customer folders, etc.) don't collide:

| Path                                              | Proposed id                              |
| ------------------------------------------------- | ---------------------------------------- |
| `foo.md` (root)                                   | `foo`                                    |
| `sub/foo.md`                                      | `sub-foo`                                |
| `pkg/utils/__init__.md`                           | `pkg-utils-init`                         |
| `regions/east/notes.md`                           | `regions-east-notes`                     |
| `alerts/self.md`                                  | `alerts` (directory-index convention)    |
| `self.md` (root)                                  | `root`                                   |

Collisions (rare path-slug ambiguities, or `foo.md` at root competing with `foo/self.md`) are reported and the command refuses to write.

### `--dry-run`

Every mutating command (`move`, `rename`, `init --adopt`) accepts `--dry-run`. It runs every validation and prints the plan but writes nothing.

## Concepts

### Frontmatter

Every managed `.md` file has YAML frontmatter:

```yaml
---
id: registry-pattern
title: Registry pattern
kind: leaf
links:
  - { to: design-index, strength: strong }
  - { to: obsidian-comparison, strength: weak }
---

The body can reference other docs: [[id:design-index]] or [[see:obsidian-comparison]].
```

**Required fields:** `id`, `title`, `kind`, `links`.

**Id grammar:** `[A-Za-z0-9_.-]+`. UUIDs, slugs, dotted namespaces all legal. Spaces and slashes aren't.

**`kind`:** `self` for directory index files (`self.md`), `leaf` for everything else.

### Strong vs weak links

A **strong link** pins its target. If the target is deleted or renamed, the commit is refused.

```yaml
links:
  - { to: adr-0042, strength: strong }
```

A **weak link** cites a target without pinning it. If the target is absent, the link is silently tolerated — it's the "see also" tier.

```yaml
links:
  - { to: obsidian-comparison, strength: weak }
```

Use strong for load-bearing citations (a runbook referencing the ADR it implements). Use weak for breadcrumbs.

### The five rules

`croc check` enforces:

1. **Ownership** — every `.md` has a unique `id`.
2. **Schema** — frontmatter has `title`, `kind`, `links`.
3. **No dangling ref** — every `[[id:X]]` in body text resolves to a doc.
4. **Lifetime bound** — strong links in frontmatter point to docs that exist.
5. **Identity stable** — the set of strong links declared in frontmatter equals the set of `[[id:X]]` in the body.

Weak links are exempt from rules 3 and 4 by design.

### Where croc fits (and doesn't)

**Good fits**

- Engineering knowledge bases (ADRs, runbooks, postmortems that cite each other)
- LLM/agent context stores where agents read and write the tree and need integrity guarantees
- Compliance and audit trails where "the chain is unbroken" is the artifact
- Internal dev docs at 50+ engineer companies where rot is the rule

**Bad fits**

- Personal Zettelkasten — Obsidian's ergonomics (graph view, backlinks pane) beat a linter for daily use
- Fast-moving drafts and brainstorming — the schema is friction before content exists
- Teams that don't run CI or pre-commit hooks — the whole value is mechanical enforcement

## Using croc in CI

### As a pre-commit hook

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

### In GitHub Actions

```yaml
- name: croc check
  run: |
    uv sync
    uv run croc check path/to/docs/
```

## For contributors

### Layout

```
croc/
├── croc/
│   ├── __init__.py
│   ├── check.py       # borrow checker; pure over list[Doc]
│   └── ops.py         # transformations: move, rename, init, adopt
├── main.py            # Typer CLI — thin wrapper around ops
├── tests/
│   ├── conftest.py    # shared fixtures (tmp_path trees)
│   ├── test_check.py  # parser + five rules
│   └── test_ops.py    # move, rename, init, adopt, dry-run
├── docs/design.md     # full Rust-inspired rationale
├── examples/thoughts/ # canonical sample tree
└── pyproject.toml
```

### Design principles

**Separation of concerns.** `check.py` is verification (pure, no I/O). `ops.py` is transformation (parse → check → plan → simulate → commit). Each raises a typed exception — `TreeError` for parse failures, `OpError` for precondition failures — so the CLI can map cleanly to exit codes.

**Validate-then-commit.** No operation writes to disk until its plan has been simulated and re-checked in memory. If validation fails, nothing is half-committed; if the physical commit fails mid-sequence, a snapshot-based rollback restores the already-written files.

**Newtype discipline.** `DocId` and `DocPath` are distinct `NewType` aliases over `str`. The parser enforces the id grammar at the boundary, so runtime values match their declared types.

**`--dry-run` is universal.** Every mutating operation accepts `dry_run=True` and skips only the final commit step. The simulation machinery is the same either way, so dry-run and real runs exercise identical code paths.

### Adding a new command

1. Add the implementation to `croc/ops.py`. Raise `OpError` on precondition failures. Follow load → check → plan → simulate → commit.
2. Wire the Typer command in `main.py`. Keep it thin — CLI only formats output and maps exceptions to exit codes.
3. Add tests in `tests/test_ops.py`. Use the `tmp_path` + `write_doc` / `sample_tree` fixtures.
4. Add a `--dry-run` flag if the command writes.

### Running tests

```bash
uv sync --group dev
uv run pytest                # 62 tests, ~0.1s
uv run pytest -v             # verbose
uv run pytest -k rename      # filter by name
```

The test suite encodes the guarantees as regressions. Notable cases:

- `test_failed_rename_leaves_tree_unchanged` — fingerprints the tree, runs four failing renames back-to-back, asserts no file changed. Captures the validate-then-commit property.
- `test_post_adopt_check_passes` — adopts a fresh unmanaged tree, then runs `check`. Proves `init --adopt` produces a sound tree out of the gate.
- `test_dry_run_writes_nothing` (×3) — fingerprint-before / dry-run / fingerprint-after, applied uniformly across move, rename, and adopt.

### Known limitations

- **YAML round-trip formatting.** `rename` re-serializes frontmatter; inline flow style `{ to: X, strength: Y }` may render as `{to: X, strength: Y}`. Cosmetic — swap `yaml.dump` for `ruamel.yaml` if formatting preservation matters.
- **No `.crocignore`.** Trees with vendored READMEs or generated files need `init --adopt` pointed at a subdirectory.
- **Symlinked subtrees are not traversed.** `scan_symlinks` emits warnings; the user decides whether to follow.

### Further reading

- [`docs/design.md`](docs/design.md) — full design rationale and the Rust-to-croc mapping.
