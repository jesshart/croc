"""Tests for croc/bask.py — flatten, plan, and ref rewriting.

CLI-surface tests live alongside the library tests at the bottom; this
mirrors the test_crawl + test_cli split for symmetry but keeps bask
self-contained while the surface is small.
"""

from __future__ import annotations

import pathlib

import pytest
from typer.testing import CliRunner

from croc.bask import BaskError, _rewrite_path_refs, flatten_name, plan_bask
from main import app


def _make_tree(root: pathlib.Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ---------------------------------------------------------------------------
# flatten_name
# ---------------------------------------------------------------------------


class TestFlattenName:
    def test_single_segment_unchanged(self) -> None:
        assert flatten_name(pathlib.Path("a.md")) == "a.md"

    def test_multi_segment_joined_with_dunder(self) -> None:
        assert flatten_name(pathlib.Path("thoughts/modules/a.md")) == "thoughts__modules__a.md"

    def test_underscores_in_segments_preserved(self) -> None:
        """Single underscores inside path parts stay single — only the
        path-separator → dunder rewrite happens. This is what makes the
        joiner collision-safe."""
        assert flatten_name(pathlib.Path("dir/file_name.md")) == "dir__file_name.md"
        assert flatten_name(pathlib.Path("my_dir/a.md")) == "my_dir__a.md"
        assert flatten_name(pathlib.Path("a/b/file_name_with_unders.md")) == "a__b__file_name_with_unders.md"

    def test_self_md_no_special_handling(self) -> None:
        """MVP: no promotion of self.md to its parent's name. self.md
        is just another filename."""
        assert flatten_name(pathlib.Path("modules/self.md")) == "modules__self.md"
        assert flatten_name(pathlib.Path("self.md")) == "self.md"


# ---------------------------------------------------------------------------
# plan_bask — happy paths and rejections
# ---------------------------------------------------------------------------


class TestPlanBask:
    def test_example_tree(self, tmp_path: pathlib.Path) -> None:
        """The README's promised transformation, end-to-end."""
        thoughts = tmp_path / "thoughts"
        _make_tree(
            thoughts,
            {
                "self.md": "# root\n",
                "app.md": "# app\n",
                "modules/self.md": "# modules\n",
                "modules/a.md": "# a\n",
                "modules/b.md": "# b\n",
            },
        )
        plan, notes = plan_bask(thoughts, tmp_path / "out")
        names = {p.name for p, _ in plan}
        assert names == {
            "thoughts__self.md",
            "thoughts__app.md",
            "thoughts__modules__self.md",
            "thoughts__modules__a.md",
            "thoughts__modules__b.md",
        }
        assert notes == []

    def test_relative_to_root_parent(self, tmp_path: pathlib.Path) -> None:
        """Every output filename leads with the root directory's name."""
        thoughts = tmp_path / "thoughts"
        _make_tree(thoughts, {"a.md": "x", "sub/b.md": "y"})
        plan, _ = plan_bask(thoughts, tmp_path / "out")
        assert all(p.name.startswith("thoughts__") for p, _ in plan)

    def test_output_paths_under_output_dir(self, tmp_path: pathlib.Path) -> None:
        thoughts = tmp_path / "thoughts"
        _make_tree(thoughts, {"a.md": "x"})
        out_dir = tmp_path / "elsewhere"
        plan, _ = plan_bask(thoughts, out_dir)
        for p, _ in plan:
            assert p.parent == out_dir

    def test_embedded_dunder_in_dirname_rejected(self, tmp_path: pathlib.Path) -> None:
        thoughts = tmp_path / "thoughts"
        _make_tree(thoughts, {"a__b/c.md": "x"})
        with pytest.raises(BaskError) as excinfo:
            plan_bask(thoughts, tmp_path / "out")
        assert "a__b" in str(excinfo.value)
        assert "rename" in str(excinfo.value).lower()

    def test_embedded_dunder_in_filename_rejected(self, tmp_path: pathlib.Path) -> None:
        thoughts = tmp_path / "thoughts"
        _make_tree(thoughts, {"dir/a__b.md": "x"})
        with pytest.raises(BaskError) as excinfo:
            plan_bask(thoughts, tmp_path / "out")
        assert "a__b.md" in str(excinfo.value)

    def test_embedded_dunder_lists_all_offenders(self, tmp_path: pathlib.Path) -> None:
        """Surface every offender at once, not just the first."""
        thoughts = tmp_path / "thoughts"
        _make_tree(thoughts, {"a__b/c.md": "x", "d/e__f.md": "y", "g/h.md": "z"})
        with pytest.raises(BaskError) as excinfo:
            plan_bask(thoughts, tmp_path / "out")
        msg = str(excinfo.value)
        assert "a__b" in msg
        assert "e__f.md" in msg

    def test_non_md_files_ignored(self, tmp_path: pathlib.Path) -> None:
        thoughts = tmp_path / "thoughts"
        _make_tree(
            thoughts,
            {
                "a.md": "x",
                ".croc.toml": 'version = "0.1"\n',
                "stuff.txt": "not markdown",
                "code.py": "print('hi')",
            },
        )
        plan, _ = plan_bask(thoughts, tmp_path / "out")
        names = {p.name for p, _ in plan}
        assert names == {"thoughts__a.md"}

    def test_git_files_filter_excludes_nonmembers(self, tmp_path: pathlib.Path) -> None:
        thoughts = tmp_path / "thoughts"
        _make_tree(thoughts, {"keep.md": "x", "drop.md": "y"})
        keep = (thoughts / "keep.md").resolve()
        plan, _ = plan_bask(thoughts, tmp_path / "out", git_files={keep})
        assert {p.name for p, _ in plan} == {"thoughts__keep.md"}

    def test_empty_root_returns_empty_plan(self, tmp_path: pathlib.Path) -> None:
        thoughts = tmp_path / "thoughts"
        thoughts.mkdir()
        plan, notes = plan_bask(thoughts, tmp_path / "out")
        assert plan == []
        assert notes == []

    def test_root_must_be_a_directory(self, tmp_path: pathlib.Path) -> None:
        target = tmp_path / "missing"
        with pytest.raises(BaskError):
            plan_bask(target, tmp_path / "out")

    def test_bodies_passed_through_verbatim_with_rewrite_off(self, tmp_path: pathlib.Path) -> None:
        """rewrite_refs=False → byte-for-byte equality with source."""
        thoughts = tmp_path / "thoughts"
        body = (
            "---\n"
            "id: a\n"
            "title: A\n"
            "kind: leaf\n"
            "links: []\n"
            "---\n"
            "\n"
            "Croc ref: [[id:foo]]\n"
            "Path ref: [b](b.md)\n"
            "Unicode: café 🐊\n"
            "Trailing space:   \n"
        )
        _make_tree(thoughts, {"a.md": body, "b.md": "# b\n"})
        plan, _ = plan_bask(thoughts, tmp_path / "out", rewrite_refs=False)
        a_body = next(c for p, c in plan if p.name == "thoughts__a.md")
        assert a_body == body


# ---------------------------------------------------------------------------
# Path-ref rewriting
# ---------------------------------------------------------------------------


class TestRewritePathRefs:
    def test_rewrites_sibling_ref_to_flat_filename(self, tmp_path: pathlib.Path) -> None:
        thoughts = tmp_path / "thoughts"
        _make_tree(thoughts, {"a.md": "Link: [b](b.md)\n", "b.md": "# b\n"})
        plan, notes = plan_bask(thoughts, tmp_path / "out")
        a_body = next(c for p, c in plan if p.name == "thoughts__a.md")
        assert "[b](thoughts__b.md)" in a_body
        assert notes == []

    def test_rewrites_descendant_ref(self, tmp_path: pathlib.Path) -> None:
        thoughts = tmp_path / "thoughts"
        _make_tree(thoughts, {"a.md": "[c](modules/c.md)\n", "modules/c.md": "# c\n"})
        plan, _ = plan_bask(thoughts, tmp_path / "out")
        a_body = next(c for p, c in plan if p.name == "thoughts__a.md")
        assert "[c](thoughts__modules__c.md)" in a_body

    def test_rewrites_ancestor_ref(self, tmp_path: pathlib.Path) -> None:
        thoughts = tmp_path / "thoughts"
        _make_tree(thoughts, {"app.md": "# root app\n", "modules/a.md": "[r](../app.md)\n"})
        plan, _ = plan_bask(thoughts, tmp_path / "out")
        a_body = next(c for p, c in plan if p.name == "thoughts__modules__a.md")
        assert "[r](thoughts__app.md)" in a_body

    def test_anchor_preserved(self, tmp_path: pathlib.Path) -> None:
        thoughts = tmp_path / "thoughts"
        _make_tree(thoughts, {"a.md": "[s](b.md#section)\n", "b.md": "# b\n"})
        plan, _ = plan_bask(thoughts, tmp_path / "out")
        a_body = next(c for p, c in plan if p.name == "thoughts__a.md")
        assert "[s](thoughts__b.md#section)" in a_body

    def test_link_text_preserved(self, tmp_path: pathlib.Path) -> None:
        thoughts = tmp_path / "thoughts"
        _make_tree(thoughts, {"a.md": "[Display Text Here](b.md)\n", "b.md": "# b\n"})
        plan, _ = plan_bask(thoughts, tmp_path / "out")
        a_body = next(c for p, c in plan if p.name == "thoughts__a.md")
        assert "[Display Text Here](thoughts__b.md)" in a_body

    def test_id_refs_untouched(self, tmp_path: pathlib.Path) -> None:
        thoughts = tmp_path / "thoughts"
        _make_tree(thoughts, {"a.md": "Croc: [[id:foo]] [[see:bar]]\n", "b.md": "# b\n"})
        plan, _ = plan_bask(thoughts, tmp_path / "out")
        a_body = next(c for p, c in plan if p.name == "thoughts__a.md")
        assert "[[id:foo]]" in a_body
        assert "[[see:bar]]" in a_body

    def test_masked_regions_untouched(self, tmp_path: pathlib.Path) -> None:
        """Refs inside fenced code, inline code, and `\\[`-escaped
        brackets pass through verbatim — they're documentation."""
        thoughts = tmp_path / "thoughts"
        body = (
            "Real ref: [b](b.md)\n"
            "\n"
            "Inline code: `[b](b.md)` ← documentation, not live.\n"
            "\n"
            "```markdown\n"
            "Fenced: [b](b.md)\n"
            "```\n"
            "\n"
            "Escaped: \\[b\\](b.md)\n"
        )
        _make_tree(thoughts, {"a.md": body, "b.md": "# b\n"})
        plan, _ = plan_bask(thoughts, tmp_path / "out")
        a_body = next(c for p, c in plan if p.name == "thoughts__a.md")
        # Real ref rewritten
        assert "Real ref: [b](thoughts__b.md)" in a_body
        # Inline code: original survives, no flattened name leaks in
        assert "`[b](b.md)`" in a_body
        # Fenced block: original survives
        assert "Fenced: [b](b.md)" in a_body
        # Escaped: original survives
        assert "\\[b\\](b.md)" in a_body
        # Exactly one rewrite: the real ref. Three masked refs survive
        # untouched, so the flattened name appears only once.
        assert a_body.count("thoughts__b.md") == 1

    def test_external_ref_surfaces_skip_note(self, tmp_path: pathlib.Path) -> None:
        """Ref that escapes the bask root → unchanged + SKIP-REF."""
        thoughts = tmp_path / "thoughts"
        sibling = tmp_path / "other"
        _make_tree(thoughts, {"a.md": "[x](../other/file.md)\n"})
        _make_tree(sibling, {"file.md": "# elsewhere\n"})
        plan, notes = plan_bask(thoughts, tmp_path / "out")
        a_body = next(c for p, c in plan if p.name == "thoughts__a.md")
        assert "[x](../other/file.md)" in a_body
        assert any("SKIP-REF" in n and "escapes tree root" in n for n in notes)

    def test_unresolved_ref_surfaces_skip_note(self, tmp_path: pathlib.Path) -> None:
        thoughts = tmp_path / "thoughts"
        _make_tree(thoughts, {"a.md": "[x](ghost.md)\n"})
        plan, notes = plan_bask(thoughts, tmp_path / "out")
        a_body = next(c for p, c in plan if p.name == "thoughts__a.md")
        assert "[x](ghost.md)" in a_body
        assert any("SKIP-REF" in n and "ghost.md" in n for n in notes)

    def test_non_md_path_ref_untouched(self, tmp_path: pathlib.Path) -> None:
        """`[code](src/app.py)` doesn't match MD_PATH_REF; passes
        through with no rewrite, no skip note."""
        thoughts = tmp_path / "thoughts"
        _make_tree(thoughts, {"a.md": "[code](../src/app.py)\n"})
        plan, notes = plan_bask(thoughts, tmp_path / "out")
        a_body = next(c for p, c in plan if p.name == "thoughts__a.md")
        assert "[code](../src/app.py)" in a_body
        assert notes == []

    def test_case_mismatch_md_extension_surfaces_skip(self, tmp_path: pathlib.Path) -> None:
        thoughts = tmp_path / "thoughts"
        _make_tree(thoughts, {"a.md": "[x](b.MD)\n", "b.md": "# b\n"})
        plan, notes = plan_bask(thoughts, tmp_path / "out")
        a_body = next(c for p, c in plan if p.name == "thoughts__a.md")
        assert "[x](b.MD)" in a_body
        assert any("SKIP-REF" in n and "non-lowercase" in n for n in notes)

    def test_rewrite_helper_is_pure(self, tmp_path: pathlib.Path) -> None:
        """`_rewrite_path_refs` directly: pure function over already-built map."""
        thoughts = tmp_path / "thoughts"
        a = thoughts / "a.md"
        b = thoughts / "b.md"
        _make_tree(thoughts, {"a.md": "[b](b.md)\n", "b.md": "# b\n"})
        src_to_flat = {a.resolve(): "thoughts__a.md", b.resolve(): "thoughts__b.md"}
        new_body, notes = _rewrite_path_refs("[b](b.md)\n", a.resolve(), thoughts.resolve(), src_to_flat)
        assert new_body == "[b](thoughts__b.md)\n"
        assert notes == []


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


class TestBaskCli:
    def test_basic_run(self, tmp_path: pathlib.Path, runner: CliRunner) -> None:
        thoughts = tmp_path / "thoughts"
        _make_tree(thoughts, {"a.md": "x", "sub/b.md": "y"})
        out = tmp_path / "out"
        result = runner.invoke(app, ["bask", str(thoughts), "-o", str(out)])
        assert result.exit_code == 0, result.stdout + result.stderr
        names = {p.name for p in out.iterdir()}
        assert names == {"thoughts__a.md", "thoughts__sub__b.md"}

    def test_default_output_path(
        self,
        tmp_path: pathlib.Path,
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Default lands at ./tmp/<root-name>-bask/ relative to cwd."""
        thoughts = tmp_path / "thoughts"
        _make_tree(thoughts, {"a.md": "x"})
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["bask", str(thoughts)])
        assert result.exit_code == 0, result.stdout + result.stderr
        default_out = tmp_path / "tmp" / "thoughts-bask"
        assert default_out.is_dir()
        assert (default_out / "thoughts__a.md").exists()

    def test_dry_run_writes_nothing(self, tmp_path: pathlib.Path, runner: CliRunner) -> None:
        thoughts = tmp_path / "thoughts"
        _make_tree(thoughts, {"a.md": "x"})
        out = tmp_path / "out"
        result = runner.invoke(app, ["bask", str(thoughts), "-o", str(out), "--dry-run"])
        assert result.exit_code == 0, result.stdout + result.stderr
        assert not out.exists() or list(out.iterdir()) == []
        assert "would BASK" in result.stdout

    def test_force_overwrites(self, tmp_path: pathlib.Path, runner: CliRunner) -> None:
        thoughts = tmp_path / "thoughts"
        _make_tree(thoughts, {"a.md": "fresh body\n"})
        out = tmp_path / "out"
        out.mkdir()
        (out / "thoughts__a.md").write_text("STALE")
        result = runner.invoke(app, ["bask", str(thoughts), "-o", str(out), "--force"])
        assert result.exit_code == 0, result.stdout + result.stderr
        assert (out / "thoughts__a.md").read_text() == "fresh body\n"

    def test_existing_kept_without_force(self, tmp_path: pathlib.Path, runner: CliRunner) -> None:
        thoughts = tmp_path / "thoughts"
        _make_tree(thoughts, {"a.md": "fresh\n"})
        out = tmp_path / "out"
        out.mkdir()
        (out / "thoughts__a.md").write_text("STALE")
        result = runner.invoke(app, ["bask", str(thoughts), "-o", str(out)])
        assert result.exit_code == 0, result.stdout + result.stderr
        assert (out / "thoughts__a.md").read_text() == "STALE"
        assert "1 existing file(s) kept" in result.stderr

    def test_no_rewrite_refs_flag(self, tmp_path: pathlib.Path, runner: CliRunner) -> None:
        thoughts = tmp_path / "thoughts"
        _make_tree(thoughts, {"a.md": "[b](b.md)\n", "b.md": "# b\n"})
        out = tmp_path / "out"
        result = runner.invoke(app, ["bask", str(thoughts), "-o", str(out), "--no-rewrite-refs"])
        assert result.exit_code == 0, result.stdout + result.stderr
        # Body untouched
        assert (out / "thoughts__a.md").read_text() == "[b](b.md)\n"

    def test_strict_refs_exits_nonzero_when_unresolvable(self, tmp_path: pathlib.Path, runner: CliRunner) -> None:
        thoughts = tmp_path / "thoughts"
        _make_tree(thoughts, {"a.md": "[x](ghost.md)\n"})
        out = tmp_path / "out"
        result = runner.invoke(app, ["bask", str(thoughts), "-o", str(out), "--strict-refs"])
        assert result.exit_code == 1
        assert "bask OK" in result.stdout
        assert "ghost.md" in result.stderr

    def test_no_md_files_exits_nonzero(self, tmp_path: pathlib.Path, runner: CliRunner) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        result = runner.invoke(app, ["bask", str(empty), "-o", str(tmp_path / "out")])
        assert result.exit_code == 1
        assert "no .md files" in result.stderr

    def test_root_not_a_directory_exits_nonzero(self, tmp_path: pathlib.Path, runner: CliRunner) -> None:
        result = runner.invoke(app, ["bask", str(tmp_path / "missing")])
        assert result.exit_code == 1
        assert "not a directory" in result.stderr

    def test_embedded_dunder_exits_nonzero(self, tmp_path: pathlib.Path, runner: CliRunner) -> None:
        thoughts = tmp_path / "thoughts"
        _make_tree(thoughts, {"a__b/c.md": "x"})
        result = runner.invoke(app, ["bask", str(thoughts), "-o", str(tmp_path / "out")])
        assert result.exit_code == 1
        assert "a__b" in result.stderr
