"""Hunt tests — tracks frontmatter parsing and staleness detection.

Uses real `git init` + commits to exercise the staged / commit-range
diff modes. `git diff --cached` requires HEAD to exist, so each
fixture commits once up front.
"""

from __future__ import annotations

import pathlib
import subprocess

import pytest

from croc.hunt import HuntAlert, _read_tracks, hunt_tree
from croc.ops import OpError


def _run(repo: pathlib.Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _init_repo(repo: pathlib.Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    _run(repo, "config", "user.email", "t@t")
    _run(repo, "config", "user.name", "t")
    _run(repo, "config", "commit.gpgsign", "false")


def _write(path: pathlib.Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _doc_with_tracks(tracks: list[str], extra_body: str = "") -> str:
    tracks_yaml = "\n".join(f"- {t}" for t in tracks)
    return f"---\nid: x\ntitle: X\nkind: leaf\nlinks: []\ntracks:\n{tracks_yaml}\n---\n\n# X\n{extra_body}"


# ---------------------------------------------------------------------------
# _read_tracks
# ---------------------------------------------------------------------------


class TestReadTracks:
    def test_clean_tracks_list(self):
        raw = _doc_with_tracks(["src/a.py", "src/b.py"])
        assert _read_tracks(raw) == ["src/a.py", "src/b.py"]

    def test_no_frontmatter(self):
        assert _read_tracks("# just a heading\n") == []

    def test_frontmatter_without_tracks(self):
        raw = "---\nid: x\ntitle: T\n---\n\nbody\n"
        assert _read_tracks(raw) == []

    def test_malformed_yaml(self):
        raw = "---\nid: [unterminated\n---\nbody\n"
        assert _read_tracks(raw) == []

    def test_tracks_not_a_list(self):
        raw = "---\ntracks: oops\n---\nbody\n"
        assert _read_tracks(raw) == []

    def test_tracks_with_non_string_entries_filtered(self):
        raw = "---\ntracks:\n- src/a.py\n- 42\n---\nbody\n"
        assert _read_tracks(raw) == ["src/a.py"]

    def test_unterminated_frontmatter(self):
        raw = "---\nid: x\n\nno closing fence\n"
        assert _read_tracks(raw) == []


# ---------------------------------------------------------------------------
# hunt_tree — strict vs forgiving, staged vs base
# ---------------------------------------------------------------------------


@pytest.fixture
def committed_repo(tmp_path):
    """Repo with one initial commit containing:
    src/producer.py, src/reader.py
    thoughts/revenue.md  (tracks: src/producer.py)
    """
    _init_repo(tmp_path)
    _write(tmp_path / "src/producer.py", "# initial producer\n")
    _write(tmp_path / "src/reader.py", "# initial reader\n")
    _write(tmp_path / "thoughts/revenue.md", _doc_with_tracks(["src/producer.py"]))
    _run(tmp_path, "add", "-A")
    _run(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


class TestHuntTreeClean:
    def test_no_diff_no_alerts(self, committed_repo):
        alerts = hunt_tree(committed_repo / "thoughts", strict=True)
        assert alerts == []

    def test_no_bindings_no_alerts(self, tmp_path):
        _init_repo(tmp_path)
        _write(tmp_path / "thoughts/any.md", "# no frontmatter\n")
        _write(tmp_path / "src/unrelated.py", "# code\n")
        _run(tmp_path, "add", "-A")
        _run(tmp_path, "commit", "-q", "-m", "init")
        # Stage a change to the source.
        _write(tmp_path / "src/unrelated.py", "# changed\n")
        _run(tmp_path, "add", "src/unrelated.py")
        alerts = hunt_tree(tmp_path / "thoughts", strict=True)
        assert alerts == []


class TestHuntTreeStrict:
    def test_staged_source_change_triggers_alert(self, committed_repo):
        _write(committed_repo / "src/producer.py", "# changed\n")
        _run(committed_repo, "add", "src/producer.py")
        alerts = hunt_tree(committed_repo / "thoughts", strict=True)
        assert alerts == [HuntAlert(doc_rel="revenue.md", source_rel="src/producer.py")]

    def test_staged_source_and_doc_both_still_alerts(self, committed_repo):
        # Strict mode: source changed → alert, even if doc was updated
        # in the same commit.
        _write(committed_repo / "src/producer.py", "# changed\n")
        _write(committed_repo / "thoughts/revenue.md", _doc_with_tracks(["src/producer.py"], extra_body="notes\n"))
        _run(committed_repo, "add", "-A")
        alerts = hunt_tree(committed_repo / "thoughts", strict=True)
        assert len(alerts) == 1

    def test_unstaged_change_does_not_trigger(self, committed_repo):
        # Change on disk, but not `git add`ed: not in --cached diff.
        (committed_repo / "src/producer.py").write_text("# dirty\n")
        alerts = hunt_tree(committed_repo / "thoughts", strict=True)
        assert alerts == []

    def test_unrelated_source_change_does_not_trigger(self, committed_repo):
        _write(committed_repo / "src/reader.py", "# changed\n")
        _run(committed_repo, "add", "src/reader.py")
        alerts = hunt_tree(committed_repo / "thoughts", strict=True)
        assert alerts == []


class TestHuntTreeForgiving:
    def test_source_only_still_alerts(self, committed_repo):
        _write(committed_repo / "src/producer.py", "# changed\n")
        _run(committed_repo, "add", "src/producer.py")
        alerts = hunt_tree(committed_repo / "thoughts", strict=False)
        assert len(alerts) == 1

    def test_source_plus_doc_suppresses_alert(self, committed_repo):
        _write(committed_repo / "src/producer.py", "# changed\n")
        _write(committed_repo / "thoughts/revenue.md", _doc_with_tracks(["src/producer.py"], extra_body="refreshed\n"))
        _run(committed_repo, "add", "-A")
        alerts = hunt_tree(committed_repo / "thoughts", strict=False)
        assert alerts == []


class TestHuntTreeBaseRange:
    def test_base_range_detects_committed_change(self, committed_repo):
        # Commit a source change on the same branch.
        _write(committed_repo / "src/producer.py", "# changed\n")
        _run(committed_repo, "add", "-A")
        _run(committed_repo, "commit", "-q", "-m", "update producer")
        # `--base HEAD~1` should see the change.
        alerts = hunt_tree(committed_repo / "thoughts", strict=True, base="HEAD~1")
        assert len(alerts) == 1
        # `--base HEAD` should see nothing (empty range).
        alerts = hunt_tree(committed_repo / "thoughts", strict=True, base="HEAD")
        assert alerts == []

    def test_base_range_forgiving_counts_committed_doc_change(self, committed_repo):
        # Commit both the source and the doc together.
        _write(committed_repo / "src/producer.py", "# changed\n")
        _write(committed_repo / "thoughts/revenue.md", _doc_with_tracks(["src/producer.py"], extra_body="refreshed\n"))
        _run(committed_repo, "add", "-A")
        _run(committed_repo, "commit", "-q", "-m", "update both")
        alerts = hunt_tree(committed_repo / "thoughts", strict=False, base="HEAD~1")
        assert alerts == []


class TestHuntTreeMultiple:
    def test_multi_track_only_reports_changed(self, tmp_path):
        _init_repo(tmp_path)
        _write(tmp_path / "src/a.py", "# a\n")
        _write(tmp_path / "src/b.py", "# b\n")
        _write(tmp_path / "thoughts/revenue.md", _doc_with_tracks(["src/a.py", "src/b.py"]))
        _run(tmp_path, "add", "-A")
        _run(tmp_path, "commit", "-q", "-m", "init")
        _write(tmp_path / "src/a.py", "# changed\n")
        _run(tmp_path, "add", "src/a.py")
        alerts = hunt_tree(tmp_path / "thoughts", strict=True)
        assert alerts == [HuntAlert(doc_rel="revenue.md", source_rel="src/a.py")]

    def test_multi_track_both_changed(self, tmp_path):
        _init_repo(tmp_path)
        _write(tmp_path / "src/a.py", "# a\n")
        _write(tmp_path / "src/b.py", "# b\n")
        _write(tmp_path / "thoughts/revenue.md", _doc_with_tracks(["src/a.py", "src/b.py"]))
        _run(tmp_path, "add", "-A")
        _run(tmp_path, "commit", "-q", "-m", "init")
        _write(tmp_path / "src/a.py", "# changed\n")
        _write(tmp_path / "src/b.py", "# changed\n")
        _run(tmp_path, "add", "-A")
        alerts = hunt_tree(tmp_path / "thoughts", strict=True)
        # Sorted by (doc, source) — two alerts on same doc.
        assert alerts == [
            HuntAlert(doc_rel="revenue.md", source_rel="src/a.py"),
            HuntAlert(doc_rel="revenue.md", source_rel="src/b.py"),
        ]


class TestHuntTreePreconditions:
    def test_root_not_directory(self, committed_repo):
        with pytest.raises(OpError, match="not a directory"):
            hunt_tree(committed_repo / "nonexistent", strict=True)

    def test_outside_git_repo(self, tmp_path):
        # tmp_path without `git init` is outside a repo.
        _write(tmp_path / "thoughts/a.md", _doc_with_tracks(["src/x.py"]))
        with pytest.raises(OpError, match="requires a git repo"):
            hunt_tree(tmp_path / "thoughts", strict=True)

    def test_bad_base_ref(self, committed_repo):
        # Nonexistent ref → OpError from `git diff` stderr.
        with pytest.raises(OpError, match="git diff failed"):
            hunt_tree(committed_repo / "thoughts", strict=True, base="nonexistent-ref")


class TestHuntTreeGitFilter:
    def test_git_files_filter_excludes_doc(self, committed_repo):
        # Stage a source change.
        _write(committed_repo / "src/producer.py", "# changed\n")
        _run(committed_repo, "add", "src/producer.py")
        # Filter excludes the only doc with tracks.
        allowed: set[pathlib.Path] = set()
        alerts = hunt_tree(committed_repo / "thoughts", strict=True, git_files=allowed)
        assert alerts == []
