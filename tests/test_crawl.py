"""Tests for croc/crawl.py — plan, build, filters, and the adopt/molt cycle.

These tests assert crawl's core contract:
  - Plain-markdown output (no croc-specific frontmatter).
  - Shape-compatibility with the post-molt state so the cycle round-trips.
  - Determinism, idempotency, and gitignore honoring.

CLI-surface tests live in test_cli.py.
"""

from __future__ import annotations

import pathlib
import subprocess

import yaml

from croc.check import check, load_tree
from croc.crawl import (
    build_crawl,
    list_git_files,
    list_tracked_only_files,
    plan_crawl,
    resolve_file_filter,
)
from croc.ops import adopt_tree, init_tree, molt_tree


def _make_tree(root: pathlib.Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)


def test_plan_crawl_mirrors_tree(tmp_path: pathlib.Path) -> None:
    src = tmp_path / "src"
    _make_tree(src, {"a.py": "a=1", "sub/b.py": "b=2"})
    out = tmp_path / "thoughts" / "src"
    planned = plan_crawl(src, out)
    paths = {p.relative_to(out).as_posix() for p, _ in planned}
    assert paths == {"self.md", "a.md", "sub/self.md", "sub/b.md"}


def test_self_md_per_directory(tmp_path: pathlib.Path) -> None:
    src = tmp_path / "src"
    _make_tree(src, {"a.py": "", "x/b.py": "", "x/y/c.py": ""})
    out = tmp_path / "out"
    planned = plan_crawl(src, out)
    dirs_with_self = {p.parent for p, _ in planned if p.name == "self.md"}
    assert dirs_with_self == {out, out / "x", out / "x" / "y"}


def test_file_types_default_includes_all(tmp_path: pathlib.Path) -> None:
    """Default is 'all' — croc isn't language-specific. Users narrow
    via --file-types or tighten .gitignore."""
    src = tmp_path / "src"
    _make_tree(src, {"a.py": "", "b.ts": "", "c.md": ""})
    planned = plan_crawl(src, tmp_path / "out")
    stems = {pathlib.Path(p.name).stem for p, _ in planned}
    assert stems == {"self", "a", "b", "c"}


def test_file_types_multiple(tmp_path: pathlib.Path) -> None:
    src = tmp_path / "src"
    _make_tree(src, {"a.py": "", "b.ts": "", "c.md": ""})
    planned = plan_crawl(src, tmp_path / "out", file_types=[".py", ".ts"])
    stems = {pathlib.Path(p.name).stem for p, _ in planned}
    assert stems == {"self", "a", "b"}


def test_file_types_all(tmp_path: pathlib.Path) -> None:
    src = tmp_path / "src"
    _make_tree(src, {"a.py": "", "b.ts": "", "c.md": ""})
    planned = plan_crawl(src, tmp_path / "out", file_types=["all"])
    stems = {pathlib.Path(p.name).stem for p, _ in planned}
    assert stems == {"self", "a", "b", "c"}


def test_gitignore_respected(tmp_path: pathlib.Path) -> None:
    """With a synthetic git_files set, files outside it are pruned.
    No real git repo needed — plan_crawl accepts the set directly."""
    src = tmp_path / "src"
    _make_tree(src, {"a.py": "", "b.py": ""})
    git_files = {(src / "a.py").resolve()}
    planned = plan_crawl(src, tmp_path / "out", git_files=git_files)
    stems = {pathlib.Path(p.name).stem for p, _ in planned}
    assert stems == {"self", "a"}


def test_gitignore_prunes_dirs_with_no_tracked_content(tmp_path: pathlib.Path) -> None:
    """A subdir that contains no tracked files shouldn't produce a self.md."""
    src = tmp_path / "src"
    _make_tree(src, {"a.py": "", "untracked_dir/x.py": ""})
    git_files = {(src / "a.py").resolve()}
    planned = plan_crawl(src, tmp_path / "out", git_files=git_files)
    paths = {p.relative_to(tmp_path / "out").as_posix() for p, _ in planned}
    assert paths == {"self.md", "a.md"}


