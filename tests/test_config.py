"""Config loader tests — `.croc.toml` schema and validation."""

from __future__ import annotations

import pathlib
import subprocess

import pytest

from croc.config import ConfigError, CrocConfig, HuntConfig, load_config


def _write(path: pathlib.Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _git_init(repo: pathlib.Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)


@pytest.fixture
def repo(tmp_path):
    _git_init(tmp_path)
    return tmp_path


_TRACE = """\
[[trace]]
name = "persist_parquet"
pattern = '''persist_parquet\\(["']([^"']+)["']\\)'''
code_globs = ["src/**/*.py"]
"""


class TestLoadConfig:
    def test_missing_file_returns_default(self, tmp_path):
        cfg = load_config(tmp_path)
        assert cfg == CrocConfig(version=None)
        assert cfg.traces == ()
        assert cfg.hunt == HuntConfig(strict=True)

    def test_marker_only(self, tmp_path):
        _write(tmp_path / ".croc.toml", 'version = "0.1"\n')
        cfg = load_config(tmp_path)
        assert cfg.version == "0.1"
        assert cfg.traces == ()
        assert cfg.hunt.strict is True

    def test_single_trace_entry(self, tmp_path):
        _write(
            tmp_path / ".croc.toml",
            """\
version = "0.1"

[[trace]]
name = "persist_parquet"
pattern = '''persist_parquet\\(["']([^"']+)["']\\)'''
code_globs = ["**/*.py"]
""",
        )
        cfg = load_config(tmp_path)
        assert len(cfg.traces) == 1
        t = cfg.traces[0]
        assert t.name == "persist_parquet"
        assert t.code_globs == ("**/*.py",)
        assert t.pattern.groups == 1
        m = t.pattern.search('persist_parquet("revenue")')
        assert m is not None
        assert m.group(1) == "revenue"

    def test_multiple_traces_preserve_order(self, tmp_path):
        _write(
            tmp_path / ".croc.toml",
            """\
[[trace]]
name = "alpha"
pattern = 'alpha\\(([a-z]+)\\)'
code_globs = ["**/*.py"]

[[trace]]
name = "beta"
pattern = 'beta\\(([a-z]+)\\)'
code_globs = ["**/*.ts"]
""",
        )
        cfg = load_config(tmp_path)
        assert [t.name for t in cfg.traces] == ["alpha", "beta"]
        assert cfg.traces[1].code_globs == ("**/*.ts",)

    def test_hunt_strict_false(self, tmp_path):
        _write(
            tmp_path / ".croc.toml",
            """\
version = "0.1"

[hunt]
strict = false
""",
        )
        cfg = load_config(tmp_path)
        assert cfg.hunt.strict is False

    def test_invalid_toml(self, tmp_path):
        _write(tmp_path / ".croc.toml", "not = valid = toml\n")
        with pytest.raises(ConfigError, match="invalid TOML"):
            load_config(tmp_path)

    def test_version_must_be_string(self, tmp_path):
        _write(tmp_path / ".croc.toml", "version = 1\n")
        with pytest.raises(ConfigError, match="`version` must be a string"):
            load_config(tmp_path)

    def test_trace_not_array(self, tmp_path):
        _write(tmp_path / ".croc.toml", 'trace = "oops"\n')
        with pytest.raises(ConfigError, match="`trace` must be an array"):
            load_config(tmp_path)

    def test_trace_missing_name(self, tmp_path):
        _write(
            tmp_path / ".croc.toml",
            """\
[[trace]]
pattern = 'foo\\(([a-z]+)\\)'
code_globs = ["**/*.py"]
""",
        )
        with pytest.raises(ConfigError, match=r"\[\[trace\]\] #1: `name`"):
            load_config(tmp_path)

    def test_trace_missing_pattern(self, tmp_path):
        _write(
            tmp_path / ".croc.toml",
            """\
[[trace]]
name = "foo"
code_globs = ["**/*.py"]
""",
        )
        with pytest.raises(ConfigError, match="`pattern` must be a non-empty string"):
            load_config(tmp_path)

    def test_trace_missing_code_globs(self, tmp_path):
        _write(
            tmp_path / ".croc.toml",
            """\
[[trace]]
name = "foo"
pattern = 'foo\\(([a-z]+)\\)'
""",
        )
        with pytest.raises(ConfigError, match="`code_globs` must be a non-empty array"):
            load_config(tmp_path)

    def test_trace_code_globs_not_list(self, tmp_path):
        _write(
            tmp_path / ".croc.toml",
            """\
[[trace]]
name = "foo"
pattern = 'foo\\(([a-z]+)\\)'
code_globs = "**/*.py"
""",
        )
        with pytest.raises(ConfigError, match="`code_globs` must be a non-empty array"):
            load_config(tmp_path)

    def test_trace_code_globs_non_string_entry(self, tmp_path):
        _write(
            tmp_path / ".croc.toml",
            """\
[[trace]]
name = "foo"
pattern = 'foo\\(([a-z]+)\\)'
code_globs = ["**/*.py", 42]
""",
        )
        with pytest.raises(ConfigError, match="`code_globs` must contain only strings"):
            load_config(tmp_path)

    def test_trace_regex_fails_to_compile(self, tmp_path):
        _write(
            tmp_path / ".croc.toml",
            """\
[[trace]]
name = "foo"
pattern = '[unterminated'
code_globs = ["**/*.py"]
""",
        )
        with pytest.raises(ConfigError, match="invalid regex"):
            load_config(tmp_path)

    def test_trace_regex_zero_capture_groups(self, tmp_path):
        _write(
            tmp_path / ".croc.toml",
            """\
[[trace]]
name = "foo"
pattern = 'foo'
code_globs = ["**/*.py"]
""",
        )
        with pytest.raises(ConfigError, match="exactly one capture group"):
            load_config(tmp_path)

    def test_trace_regex_two_capture_groups(self, tmp_path):
        _write(
            tmp_path / ".croc.toml",
            """\
[[trace]]
name = "foo"
pattern = '(foo)(bar)'
code_globs = ["**/*.py"]
""",
        )
        with pytest.raises(ConfigError, match=r"exactly one capture group \(found 2\)"):
            load_config(tmp_path)

    def test_hunt_not_table(self, tmp_path):
        _write(tmp_path / ".croc.toml", 'hunt = "oops"\n')
        with pytest.raises(ConfigError, match=r"`\[hunt\]` must be a table"):
            load_config(tmp_path)

    def test_hunt_strict_not_bool(self, tmp_path):
        _write(
            tmp_path / ".croc.toml",
            """\
[hunt]
strict = "yes"
""",
        )
        with pytest.raises(ConfigError, match="`hunt.strict` must be a boolean"):
            load_config(tmp_path)


class TestDiscovery:
    """Walk-up discovery from a doc-tree ROOT to the git repo root."""

    def test_walks_up_to_repo_root(self, repo):
        _write(repo / ".croc.toml", 'version = "0.1"\n\n' + _TRACE)
        (repo / "thoughts").mkdir()
        cfg = load_config(repo / "thoughts")
        assert cfg.version == "0.1"
        assert [t.name for t in cfg.traces] == ["persist_parquet"]
        assert cfg.traces[0].code_globs == ("src/**/*.py",)

    def test_walks_up_through_nested_tree(self, repo):
        _write(repo / ".croc.toml", _TRACE)
        (repo / "docs" / "internal").mkdir(parents=True)
        cfg = load_config(repo / "docs" / "internal")
        assert [t.name for t in cfg.traces] == ["persist_parquet"]

    def test_tree_local_wins_over_repo_root(self, repo):
        _write(repo / ".croc.toml", _TRACE)  # repo-wide
        (repo / "thoughts").mkdir()
        _write(repo / "thoughts" / ".croc.toml", 'version = "9.9"\n')  # tree-local
        cfg = load_config(repo / "thoughts")
        # The tree-local file is found first; its (empty) trace set wins.
        assert cfg.version == "9.9"
        assert cfg.traces == ()

    def test_no_file_anywhere_returns_default(self, repo):
        (repo / "thoughts").mkdir()
        assert load_config(repo / "thoughts") == CrocConfig(version=None)

    def test_walk_up_bounded_by_repo_root(self, tmp_path):
        # A `.croc.toml` ABOVE the git repo root must not be picked up.
        _write(tmp_path / ".croc.toml", 'version = "outside"\n')
        repo = tmp_path / "myrepo"
        repo.mkdir()
        _git_init(repo)
        (repo / "thoughts").mkdir()
        assert load_config(repo / "thoughts") == CrocConfig(version=None)

    def test_outside_git_repo_only_tree_local(self, tmp_path):
        # No git repo: walk-up is disabled, only the tree-local file counts.
        (tmp_path / "thoughts").mkdir()
        _write(tmp_path / ".croc.toml", _TRACE)  # one level up, no git
        assert load_config(tmp_path / "thoughts") == CrocConfig(version=None)
        # But a tree-local file is still read.
        _write(tmp_path / "thoughts" / ".croc.toml", 'version = "0.1"\n')
        assert load_config(tmp_path / "thoughts").version == "0.1"


class TestMultiTree:
    """`[trees."<path>"]` namespacing and merge-over-top-level."""

    def test_per_tree_table_selected_by_relative_path(self, repo):
        _write(
            repo / ".croc.toml",
            """\
[trees."thoughts"]
version = "0.1"

[[trees."thoughts".trace]]
name = "thoughts_pat"
pattern = 'a(.)'
code_globs = ["src/**/*.py"]

[trees."docs"]
[[trees."docs".trace]]
name = "docs_pat"
pattern = 'b(.)'
code_globs = ["lib/**/*.py"]
""",
        )
        (repo / "thoughts").mkdir()
        (repo / "docs").mkdir()
        assert [t.name for t in load_config(repo / "thoughts").traces] == ["thoughts_pat"]
        assert [t.name for t in load_config(repo / "docs").traces] == ["docs_pat"]

    def test_nested_tree_key_requires_quoted_path(self, repo):
        _write(
            repo / ".croc.toml",
            """\
[trees."docs/internal"]
[[trees."docs/internal".trace]]
name = "nested"
pattern = 'a(.)'
code_globs = ["src/**/*.py"]
""",
        )
        (repo / "docs" / "internal").mkdir(parents=True)
        assert [t.name for t in load_config(repo / "docs" / "internal").traces] == ["nested"]

    def test_bare_and_quoted_keys_equivalent(self, repo):
        # `[trees.thoughts]` and `[trees."thoughts"]` are the same key.
        _write(
            repo / ".croc.toml",
            """\
[trees.thoughts]
version = "bare"
""",
        )
        (repo / "thoughts").mkdir()
        assert load_config(repo / "thoughts").version == "bare"

    def test_per_tree_overrides_top_level(self, repo):
        _write(
            repo / ".croc.toml",
            """\
version = "top"

[[trace]]
name = "top_pat"
pattern = 'a(.)'
code_globs = ["src/**/*.py"]

[hunt]
strict = true

[trees."thoughts"]
version = "tree"

[[trees."thoughts".trace]]
name = "tree_pat"
pattern = 'b(.)'
code_globs = ["lib/**/*.py"]

[trees."thoughts".hunt]
strict = false
""",
        )
        (repo / "thoughts").mkdir()
        cfg = load_config(repo / "thoughts")
        assert cfg.version == "tree"
        assert [t.name for t in cfg.traces] == ["tree_pat"]
        assert cfg.hunt.strict is False

    def test_unspecified_per_tree_fields_inherit_top_level(self, repo):
        _write(
            repo / ".croc.toml",
            """\
version = "top"

[[trace]]
name = "top_pat"
pattern = 'a(.)'
code_globs = ["src/**/*.py"]

[hunt]
strict = false

[trees."thoughts"]
# declares nothing — inherits every top-level default
""",
        )
        (repo / "thoughts").mkdir()
        cfg = load_config(repo / "thoughts")
        assert cfg.version == "top"
        assert [t.name for t in cfg.traces] == ["top_pat"]
        assert cfg.hunt.strict is False

    def test_unmatched_tree_uses_top_level(self, repo):
        _write(
            repo / ".croc.toml",
            'version = "top"\n\n[trees."other"]\nversion = "other"\n',
        )
        (repo / "thoughts").mkdir()
        assert load_config(repo / "thoughts").version == "top"

    def test_trees_not_table(self, repo):
        _write(repo / ".croc.toml", 'trees = "oops"\n')
        (repo / "thoughts").mkdir()
        with pytest.raises(ConfigError, match="`trees` must be a table of tables"):
            load_config(repo / "thoughts")

    def test_per_tree_trace_validated(self, repo):
        _write(
            repo / ".croc.toml",
            """\
[trees."thoughts"]
[[trees."thoughts".trace]]
name = "bad"
pattern = '(a)(b)'
code_globs = ["src/**/*.py"]
""",
        )
        (repo / "thoughts").mkdir()
        with pytest.raises(ConfigError, match=r'\[trees."thoughts"\].*exactly one capture group'):
            load_config(repo / "thoughts")
