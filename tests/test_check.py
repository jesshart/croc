"""Borrow checker tests — five rules, plus parser robustness."""

from __future__ import annotations

import pytest

from croc.check import (
    STRONG_REF,
    TreeError,
    check,
    in_any_span,
    load_tree,
    parse_frontmatter,
    scan_symlinks,
    scannable_spans,
)
from croc.ops import MD_PATH_REF


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

    def test_git_files_filter_excludes_nonmembers(self, sample_tree):
        """With a git_files set, docs outside the set are silently
        skipped — not parsed, not errored."""
        allowed = {(sample_tree / "design/self.md").resolve()}
        docs = load_tree(sample_tree, git_files=allowed)
        assert [d.id for d in docs] == ["self"]

    def test_git_files_none_walks_everything(self, sample_tree):
        """`git_files=None` is the documented 'no filter' sentinel."""
        docs = load_tree(sample_tree, git_files=None)
        assert sorted(d.id for d in docs) == ["obsidian", "registry", "self"]

    def test_git_files_filter_skips_before_parse(self, sample_tree):
        """A filtered-out file with broken frontmatter must not fail
        the load — that's the whole point of the filter."""
        broken = sample_tree / "design/broken.md"
        broken.write_text("---\nunterminated")
        # Include only the three valid docs; `broken.md` is excluded.
        allowed = {
            (sample_tree / "design/self.md").resolve(),
            (sample_tree / "patterns/registry.md").resolve(),
            (sample_tree / "notes/obsidian.md").resolve(),
        }
        docs = load_tree(sample_tree, git_files=allowed)
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
        ["adr-0042", "design.registry.pattern", "runbook_auth", "7f3a2c01-4b2d-4e6f-9a1b-3c5d7e9f1a2b"],
    )
    def test_valid_id_shapes(self, tmp_path, id_value):
        p = tmp_path / "x.md"
        fm, body = parse_frontmatter(p, f"---\nid: {id_value}\ntitle: t\nkind: leaf\nlinks: []\n---\nbody")
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
            tmp_path,
            "a.md",
            "a",
            links=[{"to": "ghost", "strength": "weak"}],
            body="See [[see:ghost]].",
        )
        assert check(load_tree(tmp_path)) == []

    def test_schema_missing_field(self, tmp_path):
        (tmp_path / "x.md").write_text("---\nid: a\ntitle: t\nkind: leaf\n---\nbody")
        errors = check(load_tree(tmp_path))
        assert any("missing `links`" in e for e in errors)

    def test_schema_links_not_a_list(self, tmp_path):
        (tmp_path / "x.md").write_text("---\nid: a\ntitle: t\nkind: leaf\nlinks: oops\n---\nbody")
        errors = check(load_tree(tmp_path))
        assert any("`links` must be a list" in e for e in errors)
        # No per-character cascade
        assert sum("must be a list" in e for e in errors) == 1

    def test_link_missing_to(self, tmp_path):
        (tmp_path / "x.md").write_text("---\nid: a\ntitle: t\nkind: leaf\nlinks:\n  - { strength: strong }\n---\nbody")
        errors = check(load_tree(tmp_path))
        assert any("link missing `to`" in e for e in errors)

    def test_identity_mismatch(self, tmp_path, write_doc):
        # Body references X but frontmatter doesn't declare a strong link to X
        write_doc(tmp_path, "target.md", "target")
        write_doc(
            tmp_path,
            "src.md",
            "src",
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
            tmp_path,
            "src.md",
            "src",
            links=[{"to": "target", "strength": "strong"}],
            body="see [[id:target]]",
        )
        assert check(load_tree(tmp_path)) == []

    def test_id_with_anchor(self, tmp_path, write_doc):
        write_doc(tmp_path, "target.md", "target")
        write_doc(
            tmp_path,
            "src.md",
            "src",
            links=[{"to": "target", "strength": "strong"}],
            body="see [[id:target#section]]",
        )
        assert check(load_tree(tmp_path)) == []

    def test_id_with_display_text(self, tmp_path, write_doc):
        write_doc(tmp_path, "target.md", "target")
        write_doc(
            tmp_path,
            "src.md",
            "src",
            links=[{"to": "target", "strength": "strong"}],
            body="see [[id:target|The Target]]",
        )
        assert check(load_tree(tmp_path)) == []

    def test_id_with_anchor_and_display(self, tmp_path, write_doc):
        write_doc(tmp_path, "target.md", "target")
        write_doc(
            tmp_path,
            "src.md",
            "src",
            links=[{"to": "target", "strength": "strong"}],
            body="see [[id:target#section|The Section]]",
        )
        assert check(load_tree(tmp_path)) == []

    def test_dangling_still_detected_with_richer_syntax(self, tmp_path, write_doc):
        write_doc(
            tmp_path,
            "src.md",
            "src",
            body="see [[id:ghost#section|Ghost]]",
        )
        errors = check(load_tree(tmp_path))
        assert any("E-DANGLING" in e and "ghost" in e for e in errors)