def test_skip_dot_and_pycache_dirs(tmp_path: pathlib.Path) -> None:
    src = tmp_path / "src"
    _make_tree(
        src,
        {
            "a.py": "",
            ".hidden/x.py": "",
            "__pycache__/y.py": "",
            "__pypackages__/z.py": "",
        },
    )
    planned = plan_crawl(src, tmp_path / "out")
    paths = {p.relative_to(tmp_path / "out").as_posix() for p, _ in planned}
    assert paths == {"self.md", "a.md"}


def _init_repo(root: pathlib.Path) -> None:
    """Set up a minimal git repo inside `root`, with a stable identity
    so `git commit` works in CI where user.name/email may be unset."""
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=root, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=root, check=True)


def test_list_tracked_only_returns_none_outside_repo(tmp_path: pathlib.Path) -> None:
    """Outside a git repo, the helper signals 'no filter' with None,
    matching list_git_files' existing shape."""
    assert list_tracked_only_files(tmp_path) is None


def test_list_tracked_only_excludes_untracked(tmp_path: pathlib.Path) -> None:
    """tracked-only returns just `git ls-files` — drafts are absent."""
    _init_repo(tmp_path)
    (tmp_path / "tracked.md").write_text("# tracked")
    subprocess.run(["git", "add", "tracked.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    (tmp_path / "draft.md").write_text("# draft")  # untracked, not ignored

    tracked_only = list_tracked_only_files(tmp_path)
    all_non_ignored = list_git_files(tmp_path)

    assert tracked_only is not None
    assert all_non_ignored is not None
    assert (tmp_path / "tracked.md").resolve() in tracked_only
    assert (tmp_path / "draft.md").resolve() not in tracked_only
    assert (tmp_path / "draft.md").resolve() in all_non_ignored
    assert tracked_only.issubset(all_non_ignored)


def test_list_tracked_only_excludes_ignored(tmp_path: pathlib.Path) -> None:
    """Ignored files never surface in either helper — envelope invariant."""
    _init_repo(tmp_path)
    (tmp_path / ".gitignore").write_text("ignored.md\n")
    (tmp_path / "tracked.md").write_text("# tracked")
    subprocess.run(["git", "add", ".gitignore", "tracked.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    (tmp_path / "ignored.md").write_text("# ignored")

    tracked_only = list_tracked_only_files(tmp_path)
    all_non_ignored = list_git_files(tmp_path)

    assert tracked_only is not None
    assert all_non_ignored is not None
    assert (tmp_path / "ignored.md").resolve() not in tracked_only
    assert (tmp_path / "ignored.md").resolve() not in all_non_ignored


def test_resolve_file_filter_dispatches_on_flag(tmp_path: pathlib.Path) -> None:
    """resolve_file_filter picks the right helper based on the flag."""
    _init_repo(tmp_path)
    (tmp_path / "tracked.md").write_text("# tracked")
    subprocess.run(["git", "add", "tracked.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    (tmp_path / "draft.md").write_text("# draft")

    default = resolve_file_filter(tmp_path, include_untracked=False)
    widened = resolve_file_filter(tmp_path, include_untracked=True)

    assert default == list_tracked_only_files(tmp_path)
    assert widened == list_git_files(tmp_path)


def test_resolve_file_filter_outside_repo_returns_none(tmp_path: pathlib.Path) -> None:
    """Outside a git repo, both branches return None (no filter)."""
    assert resolve_file_filter(tmp_path, include_untracked=False) is None
    assert resolve_file_filter(tmp_path, include_untracked=True) is None


def test_has_git_files_no_sibling_prefix_bug(tmp_path: pathlib.Path) -> None:
    """Regression: sibling dirs sharing a name prefix (`foo` vs `foobar`)
    must not confuse the directory-has-tracked-content check."""
    src = tmp_path / "src"
    _make_tree(src, {"foo/a.py": "", "foobar/b.py": ""})
    git_files = {(src / "foobar/b.py").resolve()}
    planned = plan_crawl(src, tmp_path / "out", git_files=git_files)
    paths = {p.relative_to(tmp_path / "out").as_posix() for p, _ in planned}
    assert paths == {"self.md", "foobar/self.md", "foobar/b.md"}


def test_build_crawl_is_idempotent(tmp_path: pathlib.Path) -> None:
    src = tmp_path / "src"
    _make_tree(src, {"a.py": ""})
    out = tmp_path / "out"
    first = build_crawl(src, out, force=False)
    second = build_crawl(src, out, force=False)
    assert len(first.created) == 2  # self.md + a.md
    assert len(first.skipped) == 0
    assert len(second.created) == 0
    assert len(second.skipped) == 2


def test_build_crawl_force_overwrites(tmp_path: pathlib.Path) -> None:
    src = tmp_path / "src"
    _make_tree(src, {"a.py": ""})
    out = tmp_path / "out"
    build_crawl(src, out, force=False)
    (out / "a.md").write_text("hand-edited")
    result = build_crawl(src, out, force=True)
    assert len(result.created) == 2
    assert "hand-edited" not in (out / "a.md").read_text()


def test_crawl_output_is_plain_markdown(tmp_path: pathlib.Path) -> None:
    """crawl's output carries only `mirrors:` — no croc-specific fields.
    This is the load-bearing property that makes the adopt/molt cycle
    symmetric around crawl's output."""
    src = tmp_path / "src"
    _make_tree(src, {"a.py": "", "sub/b.py": ""})
    out = tmp_path / "out"
    build_crawl(src, out, force=False)

    for p in out.rglob("*.md"):
        text = p.read_text()
        assert text.startswith("---\n"), f"{p}: expected frontmatter"
        fm_text = text.split("---\n", 2)[1]
        fm = yaml.safe_load(fm_text)
        assert set(fm.keys()) == {"mirrors"}, f"{p}: expected only mirrors, got {set(fm.keys())}"


def test_mirrors_field_points_back_to_source(tmp_path: pathlib.Path) -> None:
    src = tmp_path / "src"
    _make_tree(src, {"a.py": "", "sub/b.py": ""})
    out = tmp_path / "out"
    build_crawl(src, out, force=False)

    root_self = yaml.safe_load((out / "self.md").read_text().split("---\n", 2)[1])
    assert root_self["mirrors"] == "src"

    file_fm = yaml.safe_load((out / "a.md").read_text().split("---\n", 2)[1])
    assert file_fm["mirrors"] == "src/a.py"

    nested = yaml.safe_load((out / "sub" / "b.md").read_text().split("---\n", 2)[1])
    assert nested["mirrors"] == "src/sub/b.py"


def test_crawl_then_adopt_passes_check(tmp_path: pathlib.Path) -> None:
    """The end-to-end croc-readiness guarantee: crawl → adopt → check clean."""
    src = tmp_path / "src"
    _make_tree(src, {"a.py": "", "sub/b.py": ""})
    out = tmp_path / "out"
    build_crawl(src, out, force=False)
    init_tree(out)
    adopt_tree(out)
    assert check(load_tree(out)) == []


def test_crawl_adopt_molt_cycle_preserves_body(tmp_path: pathlib.Path) -> None:
    """crawl → adopt → molt round-trips body content byte-for-byte.
    Frontmatter may gain/lose croc fields; the human-written body does not."""
    src = tmp_path / "src"
    _make_tree(src, {"a.py": ""})
    out = tmp_path / "out"
    build_crawl(src, out, force=False)

    plain_body = (out / "a.md").read_text().split("---\n", 2)[2]

    init_tree(out)
    adopt_tree(out)
    molt_tree(out)

    after = (out / "a.md").read_text()
    # After molt, file may be fully plain (no frontmatter) or carry
    # only non-croc fields like `mirrors:`. Either way, body is stable.
    after_body = after.split("---\n", 2)[-1] if after.startswith("---\n") else after
    assert plain_body.strip() == after_body.strip()
