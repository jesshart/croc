"""Operation tests — move, rename, init, adopt, plus --dry-run."""

from __future__ import annotations

import hashlib
import pathlib

import pytest
import yaml

from croc.check import check, load_tree, parse_frontmatter
from croc.ops import (
    OpError,
    _molt_body,
    _molt_frontmatter,
    _propose_id,
    _slugify,
    _title_from_stem,
    adopt_tree,
    init_tree,
    molt_tree,
    move_file,
    rename_id,
    scan_path_refs,
)


def _tree_fingerprint(root: pathlib.Path) -> dict[str, str]:
    """Map every file under root to its content hash. Used to assert no-writes."""
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


# ---------------------------------------------------------------------------
# move
# ---------------------------------------------------------------------------


class TestMove:
    def test_happy_path(self, sample_tree):
        src = sample_tree / "patterns/registry.md"
        dst_dir = sample_tree / "design"
        new_dst = move_file(sample_tree, src, dst_dir)
        assert new_dst == (sample_tree / "design/registry.md").resolve()
        assert not src.exists()
        assert new_dst.exists()
        # Tree still sound — ID-based links don't care about paths.
        assert check(load_tree(sample_tree)) == []

    def test_nonexistent_src(self, sample_tree):
        with pytest.raises(OpError, match="does not exist"):
            move_file(
                sample_tree,
                sample_tree / "ghost.md",
                sample_tree / "design/",
            )

    def test_dst_already_exists(self, sample_tree):
        with pytest.raises(OpError, match="already exists"):
            move_file(
                sample_tree,
                sample_tree / "design/self.md",
                sample_tree / "patterns/registry.md",
            )

    def test_src_not_regular_file(self, sample_tree):
        with pytest.raises(OpError, match="not a regular file"):
            move_file(
                sample_tree,
                sample_tree / "design",
                sample_tree / "elsewhere",
            )

    def test_refuses_on_broken_tree(self, sample_tree):
        # Add a dangling strong link to break the tree.
        (sample_tree / "broken.md").write_text(
            "---\nid: broken\ntitle: t\nkind: leaf\n"
            "links:\n  - { to: ghost, strength: strong }\n---\n[[id:ghost]]\n"
        )
        with pytest.raises(OpError, match="not sound"):
            move_file(
                sample_tree,
                sample_tree / "design/self.md",
                sample_tree / "notes/",
            )

    def test_dry_run_writes_nothing(self, sample_tree):
        before = _tree_fingerprint(sample_tree)
        dst_dir = sample_tree / "design"
        result = move_file(
            sample_tree,
            sample_tree / "patterns/registry.md",
            dst_dir,
            dry_run=True,
        )
        assert result == (dst_dir / "registry.md").resolve()
        assert _tree_fingerprint(sample_tree) == before


# ---------------------------------------------------------------------------
# rename
# ---------------------------------------------------------------------------


class TestRename:
    def test_happy_path_rewrites_all_refs(self, sample_tree):
        changed = rename_id(sample_tree, "registry", "registry-pattern")
        assert set(changed) == {
            "design/self.md",
            "notes/obsidian.md",
            "patterns/registry.md",
        }
        assert check(load_tree(sample_tree)) == []
        # Post-rename: no `registry` left except as a substring of registry-pattern.
        for p in sample_tree.rglob("*.md"):
            text = p.read_text()
            assert "registry-pattern" in text or "self" in text or "obsidian" in text
            # No bare `registry` as an id anywhere.
            assert "id: registry\n" not in text
            assert "[[id:registry]]" not in text
            assert "[[see:registry]]" not in text

    def test_nonexistent_old_id(self, sample_tree):
        with pytest.raises(OpError, match="no doc with id"):
            rename_id(sample_tree, "ghost", "foo")

    def test_new_id_in_use(self, sample_tree):
        with pytest.raises(OpError, match="already in use"):
            rename_id(sample_tree, "registry", "obsidian")

    def test_illegal_new_id(self, sample_tree):
        with pytest.raises(OpError, match="not a valid id"):
            rename_id(sample_tree, "registry", "has spaces")

    def test_same_id(self, sample_tree):
        with pytest.raises(OpError, match="same"):
            rename_id(sample_tree, "registry", "registry")

    def test_refuses_on_broken_tree(self, sample_tree):
        (sample_tree / "broken.md").write_text(
            "---\nid: broken\ntitle: t\nkind: leaf\n"
            "links:\n  - { to: ghost, strength: strong }\n---\n[[id:ghost]]\n"
        )
        with pytest.raises(OpError, match="not sound"):
            rename_id(sample_tree, "registry", "registry-pattern")

    def test_dry_run_writes_nothing_and_reports_plan(self, sample_tree):
        before = _tree_fingerprint(sample_tree)
        changed = rename_id(
            sample_tree, "registry", "registry-pattern", dry_run=True
        )
        assert set(changed) == {
            "design/self.md",
            "notes/obsidian.md",
            "patterns/registry.md",
        }
        assert _tree_fingerprint(sample_tree) == before

    def test_failed_rename_leaves_tree_unchanged(self, sample_tree):
        """Validate-then-commit: refuses should never touch disk."""
        before = _tree_fingerprint(sample_tree)
        for old, new, match in [
            ("ghost", "foo", "no doc with id"),
            ("registry", "obsidian", "already in use"),
            ("registry", "has spaces", "not a valid id"),
            ("registry", "registry", "same"),
        ]:
            with pytest.raises(OpError, match=match):
                rename_id(sample_tree, old, new)
        assert _tree_fingerprint(sample_tree) == before


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


class TestInit:
    def test_creates_marker(self, tmp_path):
        actions = init_tree(tmp_path)
        assert (tmp_path / ".croc.toml").exists()
        assert any("CREATE" in a for a in actions)
        assert (tmp_path / ".croc.toml").read_text().startswith("# croc")

    def test_idempotent_when_marker_exists(self, tmp_path):
        (tmp_path / ".croc.toml").write_text("# preexisting\n")
        assert init_tree(tmp_path) == []
        # Did not overwrite
        assert (tmp_path / ".croc.toml").read_text() == "# preexisting\n"

    def test_dry_run_writes_nothing(self, tmp_path):
        actions = init_tree(tmp_path, dry_run=True)
        assert actions
        assert not (tmp_path / ".croc.toml").exists()


# ---------------------------------------------------------------------------
# adopt
# ---------------------------------------------------------------------------


