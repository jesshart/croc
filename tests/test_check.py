"""Borrow checker tests — five rules, plus parser robustness."""

from __future__ import annotations

import pathlib

import pytest

from croc.check import (
    TreeError,
    check,
    load_tree,
    parse_frontmatter,
    scan_symlinks,
)


class TestLoadTree:
    def test_missing_root(self, tmp_path):
        ghost = tmp_path / "ghost"
        with pytest.raises(TreeError, match="does not exist"):
            load_tree(ghost)

    def test_root_is_file(self, tmp_path):
        f = tmp_path / "not-a-dir.md"
        f.write_text("")
        with pytest.raises(TreeError, match="not a directory"):
            load_tree(f)

    def test_empty_tree(self, tmp_path):
        assert load_tree(tmp_path) == []

    def test_valid_tree(self, sample_tree):
        docs = load_tree(sample_tree)
        assert sorted(d.id for d in docs) == ["obsidian", "registry", "self"]


class TestParseFrontmatter:
    def test_missing_frontmatter(self, tmp_path):
        p = tmp_path / "x.md"
        with pytest.raises(TreeError, match="missing frontmatter"):
            parse_frontmatter(p, "hello")

    def test_unterminated_frontmatter(self, tmp_path):
        p = tmp_path / "x.md"
        with pytest.raises(TreeError, match="unterminated"):
            parse_frontmatter(p, "---\nid: a\ntitle: t\n")

    def test_malformed_yaml(self, tmp_path):
        p = tmp_path / "x.md"
        with pytest.raises(TreeError, match="invalid YAML"):
            parse_frontmatter(p, '---\ntitle: "unclosed\n---\nbody')

    def test_frontmatter_not_mapping(self, tmp_path):
        p = tmp_path / "x.md"
        with pytest.raises(TreeError, match="must be a mapping"):
            parse_frontmatter(p, "---\n- a\n- b\n---\nbody")

    def test_missing_id(self, tmp_path):
        p = tmp_path / "x.md"
        with pytest.raises(TreeError, match="no `id`"):
            parse_frontmatter(p, "---\ntitle: t\n---\nbody")

    def test_numeric_id_rejected(self, tmp_path):
        p = tmp_path / "x.md"
        with pytest.raises(TreeError, match="must be a string"):
            parse_frontmatter(p, "---\nid: 12345\ntitle: t\n---\nbody")

    def test_illegal_id_chars(self, tmp_path):
        p = tmp_path / "x.md"
        with pytest.raises(TreeError, match="illegal characters"):
            parse_frontmatter(p, "---\nid: has spaces\ntitle: t\n---\nbody")

    @pytest.mark.parametrize(
        "id_value",
        ["adr-0042", "design.registry.pattern", "runbook_auth",
         "7f3a2c01-4b2d-4e6f-9a1b-3c5d7e9f1a2b"],
    )
    def test_valid_id_shapes(self, tmp_path, id_value):
        p = tmp_path / "x.md"
        fm, body = parse_frontmatter(
            p, f"---\nid: {id_value}\ntitle: t\nkind: leaf\nlinks: []\n---\nbody"
        )
        assert fm["id"] == id_value


