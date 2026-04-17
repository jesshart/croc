"""Operation tests — move, rename, init, adopt, plus --dry-run."""

from __future__ import annotations

import hashlib
import pathlib

import pytest

from croc.check import check, load_tree, parse_frontmatter
from croc.ops import (
    OpError,
    _propose_id,
    _slugify,
    _title_from_stem,
    adopt_tree,
    init_tree,
    move_file,
    rename_id,
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