class TestAdopt:
    def test_scaffolds_missing_frontmatter(self, tmp_path):
        (tmp_path / "some-runbook.md").write_text("# Runbook\n\nDo the thing.\n")
        (tmp_path / "README.md").write_text("# Project\n")
        actions = adopt_tree(tmp_path)
        assert len(actions) == 2
        # Both files now have valid frontmatter
        for p in (tmp_path / "some-runbook.md", tmp_path / "README.md"):
            raw = p.read_text()
            assert raw.startswith("---\n")
            fm, _ = parse_frontmatter(p, raw)
            assert fm["kind"] == "leaf"
            assert fm["links"] == []
        # Slugified ids
        ids = {parse_frontmatter(p, p.read_text())[0]["id"]
               for p in tmp_path.rglob("*.md")}
        assert ids == {"some-runbook", "readme"}

    def test_preserves_body_content(self, tmp_path):
        original = "# Runbook\n\nDo the thing.\n"
        (tmp_path / "x.md").write_text(original)
        adopt_tree(tmp_path)
        assert original in (tmp_path / "x.md").read_text()

    def test_self_md_gets_kind_self(self, tmp_path):
        (tmp_path / "dir").mkdir()
        (tmp_path / "dir/self.md").write_text("# Dir")
        adopt_tree(tmp_path)
        fm, _ = parse_frontmatter(
            tmp_path / "dir/self.md",
            (tmp_path / "dir/self.md").read_text(),
        )
        assert fm["kind"] == "self"

    def test_skips_files_with_existing_frontmatter(self, tmp_path, write_doc):
        write_doc(tmp_path, "managed.md", "managed")
        (tmp_path / "unmanaged.md").write_text("# Unmanaged\n")
        before = (tmp_path / "managed.md").read_text()
        actions = adopt_tree(tmp_path)
        # Only one scaffold action
        scaffolds = [a for a in actions if a.startswith("SCAFFOLD")]
        assert len(scaffolds) == 1
        assert "unmanaged.md" in scaffolds[0]
        # Managed file untouched
        assert (tmp_path / "managed.md").read_text() == before

    def test_reports_skip_for_malformed_frontmatter(self, tmp_path):
        (tmp_path / "broken.md").write_text("---\nid: a\n")  # unterminated
        actions = adopt_tree(tmp_path)
        assert any(a.startswith("SKIP") and "broken.md" in a for a in actions)

    def test_refuses_on_collision_between_proposed(self, tmp_path):
        """Path-slug ambiguity: two different paths slugify to the same id.

        `foo-bar/baz.md` and `foo/bar-baz.md` both slugify to `foo-bar-baz`.
        Hierarchical derivation usually dodges collisions, but can't help
        when the path components themselves alias under slugification.
        """
        (tmp_path / "foo-bar").mkdir()
        (tmp_path / "foo").mkdir()
        (tmp_path / "foo-bar/baz.md").write_text("x")
        (tmp_path / "foo/bar-baz.md").write_text("y")
        with pytest.raises(OpError, match="collisions"):
            adopt_tree(tmp_path)

    def test_refuses_on_collision_between_self_and_root_file(self, tmp_path):
        """`foo.md` (root) and `foo/self.md` both propose `foo`."""
        (tmp_path / "foo.md").write_text("x")
        (tmp_path / "foo").mkdir()
        (tmp_path / "foo/self.md").write_text("y")
        with pytest.raises(OpError, match="collisions"):
            adopt_tree(tmp_path)

    def test_refuses_on_collision_with_existing(self, tmp_path, write_doc):
        write_doc(tmp_path, "managed.md", "some-doc")
        (tmp_path / "some-doc.md").write_text("unmanaged")  # would scaffold as some-doc
        with pytest.raises(OpError, match="collisions"):
            adopt_tree(tmp_path)

    def test_filename_cannot_be_slugified(self, tmp_path):
        (tmp_path / "---.md").write_text("x")
        with pytest.raises(OpError, match="cannot derive"):
            adopt_tree(tmp_path)

    def test_dry_run_writes_nothing(self, tmp_path):
        (tmp_path / "some-runbook.md").write_text("# Runbook\n")
        before = _tree_fingerprint(tmp_path)
        actions = adopt_tree(tmp_path, dry_run=True)
        assert actions
        assert _tree_fingerprint(tmp_path) == before

    def test_idempotent(self, tmp_path):
        (tmp_path / "doc.md").write_text("# Doc\n")
        adopt_tree(tmp_path)
        actions = adopt_tree(tmp_path)  # second run
        assert not any(a.startswith("SCAFFOLD") for a in actions)

    def test_post_adopt_check_passes(self, tmp_path):
        (tmp_path / "a.md").write_text("# a")
        (tmp_path / "b.md").write_text("# b")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub/c.md").write_text("# c")
        adopt_tree(tmp_path)
        assert check(load_tree(tmp_path)) == []


# ---------------------------------------------------------------------------
# adopt — brownfield migration (foreign frontmatter, self.md convention)
# ---------------------------------------------------------------------------


class TestAdoptBrownfield:
    def test_augments_frontmatter_missing_only_id(self, tmp_path):
        """File has foreign frontmatter but no `id` — adopt fills the gap."""
        (tmp_path / "doc.md").write_text(
            "---\ntype: foo\nauthor: jane\n---\n# body\n"
        )
        actions = adopt_tree(tmp_path)
        assert any(a.startswith("AUGMENT") and "doc.md" in a for a in actions)
        fm, _ = parse_frontmatter(
            tmp_path / "doc.md", (tmp_path / "doc.md").read_text()
        )
        assert fm["id"] == "doc"
        # Foreign fields preserved
        assert fm["type"] == "foo"
        assert fm["author"] == "jane"
        # Missing croc fields filled
        assert fm["kind"] == "leaf"
        assert fm["links"] == []

    def test_augment_preserves_complex_foreign_fields(self, tmp_path):
        """type: directory-index, mirrors list, date strings — all preserved."""
        (tmp_path / "alerts").mkdir()
        (tmp_path / "alerts/self.md").write_text(
            "---\n"
            "type: directory-index\n"
            "mirrors:\n"
            "  - service-a\n"
            "  - service-b\n"
            'created: "2024-01-01"\n'
            "last_updated_by: jesse\n"
            "---\n\n"
            "# Alerts\n"
        )
        adopt_tree(tmp_path)
        fm, _ = parse_frontmatter(
            tmp_path / "alerts/self.md",
            (tmp_path / "alerts/self.md").read_text(),
        )
        assert fm["type"] == "directory-index"
        assert fm["mirrors"] == ["service-a", "service-b"]
        assert fm["created"] == "2024-01-01"
        assert fm["last_updated_by"] == "jesse"
        # And the croc fields were added
        assert fm["id"] == "alerts"
        assert fm["kind"] == "self"
        assert fm["links"] == []

    def test_augment_skips_file_that_is_already_fully_managed(self, tmp_path, write_doc):
        """A file with all four required croc fields gets no AUGMENT."""
        write_doc(tmp_path, "managed.md", "managed")
        before = (tmp_path / "managed.md").read_text()
        actions = adopt_tree(tmp_path)
        assert not any("managed.md" in a for a in actions)
        assert (tmp_path / "managed.md").read_text() == before

    def test_multiple_self_md_no_collision(self, tmp_path):
        """Three sibling self.md files get distinct parent-dir-derived ids."""
        for d in ("alerts", "runbooks", "adr"):
            (tmp_path / d).mkdir()
            (tmp_path / d / "self.md").write_text("# Index\n")
        adopt_tree(tmp_path)  # should not raise
        ids = {
            parse_frontmatter(
                tmp_path / d / "self.md",
                (tmp_path / d / "self.md").read_text(),
            )[0]["id"]
            for d in ("alerts", "runbooks", "adr")
        }
        assert ids == {"alerts", "runbooks", "adr"}

    def test_deep_self_md_uses_full_parent_path(self, tmp_path):
        deep = tmp_path / "core-validations/service-a"
        deep.mkdir(parents=True)
        (deep / "self.md").write_text("# Index\n")
        adopt_tree(tmp_path)
        fm, _ = parse_frontmatter(
            deep / "self.md", (deep / "self.md").read_text()
        )
        assert fm["id"] == "core-validations-service-a"

    def test_root_self_md_gets_root_id(self, tmp_path):
        (tmp_path / "self.md").write_text("# Root\n")
        adopt_tree(tmp_path)
        fm, _ = parse_frontmatter(
            tmp_path / "self.md", (tmp_path / "self.md").read_text()
        )
        assert fm["id"] == "root"

    def test_malformed_existing_id_is_skipped(self, tmp_path):
        """An existing id that doesn't match the grammar is not auto-fixed."""
        original = "---\nid: 12345\ntitle: t\n---\nbody"
        (tmp_path / "doc.md").write_text(original)
        actions = adopt_tree(tmp_path)
        assert any(a.startswith("SKIP") and "doc.md" in a for a in actions)
        assert (tmp_path / "doc.md").read_text() == original

    def test_invalid_yaml_is_skipped(self, tmp_path):
        (tmp_path / "doc.md").write_text('---\ntitle: "unclosed\n---\nbody')
        actions = adopt_tree(tmp_path)
        assert any(a.startswith("SKIP") and "invalid YAML" in a for a in actions)

    def test_frontmatter_not_mapping_is_skipped(self, tmp_path):
        (tmp_path / "doc.md").write_text("---\n- a\n- b\n---\nbody")
        actions = adopt_tree(tmp_path)
        assert any(a.startswith("SKIP") and "mapping" in a for a in actions)

    def test_brownfield_end_to_end(self, tmp_path):
        """A real-world brownfield shape survives adopt and passes check.

        Mixed tree: foreign-frontmatter `self.md` files (with non-croc
        schemas like `type: directory-index`), bare-body `self.md` deep
        in the tree, and plain markdown leaves.
        """
        (tmp_path / "alerts").mkdir()
        (tmp_path / "runbooks").mkdir()
        (tmp_path / "core/service-a").mkdir(parents=True)
        # Two self.md files with foreign (non-croc) frontmatter
        (tmp_path / "alerts/self.md").write_text(
            "---\ntype: directory-index\nmirrors:\n  - x\n---\n\n# Alerts\n"
        )
        (tmp_path / "runbooks/self.md").write_text(
            "---\ntype: directory-index\n---\n\n# Runbooks\n"
        )
        # Deep self.md at a nested path
        (tmp_path / "core/service-a/self.md").write_text("# Service A\n")
        # Plain markdown leaves
        (tmp_path / "alerts/fire.md").write_text("# Fire alert\n")
        (tmp_path / "runbooks/onboarding.md").write_text("# Onboarding\n")
        actions = adopt_tree(tmp_path)
        # 2 AUGMENT (the foreign-frontmatter self.md's), 3 SCAFFOLD (deep self + 2 leaves)
        assert sum("AUGMENT" in a for a in actions) == 2
        assert sum("SCAFFOLD" in a for a in actions) == 3
        # Tree is now sound
        assert check(load_tree(tmp_path)) == []


