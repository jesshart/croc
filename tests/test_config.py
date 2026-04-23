"""Config loader tests — `.croc.toml` schema and validation."""

from __future__ import annotations

import pathlib

import pytest

from croc.config import ConfigError, CrocConfig, HuntConfig, load_config


def _write(path: pathlib.Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


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
