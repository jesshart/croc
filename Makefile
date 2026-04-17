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

pypi:
	rm -rf dist
	uv build
	uv publish

clean:
	rm -rf dist build *.egg-info .pytest_cache