# ---------------------------------------------------------------------------
# _propose_id
# ---------------------------------------------------------------------------


class TestProposeId:
    def test_self_md_uses_parent_dir(self, tmp_path):
        (tmp_path / "alerts").mkdir()
        (tmp_path / "alerts/self.md").write_text("")
        assert _propose_id(tmp_path / "alerts/self.md", tmp_path) == "alerts"

    def test_deep_self_md_uses_full_parent_path(self, tmp_path):
        (tmp_path / "a/b/c").mkdir(parents=True)
        (tmp_path / "a/b/c/self.md").write_text("")
        assert _propose_id(tmp_path / "a/b/c/self.md", tmp_path) == "a-b-c"

    def test_root_self_md_returns_root(self, tmp_path):
        (tmp_path / "self.md").write_text("")
        assert _propose_id(tmp_path / "self.md", tmp_path) == "root"

    def test_root_level_file_uses_bare_stem(self, tmp_path):
        """Root-level files have no parent path, so id is just the stem."""
        (tmp_path / "foo.md").write_text("")
        assert _propose_id(tmp_path / "foo.md", tmp_path) == "foo"

    def test_nested_file_uses_full_relative_path(self, tmp_path):
        (tmp_path / "alerts").mkdir()
        (tmp_path / "alerts/fire-alert.md").write_text("")
        assert (
            _propose_id(tmp_path / "alerts/fire-alert.md", tmp_path)
            == "alerts-fire-alert"
        )

    def test_python_init_mirror_files_are_path_unique(self, tmp_path):
        """`__init__.md` mirrored from `__init__.py` across a package tree.

        Under stem-only derivation, every `__init__.md` slugifies to
        `init` and all such files collide. Under hierarchical derivation,
        each gets a path-unique id.
        """
        for d in ("pkg/utils", "pkg/submod/alpha", "pkg/submod/beta"):
            (tmp_path / d).mkdir(parents=True)
            (tmp_path / d / "__init__.md").write_text("")
        ids = {
            _propose_id(p, tmp_path)
            for p in tmp_path.rglob("__init__.md")
        }
        assert ids == {
            "pkg-utils-init",
            "pkg-submod-alpha-init",
            "pkg-submod-beta-init",
        }

    def test_sibling_dirs_with_same_stem_dont_collide(self, tmp_path):
        """Per-tenant / per-region folder pattern: `{a,b,c}/notes.md`."""
        for region in ("east", "west", "central"):
            (tmp_path / "regions" / region).mkdir(parents=True)
            (tmp_path / "regions" / region / "notes.md").write_text("")
        ids = {
            _propose_id(p, tmp_path)
            for p in tmp_path.rglob("notes.md")
        }
        assert ids == {
            "regions-east-notes",
            "regions-west-notes",
            "regions-central-notes",
        }

    def test_dir_with_non_alnum_chars_gets_slugified(self, tmp_path):
        (tmp_path / "My Dir (v2)").mkdir()
        (tmp_path / "My Dir (v2)/self.md").write_text("")
        assert (
            _propose_id(tmp_path / "My Dir (v2)/self.md", tmp_path)
            == "my-dir-v2"
        )


