# croc — design notes

> A Rust-inspired, Typer-powered CLI for reliably managing project docs.

## The problem

A `thoughts/` tree has many branching paths, nested directories with
`self.md` files, and files that reference other files by path. When a
file moves, every referrer has to be updated — a maintenance burden
that makes reorganization painful and error-prone.

The design goal: **a single source of truth for file paths**, so that
moving a file is a one-line operation and every reference keeps working.

## The mental model

Think of the tree the way Rust thinks about memory.

| Rust concept       | croc concept                             |
| ------------------ | ---------------------------------------- |
| Ownership          | Each `.md` has a unique `id` (frontmatter). |
| Move semantics     | `mv` relocates bytes; id travels with the file. |
| Borrowing (`&T`)   | `[[id:X]]` — a strong link to another doc. |
| `Weak<T>`          | `[[see:X]]` — a soft citation that does not pin. |
| Lifetimes          | Strong links must outlive no shorter than their target. |
| Newtype pattern    | `DocId` and `DocPath` are distinct types. |
| Borrow checker     | `croc check` refuses a tree with broken invariants. |
| `#[must_use]`      | A planned lint: "you read the index but bypassed the resolver". |

## The five rules

`croc check` enforces five invariants. Each maps to a Rust guarantee:

1. **Ownership** — every `.md` has a unique `id`. Two files with the
   same id is a double-owner, just like `Box<T>` with two owners.
2. **Schema** — frontmatter has `title`, `kind`, and `links`. If a
   struct field is missing, it's a type error.
3. **No dangling ref** — every `[[id:X]]` in body text resolves to a
   real doc. A dangling strong link is a use-after-free.
4. **Lifetime bound** — strong links declared in frontmatter point to
   docs that exist. Deleting a doc with inbound strong links requires
   retargeting in the same commit.
5. **Identity stable** — the set of strong links declared in
   frontmatter equals the set of `[[id:X]]` references in the body.
   Declared type must match usage.

Weak links (`[[see:X]]`) are deliberately exempt from rules 3 and 4.
`Weak<T>` does not pin its target, so deleting a weakly-referenced doc
is fine — the weak link becomes a tombstone, not a compile error.

## Why "derived, not authoritative"

The `id → path` index is a *view* over the tree, not a database. Any
script that walks the tree and reads `id` from each frontmatter
regenerates the full index in milliseconds. This matters:

- **No drift.** A hand-edited `.index.json` could disagree with the
  filesystem. A regenerable index cannot.
- **Resilient to out-of-band edits.** `mv`, `git mv`, IDE refactor,
  `rsync` — all fine. Run `croc check` and the tree is reconciled.
- **Trivial backup.** The source of truth is the files themselves;
  lose the index, rebuild it.

## Commands

Shipped:

| Command        | What it does                                                    |
| -------------- | --------------------------------------------------------------- |
| `croc check`   | Run the five-rule borrow checker.                               |
| `croc index`   | Print the derived id → path index as JSON.                      |
| `croc move`    | Relocate a file. IDs travel with the file; no refs rewritten.   |
| `croc rename`  | Rename an id. Every strong and weak reference rewritten atomically, validate-then-commit. |
| `croc init`    | Create `.croc.toml`. With `--adopt`, scaffold frontmatter into every `.md` that lacks it. |

Every mutating command (`move`, `rename`, `init --adopt`) accepts
`--dry-run`, which runs all validation and prints the plan without
writing.

Planned:

| Command        | What it would do                                                |
| -------------- | --------------------------------------------------------------- |
| `croc refs`    | Reverse index — "who points at this doc?"                       |
| `croc gc`      | Report docs with zero inbound strong links.                     |
| `croc schema`  | Externalize the frontmatter schema.                             |

## The guarantee

Wire `croc check` to a `pre-commit` hook and **every commit on `HEAD`
is a tree in which no doc can produce a broken link**.

`croc rename` extends that guarantee into refactors: because the
rewrite is simulated in memory and re-checked before any disk write,
a rename either completes soundly or leaves the tree untouched. There
is no half-renamed intermediate state to commit by accident.

Together: reorganization becomes cheap, refactoring becomes safe, and
the system refuses to be broken.