class TestSymlinks:
    def test_symlinked_dir_produces_warning(self, tmp_path):
        external = tmp_path / "external"
        external.mkdir()
        (external / "hidden.md").write_text("---\nid: hidden\ntitle: t\nkind: leaf\nlinks: []\n---\nbody")
        main = tmp_path / "main"
        main.mkdir()
        (main / "linked").symlink_to(external)
        warnings = scan_symlinks(main)
        assert any("not traversed" in w for w in warnings)

    def test_symlink_outside_filter_is_ignored(self, tmp_path):
        """A symlink the user excluded from their tree shouldn't warn."""
        external = tmp_path / "external"
        external.mkdir()
        main = tmp_path / "main"
        main.mkdir()
        (main / "linked").symlink_to(external)
        # Filter set excludes the symlink. It resolves to external/,
        # not to itself, so it would only match the set if the set
        # contained external/ — which it doesn't.
        warnings = scan_symlinks(main, git_files=set())
        assert warnings == []

    def test_symlink_inside_filter_still_warns(self, tmp_path):
        """Symlinks the filter DOES include continue to warn, mirroring
        the in-repo behavior where `git ls-files` reports the symlink
        and its resolved path lands in `git_files`."""
        external = tmp_path / "external"
        external.mkdir()
        main = tmp_path / "main"
        main.mkdir()
        link = main / "linked"
        link.symlink_to(external)
        # Replicate list_git_files' shape: each tracked entry is resolved.
        git_files = {link.resolve()}
        warnings = scan_symlinks(main, git_files=git_files)
        assert any("not traversed" in w for w in warnings)