class TestAdoptMigrateRefs:
    """--migrate-refs rewrites markdown path-refs to the croc dialect."""

    def test_simple_path_ref_migrated(self, tmp_path):
        """[foo](foo.md) → [[id:foo|foo]] + frontmatter link added."""
        (tmp_path / "target.md").write_text("# Target\n")
        (tmp_path / "src.md").write_text(
            "# Src\n\nLink: [target](target.md).\n"
        )
        adopt_tree(tmp_path, migrate_refs=True)
        src_content = (tmp_path / "src.md").read_text()
        assert "[[id:target|target]]" in src_content
        assert "(target.md)" not in src_content
        fm, _ = parse_frontmatter(tmp_path / "src.md", src_content)
        assert any(l.get("to") == "target" for l in fm["links"])
        # Tree is sound
        assert check(load_tree(tmp_path)) == []

    def test_relative_path_migrated(self, tmp_path):
        (tmp_path / "sub").mkdir()
        (tmp_path / "target.md").write_text("# Target\n")
        (tmp_path / "sub/src.md").write_text(
            "# Src\n\n[up one](../target.md)\n"
        )
        adopt_tree(tmp_path, migrate_refs=True)
        content = (tmp_path / "sub/src.md").read_text()
        assert "[[id:target|up one]]" in content
        assert check(load_tree(tmp_path)) == []

    def test_anchor_preserved(self, tmp_path):
        (tmp_path / "target.md").write_text("# Target\n")
        (tmp_path / "src.md").write_text(
            "# Src\n\n[Section X](target.md#section-x)\n"
        )
        adopt_tree(tmp_path, migrate_refs=True)
        content = (tmp_path / "src.md").read_text()
        assert "[[id:target#section-x|Section X]]" in content

    def test_display_text_preserved(self, tmp_path):
        (tmp_path / "data-glossary.md").write_text("# Data Glossary\n")
        (tmp_path / "src.md").write_text(
            "# Src\n\n[Data Glossary](data-glossary.md)\n"
        )
        adopt_tree(tmp_path, migrate_refs=True)
        content = (tmp_path / "src.md").read_text()
        assert "[[id:data-glossary|Data Glossary]]" in content

    def test_unresolvable_ref_skipped_with_note(self, tmp_path):
        (tmp_path / "src.md").write_text(
            "# Src\n\n[ghost](missing-target.md)\n"
        )
        actions = adopt_tree(tmp_path, migrate_refs=True)
        # Original ref is left in place
        content = (tmp_path / "src.md").read_text()
        assert "[ghost](missing-target.md)" in content
        # SKIP-REF note emitted
        assert any(a.startswith("SKIP-REF") for a in actions)
        # Note includes the raw path AND the resolution attempt so
        # case-sensitivity / symlink issues are diagnosable at a glance.
        skip_note = next(a for a in actions if a.startswith("SKIP-REF"))
        assert "missing-target.md" in skip_note
        assert "tried: missing-target.md" in skip_note

    def test_unresolvable_nested_ref_shows_resolved_tree_path(self, tmp_path):
        """A ref to `../sibling/foo.md` from a subdir should report the
        tree-relative resolved location, not just the raw path."""
        (tmp_path / "one").mkdir()
        (tmp_path / "two").mkdir()
        (tmp_path / "one/src.md").write_text("[sibling](../two/missing.md)")
        actions = adopt_tree(tmp_path, migrate_refs=True)
        skip_note = next(a for a in actions if a.startswith("SKIP-REF"))
        # Raw path preserved for the author's reference
        assert "'../two/missing.md'" in skip_note
        # Resolved tree-relative path for the debugger
        assert "tried: two/missing.md" in skip_note

    def test_non_lowercase_extension_is_detected_and_reported(self, tmp_path):
        """`.MD` is the silent-rot failure mode: old detection missed it
        entirely. New detection catches it and flags the case mismatch."""
        (tmp_path / "target.md").write_text("# t")
        (tmp_path / "src.md").write_text("[x](target.MD)")
        actions = adopt_tree(tmp_path, migrate_refs=True)
        # Body unchanged; ref was not migrated
        assert "[x](target.MD)" in (tmp_path / "src.md").read_text()
        # But it IS surfaced as a SKIP-REF with a case-specific message
        skip = next(a for a in actions if a.startswith("SKIP-REF") and "target.MD" in a)
        assert "non-lowercase" in skip
        assert ".md" in skip.lower()

    def test_case_mismatch_hints_at_matching_lowercase_target(self, tmp_path):
        """When a lowercase-extension file exists, the SKIP-REF should
        suggest it directly — minimizes time to fix."""
        (tmp_path / "target.md").write_text("# t")
        (tmp_path / "src.md").write_text("[x](target.MD)")
        actions = adopt_tree(tmp_path, migrate_refs=True)
        skip = next(a for a in actions if a.startswith("SKIP-REF"))
        assert "did you mean 'target.md'" in skip

    def test_case_mismatch_with_no_matching_target_has_no_hint(self, tmp_path):
        """No matching `.md` file → plain case-mismatch message, no hint."""
        (tmp_path / "src.md").write_text("[x](phantom.MD)")
        actions = adopt_tree(tmp_path, migrate_refs=True)
        skip = next(a for a in actions if a.startswith("SKIP-REF"))
        assert "non-lowercase" in skip
        assert "did you mean" not in skip

    def test_all_three_case_variants_detected(self, tmp_path):
        """`.MD`, `.Md`, and `.mD` all produce case-mismatch reports."""
        (tmp_path / "src.md").write_text(
            "[a](one.MD) [b](two.Md) [c](three.mD)"
        )
        actions = adopt_tree(tmp_path, migrate_refs=True)
        skips = [a for a in actions if a.startswith("SKIP-REF")]
        assert len(skips) == 3
        for variant in ("one.MD", "two.Md", "three.mD"):
            assert any(variant in s for s in skips)

    def test_lowercase_md_unaffected(self, tmp_path):
        """Regression guard: lowercase `.md` still resolves normally."""
        (tmp_path / "target.md").write_text("# t")
        (tmp_path / "src.md").write_text("[x](target.md)")
        actions = adopt_tree(tmp_path, migrate_refs=True)
        assert not any(a.startswith("SKIP-REF") for a in actions)
        assert "[[id:target|x]]" in (tmp_path / "src.md").read_text()

    def test_ref_escaping_tree_root_is_unresolvable(self, tmp_path):
        (tmp_path / "inner").mkdir()
        (tmp_path / "inner/src.md").write_text(
            "# Src\n\n[outside](../../outside.md)\n"
        )
        actions = adopt_tree(tmp_path / "inner", migrate_refs=True)
        skip_note = next(a for a in actions if a.startswith("SKIP-REF"))
        # Escape message uses the absolute resolved path so author can see
        # where the ref actually pointed.
        assert "escapes tree root" in skip_note
        assert "resolved to:" in skip_note
        # Content unchanged for that ref
        assert "../../outside.md" in (tmp_path / "inner/src.md").read_text()

    def test_refs_require_adopt(self, tmp_path):
        """Tree with pre-existing croc refs but missing frontmatter is
        adopted and the refs become declared strong links (Rule 5)."""
        (tmp_path / "target.md").write_text("# Target\n")
        (tmp_path / "src.md").write_text(
            "# Src\n\n[target](target.md)\n"
        )
        adopt_tree(tmp_path, migrate_refs=True)
        # Both files have frontmatter now and src declares the link
        fm_src, _ = parse_frontmatter(
            tmp_path / "src.md", (tmp_path / "src.md").read_text()
        )
        assert any(
            l.get("to") == "target" and l.get("strength") == "strong"
            for l in fm_src["links"]
        )

    def test_multiple_refs_to_same_target_only_one_link_entry(self, tmp_path):
        (tmp_path / "target.md").write_text("# Target\n")
        (tmp_path / "src.md").write_text(
            "# Src\n\n[a](target.md) and again [b](target.md#s).\n"
        )
        adopt_tree(tmp_path, migrate_refs=True)
        fm, _ = parse_frontmatter(
            tmp_path / "src.md", (tmp_path / "src.md").read_text()
        )
        matching = [l for l in fm["links"] if l.get("to") == "target"]
        assert len(matching) == 1

    def test_default_adopt_migrates_refs(self, tmp_path):
        """Bare adopt (no flag) migrates body refs — the default-on policy."""
        (tmp_path / "target.md").write_text("# Target\n")
        (tmp_path / "src.md").write_text("# Src\n\n[t](target.md)\n")
        adopt_tree(tmp_path)
        content = (tmp_path / "src.md").read_text()
        assert "[t](target.md)" not in content
        assert "[[id:target|t]]" in content

    def test_no_migrate_refs_flag_preserves_path_refs(self, tmp_path):
        """Explicit opt-out: frontmatter adopted but body untouched."""
        (tmp_path / "target.md").write_text("# Target\n")
        (tmp_path / "src.md").write_text("# Src\n\n[t](target.md)\n")
        adopt_tree(tmp_path, migrate_refs=False)
        content = (tmp_path / "src.md").read_text()
        assert "[t](target.md)" in content
        assert "[[id:" not in content
        # But frontmatter is still scaffolded
        assert content.startswith("---\nid: src")

    def test_dry_run_with_migrate_refs_writes_nothing(self, tmp_path):
        (tmp_path / "target.md").write_text("# Target\n")
        (tmp_path / "src.md").write_text("# Src\n\n[t](target.md)\n")
        before = _tree_fingerprint(tmp_path)
        adopt_tree(tmp_path, migrate_refs=True, dry_run=True)
        assert _tree_fingerprint(tmp_path) == before

    def test_rerun_adopt_migrates_refs_in_already_managed_files(self, tmp_path):
        """The re-run gap: after a first adoption, newly-introduced path-refs
        in managed files must still be reachable on the next adopt."""
        (tmp_path / "target.md").write_text("# Target\n")
        (tmp_path / "src.md").write_text("# Src (no refs initially)\n")
        adopt_tree(tmp_path)  # first adoption — both files get frontmatter
        # Now simulate the user pasting a path-ref into the managed file
        existing = (tmp_path / "src.md").read_text()
        (tmp_path / "src.md").write_text(
            existing + "\n[late ref](target.md)\n"
        )
        # Second adopt must reach the managed file and migrate the ref
        actions = adopt_tree(tmp_path)
        src_actions = [a for a in actions if "src.md" in a and "MIGRATE" in a]
        assert len(src_actions) == 1
        content = (tmp_path / "src.md").read_text()
        assert "[late ref](target.md)" not in content
        assert "[[id:target|late ref]]" in content
        assert check(load_tree(tmp_path)) == []

    def test_rerun_adopt_is_noop_on_clean_tree(self, tmp_path):
        """Idempotency: a second run on a tree with no rot produces 0 actions."""
        (tmp_path / "target.md").write_text("# Target\n")
        (tmp_path / "src.md").write_text("# Src\n\n[t](target.md)\n")
        first = adopt_tree(tmp_path)
        assert any("SCAFFOLD" in a for a in first)
        second = adopt_tree(tmp_path)
        assert second == []  # nothing to do

    def test_rerun_with_only_unresolvable_new_refs_does_not_write(self, tmp_path):
        """Managed file grows a ref to a nonexistent target — the MIGRATE
        entry is pruned (nothing to migrate), only a SKIP-REF is emitted."""
        (tmp_path / "src.md").write_text("# Src\n")
        adopt_tree(tmp_path)
        before = (tmp_path / "src.md").read_text()
        existing = before
        (tmp_path / "src.md").write_text(existing + "\n[ghost](nope.md)\n")
        pre_rerun = _tree_fingerprint(tmp_path)
        actions = adopt_tree(tmp_path)
        # SKIP-REF emitted
        assert any(a.startswith("SKIP-REF") and "nope.md" in a for a in actions)
        # No MIGRATE action in the log (nothing was actually migrated)
        assert not any("MIGRATE" in a and "src.md" in a for a in actions)
        # And the file wasn't rewritten
        assert _tree_fingerprint(tmp_path) == pre_rerun

    def test_brownfield_with_refs_end_to_end(self, tmp_path):
        """Foreign-frontmatter self.md + path-linked leaves → all sound."""
        (tmp_path / "alerts").mkdir()
        (tmp_path / "alerts/self.md").write_text(
            "---\ntype: directory-index\n---\n\n"
            "# Alerts\n\n"
            "See [fire runbook](fire.md) and [glossary](../glossary.md).\n"
        )
        (tmp_path / "alerts/fire.md").write_text("# Fire\n")
        (tmp_path / "glossary.md").write_text("# Glossary\n")
        actions = adopt_tree(tmp_path, migrate_refs=True)
        # No unresolved refs
        assert not any(a.startswith("SKIP-REF") for a in actions)
        # Content migrated
        self_text = (tmp_path / "alerts/self.md").read_text()
        assert "[[id:alerts-fire|fire runbook]]" in self_text
        assert "[[id:glossary|glossary]]" in self_text
        # Tree sound
        assert check(load_tree(tmp_path)) == []