class TestRules:
    def test_clean_tree(self, sample_tree):
        assert check(load_tree(sample_tree)) == []

    def test_ownership_duplicate_id(self, sample_tree, write_doc):
        write_doc(sample_tree, "notes/dup.md", "registry")  # collides with patterns/registry.md
        errors = check(load_tree(sample_tree))
        assert any("E-OWNERSHIP" in e for e in errors)

    def test_dangling_strong_ref_in_body(self, sample_tree):
        # Remove the registry target; self.md still [[id:registry]]
        (sample_tree / "patterns/registry.md").unlink()
        errors = check(load_tree(sample_tree))
        assert any("E-DANGLING" in e for e in errors)
        assert any("E-LIFETIME" in e for e in errors)

    def test_weak_link_to_missing_is_tolerated(self, tmp_path, write_doc):
        write_doc(
            tmp_path, "a.md", "a",
            links=[{"to": "ghost", "strength": "weak"}],
            body="See [[see:ghost]].",
        )
        assert check(load_tree(tmp_path)) == []

    def test_schema_missing_field(self, tmp_path):
        (tmp_path / "x.md").write_text("---\nid: a\ntitle: t\nkind: leaf\n---\nbody")
        errors = check(load_tree(tmp_path))
        assert any("missing `links`" in e for e in errors)

    def test_schema_links_not_a_list(self, tmp_path):
        (tmp_path / "x.md").write_text(
            "---\nid: a\ntitle: t\nkind: leaf\nlinks: oops\n---\nbody"
        )
        errors = check(load_tree(tmp_path))
        assert any("`links` must be a list" in e for e in errors)
        # No per-character cascade
        assert sum("must be a list" in e for e in errors) == 1

    def test_link_missing_to(self, tmp_path):
        (tmp_path / "x.md").write_text(
            "---\nid: a\ntitle: t\nkind: leaf\nlinks:\n  - { strength: strong }\n---\nbody"
        )
        errors = check(load_tree(tmp_path))
        assert any("link missing `to`" in e for e in errors)

    def test_identity_mismatch(self, tmp_path, write_doc):
        # Body references X but frontmatter doesn't declare a strong link to X
        write_doc(tmp_path, "target.md", "target")
        write_doc(
            tmp_path, "src.md", "src",
            links=[],
            body="[[id:target]]",
        )
        errors = check(load_tree(tmp_path))
        assert any("E-IDENTITY" in e for e in errors)


class TestExtendedRefDialect:
    """STRONG_REF/WEAK_REF accept #anchor and |display; id is still group 1."""

    def test_bare_id_still_works(self, tmp_path, write_doc):
        write_doc(tmp_path, "target.md", "target")
        write_doc(
            tmp_path, "src.md", "src",
            links=[{"to": "target", "strength": "strong"}],
            body="see [[id:target]]",
        )
        assert check(load_tree(tmp_path)) == []

    def test_id_with_anchor(self, tmp_path, write_doc):
        write_doc(tmp_path, "target.md", "target")
        write_doc(
            tmp_path, "src.md", "src",
            links=[{"to": "target", "strength": "strong"}],
            body="see [[id:target#section]]",
        )
        assert check(load_tree(tmp_path)) == []

    def test_id_with_display_text(self, tmp_path, write_doc):
        write_doc(tmp_path, "target.md", "target")
        write_doc(
            tmp_path, "src.md", "src",
            links=[{"to": "target", "strength": "strong"}],
            body="see [[id:target|The Target]]",
        )
        assert check(load_tree(tmp_path)) == []

    def test_id_with_anchor_and_display(self, tmp_path, write_doc):
        write_doc(tmp_path, "target.md", "target")
        write_doc(
            tmp_path, "src.md", "src",
            links=[{"to": "target", "strength": "strong"}],
            body="see [[id:target#section|The Section]]",
        )
        assert check(load_tree(tmp_path)) == []

    def test_dangling_still_detected_with_richer_syntax(self, tmp_path, write_doc):
        write_doc(
            tmp_path, "src.md", "src",
            body="see [[id:ghost#section|Ghost]]",
        )
        errors = check(load_tree(tmp_path))
        assert any("E-DANGLING" in e and "ghost" in e for e in errors)


class TestSymlinks:
    def test_symlinked_dir_produces_warning(self, tmp_path):
        external = tmp_path / "external"
        external.mkdir()
        (external / "hidden.md").write_text(
            "---\nid: hidden\ntitle: t\nkind: leaf\nlinks: []\n---\nbody"
        )
        main = tmp_path / "main"
        main.mkdir()
        (main / "linked").symlink_to(external)
        warnings = scan_symlinks(main)
        assert any("not traversed" in w for w in warnings)