class TestScannableSpans:
    """Non-scannable region computation. The primitive behind every
    ref-parser call site — tested in isolation before integration."""

    def test_no_special_regions(self):
        assert scannable_spans("plain text with [[id:foo]] ref") == []

    def test_empty_body(self):
        assert scannable_spans("") == []

    def test_fenced_block_masks_body_ref(self):
        body = "before\n```\n[[id:X]]\n```\nafter"
        match = STRONG_REF.search(body)
        assert match is not None
        assert in_any_span(match.start(), scannable_spans(body))

    def test_fence_with_language_tag(self):
        body = "```python\n[[id:X]]\n```"
        match = STRONG_REF.search(body)
        assert match is not None
        assert in_any_span(match.start(), scannable_spans(body))

    def test_tilde_fence(self):
        body = "~~~\n[[id:X]]\n~~~"
        match = STRONG_REF.search(body)
        assert match is not None
        assert in_any_span(match.start(), scannable_spans(body))

    def test_backtick_and_tilde_fences_do_not_cross_close(self):
        """A ~~~ line cannot close a ``` fence, and vice-versa — the
        closing char must match the opener."""
        body = "```\n[[id:X]]\n~~~\n[[id:Y]]\n```"
        spans = scannable_spans(body)
        # Whole body between first ``` and closing ``` is masked,
        # including the ~~~ line in the middle.
        matches = list(STRONG_REF.finditer(body))
        assert len(matches) == 2
        for m in matches:
            assert in_any_span(m.start(), spans)

    def test_longer_inner_fence_closes_shorter_opener(self):
        """Per CommonMark: the closing fence must be >= opener length.
        A 4-backtick line closes a 3-backtick opener, so a ref on the
        following line is OUTSIDE the fenced span (unmasked)."""
        body = "```\n````\nafter: [[id:X]]"
        match = STRONG_REF.search(body)
        assert match is not None
        assert not in_any_span(match.start(), scannable_spans(body))

    def test_shorter_inner_fence_does_not_close(self):
        """A 2-backtick line inside a 4-backtick opener is content."""
        body = "````\n``\n[[id:X]]\n````"
        match = STRONG_REF.search(body)
        assert match is not None
        assert in_any_span(match.start(), scannable_spans(body))

    def test_longer_opener_closed_by_same_length_run(self):
        """A 4-backtick opener is closed by a 4-backtick run (not 3)."""
        body = "````\n[[id:X]]\n````\nreal: [[id:foo]]"
        matches = list(STRONG_REF.finditer(body))
        assert len(matches) == 2
        spans = scannable_spans(body)
        assert in_any_span(matches[0].start(), spans)
        assert not in_any_span(matches[1].start(), spans)

    def test_unterminated_fence_extends_to_end(self):
        body = "```\n[[id:X]]\nno closing fence"
        match = STRONG_REF.search(body)
        assert match is not None
        assert in_any_span(match.start(), scannable_spans(body))

    def test_inline_code_masks_body_ref(self):
        body = "prose `[[id:X]]` more prose"
        match = STRONG_REF.search(body)
        assert match is not None
        assert in_any_span(match.start(), scannable_spans(body))

    def test_double_backtick_inline_code(self):
        body = "prose ``[[id:X]]`` more"
        match = STRONG_REF.search(body)
        assert match is not None
        assert in_any_span(match.start(), scannable_spans(body))

    def test_inline_code_with_embedded_backticks(self):
        """A 1-run opener is closed by the NEXT 1-run, not by an
        intermediate 2-run. Rare in croc docs but correct per spec."""
        body = "see `` `[[id:X]]` `` literal"
        # The `` `` `` opener encloses `` `[[id:X]]` `` and is closed
        # by the next `` `` `` — the whole thing is inline code.
        match = STRONG_REF.search(body)
        assert match is not None
        assert in_any_span(match.start(), scannable_spans(body))

    def test_unterminated_inline_code_falls_through(self):
        """An unmatched single backtick doesn't mask anything — the
        ref that follows is treated as a real ref."""
        body = "unterminated ` backtick [[id:X]]"
        match = STRONG_REF.search(body)
        assert match is not None
        assert not in_any_span(match.start(), scannable_spans(body))

    def test_escape_brackets_mask(self):
        r"""`\[[id:X]]` has a backslash-escaped leading `[`; the ref
        regex either won't anchor, or its anchor lands in a masked
        span — either way, no match should slip through."""
        body = r"prose \[[id:X]] more"
        match = STRONG_REF.search(body)
        if match is not None:
            assert in_any_span(match.start(), scannable_spans(body))

    def test_escape_parens_mask_path_ref(self):
        body = r"See \(outputs.md\) for examples."
        match = MD_PATH_REF.search(body)
        if match is not None:
            assert in_any_span(match.start(), scannable_spans(body))

    def test_real_ref_outside_fence_is_unmasked(self):
        body = "```\n[[id:X]]\n```\n\nReal: [[id:foo]]"
        matches = list(STRONG_REF.finditer(body))
        assert len(matches) == 2
        spans = scannable_spans(body)
        assert in_any_span(matches[0].start(), spans)
        assert not in_any_span(matches[1].start(), spans)

    def test_one_masked_one_real_in_prose(self):
        """The reported case: inline-code example + real ref."""
        body = "Example: `[[id:X]]`. Real: [[id:foo]]"
        matches = list(STRONG_REF.finditer(body))
        assert len(matches) == 2
        spans = scannable_spans(body)
        assert in_any_span(matches[0].start(), spans)
        assert not in_any_span(matches[1].start(), spans)

    def test_backticks_inside_fence_do_not_open_inline(self):
        """A lone backtick inside a fenced block must not start a
        nested inline span; we'd double-count positions or worse."""
        body = "```\nsome ` single tick inside\n```"
        # No refs to test, but spans should be well-formed: the fence
        # span covers everything, no stray inline span overlaps.
        spans = scannable_spans(body)
        assert len(spans) == 1
        start, end = spans[0]
        assert start == 0
        # Span must cover the backtick inside the fence (position ~9).
        assert start <= body.index(" ` ") < end

    def test_spans_are_sorted_and_nonoverlapping(self):
        body = "a `b` c ```\nd\n``` e \\[ f"
        spans = scannable_spans(body)
        for prev, nxt in zip(spans, spans[1:], strict=False):
            assert prev[1] <= nxt[0]

    def test_adjacent_spans_merge(self):
        """Fence immediately followed by an escape: spans should not
        overlap, and `in_any_span` works across the boundary."""
        body = "```\n[[id:X]]\n```\\["
        spans = scannable_spans(body)
        # No position is reported twice.
        for prev, nxt in zip(spans, spans[1:], strict=False):
            assert prev[1] <= nxt[0]

    def test_in_any_span_basic(self):
        spans = [(0, 5), (10, 15)]
        assert in_any_span(0, spans)
        assert in_any_span(4, spans)
        assert not in_any_span(5, spans)  # half-open
        assert not in_any_span(9, spans)
        assert in_any_span(10, spans)
        assert in_any_span(14, spans)
        assert not in_any_span(15, spans)
        assert not in_any_span(100, spans)

    def test_in_any_span_empty(self):
        assert not in_any_span(0, [])
        assert not in_any_span(42, [])


