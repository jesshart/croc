---
icon: lucide/rocket
---

# croc

> *ids are owners, links are borrows, `croc check` is the borrow checker.*

A Rust-inspired, Typer-powered CLI for reliably managing project documentation.

`croc` treats a markdown doc tree the way Rust treats memory: **ids are owners, links are borrows, `croc check` is the borrow checker, `croc rename` is an atomic refactor.** Move a file and every reference keeps working. Introduce a dangling link and the commit is refused.

## The problem

A `thoughts/` tree grows branching paths, nested directories with `self.md` files, and files that reference other files by path. When a file moves, every referrer has to be updated. The usual options all fail:

- **By hand:** grep for every reference and hope you didn't miss one.
- **With `sed`:** risk rewriting substring matches inside prose or code blocks.
- **With Obsidian/Notion:** silent link rot whenever someone edits outside the editor.

None of these prevent the *broken intermediate state* — the window where `main` has dangling refs and no one has noticed. `croc check` closes that window by refusing the commit; `croc rename` makes the refactor an atomic transaction.

## The idea

Replace path-based references with **stable ids**. A reference like `[[id:registry-pattern]]` resolves through a derived index of every `id` in the tree. When a file moves, the id travels with it; every link still works. When an id changes, one command rewrites every referrer atomically.

### Tree-as-memory

| Rust concept         | croc concept                                          |
| -------------------- | ----------------------------------------------------- |
| Ownership            | Each `.md` has a unique `id` in frontmatter           |
| Move semantics       | `mv` relocates bytes; id travels with the file        |
| `&T` (borrow)        | `[[id:X]]` — strong link                              |
| `Weak<T>`            | `[[see:X]]` — soft citation, may dangle               |
| Lifetimes            | Strong links may not outlive their target             |
| Newtype pattern      | `DocId` and `DocPath` are distinct types              |
| Borrow checker       | `croc check` refuses trees with broken invariants     |
| Validate-then-commit | Rewrites simulated in memory before any disk write    |

## Quick start

Requires Python `>=3.13` and [`uv`](https://docs.astral.sh/uv/).

```bash
# Install
uv sync

# Check the bundled example
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

## Where to go next

- **[Install](install.md)** — install instructions, supported Python versions.
- **[Concepts](concepts.md)** — frontmatter, the five rules, strong vs weak links.
- **[Commands](commands.md)** — auto-generated reference for every CLI command.
- **[CI integration](ci.md)** — pre-commit hooks and GitHub Actions recipes.
- **[Design notes](design.md)** — the full Rust-to-croc mapping.
- **[Contributing](contributing.md)** — layout, principles, release process.

## Where croc fits (and doesn't)

!!! success "Good fits"

    - Engineering knowledge bases (ADRs, runbooks, postmortems that cite each other)
    - LLM/agent context stores where agents read and write the tree and need integrity guarantees
    - Compliance and audit trails where "the chain is unbroken" is the artifact
    - Internal dev docs at 50+ engineer companies where rot is the rule

!!! failure "Bad fits"

    - Personal Zettelkasten — Obsidian's ergonomics (graph view, backlinks pane) beat a linter for daily use
    - Fast-moving drafts and brainstorming — the schema is friction before content exists
    - Teams that don't run CI or pre-commit hooks — the whole value is mechanical enforcement
