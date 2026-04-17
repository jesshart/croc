"""Doctest the README.

Uses `mktestdocs` to execute any Python code blocks in README.md. Today the
README only has bash/yaml blocks (which mktestdocs skips), so this is a
forward-looking guard: the moment a Python example is added, it runs in CI.
"""

from pathlib import Path

from mktestdocs import check_md_file


def test_readme_python_blocks_execute():
    check_md_file(fpath=Path(__file__).parent.parent / "README.md")