class TestMaskingIntegration:
    """`check()` honors scannable_spans for rules 3 (dangling) and 5
    (identity). A doc that teaches croc syntax must not fail the
    borrow checker on its own examples."""

    def test_inline_code_ref_does_not_dangle(self, tmp_path, write_doc):
        write_doc(
            tmp_path,
            "tutorial.md",
            "tutorial",
            body="References look like `[[id:X]]`.",
        )
        assert check(load_tree(tmp_path)) == []

    def test_fenced_block_ref_does_not_dangle(self, tmp_path, write_doc):
        write_doc(
            tmp_path,
            "tutorial.md",
            "tutorial",
            body="Example:\n\n```\n[[id:X]]\n```\n",
        )
        assert check(load_tree(tmp_path)) == []

    def test_escaped_ref_does_not_dangle(self, tmp_path, write_doc):
        write_doc(
            tmp_path,
            "tutorial.md",
            "tutorial",
            body=r"An escaped ref: \[[id:X]].",
        )
        assert check(load_tree(tmp_path)) == []

    def test_one_masked_one_real_flags_only_the_real(self, tmp_path, write_doc):
        """Mixed body: both occurrences target a nonexistent id, but
        only the unmasked one produces an E-DANGLING."""
        write_doc(
            tmp_path,
            "tutorial.md",
            "tutorial",
            body="Example: `[[id:ghost]]`. Real: [[id:ghost]]",
        )
        errors = check(load_tree(tmp_path))
        dangling = [e for e in errors if "E-DANGLING" in e]
        assert len(dangling) == 1

    def test_masked_ref_does_not_trigger_identity(self, tmp_path, write_doc):
        """A masked `[[id:target]]` in the body isn't a real ref, so
        it does NOT need to be declared in frontmatter `links:`."""
        write_doc(tmp_path, "target.md", "target")
        write_doc(
            tmp_path,
            "src.md",
            "src",
            links=[],
            body="Example syntax: `[[id:target]]`.",
        )
        errors = check(load_tree(tmp_path))
        assert not any("E-IDENTITY" in e for e in errors)

    def test_masked_ref_inside_fenced_block_does_not_trigger_identity(self, tmp_path, write_doc):
        write_doc(tmp_path, "target.md", "target")
        write_doc(
            tmp_path,
            "src.md",
            "src",
            links=[],
            body="```markdown\n[[id:target]]\n```\n",
        )
        errors = check(load_tree(tmp_path))
        assert not any("E-IDENTITY" in e for e in errors)

    def test_real_ref_alongside_masked_still_requires_identity(self, tmp_path, write_doc):
        """If a real ref exists alongside a masked one, the real ref
        still needs a frontmatter link declaration."""
        write_doc(tmp_path, "target.md", "target")
        write_doc(
            tmp_path,
            "src.md",
            "src",
            links=[],
            body="Example: `[[id:target]]`. Real: [[id:target]]",
        )
        errors = check(load_tree(tmp_path))
        assert any("E-IDENTITY" in e and "target" in e for e in errors)