class TestAdoptMigrateRefsReporting:
    """Action log surfaces ref migrations per file so dry-run is auditable."""

    def test_action_line_includes_count_and_source_paths(self, tmp_path):
        (tmp_path / "target.md").write_text("# t")
        (tmp_path / "src.md").write_text("[t](target.md)")
        actions = adopt_tree(tmp_path, migrate_refs=True)
        src_action = next(a for a in actions if "src.md" in a)
        assert "migrated 1 ref" in src_action
        assert "target.md" in src_action

    def test_augment_and_migrate_collapse_to_one_line(self, tmp_path):
        """One write per file → one action line. AUGMENT + migration
        must not render as two separate lines."""
        (tmp_path / "target.md").write_text("# t")
        (tmp_path / "src.md").write_text(
            "---\ntype: foo\n---\n\n[t](target.md)\n"
        )
        actions = adopt_tree(tmp_path, migrate_refs=True)
        src_actions = [a for a in actions if "src.md" in a and not a.startswith("SKIP-REF")]
        assert len(src_actions) == 1
        assert "AUGMENT" in src_actions[0]
        assert "migrated 1 ref" in src_actions[0]

    def test_many_refs_are_truncated_with_count(self, tmp_path):
        for name in ("a", "b", "c", "d", "e"):
            (tmp_path / f"{name}.md").write_text(f"# {name}")
        (tmp_path / "src.md").write_text(
            "[a](a.md) [b](b.md) [c](c.md) [d](d.md) [e](e.md)"
        )
        actions = adopt_tree(tmp_path, migrate_refs=True)
        src_action = next(a for a in actions if "src.md" in a and "migrated" in a)
        assert "migrated 5 refs" in src_action
        assert "+2 more" in src_action
        # First 3 should appear; the last 2 shouldn't be enumerated
        assert "a.md" in src_action
        assert "c.md" in src_action

    def test_duplicate_refs_to_same_target_counted_once(self, tmp_path):
        (tmp_path / "target.md").write_text("# t")
        (tmp_path / "src.md").write_text(
            "[one](target.md) [two](target.md) [three](target.md)"
        )
        actions = adopt_tree(tmp_path, migrate_refs=True)
        src_action = next(a for a in actions if "src.md" in a and "migrated" in a)
        assert "migrated 1 ref" in src_action

    def test_non_migrating_entry_has_no_migrated_clause(self, tmp_path):
        """A plain file with no body refs → no "migrated N refs" in its line."""
        (tmp_path / "plain.md").write_text("# Just body, no links")
        actions = adopt_tree(tmp_path, migrate_refs=True)
        plain_action = next(a for a in actions if "plain.md" in a)
        assert "migrated" not in plain_action

    def test_mixed_resolved_and_unresolved_refs(self, tmp_path):
        """Follow-up: file with one resolvable and one unresolvable ref.

        Exercises SKIP-REF path alongside successful migration so both
        code paths run in the same plan entry.
        """
        (tmp_path / "good.md").write_text("# good")
        (tmp_path / "src.md").write_text(
            "[good](good.md) and [bad](ghost.md)"
        )
        actions = adopt_tree(tmp_path, migrate_refs=True)
        # One migration in the SCAFFOLD action line
        src_actions = [a for a in actions if "src.md" in a and not a.startswith("SKIP-REF")]
        assert len(src_actions) == 1
        assert "migrated 1 ref" in src_actions[0]
        # Plus one SKIP-REF for the unresolvable one
        skips = [a for a in actions if a.startswith("SKIP-REF")]
        assert len(skips) == 1
        assert "ghost.md" in skips[0]
        # And the unresolvable ref is left as-is in the body
        assert "[bad](ghost.md)" in (tmp_path / "src.md").read_text()
        # And the resolvable one was rewritten
        assert "[[id:good|good]]" in (tmp_path / "src.md").read_text()

    def test_dry_run_reports_migrations(self, tmp_path):
        """Dry-run must surface the planned migrations or it fails its purpose."""
        (tmp_path / "target.md").write_text("# t")
        (tmp_path / "src.md").write_text("[t](target.md)")
        before = _tree_fingerprint(tmp_path)
        actions = adopt_tree(tmp_path, migrate_refs=True, dry_run=True)
        # Nothing was written
        assert _tree_fingerprint(tmp_path) == before
        # But the plan still reports the migration
        src_action = next(a for a in actions if "src.md" in a)
        assert "migrated 1 ref" in src_action


