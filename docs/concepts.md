---
icon: lucide/book-open
---

# Concepts

## Frontmatter

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

**Ref dialect:**

| Form                              | Meaning                                    |
| --------------------------------- | ------------------------------------------ |
| `[[id:X]]`                        | Strong link to id `X`                      |
| `[[id:X#anchor]]`                 | Strong link to a heading anchor            |
| `[[id:X\|display text]]`           | Strong link with custom display text       |
| `[[id:X#anchor\|display text]]`    | Both                                       |
| `[[see:X]]`                       | Weak link (citation, may dangle)           |

Only the id is load-bearing for invariant checking; the anchor and display text are preserved for renderers and consumers.

**Id grammar:** `[A-Za-z0-9_.-]+`. UUIDs, slugs, dotted namespaces all legal. Spaces and slashes aren't.

**`kind`:** `self` for directory-index files (`self.md`), `leaf` for everything else.

## Strong vs weak links

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

## The five rules

`croc check` enforces five invariants. Each maps to a Rust guarantee:

1. **Ownership** — every `.md` has a unique `id`. Two files with the same id is a double-owner, just like `Box<T>` with two owners.
2. **Schema** — frontmatter has `title`, `kind`, `links`. If a struct field is missing, it's a type error.
3. **No dangling ref** — every `[[id:X]]` in body text resolves to a real doc. A dangling strong link is a use-after-free.
4. **Lifetime bound** — strong links declared in frontmatter point to docs that exist. Deleting a doc with inbound strong links requires retargeting in the same commit.
5. **Identity stable** — the set of strong links declared in frontmatter equals the set of `[[id:X]]` references in the body. Declared type must match usage.

Weak links (`[[see:X]]`) are deliberately exempt from rules 3 and 4. `Weak<T>` does not pin its target, so deleting a weakly-referenced doc is fine — the weak link becomes a tombstone, not a compile error.

## Documenting the syntax

A doc that teaches croc — a tutorial, an ADR about the convention, an implementation plan quoting the ref syntax — needs to mention `[[id:X]]` and `[text](path.md)` literally without those sequences being parsed as live references. Three escape hatches, all recognized by every croc command:

- **Fenced code blocks** (` ``` ` or `~~~`). Everything between opener and matching closer is literal.
- **Inline code** (any matching backtick run: `` `…` ``, ` `` …`` `, …). Use when you're quoting a ref mid-sentence.
- **Backslash escapes** on a single bracket or paren: `\[`, `\]`, `\(`, `\)`. Use for a one-off literal in prose where you don't want the code styling.

Refs in masked regions don't count toward `E-DANGLING` / `E-IDENTITY`, don't get auto-filled into frontmatter `links:` on `init --adopt`, aren't rewritten by `rename` or `molt`, and aren't reported by `refs`. They survive the full adopt → molt round-trip byte-for-byte.

## I/O bindings

Docs can declare a `tracks:` field in frontmatter — a list of source files whose contents are load-bearing for the doc. [`croc attack`](commands.md#croc-attack) populates this field by scanning code for user-declared regex patterns; [`croc hunt`](commands.md#croc-hunt) alerts when any tracked file has changed in a git diff and the bound doc hasn't.

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

### Where `.croc.toml` lives

The trace/hunt config is **repo-scoped**: `code_globs` are matched from the git repo root and `tracks:` paths are stored repo-root-relative (so `hunt` can line them up against `git diff`). To match that scope, `attack`/`hunt` discover `.croc.toml` by **walking up** from the tree `ROOT` to the git repo root — so the file can sit at the project root next to `pyproject.toml`. A tree-local `.croc.toml` (the file `init` writes at the tree root) is still found first, so existing layouts keep working.

One repo can host several doc trees from a single repo-root file using `[trees."<path>"]` tables, keyed by each tree's path relative to the file (e.g. `[trees."thoughts"]`, `[trees."docs/internal"]` — quote keys that contain `/`). A per-tree table's `trace`/`hunt`/`version` override the top-level defaults for that tree; anything it omits is inherited.

The top-level `version` is an inert management marker — nothing branches on it, and `molt` no longer strips or deletes `.croc.toml`. The file is yours to keep or remove; `molt` only notes that a tree-local one was left in place.

## Why "derived, not authoritative"

The `id → path` index is a *view* over the tree, not a database. Any script that walks the tree and reads `id` from each frontmatter regenerates the full index in milliseconds. This matters:

- **No drift.** A hand-edited `.index.json` could disagree with the filesystem. A regenerable index cannot.
- **Resilient to out-of-band edits.** `mv`, `git mv`, IDE refactor, `rsync` — all fine. Run `croc check` and the tree is reconciled.
- **Trivial backup.** The source of truth is the files themselves; lose the index, rebuild it.
