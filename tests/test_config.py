"""Tests for pyproject.toml configuration loading."""

from __future__ import annotations

from pathlib import Path

import pytest

import envsleuth.config as config_module
from envsleuth.config import (
    MAX_CONFIG_BYTES,
    ConfigError,
    discover_pyproject,
    load_config,
)


def write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_loads_and_resolves_valid_config_relative_to_pyproject(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    nested = project / "src" / "pkg"
    nested.mkdir(parents=True)
    config_file = write(
        project / "pyproject.toml",
        """
[tool.envsleuth]
path = "src"
env = [".env", "profiles/dev.env", ".env"]
envignore = "config/.envignore"
strict = false
fail-on = ["extra", "missing", "extra"]
exclude = ["vendor", "Build", "build"]
extensions = ["pyi", ".PYX", ".pyi"]
""",
    )

    config = load_config(start=nested)

    assert config.active is True
    assert config.source == config_file
    assert config.path == (project / "src").resolve()
    assert config.env_files == (
        (project / ".env").resolve(),
        (project / "profiles" / "dev.env").resolve(),
    )
    assert config.envignore == (project / "config" / ".envignore").resolve()
    assert config.strict is False
    assert config.fail_on == ("extra", "missing")
    assert config.exclude == ("vendor", "Build")
    assert config.extensions == (".pyi", ".PYX")


def test_env_may_be_a_single_string(tmp_path: Path) -> None:
    config_file = write(
        tmp_path / "custom.toml",
        '[tool.envsleuth]\nenv = "settings/test.env"\n',
    )

    config = load_config(config_path=config_file)

    assert config.env_files == ((tmp_path / "settings" / "test.env").resolve(),)


def test_nearest_pyproject_stops_search_even_without_table(
    tmp_path: Path,
) -> None:
    write(
        tmp_path / "pyproject.toml",
        '[tool.envsleuth]\npath = "parent-src"\n',
    )
    child = tmp_path / "child"
    nested = child / "nested"
    nested.mkdir(parents=True)
    nearest = write(child / "pyproject.toml", "[project]\nname = 'child'\n")

    config = load_config(start=nested)

    assert config.active is False
    assert config.source == nearest
    assert config.path is None


def test_discovery_accepts_a_file_start_path(tmp_path: Path) -> None:
    config = write(tmp_path / "pyproject.toml", "[project]\nname = 'demo'\n")
    source = write(tmp_path / "src" / "app.py", "value = 1\n")

    assert discover_pyproject(source) == config


def test_auto_discovery_without_pyproject_is_inactive(
    tmp_path: Path,
) -> None:
    config = load_config(start=tmp_path)

    assert config.active is False
    assert config.source is None


def test_explicit_config_requires_envsleuth_table(tmp_path: Path) -> None:
    path = write(tmp_path / "settings.toml", "[project]\nname = 'demo'\n")

    with pytest.raises(ConfigError, match=r"missing required \[tool\.envsleuth\]"):
        load_config(config_path=path)


def test_explicit_config_must_exist(tmp_path: Path) -> None:
    missing = tmp_path / "missing.toml"

    with pytest.raises(ConfigError, match="does not exist") as error:
        load_config(config_path=missing)

    assert error.value.path == missing.resolve()


def test_no_config_returns_inactive_without_reading_files(
    tmp_path: Path,
) -> None:
    write(tmp_path / "pyproject.toml", "[tool.envsleuth\n")

    config = load_config(start=tmp_path, disabled=True)

    assert config.active is False
    assert config.source is None


def test_no_config_and_explicit_config_are_mutually_exclusive(
    tmp_path: Path,
) -> None:
    with pytest.raises(ConfigError, match="--no-config"):
        load_config(config_path=tmp_path / "config.toml", disabled=True)


def test_auto_discovered_paths_cannot_escape_project(tmp_path: Path) -> None:
    project = tmp_path / "project"
    nested = project / "src"
    nested.mkdir(parents=True)
    write(
        project / "pyproject.toml",
        "[tool.envsleuth]\nenvignore = '../outside.txt'\n",
    )

    with pytest.raises(ConfigError, match="must stay inside"):
        load_config(start=nested)


def test_explicit_config_can_opt_in_to_external_paths(tmp_path: Path) -> None:
    project = tmp_path / "project"
    path = write(
        project / "settings.toml",
        "[tool.envsleuth]\nenv = '../shared/.env'\n",
    )

    config = load_config(config_path=path)

    assert config.env_files == ((tmp_path / "shared" / ".env").resolve(),)


@pytest.mark.parametrize(
    "content, message",
    [
        ("[tool.envsleuth]\nunknown = true\n", "unknown"),
        ("[tool]\nenvsleuth = true\n", "must be a table"),
        ("[tool.envsleuth]\npath = 1\n", "'path' must be a string"),
        ("[tool.envsleuth]\nenv = []\n", "must not be an empty list"),
        ("[tool.envsleuth]\nenv = [1]\n", r"'env\[0\]' must be a string"),
        ("[tool.envsleuth]\nenvignore = false\n", "must be a string"),
        ("[tool.envsleuth]\nstrict = 1\n", "must be a boolean"),
        ("[tool.envsleuth]\nfail-on = 'missing'\n", "must be a list"),
        ("[tool.envsleuth]\nfail-on = ['other']\n", "is invalid"),
        ("[tool.envsleuth]\nexclude = 'vendor'\n", "must be a list"),
        ("[tool.envsleuth]\nexclude = ['']\n", "must not be empty"),
        ("[tool.envsleuth]\nextensions = '.pyi'\n", "must be a list"),
    ],
)
def test_rejects_unknown_keys_and_wrong_types(
    tmp_path: Path, content: str, message: str,
) -> None:
    path = write(tmp_path / "pyproject.toml", content)

    with pytest.raises(ConfigError, match=message):
        load_config(config_path=path)


def test_nested_envsleuth_table_is_rejected_as_unknown_key(
    tmp_path: Path,
) -> None:
    path = write(
        tmp_path / "pyproject.toml",
        "[tool.envsleuth.output]\nformat = 'json'\n",
    )

    with pytest.raises(ConfigError, match="unknown") as error:
        load_config(config_path=path)

    assert "'output'" in str(error.value)


def test_rejects_malformed_toml(tmp_path: Path) -> None:
    path = write(tmp_path / "pyproject.toml", "[tool.envsleuth\n")

    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(config_path=path)


def test_parser_resource_failure_becomes_config_error(
    tmp_path: Path, monkeypatch,
) -> None:
    path = write(tmp_path / "pyproject.toml", "[tool.envsleuth]\n")

    def too_deep(_text: str):
        raise RecursionError("nested too deeply")

    monkeypatch.setattr(config_module.tomli, "loads", too_deep)

    with pytest.raises(ConfigError, match="too complex"):
        load_config(config_path=path)


def test_actual_deep_toml_does_not_escape_as_recursion_error(
    tmp_path: Path,
) -> None:
    nesting = 2000
    path = write(
        tmp_path / "pyproject.toml",
        "[tool.envsleuth]\nexclude = "
        + "[" * nesting
        + "'value'"
        + "]" * nesting
        + "\n",
    )

    with pytest.raises(ConfigError):
        load_config(config_path=path)


def test_rejects_non_utf8_toml(tmp_path: Path) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_bytes(b"[tool.envsleuth]\n# \xff\n")

    with pytest.raises(ConfigError, match="not valid UTF-8"):
        load_config(config_path=path)


@pytest.mark.parametrize(
    "bad", ["\x00", "\x01", "\x7f", "\u0085", "\u202e"],
)
def test_rejects_control_characters_in_file(
    tmp_path: Path, bad: str,
) -> None:
    path = write(
        tmp_path / "pyproject.toml",
        f"[tool.envsleuth]\n# bad{bad}comment\n",
    )

    with pytest.raises(ConfigError, match="control character"):
        load_config(config_path=path)


def test_rejects_escaped_control_character_in_value(tmp_path: Path) -> None:
    path = write(
        tmp_path / "pyproject.toml",
        r'[tool.envsleuth]' "\n" r'path = "src\u007fdir"' "\n",
    )

    with pytest.raises(ConfigError, match="control character"):
        load_config(config_path=path)


def test_rejects_oversized_config_before_parsing(tmp_path: Path) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_bytes(b"#" * (MAX_CONFIG_BYTES + 1))

    with pytest.raises(ConfigError, match="exceeds"):
        load_config(config_path=path)


def test_rejects_non_regular_config_path(tmp_path: Path) -> None:
    directory = tmp_path / "pyproject.toml"
    directory.mkdir()

    with pytest.raises(ConfigError, match="regular file"):
        load_config(config_path=directory)


def test_rejects_more_than_64_env_files(tmp_path: Path) -> None:
    entries = ", ".join(f"'env-{index}'" for index in range(65))
    path = write(
        tmp_path / "pyproject.toml",
        f"[tool.envsleuth]\nenv = [{entries}]\n",
    )

    with pytest.raises(ConfigError, match="more than 64"):
        load_config(config_path=path)


@pytest.mark.parametrize(
    "extension",
    ["", ".", "..py", ".tar.gz", "dir/file", r"dir\file", " py", "py ", "C:py"],
)
def test_rejects_weird_extensions(
    tmp_path: Path, extension: str,
) -> None:
    path = write(
        tmp_path / "pyproject.toml",
        f"[tool.envsleuth]\nextensions = [{extension!r}]\n",
    )

    with pytest.raises(ConfigError, match="extension"):
        load_config(config_path=path)


def test_config_error_includes_source_path(tmp_path: Path) -> None:
    path = write(tmp_path / "pyproject.toml", "[tool.envsleuth]\nstrict = 1\n")

    with pytest.raises(ConfigError) as error:
        load_config(config_path=path)

    assert str(path) in str(error.value)
    assert error.value.path == path.resolve()