class TestScanPathRefs:
    """croc refs diagnostic — walks tree, reports path-refs."""

    def test_empty_tree(self, tmp_path):
        assert scan_path_refs(tmp_path) == []

    def test_missing_root_errors(self, tmp_path):
        with pytest.raises(OpError, match="not a directory"):
            scan_path_refs(tmp_path / "ghost")

    def test_finds_resolved_refs(self, tmp_path):
        (tmp_path / "target.md").write_text("# t")
        (tmp_path / "src.md").write_text("[t](target.md)")
        reports = scan_path_refs(tmp_path)
        assert len(reports) == 1
        assert reports[0].resolved
        assert reports[0].target == "target.md"

    def test_finds_unresolved_refs(self, tmp_path):
        (tmp_path / "src.md").write_text("[ghost](missing.md)")
        reports = scan_path_refs(tmp_path)
        assert len(reports) == 1
        assert not reports[0].resolved
        assert reports[0].target is None
        assert reports[0].raw_path == "missing.md"

    def test_ignores_non_md_paths(self, tmp_path):
        (tmp_path / "src.md").write_text(
            "[image](foo.png) and [site](https://example.com)"
        )
        assert scan_path_refs(tmp_path) == []

    def test_ignores_croc_dialect_refs(self, tmp_path):
        (tmp_path / "src.md").write_text("[[id:foo]] and [[see:bar]]")
        assert scan_path_refs(tmp_path) == []

    def test_pre_adoption_tree_works(self, tmp_path):
        """scan_path_refs doesn't require croc frontmatter — works on raw md."""
        (tmp_path / "target.md").write_text("# No frontmatter target\n")
        (tmp_path / "src.md").write_text("# Also no frontmatter\n\n[t](target.md)\n")
        reports = scan_path_refs(tmp_path)
        assert len(reports) == 1
        assert reports[0].resolved

    def test_non_lowercase_extension_surfaces_as_unresolved_with_note(self, tmp_path):
        """scan_path_refs must catch `.MD` — otherwise brownfield audit
        misses silent-rot refs and authors never learn about them."""
        (tmp_path / "target.md").write_text("# t")
        (tmp_path / "src.md").write_text("[x](target.MD)")
        reports = scan_path_refs(tmp_path)
        assert len(reports) == 1
        r = reports[0]
        assert not r.resolved
        assert r.note is not None
        assert "non-lowercase" in r.note

    def test_lowercase_and_uppercase_both_detected_in_same_file(self, tmp_path):
        """A tree with both conventions should report both refs — neither
        slips through the detector."""
        (tmp_path / "good.md").write_text("# g")
        (tmp_path / "bad.md").write_text("# b")
        (tmp_path / "src.md").write_text("[g](good.md) [b](bad.MD)")
        reports = scan_path_refs(tmp_path)
        assert len(reports) == 2
        by_raw = {r.raw_path: r for r in reports}
        assert by_raw["good.md"].resolved
        assert not by_raw["bad.MD"].resolved
        assert by_raw["bad.MD"].note and "non-lowercase" in by_raw["bad.MD"].note


class TestProposeTitle:
    """Adopt picks a sensible `title` for self.md (dir name, not "Self")."""

    def test_self_md_title_is_parent_dir_titled(self, tmp_path):
        (tmp_path / "alerts").mkdir()
        (tmp_path / "alerts/self.md").write_text("# x")
        adopt_tree(tmp_path)
        fm, _ = parse_frontmatter(
            tmp_path / "alerts/self.md",
            (tmp_path / "alerts/self.md").read_text(),
        )
        assert fm["title"] == "Alerts"

    def test_non_self_title_from_stem(self, tmp_path):
        (tmp_path / "fire-alert.md").write_text("# x")
        adopt_tree(tmp_path)
        fm, _ = parse_frontmatter(
            tmp_path / "fire-alert.md",
            (tmp_path / "fire-alert.md").read_text(),
        )
        assert fm["title"] == "Fire Alert"


# ---------------------------------------------------------------------------
# slug / title helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stem,expected",
    [
        ("some-runbook", "some-runbook"),
        ("README", "readme"),
        ("Some File (v2)", "some-file-v2"),
        ("---leading--trailing---", "leading-trailing"),
        ("---", ""),  # only punctuation → empty; triggers "cannot derive"
        ("adr-0012-event-bus", "adr-0012-event-bus"),
        ("Mixed_Case.File", "mixed-case-file"),
    ],
)
def test_slugify(stem, expected):
    assert _slugify(stem) == expected


@pytest.mark.parametrize(
    "stem,expected",
    [
        ("some-runbook", "Some Runbook"),
        ("README", "Readme"),
        ("adr-0012-event-bus", "Adr 0012 Event Bus"),
    ],
)
def test_title_from_stem(stem, expected):
    assert _title_from_stem(stem) == expected


# ---------------------------------------------------------------------------
# molt — reverse of adopt
# ---------------------------------------------------------------------------


