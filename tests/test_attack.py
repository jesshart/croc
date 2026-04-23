"""Attack tests — scan code, resolve captures to docs by stem, write
`tracks:` frontmatter. Covers the full matrix from Phase 2 of the plan.

Uses a `git init`-ed `tmp_path` so `attack_tree` can anchor paths.
"""

from __future__ import annotations

import pathlib
import re
import subprocess

import pytest
import yaml

from croc.attack import attack_tree
from croc.config import CrocConfig, HuntConfig, TracePattern
from croc.ops import OpError


def _git_init(repo: pathlib.Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    # Configure a dummy identity so later git operations (if any) don't
    # fail on CI. attack itself doesn't commit, but being a repo is what
    # matters; plain init is enough.
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)


def _write(path: pathlib.Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _doc(id_: str, tracks: list[str] | None = None, extra_body: str = "") -> str:
    fm: dict = {"id": id_, "title": id_, "kind": "leaf", "links": []}
    if tracks is not None:
        fm["tracks"] = tracks
    return f"---\n{yaml.dump(fm, sort_keys=False)}---\n\n# {id_}\n{extra_body}"


def _read_fm(path: pathlib.Path) -> dict:
    raw = path.read_text()
    parts = raw.split("---\n", 2)
    return yaml.safe_load(parts[1])


def _pattern(name: str, regex: str, globs: tuple[str, ...]) -> TracePattern:
    return TracePattern(name=name, pattern=re.compile(regex), code_globs=globs)


def _config(*patterns: TracePattern) -> CrocConfig:
    return CrocConfig(version="0.1", traces=patterns, hunt=HuntConfig())


@pytest.fixture
def repo(tmp_path):
    _git_init(tmp_path)
    return tmp_path


PERSIST_PARQUET = r"persist_parquet\([\"']([^\"']+)[\"']\)"
GET_PARQUET = r"get_parquet\([\"']([^\"']+)[\"']\)"


class TestAttackHappyPath:
    def test_single_pattern_single_file_single_doc(self, repo):
        _write(repo / "src/producer.py", 'persist_parquet("revenue")\n')
        _write(repo / "thoughts/revenue.md", _doc("revenue"))
        cfg = _config(_pattern("persist_parquet", PERSIST_PARQUET, ("src/**/*.py",)))

        actions = attack_tree(repo / "thoughts", cfg, include_untracked=True)

        assert actions == ["ATTACK revenue.md (tracks: src/producer.py)"]
        fm = _read_fm(repo / "thoughts/revenue.md")
        assert fm["tracks"] == ["src/producer.py"]

    def test_multiple_captures_in_one_file(self, repo):
        _write(repo / "src/producer.py", 'persist_parquet("a")\npersist_parquet("b")\n')
        _write(repo / "thoughts/a.md", _doc("a"))
        _write(repo / "thoughts/b.md", _doc("b"))
        cfg = _config(_pattern("persist_parquet", PERSIST_PARQUET, ("src/**/*.py",)))

        attack_tree(repo / "thoughts", cfg, include_untracked=True)

        assert _read_fm(repo / "thoughts/a.md")["tracks"] == ["src/producer.py"]
        assert _read_fm(repo / "thoughts/b.md")["tracks"] == ["src/producer.py"]

    def test_multiple_source_files_hitting_same_doc(self, repo):
        _write(repo / "src/a.py", 'persist_parquet("revenue")\n')
        _write(repo / "src/b.py", 'persist_parquet("revenue")\n')
        _write(repo / "thoughts/revenue.md", _doc("revenue"))
        cfg = _config(_pattern("persist_parquet", PERSIST_PARQUET, ("src/**/*.py",)))

        attack_tree(repo / "thoughts", cfg, include_untracked=True)

        assert _read_fm(repo / "thoughts/revenue.md")["tracks"] == ["src/a.py", "src/b.py"]

    def test_multiple_patterns_hitting_same_doc_from_different_files(self, repo):
        # One pattern hits src/writer.py, another hits src/reader.py;
        # both capture "revenue" → revenue.md gets both tracks.
        _write(repo / "src/writer.py", 'persist_parquet("revenue")\n')
        _write(repo / "src/reader.py", 'get_parquet("revenue")\n')
        _write(repo / "thoughts/revenue.md", _doc("revenue"))
        cfg = _config(
            _pattern("persist_parquet", PERSIST_PARQUET, ("src/**/*.py",)),
            _pattern("get_parquet", GET_PARQUET, ("src/**/*.py",)),
        )

        attack_tree(repo / "thoughts", cfg, include_untracked=True)

        assert _read_fm(repo / "thoughts/revenue.md")["tracks"] == ["src/reader.py", "src/writer.py"]

    def test_multiple_patterns_hitting_same_doc_from_same_file(self, repo):
        # Same file contains both patterns referencing the same capture.
        # Result should have a single track (deduped).
        _write(repo / "src/both.py", 'persist_parquet("revenue")\nget_parquet("revenue")\n')
        _write(repo / "thoughts/revenue.md", _doc("revenue"))
        cfg = _config(
            _pattern("persist_parquet", PERSIST_PARQUET, ("src/**/*.py",)),
            _pattern("get_parquet", GET_PARQUET, ("src/**/*.py",)),
        )

        attack_tree(repo / "thoughts", cfg, include_untracked=True)

        assert _read_fm(repo / "thoughts/revenue.md")["tracks"] == ["src/both.py"]


class TestAttackSkipTrace:
    def test_no_matching_doc(self, repo):
        _write(repo / "src/producer.py", 'persist_parquet("ghost")\n')
        _write(repo / "thoughts/self.md", _doc("self"))
        cfg = _config(_pattern("persist_parquet", PERSIST_PARQUET, ("src/**/*.py",)))

        actions = attack_tree(repo / "thoughts", cfg, include_untracked=True)

        assert any(a.startswith("SKIP-TRACE") and "ghost" in a for a in actions)
        assert not any(a.startswith("ATTACK") for a in actions)

    def test_ambiguous_capture(self, repo):
        _write(repo / "src/producer.py", 'persist_parquet("revenue")\n')
        _write(repo / "thoughts/data/revenue.md", _doc("data-revenue"))
        _write(repo / "thoughts/archive/revenue.md", _doc("archive-revenue"))
        cfg = _config(_pattern("persist_parquet", PERSIST_PARQUET, ("src/**/*.py",)))

        actions = attack_tree(repo / "thoughts", cfg, include_untracked=True)

        skip = [a for a in actions if a.startswith("SKIP-TRACE")]
        assert len(skip) == 1
        assert "ambiguous" in skip[0]
        assert "matched 2 docs" in skip[0]

    def test_no_matches_clean(self, repo):
        _write(repo / "src/producer.py", "# no persist calls\n")
        _write(repo / "thoughts/revenue.md", _doc("revenue"))
        cfg = _config(_pattern("persist_parquet", PERSIST_PARQUET, ("src/**/*.py",)))

        actions = attack_tree(repo / "thoughts", cfg, include_untracked=True)

        assert actions == []


class TestAttackIdempotency:
    def test_repeat_run_is_noop(self, repo):
        _write(repo / "src/producer.py", 'persist_parquet("revenue")\n')
        _write(repo / "thoughts/revenue.md", _doc("revenue"))
        cfg = _config(_pattern("persist_parquet", PERSIST_PARQUET, ("src/**/*.py",)))

        attack_tree(repo / "thoughts", cfg, include_untracked=True)
        actions = attack_tree(repo / "thoughts", cfg, include_untracked=True)

        assert actions == []

    def test_stale_tracks_cleared(self, repo):
        # Doc already carries a stale `tracks:` entry. No code currently
        # matches → the entry should be removed.
        _write(repo / "src/clean.py", "# no matches\n")
        _write(
            repo / "thoughts/revenue.md",
            _doc("revenue", tracks=["src/old.py"]),
        )
        cfg = _config(_pattern("persist_parquet", PERSIST_PARQUET, ("src/**/*.py",)))

        actions = attack_tree(repo / "thoughts", cfg, include_untracked=True)

        assert any("cleared tracks" in a for a in actions)
        fm = _read_fm(repo / "thoughts/revenue.md")
        assert "tracks" not in fm


class TestAttackDryRun:
    def test_dry_run_writes_nothing(self, repo):
        _write(repo / "src/producer.py", 'persist_parquet("revenue")\n')
        _write(repo / "thoughts/revenue.md", _doc("revenue"))
        cfg = _config(_pattern("persist_parquet", PERSIST_PARQUET, ("src/**/*.py",)))
        before = (repo / "thoughts/revenue.md").read_text()

        actions = attack_tree(repo / "thoughts", cfg, dry_run=True, include_untracked=True)

        assert any(a.startswith("ATTACK") for a in actions)
        assert (repo / "thoughts/revenue.md").read_text() == before


class TestAttackEdgeCases:
    def test_doc_without_frontmatter(self, repo):
        # Plain markdown stub → attack scaffolds a frontmatter block
        # with only `tracks:`.
        _write(repo / "src/producer.py", 'persist_parquet("revenue")\n')
        _write(repo / "thoughts/revenue.md", "# revenue\n\nfree-form notes.\n")
        cfg = _config(_pattern("persist_parquet", PERSIST_PARQUET, ("src/**/*.py",)))

        attack_tree(repo / "thoughts", cfg, include_untracked=True)

        raw = (repo / "thoughts/revenue.md").read_text()
        assert raw.startswith("---\n")
        fm = _read_fm(repo / "thoughts/revenue.md")
        assert fm == {"tracks": ["src/producer.py"]}
        # Body preserved
        assert "free-form notes." in raw

    def test_doc_without_frontmatter_and_no_match_is_noop(self, repo):
        # Plain markdown stub + no capture → don't scaffold a stub frontmatter
        _write(repo / "src/clean.py", "# no matches\n")
        _write(repo / "thoughts/revenue.md", "# revenue\n\nnotes\n")
        before = (repo / "thoughts/revenue.md").read_text()
        cfg = _config(_pattern("persist_parquet", PERSIST_PARQUET, ("src/**/*.py",)))

        attack_tree(repo / "thoughts", cfg, include_untracked=True)

        assert (repo / "thoughts/revenue.md").read_text() == before


class TestAttackGitFilesFilter:
    def test_filter_excludes_source_file(self, repo):
        _write(repo / "src/draft.py", 'persist_parquet("revenue")\n')
        _write(repo / "thoughts/revenue.md", _doc("revenue"))
        # Filter excludes src/draft.py.
        allowed = {(repo / "thoughts/revenue.md").resolve()}
        cfg = _config(_pattern("persist_parquet", PERSIST_PARQUET, ("src/**/*.py",)))

        actions = attack_tree(repo / "thoughts", cfg, git_files=allowed)

        assert actions == []
        assert "tracks" not in _read_fm(repo / "thoughts/revenue.md")

    def test_filter_excludes_doc_file(self, repo):
        _write(repo / "src/producer.py", 'persist_parquet("revenue")\n')
        _write(repo / "thoughts/revenue.md", _doc("revenue"))
        # Filter excludes revenue.md from the stem index.
        allowed = {(repo / "src/producer.py").resolve()}
        cfg = _config(_pattern("persist_parquet", PERSIST_PARQUET, ("src/**/*.py",)))

        actions = attack_tree(repo / "thoughts", cfg, git_files=allowed)

        assert any(a.startswith("SKIP-TRACE") and "revenue" in a for a in actions)


class TestAttackPreconditions:
    def test_no_patterns_configured(self, repo):
        _write(repo / ".croc.toml", 'version = "0.1"\n')
        cfg = CrocConfig(version="0.1", traces=(), hunt=HuntConfig())

        with pytest.raises(OpError, match="no \\[\\[trace\\]\\] patterns"):
            attack_tree(repo, cfg)

    def test_outside_git_repo(self, tmp_path):
        # tmp_path has no git init.
        _write(tmp_path / "src/a.py", 'persist_parquet("revenue")\n')
        _write(tmp_path / "thoughts/revenue.md", _doc("revenue"))
        cfg = _config(_pattern("persist_parquet", PERSIST_PARQUET, ("src/**/*.py",)))

        with pytest.raises(OpError, match="requires a git repo"):
            attack_tree(tmp_path / "thoughts", cfg)

    def test_root_not_directory(self, repo):
        cfg = _config(_pattern("persist_parquet", PERSIST_PARQUET, ("src/**/*.py",)))
        with pytest.raises(OpError, match="not a directory"):
            attack_tree(repo / "nonexistent", cfg)
