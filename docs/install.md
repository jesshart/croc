---
icon: lucide/download
---

# Install

## Requirements

- Python `>=3.13`
- [`uv`](https://docs.astral.sh/uv/) (recommended) or `pip`
- Git, if you want `croc` to honor `.gitignore` and use `git mv` on `croc move`

## With `uv` (recommended)

From a checkout of the croc repo:

```bash
uv sync
uv run croc --help
```

This installs croc and its runtime deps into a project-local `.venv/`. Every command in these docs assumes the `uv run` prefix.

To use croc as a standalone tool elsewhere on your machine:

```bash
uv tool install croc-cli
croc --help
```

!!! note "Package name vs CLI name"

    The PyPI distribution is published as `croc-cli` because the `croc` name was already taken by an unrelated scientific package. The installed CLI command and import name are still `croc`.

## With `pip`

```bash
pip install croc-cli
croc --help
```

## Verify the install

croc ships an example tree at `examples/thoughts/`. Check that the borrow checker runs:

```bash
uv run croc check examples/thoughts
# (no output, exit 0 = sound tree)

uv run croc index examples/thoughts
# Prints the derived id → path map as JSON
```

If `check` exits 0 you're done — head to [Concepts](concepts.md) or [Commands](commands.md).

## Optional: shell completion

croc inherits Typer's completion installer:

```bash
croc --install-completion
```

This writes a completion script for your current shell. Restart the shell to pick it up.
