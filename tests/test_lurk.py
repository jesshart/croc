"""Tests for croc/lurk.py — line-count helper and tree walker.

CLI-surface tests live in test_cli.py; these cover the library
contract directly.
"""

from __future__ import annotations

import os
import pathlib
import sys

import pytest

from croc.lurk import (
    LurkViolation,
    count_content_lines,
    lurk_tree,
)


class TestCountContentLines:
    def test_empty_string(self) -> None:
        assert count_content_lines("", include_frontmatter=False) == 0
        assert count_content_lines("", include_frontmatter=True) == 0

    def test_trailing_newline_and_no_trailing_newline_agree(self) -> None:
        """`splitlines()` treats `a\\nb\\n` and `a\\nb` as the same 2-line file.
        Literal by design — users asked for literal and obvious."""
        with_newline = "line1\nline2\n"
        without_newline = "line1\nline2"
        assert count_content_lines(with_newline, include_frontmatter=True) == 2
        assert count_content_lines(without_newline, include_frontmatter=True) == 2

    def test_frontmatter_excluded_by_default(self) -> None:
        text = "---\nid: foo\ntitle: t\nkind: leaf\nlinks: []\n---\n\nbody line one\nbody line two\n"
        # Body is `\nbody line one\nbody line two\n` → 3 splitlines (blank, two body)
        assert count_content_lines(text, include_frontmatter=False) == 3

    def test_frontmatter_included_when_flag_set(self) -> None:
        text = "---\nid: foo\ntitle: t\nkind: leaf\nlinks: []\n---\n\nbody line one\nbody line two\n"
        # Whole file: ---, id, title, kind, links, ---, blank, b1, b2 → 9 lines
        assert count_content_lines(text, include_frontmatter=True) == 9

    def test_malformed_frontmatter_falls_through_to_literal(self) -> None:
        """Opening fence, no closing fence. Lurk doesn't TreeError; it
        counts literal lines regardless of flag."""
        text = "---\nid: whatever\n(no closing fence)\njust text\n"
        # 4 splitlines
        assert count_content_lines(text, include_frontmatter=False) == 4
        assert count_content_lines(text, include_frontmatter=True) == 4

    def test_pure_frontmatter_no_body(self) -> None:
        """Frontmatter block only, no body content — 0 content lines."""
        text = "---\nid: x\ntitle: t\nkind: leaf\nlinks: []\n---\n"
        assert count_content_lines(text, include_frontmatter=False) == 0
        # Literal count: ---, id, title, kind, links, --- → 6 lines
        assert count_content_lines(text, include_frontmatter=True) == 6

    def test_no_frontmatter_unadopted_tree(self) -> None:
        """Plain markdown — lurk works without frontmatter."""
        text = "# Heading\n\nParagraph one.\n\nParagraph two.\n"
        assert count_content_lines(text, include_frontmatter=False) == 5
        assert count_content_lines(text, include_frontmatter=True) == 5


class TestLurkTree:
    def _write(self, root: pathlib.Path, rel: str, content: str) -> pathlib.Path:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return p

    def test_empty_tree_no_violations(self, tmp_path: pathlib.Path) -> None:
        assert lurk_tree(tmp_path, max_lines=100) == []

    def test_flags_only_violators(self, tmp_path: pathlib.Path) -> None:
        self._write(tmp_path, "small.md", "line\n" * 50)
        self._write(tmp_path, "big.md", "line\n" * 150)
        violations = lurk_tree(tmp_path, max_lines=100)
        assert len(violations) == 1
        assert violations[0].path == "big.md"
        assert violations[0].line_count == 150
        assert violations[0].limit == 100

    def test_exact_limit_is_not_a_violation(self, tmp_path: pathlib.Path) -> None:
        """100 lines at `-n 100` is fine; 101 is not. Strictly `>`."""
        self._write(tmp_path, "exact.md", "line\n" * 100)
        self._write(tmp_path, "over.md", "line\n" * 101)
        violations = lurk_tree(tmp_path, max_lines=100)
        paths = [v.path for v in violations]
        assert paths == ["over.md"]

    def test_frontmatter_exclusion_affects_count(self, tmp_path: pathlib.Path) -> None:
        """A file with 80 lines of body + 30-line frontmatter sits under
        100 by default, over 100 with --include-frontmatter."""
        body = "content\n" * 80
        links = "\n".join(f"  - to: id{i}" for i in range(25))  # 25 link lines
        content = f"---\nid: big\ntitle: t\nkind: leaf\nlinks:\n{links}\n---\n\n{body}"

        default_v = lurk_tree(tmp_path / ".", max_lines=100, include_frontmatter=False)
        self._write(tmp_path, "big.md", content)
        default_v = lurk_tree(tmp_path, max_lines=100, include_frontmatter=False)
        assert default_v == []  # body (81 splitlines: blank + 80) ≤ 100

        inclusive_v = lurk_tree(tmp_path, max_lines=100, include_frontmatter=True)
        assert len(inclusive_v) == 1  # full file crosses 100

    def test_git_files_filter_excludes_nonmembers(self, tmp_path: pathlib.Path) -> None:
        """Files outside the filter are skipped even if they would violate."""
        big_in = self._write(tmp_path, "a.md", "x\n" * 200)
        self._write(tmp_path, "b.md", "x\n" * 200)
        allowed = {big_in.resolve()}
        violations = lurk_tree(tmp_path, max_lines=100, git_files=allowed)
        assert [v.path for v in violations] == ["a.md"]

    def test_results_sorted_by_path(self, tmp_path: pathlib.Path) -> None:
        for name in ["z.md", "a.md", "m.md"]:
            self._write(tmp_path, name, "x\n" * 200)
        violations = lurk_tree(tmp_path, max_lines=100)
        assert [v.path for v in violations] == ["a.md", "m.md", "z.md"]

    def test_violation_dataclass_fields(self, tmp_path: pathlib.Path) -> None:
        """Violation exposes path, line_count, limit — and nothing more."""
        self._write(tmp_path, "x.md", "y\n" * 150)
        v = lurk_tree(tmp_path, max_lines=100)[0]
        assert isinstance(v, LurkViolation)
        assert v.path == "x.md"
        assert v.line_count == 150
        assert v.limit == 100

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX chmod semantics")
    def test_unreadable_file_silently_skipped(self, tmp_path: pathlib.Path) -> None:
        """If we can't read a file, don't crash the whole walk. Report
        nothing for it — same tolerance `scan_path_refs` has."""
        self._write(tmp_path, "ok.md", "x\n" * 150)
        bad = self._write(tmp_path, "bad.md", "x\n" * 150)
        try:
            os.chmod(bad, 0o000)
            # Root check: running as actual root bypasses chmod. If so,
            # skip — we can't simulate the OSError path.
            try:
                bad.read_text()
                pytest.skip("running as root; chmod doesn't restrict reads")
            except OSError:
                pass
            violations = lurk_tree(tmp_path, max_lines=100)
            assert [v.path for v in violations] == ["ok.md"]
        finally:
            os.chmod(bad, 0o644)
