"""Operation tests — move, rename, init, adopt, plus --dry-run."""

from __future__ import annotations

import hashlib
import pathlib

import pytest

from croc.check import check, load_tree, parse_frontmatter
from croc.ops import (
    OpError,
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
        (tmp_path / "sub1").mkdir()
        (tmp_path / "sub2").mkdir()
        (tmp_path / "sub1/foo.md").write_text("x")
        (tmp_path / "sub2/foo.md").write_text("y")
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
