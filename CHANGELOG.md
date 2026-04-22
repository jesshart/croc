# Changelog

All notable changes to croc are recorded here. The format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project is
pre-1.0 and does not yet commit to semver.

## Unreleased

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