class TestMolt:
    """molt_tree rewrites the croc dialect back to plain markdown."""

    def test_basic_refs_rewritten(self, sample_tree):
        actions = molt_tree(sample_tree)
        self_content = (sample_tree / "design/self.md").read_text()
        # [[id:registry|...]] became [...](../patterns/registry.md)
        assert "[[id:" not in self_content
        assert "[[see:" not in self_content
        assert "[[id:registry|" not in self_content
        # One MOLT action per changed file, plus REMOVE for the marker
        assert any("MOLT design/self.md" in a for a in actions)

    def test_anchor_preserved(self, tmp_path, write_doc):
        write_doc(tmp_path, "target.md", "target")
        write_doc(
            tmp_path, "src.md", "src",
            links=[{"to": "target", "strength": "strong"}],
            body="See [[id:target#section-x|the section]].",
        )
        molt_tree(tmp_path)
        content = (tmp_path / "src.md").read_text()
        assert "[the section](target.md#section-x)" in content

    def test_display_text_preserved(self, tmp_path, write_doc):
        write_doc(tmp_path, "target.md", "target", title="Target Doc")
        write_doc(
            tmp_path, "src.md", "src",
            links=[{"to": "target", "strength": "strong"}],
            body="See [[id:target|the custom display]].",
        )
        molt_tree(tmp_path)
        assert "[the custom display](target.md)" in (
            tmp_path / "src.md"
        ).read_text()

    def test_bare_ref_falls_back_to_target_title(self, tmp_path, write_doc):
        """Bare `[[id:X]]` (no display) uses target's `title` field."""
        write_doc(tmp_path, "target.md", "target", title="The Target Doc")
        write_doc(
            tmp_path, "src.md", "src",
            links=[{"to": "target", "strength": "strong"}],
            body="See [[id:target]].",
        )
        molt_tree(tmp_path)
        assert "[The Target Doc](target.md)" in (
            tmp_path / "src.md"
        ).read_text()

    def test_weak_ref_rewritten_to_plain_link(self, tmp_path, write_doc):
        """Weak refs become plain markdown links — the strong/weak
        distinction is intentionally lossy on molt."""
        write_doc(tmp_path, "target.md", "target", title="Target")
        write_doc(
            tmp_path, "src.md", "src",
            links=[{"to": "target", "strength": "weak"}],
            body="See [[see:target|also]].",
        )
        molt_tree(tmp_path)
        content = (tmp_path / "src.md").read_text()
        assert "[also](target.md)" in content
        assert "[[see:" not in content

    def test_cross_directory_relative_paths(self, tmp_path, write_doc):
        """Nested source → parent-dir target produces `../target.md`."""
        write_doc(tmp_path, "target.md", "target", title="Target")
        write_doc(
            tmp_path, "sub/src.md", "src",
            links=[{"to": "target", "strength": "strong"}],
            body="See [[id:target|up]].",
        )
        molt_tree(tmp_path)
        assert "[up](../target.md)" in (
            tmp_path / "sub/src.md"
        ).read_text()

    def test_deeply_nested_relative_paths(self, tmp_path, write_doc):
        write_doc(tmp_path, "a/b/c/target.md", "target", title="Target")
        write_doc(
            tmp_path, "x/y/src.md", "src",
            links=[{"to": "target", "strength": "strong"}],
            body="[[id:target|t]]",
        )
        molt_tree(tmp_path)
        # From x/y/ to a/b/c/: ../../a/b/c/target.md
        assert "[t](../../a/b/c/target.md)" in (
            tmp_path / "x/y/src.md"
        ).read_text()

    def test_paths_use_forward_slashes(self, tmp_path, write_doc):
        """Output must use `/` even on Windows-style OSes (markdown portability)."""
        write_doc(tmp_path, "sub/target.md", "target", title="t")
        write_doc(
            tmp_path, "src.md", "src",
            links=[{"to": "target", "strength": "strong"}],
            body="[[id:target|t]]",
        )
        molt_tree(tmp_path)
        content = (tmp_path / "src.md").read_text()
        assert "sub/target.md" in content
        assert "sub\\target.md" not in content

    def test_frontmatter_strips_croc_fields(self, sample_tree):
        molt_tree(sample_tree)
        # Every molted file has no id/kind/links
        for p in sample_tree.rglob("*.md"):
            text = p.read_text()
            if text.startswith("---\n"):
                parts = text.split("---\n", 2)
                fm = yaml.safe_load(parts[1]) or {}
                assert "id" not in fm
                assert "kind" not in fm
                assert "links" not in fm

    def test_frontmatter_preserves_title(self, tmp_path, write_doc):
        write_doc(tmp_path, "t.md", "t", title="My Title")
        molt_tree(tmp_path)
        text = (tmp_path / "t.md").read_text()
        assert text.startswith("---\n")
        parts = text.split("---\n", 2)
        fm = yaml.safe_load(parts[1])
        assert fm == {"title": "My Title"}

    def test_frontmatter_preserves_foreign_fields_in_order(self, tmp_path):
        (tmp_path / "x.md").write_text(
            "---\n"
            "type: directory-index\n"
            "mirrors:\n  - a\n  - b\n"
            'created: "2024-01-01"\n'
            "id: x\n"
            "title: X\n"
            "kind: leaf\n"
            "links: []\n"
            "---\n\n# X\n"
        )
        molt_tree(tmp_path)
        text = (tmp_path / "x.md").read_text()
        fm = yaml.safe_load(text.split("---\n", 2)[1])
        # Croc fields stripped, foreign ordered fields survive
        assert list(fm.keys()) == ["type", "mirrors", "created", "title"]
        assert fm["type"] == "directory-index"
        assert fm["mirrors"] == ["a", "b"]

    # NOTE: the "frontmatter block removed entirely" case is tested via
    # the `_molt_frontmatter` helper below (returns None when nothing
    # remains). A valid croc tree always has `title`, so this branch
    # can't be reached end-to-end through `molt_tree` without a tree
    # that already fails `check` — which molt refuses on principle.

    def test_croc_toml_removed(self, tmp_path, write_doc):
        write_doc(tmp_path, "x.md", "x")
        (tmp_path / ".croc.toml").write_text('version = "0.1"\n')
        actions = molt_tree(tmp_path)
        assert not (tmp_path / ".croc.toml").exists()
        assert any("REMOVE .croc.toml" in a for a in actions)

    def test_no_croc_toml_no_remove_action(self, tmp_path, write_doc):
        """If there's no marker, molt doesn't emit a REMOVE action."""
        write_doc(tmp_path, "x.md", "x")
        actions = molt_tree(tmp_path)
        assert not any("REMOVE" in a for a in actions)

    def test_refuses_on_broken_tree(self, sample_tree):
        """Can't molt an unsound tree — same precondition as rename."""
        (sample_tree / "broken.md").write_text(
            "---\nid: broken\ntitle: t\nkind: leaf\n"
            "links:\n  - {to: ghost, strength: strong}\n---\n[[id:ghost]]\n"
        )
        with pytest.raises(OpError, match="not sound"):
            molt_tree(sample_tree)

    def test_dry_run_writes_nothing(self, sample_tree):
        before = _tree_fingerprint(sample_tree)
        marker = sample_tree / ".croc.toml"
        marker.write_text('version = "0.1"\n')
        before = _tree_fingerprint(sample_tree)
        actions = molt_tree(sample_tree, dry_run=True)
        assert actions  # non-empty plan
        # Nothing on disk changed, including the marker
        assert _tree_fingerprint(sample_tree) == before
        assert marker.exists()

    def test_post_molt_no_id_refs_remain(self, sample_tree):
        """The whole point: zero croc dialect survives a molt."""
        molt_tree(sample_tree)
        for p in sample_tree.rglob("*.md"):
            text = p.read_text()
            assert "[[id:" not in text
            assert "[[see:" not in text

    def test_post_molt_all_path_refs_resolve(self, sample_tree):
        molt_tree(sample_tree)
        reports = scan_path_refs(sample_tree)
        assert reports  # the tree should have refs
        unresolved = [r for r in reports if not r.resolved]
        assert unresolved == []

    def test_adopt_molt_roundtrip_semantic_equivalence(self, tmp_path):
        """Build a plain markdown tree, adopt, molt, and compare
        the post-molt content to the pre-adopt content for the pieces
        where equivalence is verifiable."""
        (tmp_path / "target.md").write_text("# Target content\n")
        (tmp_path / "src.md").write_text(
            "# Source\n\nSee [target](target.md).\n"
        )
        original_src = (tmp_path / "src.md").read_text()
        original_target = (tmp_path / "target.md").read_text()

        adopt_tree(tmp_path)       # dialect + frontmatter added
        molt_tree(tmp_path)        # dialect + frontmatter removed

        # After the round-trip, the body link text and path form match
        # (modulo the file acquiring and losing a `title: Src` frontmatter,
        # which molt preserves — so we check body substring).
        new_src = (tmp_path / "src.md").read_text()
        new_target = (tmp_path / "target.md").read_text()
        assert "[target](target.md)" in new_src
        assert "# Target content" in new_target
        # Marker should be gone too
        assert not (tmp_path / ".croc.toml").exists()

    def test_unmanaged_files_untouched_on_error(self, tmp_path, write_doc):
        """A tree with a non-croc .md file can't pass `check`, so molt
        refuses — the unmanaged file stays untouched, as expected."""
        write_doc(tmp_path, "managed.md", "managed")
        (tmp_path / "plain.md").write_text("# Not managed")
        with pytest.raises(OpError):
            molt_tree(tmp_path)
        assert (tmp_path / "plain.md").read_text() == "# Not managed"

    def test_weak_ref_to_missing_target_does_not_crash(self, tmp_path, write_doc):
        """Weak refs to absent targets are legal by design (Rule 3/4
        exemption). Molt must not KeyError — it should leave the ref
        in place and surface a SKIP-MOLT-REF note."""
        write_doc(
            tmp_path, "src.md", "src",
            links=[{"to": "ghost", "strength": "weak"}],
            body="Aspirational link: [[see:ghost|future doc]].",
        )
        # Tree is sound — weak refs are allowed to dangle
        actions = molt_tree(tmp_path)
        # Ref left as-is; molt did not forge a fake path
        content = (tmp_path / "src.md").read_text()
        assert "[[see:ghost|future doc]]" in content
        assert "(ghost.md)" not in content  # no forged path
        # SKIP-MOLT-REF note emitted
        assert any(a.startswith("SKIP-MOLT-REF") for a in actions)
        assert any("ghost" in a for a in actions)

    def test_weak_ref_to_missing_bare_form(self, tmp_path, write_doc):
        """Bare `[[see:X]]` (no display) with missing target — same policy."""
        write_doc(
            tmp_path, "src.md", "src",
            links=[{"to": "ghost", "strength": "weak"}],
            body="See [[see:ghost]].",
        )
        actions = molt_tree(tmp_path)
        assert "[[see:ghost]]" in (tmp_path / "src.md").read_text()
        assert any(a.startswith("SKIP-MOLT-REF") for a in actions)

    def test_mixed_resolvable_and_dangling_weak_refs_in_one_file(
        self, tmp_path, write_doc
    ):
        """A file with both kinds: resolvable refs get rewritten; the
        dangling weak ref stays put; both are reflected in the log."""
        write_doc(tmp_path, "target.md", "target", title="Target")
        write_doc(
            tmp_path, "src.md", "src",
            links=[
                {"to": "target", "strength": "strong"},
                {"to": "ghost", "strength": "weak"},
            ],
            body="See [[id:target|the target]] and maybe [[see:ghost]].",
        )
        actions = molt_tree(tmp_path)
        content = (tmp_path / "src.md").read_text()
        assert "[the target](target.md)" in content  # resolvable → rewritten
        assert "[[see:ghost]]" in content            # dangling → preserved
        # Action log shows both the MOLT (for the rewrite) and the SKIP-MOLT-REF
        assert any("MOLT src.md" in a and "rewrote 1 ref" in a for a in actions)
        assert any(a.startswith("SKIP-MOLT-REF") and "ghost" in a for a in actions)

    def test_dry_run_surfaces_dangling_weak_refs_in_plan(
        self, tmp_path, write_doc
    ):
        """Users must see dangling weak refs during dry-run — discovering
        leaked croc syntax at write time would be a nasty surprise."""
        write_doc(
            tmp_path, "src.md", "src",
            links=[{"to": "ghost", "strength": "weak"}],
            body="[[see:ghost|future]]",
        )
        before = _tree_fingerprint(tmp_path)
        actions = molt_tree(tmp_path, dry_run=True)
        assert _tree_fingerprint(tmp_path) == before  # nothing written
        assert any(a.startswith("SKIP-MOLT-REF") for a in actions)


