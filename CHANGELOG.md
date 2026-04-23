# Changelog

All notable changes to croc are recorded here. The format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project is
pre-1.0 and does not yet commit to semver.

## Unreleased

## 0.6.1 — 2026-04-23

### Fixed

- **Parser: documentation-about-syntax no longer parses as usage.**
  `[[id:X]]` and `[text](path.md)` inside **fenced code blocks**
  (``` or `~~~`), **inline code** (any matched backtick run), or
  **backslash-escaped brackets/parens** (`\[`, `\]`, `\(`, `\)`) are
  now treated as literal text by every ref-handling site — `check`,
  `init --adopt` (including the auto-filled `links:` and the MIGRATE-
  gate), `molt`, `rename-id`, and `refs`. Docs that teach croc's
  syntax no longer materialize spurious `to: X` frontmatter entries,
  trip `E-DANGLING` / `E-LIFETIME` / `E-IDENTITY`, or get their
  syntax examples rewritten on molt. A ref written as documentation
  survives the full adopt → molt round-trip byte-for-byte.

## 0.6.0 — 2026-04-23

### Added

- **`croc attack [root] [--dry-run] [--strict-traces]`** — scan source
  files for regex patterns declared in `.croc.toml` `[[trace]]` entries
  and record matching source paths in each target doc's `tracks:`
  frontmatter list. Target is resolved by filename stem (capture
  `"revenue"` → `revenue.md`), which works on adopted and unadopted
  trees alike. Paths are relative to the git repo root so they line up
  with `git diff --name-only` for `hunt`. Unresolved / ambiguous
  captures emit `SKIP-TRACE` notes; pass `--strict-traces` to fail the
  run when any appear. Idempotent: re-running re-derives `tracks:` from
  scratch, so refactored-away patterns drop from doc frontmatter
  automatically. Requires a git repo.
- **`croc hunt [root] [--base REF] [--forgiving/--strict]`** — alert
  when any doc's `tracks:` entry appears in a git diff. Default diff
  scope is staged changes (`--cached`); `--base REF` switches to
  `REF...HEAD` for CI use. `--strict` (default) alerts on any tracked
  source change; `--forgiving` suppresses the alert when the bound doc
  is itself in the diff. Exit 1 on any alerts. Designed as a pre-commit
  hook or CI step; mode can also be set via `[hunt] strict = ...` in
  `.croc.toml`.
- **`.croc.toml` is now a config file, not just a marker.** Holds
  `[[trace]]` entries (regex + code globs) consumed by `attack` / `hunt`,
  plus an optional `[hunt]` table (`strict = true/false`). Validated at
  load time: every regex must compile and have exactly one capture
  group. Foreign keys are preserved through the adopt / molt lifecycle.

### Changed

- **`croc molt` preserves foreign `.croc.toml` config.** Previously it
  deleted the file unconditionally. Now it strips only the `version`
  marker (symmetric with how molt strips `id`/`kind`/`links` from
  frontmatter while preserving foreign fields). If nothing remains, the
  file is still deleted — preserving today's behavior for marker-only
  files. `croc init --adopt` re-adds the marker ahead of any surviving
  config on re-adoption. Action log prints `REWRITE .croc.toml` when
  config survives, `REMOVE .croc.toml` when it's marker-only.

### Fixed

- **`croc crawl` silently collapsed same-stem siblings into one output.**
  Files that differed only in suffix (`Dockerfile`, `Dockerfile.ecs`,
  `Dockerfile.fargate_worker`; `Makefile` + `Makefile.local`;
  `.env` + `.env.local`) all planned to the same `<stem>.md` path
  because `pathlib.Path.stem` strips only the final suffix. `apply_plan`
  then wrote them in order — last writer won, the others were lost.
  `plan_crawl` now detects output-name collisions *within a single
  output directory* and disambiguates by using the full source filename:
  `Dockerfile.md` / `Dockerfile.ecs.md` / `Dockerfile.fargate_worker.md`.
  Non-colliding trees are unchanged. The CLI prints a one-line cyan
  note on stderr whenever disambiguation triggered, so the naming
  decision isn't silent.

  **Migration:** if you have an existing crawl-ed output tree that
  *was* affected, a re-run will produce the new disambiguated names
  alongside the old bare-stem file. The old file's content corresponds
  to whichever sibling won the previous last-write race
  (unrecoverable; check its `mirrors:` field to confirm). Pass
  `--force` to overwrite, or delete the now-orphaned file manually.

## 0.5.0 — 2026-04-22

### Added

- **`croc lurk [root] [-n/--max-lines N]`** — report any `.md` file
  whose line count exceeds `N` (default `100`). One line per
  violator on stderr; exit 1 if any file is over budget. YAML
  frontmatter is excluded from the count by default; pass
  `--include-frontmatter` for a literal whole-file count. Honors
  the global `--include-untracked` flag. Works on any markdown
  tree (no croc frontmatter required). Philosophy: small docs +
  id-based refs is the croc design, and `lurk` makes that
  editorial take machine-checkable — drop it in CI next to
  `croc check`.

## 0.4.0 — 2026-04-22

### Added

- **`--include-untracked` / `--no-include-untracked`** global flag
  (name mirrors `git stash --include-untracked`). Applies to every
  tree-walking command — `check`, `index`, `move`, `rename`,
  `init --adopt`, `crawl`, `molt`, `refs`. Inside a git repo the
  default narrows the walk to tracked files only (`git ls-files`),
  skipping in-progress drafts. Pass `--include-untracked` to fold
  drafts back in. Gitignored files are always excluded when the walk
  is git-backed. Outside a git repo, the flag is a no-op and every
  file is walked (unchanged).

### Changed

- **`croc crawl` default discovery narrowed.** Previously mirrored
  every non-ignored file (tracked + untracked-but-not-ignored). Now
  mirrors tracked files only by default, matching the new global
  flag's semantics. Pass `--include-untracked` to restore prior
  behavior.

## 0.3.0 — 2026-04-20

### Added

- **`croc crawl <src>`** — scaffold a plain-markdown doc tree from a
  source directory. One `.md` stub per file, one `self.md` per
  directory. Output carries only a `mirrors:` breadcrumb in
  frontmatter (no `id` / `title` / `kind` / `links`), making crawl
  output shape-compatible with the post-molt state — the adopt/molt
  cycle round-trips cleanly through crawl-produced files. Pass
  `--adopt` to fold in `init --adopt` and get a `croc check`-clean
  tree in one step.

  Default discovery mirrors every file git tracks (dot-dirs and
  `__pycache__` are always pruned). Narrow by extension with
  `--file-types .py --file-types .ts` (repeat the flag for multiples;
  pass `all` for no filter). Honors `.gitignore` automatically when
  run inside a git repo. Re-running without `--force` skips existing
  files — idempotent.

  Subsumes the `docure` experiment; croc can now produce a starting
  tree in addition to managing one.

### Fixed

- **`.gitignore` anchoring.** The `thoughts/` and `docs/plans/` rules
  were unanchored, which meant they matched *any* directory of those
  names anywhere in the tree — silently hiding bundled example
  fixtures like `examples/thoughts/` and
  `examples/thoughts-from-code/thoughts/` from new commits. Both rules
  now carry leading slashes so they only apply at the repo root.

## 0.2.0 — 2026-04-17

### Added

- **`--strict-refs` flag on `init` and `molt`.** Exits non-zero when
  any `SKIP-REF` / `SKIP-MOLT-REF` notes were emitted. Useful in CI /
  pre-commit where unresolvable refs should gate success. Default
  behavior unchanged: a plain `init --adopt` still exits 0 even with
  skips, preserving the adoption-must-land policy.

### Changed

- **Summary line separates writes from skip notes.** Previously
  `init OK (164 actions)` blurred 161 writes with 3 skip notes; now
  it reads `init OK (161 actions, 3 skipped refs)`. Same change
  applied to `molt` and to dry-run summaries.

- **Skip notes re-echoed on stderr after the summary.** An adopt run
  on a large tree was burying SKIP-REF lines under hundreds of
  AUGMENTs; by the time the summary printed, the skips had scrolled
  off-screen. They now re-appear in a bold-yellow stderr block as
  the last thing the user sees. Inline skip lines are also colored
  yellow so they stop blending into successful actions.

## 0.1.1 — 2026-04-17

### Fixed

- **YAML frontmatter emission uses block style consistently.** Previously
  every YAML write path used `yaml.dump(..., default_flow_style=None)`,
  which lets PyYAML's size heuristic decide between block and flow style
  per node. The practical effect was that `molt` collapsed stripped-down
  frontmatter to flow style (`{title: X}`) and emitted explicit
  `!!timestamp` tags for datetime values, and `init --adopt` emitted
  `links: [- {to: X, strength: strong}]` instead of readable block-style
  items. All five emission call sites now route through a single
  `_dump_yaml` helper that forces `default_flow_style=False`, disables
  line-wrapping, and preserves Unicode verbatim. Datetime scalars round-
  trip as implicit-tag plain scalars.

- **`croc molt` no longer crashes on weak refs to missing targets.**
  `_molt_body` looked up every referenced id in the tree index without
  guarding the dangling-weak case (tolerated by Rule 3/4 by design),
  raising `KeyError` on any `[[see:X]]` where `X` was absent. Now the
  original `[[see:X]]` is preserved in the body and surfaced as a
  `SKIP-MOLT-REF` note in the action log — visible in `--dry-run` so
  the warning appears in the plan, not at write time.

## 0.1.0 — 2026-04-17

Initial public release. Published to PyPI as `croc-cli` (the bare
`croc` name is taken by an unrelated scientific package). Import
name and CLI command remain `croc`.

### Changed (breaking)

- **`init --adopt` migrates markdown path refs by default.** Previously,
  ref migration required an explicit `--migrate-refs` flag, which meant
  a first-time adoption produced a tree whose frontmatter was managed
  but whose body refs were still path-based — silently breaking on the
  next file move. The opt-in was a footgun; the opt-out is the escape
  hatch. Pass `--no-migrate-refs` to restore the old behavior (adopt
  frontmatter only).

- **`adopt_tree(..., migrate_refs=...)` default flipped from `False` to
  `True`.** Library callers relying on the old default must pass
  `migrate_refs=False` explicitly.

### Added

- **`croc molt <root>`** — reverse adoption. Rewrites `[[id:X]]` /
  `[[see:X]]` body refs back to plain-markdown `[text](path.md)` syntax,
  strips croc-specific frontmatter fields (`id`, `kind`, `links`), and
  removes `.croc.toml`. Transactional: pre-checks the tree, simulates
  the rewrite in memory, writes atomically — same skeleton as `rename`.
  Supports `--dry-run`. The tree must pass `croc check` first.

- **Re-running `init --adopt` reaches already-managed files.** If a
  managed file's body gains a new markdown path-ref after first
  adoption, the next `init --adopt` run creates a MIGRATE-only plan
  entry for that file and rewrites it. Clean trees produce zero actions
  — the command is idempotent.

- **Case-insensitive detection of `.md` refs.** `[x](foo.MD)`,
  `[x](foo.Md)`, and `[x](foo.mD)` are now detected and surfaced as
  SKIP-REF notes with a targeted case-sensitivity diagnostic. When a
  lowercase-extension target exists, the note suggests it directly
  (`did you mean 'foo.md'?`). Previously these silently fell through
  the detector.

- **Richer SKIP-REF diagnostics.** Unresolvable refs now report *why*
  they're unresolvable in three distinct classes: target missing in
  tree, ref escapes tree root (shows absolute resolved path), or
  non-lowercase `.md` extension. Each mode has a specific fix.

- **Per-file migration reporting in dry-run.** The action log now shows
  migrated ref counts and the first few source paths inline with the
  AUGMENT/SCAFFOLD/MIGRATE verb, so `--dry-run` plans are auditable
  without running the actual write.

- **`croc refs <root> [--unresolved]`** — diagnostic command that walks
  the tree and reports every markdown path ref, noting whether each
  resolves. Works on pre-adoption trees. Usable as a CI health check.
