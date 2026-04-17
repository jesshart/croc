"""Shared fixtures for croc tests."""

from __future__ import annotations

import pathlib

import pytest
import yaml


def _render_doc(id_, title="t", kind="leaf", links=None, body="body"):
    fm = {"id": id_, "title": title, "kind": kind, "links": links or []}
    fm_yaml = yaml.dump(fm, sort_keys=False, default_flow_style=False)
    return f"---\n{fm_yaml}---\n\n{body}"


@pytest.fixture
def write_doc():
    """Helper: write a minimal valid croc doc at a given rel path under root."""

    def _write(root: pathlib.Path, rel: str, id_: str, **kwargs) -> pathlib.Path:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_render_doc(id_, **kwargs))
        return target

    return _write


@pytest.fixture
def sample_tree(tmp_path, write_doc):
    """A minimal three-file tree with one strong link + one weak link.

    design/self.md  (id=self)     ---[strong]-->  patterns/registry.md (id=registry)
                                  ---[weak]---->  notes/obsidian.md    (id=obsidian)
    """
    write_doc(
        tmp_path,
        "design/self.md",
        "self",
        kind="self",
        links=[
            {"to": "registry", "strength": "strong"},
            {"to": "obsidian", "strength": "weak"},
        ],
        body="The pattern: [[id:registry]]. See [[see:obsidian]].",
    )
    write_doc(tmp_path, "patterns/registry.md", "registry")
    write_doc(
        tmp_path,
        "notes/obsidian.md",
        "obsidian",
        links=[{"to": "registry", "strength": "weak"}],
        body="See [[see:registry]]",
    )
    return tmp_path
