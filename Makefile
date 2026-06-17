.PHONY: install test check lint format smoke pypi clean docs docs-serve

install:
	uv sync --group dev
	uv run pre-commit install

test:
	uv run pytest

check: lint test smoke

lint:
	uvx ruff check croc tests main.py
	uvx ruff format --check croc tests main.py
	uv run ty check croc main.py

format:
	uvx ruff check --fix croc tests main.py
	uvx ruff format croc tests main.py

smoke:
	uv run croc check examples/thoughts
	uv run croc check examples/thoughts-from-code/thoughts
	uv run croc --include-untracked attack examples/thoughts-from-code/thoughts --dry-run
	uv run croc hunt examples/thoughts-from-code/thoughts

# Manual fallback only — DO NOT USE for canonical releases.
#
# The supported release path is:
#   1. Bump `version` in pyproject.toml and `__version__` in croc/__init__.py
#   2. Promote `## Unreleased` in CHANGELOG.md to `## X.Y.Z — YYYY-MM-DD`
#   3. Commit as `chore(release): vX.Y.Z`
#   4. git push
#   5. Write the release notes to a temp file (typically the new CHANGELOG
#      entry's body, minus the `## X.Y.Z` header) and tag with:
#        git tag -a vX.Y.Z --cleanup=verbatim -F notes.md && git push origin vX.Y.Z
#      The tag annotation becomes the GitHub Release body via
#      `--notes-from-tag` in the next step. `--cleanup=verbatim` keeps
#      `##` markdown headers — without it, git strips any line starting
#      with `#` as a comment. A one-line `-m vX.Y.Z` annotation produces
#      an empty release body; don't use it.
#   6. gh release create vX.Y.Z --notes-from-tag --title vX.Y.Z
#
# The published GitHub Release fires .github/workflows/publish.yml, which
# runs `uv build && uv publish --trusted-publishing always` via OIDC in the
# `pypi` environment. No stored PyPI tokens. See README "Releasing" section.
pypi:
	rm -rf dist
	uv build
	uv publish

clean:
	rm -rf dist build *.egg-info .pytest_cache

# Regenerate docs/commands.md from the Typer CLI source. The file is
# gitignored — the CI workflow regenerates it before each `zensical build`.
docs:
	uv run typer main.py utils docs --name croc --output docs/commands.md --title "Commands"
	@# Strip the truncated **Commands**: index block — Typer truncates each entry
	@# at 45 chars (the live CLI doesn't). Per-command ## sections below carry
	@# the full descriptions; Zensical's right-side TOC re-creates the index.
	@sed -i.bak -e '/^\*\*Commands\*\*:$$/,/^## /{ /^## /!d; }' docs/commands.md && rm docs/commands.md.bak
	@# Prepend icon frontmatter — Typer doesn't emit any.
	@printf -- '---\nicon: lucide/terminal\n---\n\n%s' "$$(cat docs/commands.md)" > docs/commands.md

docs-serve: docs
	uv run zensical serve
