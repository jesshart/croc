# croc

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.13+](https://img.shields.io/badge/python-3.13+-blue.svg)](https://www.python.org/downloads/)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![ty](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ty/main/assets/badge/v0.json)](https://github.com/astral-sh/ty)

> *ids are owners, links are borrows, `croc check` is the borrow checker.*

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

# Scaffold a doc tree from a source directory (plain markdown)
uv run croc crawl src/
# Same, but also adopt into croc shape — ready for `croc check`
uv run croc crawl src/ --adopt

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

### `croc crawl <src> [-o OUT] [--adopt] [--file-types EXT ...] [--force] [--dry-run]`

Scaffold a plain-markdown doc tree from a source directory. One `.md` stub per file, one `self.md` per directory. Output carries only a `mirrors:` breadcrumb in frontmatter — no `id` / `kind` / `links` — so the shape is compatible with croc's post-molt state and the adopt/molt cycle round-trips cleanly.

```bash
# Plain scaffold — output lands at ./thoughts/<src-name>/ by default
croc crawl src/

# Preview the plan
croc crawl src/ --dry-run

# One-step: scaffold + run `init --adopt` on the result → croc-checkable tree
croc crawl src/ --adopt

# Narrow discovery by extension (default mirrors every file git tracks)
croc crawl src/ --file-types .py --file-types .ts
```

**Default discovery** mirrors every file git is actively tracking (`git ls-files`). Dot-prefixed directories (`.git`, `.venv`, ...) and `__pycache__` are always pruned. `.gitignore` is honored automatically when run inside a git repo; for repos without `.gitignore` discipline, use `--file-types` to narrow. Pass the global `--include-untracked` flag to also mirror draft files you haven't `git add`ed yet.

**Two-step vs one-step.** The default two-step flow — `croc crawl src/` then `croc init --adopt thoughts/src/` — gives you a plain-markdown tree you can edit by hand before adopting. The one-step `--adopt` variant is for cases where you just want a croc-checkable tree immediately. Both are idempotent; re-running is a no-op unless `--force` is passed.

**Why shape-compatibility with molt matters.** `crawl` emits the same frontmatter shape a file ends up with after `molt` (nothing but non-croc fields like `mirrors:`). That means the lifecycle `crawl → (adopt → edit → check → molt)*` is symmetric around crawl's output: you can stay plain, adopt when you want the checker, molt back for sharing, re-adopt later — and crawl sits cleanly outside the cycle. Try the bundled example:

```bash
uv run croc check examples/thoughts-from-code/thoughts
```

### `croc molt <root> [--dry-run]`

Reverse adoption. Rewrites every `[[id:X]]` / `[[see:X]]` body ref back into `[text](path.md)` plain markdown, strips croc-specific frontmatter fields (`id`, `kind`, `links`), and removes `.croc.toml`. The tree must pass `croc check` first.

| Before                                    | After                                |
| ----------------------------------------- | ------------------------------------ |
| `[[id:foo\|foo]]`                          | `[foo](foo.md)`                      |
| `[[id:target#section-x\|Section X]]`       | `[Section X](target.md#section-x)`   |
| `[[id:data-glossary]]` (bare)              | `[Data Glossary](data-glossary.md)` (falls back to target's `title`) |

Foreign frontmatter (`title`, `type`, `mirrors`, any custom keys) is preserved in original order. The molted tree renders correctly in GitHub, Obsidian, or any generic markdown tool. Re-adopt with `croc init --adopt` to come back under croc management; the round-trip is semantically equivalent.

### `croc refs <root> [--unresolved]`

Walks the tree and reports every markdown-style path ref (`[text](path.md)`), showing whether each target resolves to a file under the root. Read-only; works on any markdown tree whether or not it's been adopted. Use as a health check before `init --adopt --migrate-refs`:

```bash
croc refs --unresolved path/to/docs/
# UNRESOLVED runbooks/onboarding.md: -> ghost.md
# 1 unresolved ref(s) across the tree
```

Exits 1 when any ref is unresolved. Great for CI on partially-migrated trees.

### `croc lurk <root> [-n N] [--include-frontmatter]`

Reports any `.md` file whose line count exceeds `N` (default `100`). Opinionated guardrail: small docs + id-based refs is the croc design; one 800-line doc defeats the grain the borrow checker rewards. `lurk` makes that editorial take machine-checkable.

```bash
croc lurk thoughts/
# thoughts/onboarding/deep-dive.md: 287 lines (over by 187)
#
# 1 file exceed 100 lines
```

YAML frontmatter is **excluded** from the count by default — a doc with a rich `links:` block shouldn't eat its budget on schema overhead. Pass `--include-frontmatter` for a literal whole-file count.

Works on any markdown tree, adopted or not (no frontmatter required). Honors the global `--include-untracked` flag. Exits 1 on any violation, matching the `check` contract — drop it into CI next to `croc check` for enforcement.

### `croc attack <root> [--dry-run] [--strict-traces]`

Scans source files for user-declared regex patterns in `.croc.toml` `[[trace]]` entries. Each match's capture group is resolved to a `.md` file by filename stem (`"revenue"` → `revenue.md`), and the source path is recorded in that doc's `tracks:` frontmatter list.

```toml
# .croc.toml at the tree root
version = "0.1"

[[trace]]
name = "persist_parquet"
pattern = '''persist_parquet\(["']([^"']+)["']\)'''
code_globs = ["src/**/*.py"]
```

```bash
croc attack thoughts/
# ATTACK revenue.md (tracks: src/producer.py)
# attack OK (1 action)
```

**Resolution by stem** means `attack` works on adopted and unadopted trees alike — the filename is the handle, not the `id`. Each `[[trace]]` pattern must have exactly one capture group; config is rejected at load time otherwise. Paths written to `tracks:` are relative to the git repo root (`git rev-parse --show-toplevel`), which keeps them aligned with `git diff --name-only` for `croc hunt`.

**Idempotent.** `attack` re-derives the full `tracks:` list from a fresh scan. Refactoring a `persist_parquet` call out of the code drops the corresponding track automatically; add a new call and the next run picks it up. Multiple patterns hitting the same doc from different source files all contribute (union); the same file contributing twice dedupes.

**Unresolved / ambiguous captures** surface as `SKIP-TRACE` notes — a capture `"ghost"` with no `ghost.md` in the tree, or `"revenue"` with both `data/revenue.md` and `archive/revenue.md`, is reported but not written. Pass `--strict-traces` to exit non-zero when any notes appear (for CI).

Requires a git repo (paths are repo-root-relative). Honors the global `--include-untracked` flag.

### `croc hunt <root> [--base REF] [--forgiving/--strict]`

Pre-commit / CI gate. Walks every `.md` with a `tracks:` field, compares each listed source path against `git diff --name-only`, and alerts when any bound source file has changed. Exits 1 on any alert.

```bash
croc hunt thoughts/
# thoughts/revenue.md tracks changed file src/producer.py
#
# 1 alert (strict mode)
```

**Diff scope:**

| Invocation                       | Diff computed                             | Use case      |
| -------------------------------- | ----------------------------------------- | ------------- |
| `croc hunt thoughts/`            | `git diff --cached --name-only` (staged)  | pre-commit    |
| `croc hunt thoughts/ --base main`| `git diff --name-only main...HEAD` (range)| CI / PR gate  |

**Strict vs forgiving:**

- **Strict** (default): any tracked source in the diff → alert. The doc must be reviewed even if you also edited it in the same commit.
- **Forgiving** (`--forgiving` flag, or `[hunt] strict = false` in `.croc.toml`): skip the alert when the bound doc itself is also in the diff — assumes you updated both together on purpose.

CLI flags override the config default; `--strict` turns strictness back on when the config is set to forgiving.

### I/O bindings

Docs can declare a `tracks:` field in frontmatter — a list of source files whose contents are load-bearing for the doc. `croc attack` populates this field by scanning code for user-declared regex patterns; `croc hunt` alerts when any tracked file has changed in a git diff and the bound doc hasn't.

```yaml
---
id: revenue
title: Revenue
kind: leaf
links: []
tracks:
  - src/producer.py
---
```

The binding is deliberately narrow: `tracks:` points at files *outside* the tree (code, configs, schemas), never other docs — `links:` handles doc-to-doc refs, and the five rules govern that seam. Treat `tracks:` as the same kind of contract as a strong ref, but for the code-to-doc seam.

`tracks:` is a foreign field from the perspective of adopt/molt. It survives the full lifecycle untouched — `molt` preserves it alongside `mirrors:`, `title`, and any other non-croc key.

### Ref migration (on `init --adopt`, default on)

Adoption rewrites markdown path refs in body text to the croc dialect by default:

| Before                                   | After                                               |
| ---------------------------------------- | --------------------------------------------------- |
| `[foo](foo.md)`                          | `[[id:foo\|foo]]`                                    |
| `[Section X](target.md#section-x)`       | `[[id:target#section-x\|Section X]]`                 |
| `[Data Glossary](../data_glossary.md)`   | `[[id:data-glossary\|Data Glossary]]`                |

Link text and anchors are preserved. Frontmatter `links` gets a strong entry for every migrated target (so Rule 5 — identity — is satisfied post-migration).

**Pass `--no-migrate-refs`** to adopt only the frontmatter shape and leave body content untouched — useful if you want to stage the migration separately.

**Re-running on an adopted tree is safe.** If a previously-adopted file grows new path-refs later (someone pastes a markdown link, a new doc lands), the next `init --adopt` reaches that file and migrates the new refs. Clean trees produce zero actions — the command is idempotent.

**Unresolvable refs** (target doesn't exist, or escapes the tree root, or uses non-lowercase `.md` extension) are left in place as raw markdown and surfaced as `SKIP-REF` notes. Brownfield trees always have some rot; adoption reports it rather than refusing to land.

**Why not teach `check` to recognize path refs directly?** Because path refs break on move — which is the exact failure mode croc exists to prevent. The checker's narrow `[[id:X]]` dialect IS the enforcement; loosening it would defeat the purpose.

### `--dry-run`

Every mutating command (`move`, `rename`, `init --adopt`, `init --adopt --migrate-refs`, `attack`) accepts `--dry-run`. It runs every validation and prints the plan but writes nothing.

### `--include-untracked` / `--no-include-untracked` (global)

Global flag — name mirrors `git stash --include-untracked`. Controls which files croc considers when walking a tree. Takes effect only inside a git repo.

| Mode | Files walked |
| ---- | ------------ |
| Default (`--no-include-untracked`) | Tracked only — what `git ls-files` returns. Drafts you haven't `git add`ed yet are skipped. |
| `--include-untracked` | Tracked + untracked-but-not-ignored. Useful while drafting new docs before committing. |
| Outside a git repo | Flag has no effect; every file is walked. |

Gitignored files are never touched when the walk is git-backed — same envelope in both modes. Applies to `check`, `index`, `move`, `rename`, `init --adopt`, `crawl`, `molt`, `refs`, `lurk`, `attack`, and `hunt`.

```bash
# Default: only tracked files considered
croc check thoughts/

# Include in-progress drafts in the check
croc --include-untracked check thoughts/
```

Note: `rename` follows the same scope. Refs inside a filtered-out draft are **not** rewritten, so a draft containing `[[id:old]]` will still reference `old` after `croc rename old new`. Re-run with `--include-untracked` to update drafts, or let `check` catch the dangling ref when you eventually add the draft.

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
Refs support optional anchors and display text: [[id:design-index#intro|the intro]].
```

**Required fields:** `id`, `title`, `kind`, `links`.

**Ref dialect:** `[[id:X]]`, `[[id:X#anchor]]`, `[[id:X|display text]]`, `[[id:X#anchor|display text]]`. Only the id is load-bearing for invariant checking; the anchor and display text are preserved for renderers and consumers.

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

### Documenting the syntax

A doc that teaches croc — a tutorial, an ADR about the convention, an
implementation plan quoting the ref syntax — needs to mention `[[id:X]]`
and `[text](path.md)` literally without those sequences being parsed as
live references. Three escape hatches, all recognized by every croc
command:

- **Fenced code blocks** (` ``` ` or `~~~`). Everything between opener
  and matching closer is literal.
- **Inline code** (any matching backtick run: `` `…` ``, ` `` …`` `, …).
  Use when you're quoting a ref mid-sentence.
- **Backslash escapes** on a single bracket or paren: `\[`, `\]`, `\(`,
  `\)`. Use for a one-off literal in prose where you don't want the
  code styling.

```markdown
References look like `[[id:registry]]` — the ref in backticks is
documentation, not a live ref. A real ref reads: [[id:registry]].

Path-style refs get migrated on adopt:

\`\`\`markdown
[label](target.md)
\`\`\`
```

Refs in masked regions don't count toward `E-DANGLING` / `E-IDENTITY`,
don't get auto-filled into frontmatter `links:` on `init --adopt`,
aren't rewritten by `rename-id` or `molt`, and aren't reported by
`refs`. They survive the full adopt → molt round-trip byte-for-byte.

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

### Pre-commit hook for `croc hunt`

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

`croc hunt` defaults to diffing against `--cached` (staged changes) which is what you want here; use `--base main` in CI pipelines instead.

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
│   ├── crawl.py       # scaffold plain-markdown trees from source
│   └── ops.py         # transformations: move, rename, init, adopt, molt
├── main.py            # Typer CLI — thin wrapper around ops
├── tests/
│   ├── conftest.py    # shared fixtures (tmp_path trees)
│   ├── test_check.py  # parser + five rules
│   ├── test_cli.py    # Typer CLI surface + exit codes
│   ├── test_crawl.py  # plan/build, filters, adopt/molt cycle
│   └── test_ops.py    # move, rename, init, adopt, molt, dry-run
├── docs/design.md                    # full Rust-inspired rationale
├── examples/thoughts/                # canonical sample tree
├── examples/thoughts-from-code/      # crawl fixture (src + adopted output)
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
uv run pytest                # ~190 tests, ~0.3s
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

### Releasing

Releases are cut through GitHub Releases — **not** a manual publish command. Publishing a release fires [`.github/workflows/publish.yml`](.github/workflows/publish.yml), which runs `uv build && uv publish --trusted-publishing always` inside the `pypi` environment via OIDC. There are no stored PyPI tokens to rotate.

The flow:

1. Land changes on `main`. Ensure CI is green (`style` + `test` workflows).
2. Bump `version` in [`pyproject.toml`](pyproject.toml) and `__version__` in [`croc/__init__.py`](croc/__init__.py). Keep them in sync. Pre-1.0: minor bump for new features, patch bump for fixes.
3. Promote the `## Unreleased` section in [`CHANGELOG.md`](CHANGELOG.md) to `## X.Y.Z — YYYY-MM-DD`. Add a fresh empty `## Unreleased` above it.
4. Commit as `chore(release): vX.Y.Z`. Duplicate the CHANGELOG section into the commit body so the commit stands alone.
5. Push: `git push origin main`.
6. Tag with the release notes in the annotation (not just `-m vX.Y.Z` — that leaves the GitHub Release body effectively empty): write the new CHANGELOG entry's body to a temp file and run `git tag -a vX.Y.Z -F notes.md && git push origin vX.Y.Z`. The tag annotation becomes the release body in the next step.
7. Create the GitHub Release from the tag: `gh release create vX.Y.Z --notes-from-tag --title vX.Y.Z` (or use the web UI). Publication triggers the publish workflow.

The `make pypi` target is a **manual fallback only**, not the canonical path. It runs `uv build && uv publish` locally and requires a PyPI token in the environment; it bypasses CI's clean build and the version/tag/changelog discipline above. Avoid it unless Trusted Publishing is down.

### Further reading

- [`docs/design.md`](docs/design.md) — full design rationale and the Rust-to-croc mapping.