class TestMoltHelpers:
    """Isolated helpers for precise assertions."""

    def test_molt_frontmatter_strips_only_croc_fields(self):
        fm, stripped = _molt_frontmatter(
            {"id": "x", "title": "T", "kind": "leaf", "links": [], "type": "foo"}
        )
        assert fm == {"title": "T", "type": "foo"}
        assert stripped == ["id", "kind", "links"]

    def test_molt_frontmatter_returns_none_when_empty(self):
        fm, stripped = _molt_frontmatter(
            {"id": "x", "kind": "leaf", "links": []}
        )
        assert fm is None
        assert stripped == ["id", "kind", "links"]

    def test_molt_frontmatter_preserves_insertion_order(self):
        fm, _ = _molt_frontmatter(
            {"type": "a", "id": "x", "mirrors": [1], "title": "T", "links": []}
        )
        assert list(fm.keys()) == ["type", "mirrors", "title"]


class TestRefRegexCaptureGroups:
    """Phase 1: regex must expose id/anchor/display as groups 1/2/3."""

    def test_bare_ref_captures_only_id(self):
        from croc.check import STRONG_REF
        m = STRONG_REF.search("text [[id:foo]] more")
        assert m is not None
        assert m.group(1) == "foo"
        assert m.group(2) is None
        assert m.group(3) is None

    def test_ref_with_anchor_only(self):
        from croc.check import STRONG_REF
        m = STRONG_REF.search("[[id:foo#section]]")
        assert m.group(1) == "foo"
        assert m.group(2) == "section"
        assert m.group(3) is None

    def test_ref_with_display_only(self):
        from croc.check import STRONG_REF
        m = STRONG_REF.search("[[id:foo|Display]]")
        assert m.group(1) == "foo"
        assert m.group(2) is None
        assert m.group(3) == "Display"

    def test_ref_with_anchor_and_display(self):
        from croc.check import STRONG_REF
        m = STRONG_REF.search("[[id:foo#sec|Display]]")
        assert m.group(1) == "foo"
        assert m.group(2) == "sec"
        assert m.group(3) == "Display"

    def test_weak_ref_same_group_layout(self):
        from croc.check import WEAK_REF
        m = WEAK_REF.search("[[see:foo#sec|D]]")
        assert m.group(1) == "foo"
        assert m.group(2) == "sec"
        assert m.group(3) == "D"
