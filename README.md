# croc

A Rust-inspired, Typer-powered CLI for reliably managing project documentation.

## What

`croc` treats a `thoughts/` markdown tree the way Rust treats memory:
ids are owners, links are borrows, and a pre-commit check is the
borrow checker. Move a file and every reference still works; introduce
a dangling link and the commit is refused.

## Quick start

```bash
uv sync
uv run croc check examples/thoughts
uv run croc index examples/thoughts
```

## The five rules

`croc check` refuses any tree that violates:

1. **Ownership** — every `.md` has a unique `id` in frontmatter.
2. **Schema** — frontmatter matches the declared shape.
3. **No dangling ref** — every `[[id:X]]` resolves.
4. **Lifetime bound** — strong links point to docs that exist.
5. **Identity stable** — frontmatter links match body references.

Weak links (`[[see:X]]`) are tolerated even when the target is absent.
That is the whole point of `Weak<T>`: citation without pinning.

## Layout

```
croc/
├── croc/             # library
│   └── check.py      # the borrow checker
├── main.py           # Typer CLI
├── docs/design.md    # full rationale
└── examples/thoughts # sample tree that passes all five rules
```

See `docs/design.md` for the full design rationale.
