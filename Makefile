.PHONY: install test check lint format smoke pypi clean

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
#   4. git push && git tag -a vX.Y.Z -m vX.Y.Z && git push origin vX.Y.Z
#   5. gh release create vX.Y.Z --notes-from-tag
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
