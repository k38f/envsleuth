"""Tests for envsleuth.generator.

Coverage was thin here, only adding what's needed to lock in the relative-path
fix and a couple of obvious things while we're at it.
"""

from __future__ import annotations

import ast
import os
import shutil
import stat
import subprocess
import textwrap
from io import StringIO
from pathlib import Path

import pytest
from dotenv import dotenv_values

from envsleuth.generator import (
    EnvNameCollisionError,
    IncompleteScanError,
    InvalidEnvNameError,
    build_env_example,
    write_env_example,
)
from envsleuth.scanner import EnvUsage, ScanResult, scan_project


def write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    return path


def scan_with_default(value, name: str = "VALUE", file: Path = Path("app.py")) -> ScanResult:
    return ScanResult(usages=[
        EnvUsage(
            name=name,
            file=file,
            line=1,
            has_default=True,
            default_node=ast.Constant(value=value),
        )
    ])


def test_basic_output(tmp_path: Path) -> None:
    write(tmp_path / "a.py", """
        import os
        os.getenv("DATABASE_URL")
        os.getenv("PORT", "8000")
    """)
    scan = scan_project(tmp_path)
    out = build_env_example(scan)
    assert "DATABASE_URL=" in out
    assert "PORT=8000" in out


def test_paths_are_relative_to_cwd(tmp_path: Path, monkeypatch) -> None:
    # bug fix: locations in the generated file used to be absolute paths,
    # which leaked the author's home dir into committed .env.example files.
    write(tmp_path / "src" / "main.py", """
        import os
        os.getenv("DB")
    """)
    monkeypatch.chdir(tmp_path)
    scan = scan_project(Path("src"))
    out = build_env_example(scan)

    # should mention src/main.py, not /tmp/.../src/main.py
    assert "src/main.py" in out or "src\\main.py" in out  # windows path separators
    assert str(tmp_path) not in out


def test_paths_outside_cwd_do_not_leak_absolute_prefix(
    tmp_path: Path, monkeypatch
) -> None:
    cwd = tmp_path / "project"
    cwd.mkdir()
    source = tmp_path / "external" / "app.py"
    source.parent.mkdir()
    monkeypatch.chdir(cwd)
    usage = EnvUsage(name="TOKEN", file=source, line=4)

    out = build_env_example(ScanResult(usages=[usage]))

    assert str(tmp_path) not in out
    assert "app.py" in out


def test_empty_project(tmp_path: Path) -> None:
    scan = scan_project(tmp_path)
    out = build_env_example(scan)
    assert "No environment variables" in out


def test_write_env_example_creates_file(tmp_path: Path) -> None:
    write(tmp_path / "a.py", "import os\nos.getenv('X')\n")
    scan = scan_project(tmp_path)
    target = tmp_path / ".env.example"
    write_env_example(scan, target)
    assert target.exists()
    assert "X=" in target.read_text()


def test_write_env_example_refuses_overwrite(tmp_path: Path) -> None:
    write(tmp_path / "a.py", "import os\nos.getenv('X')\n")
    scan = scan_project(tmp_path)
    target = tmp_path / ".env.example"
    target.write_text("hand written\n")
    with pytest.raises(FileExistsError):
        write_env_example(scan, target)
    assert target.read_text() == "hand written\n"


def test_write_env_example_force_overwrites(tmp_path: Path) -> None:
    write(tmp_path / "a.py", "import os\nos.getenv('FORCED')\n")
    scan = scan_project(tmp_path)
    target = tmp_path / ".env.example"
    target.write_text("old content\n")
    write_env_example(scan, target, force=True)
    assert "FORCED=" in target.read_text()


def test_keyword_default_is_extracted(tmp_path: Path) -> None:
    # bug #2: previously generator only looked at positional args[1] for default,
    # so os.getenv("X", default="foo") wrote `X=` instead of `X=foo`.
    write(tmp_path / "a.py", """
        import os
        os.getenv("PORT", default="8000")
        os.getenv("HOST", default="localhost")
    """)
    scan = scan_project(tmp_path)
    out = build_env_example(scan)
    assert "PORT=8000" in out
    assert "HOST=localhost" in out


def test_value_with_hash_is_quoted(tmp_path: Path) -> None:
    # bug #10: bare `URL=https://x.com#anchor` — bash and JS dotenv parsers cut
    # at the '#'. wrap in quotes when value has dangerous chars.
    write(tmp_path / "a.py", """
        import os
        os.getenv("URL", "https://example.com/path#fragment")
    """)
    scan = scan_project(tmp_path)
    out = build_env_example(scan)
    # should be quoted. exact form: URL="https://example.com/path#fragment"
    line = next(l for l in out.splitlines() if l.startswith("URL="))
    assert line.startswith('URL="') and line.endswith('"')
    assert "#fragment" in line


def test_value_with_spaces_is_quoted(tmp_path: Path) -> None:
    write(tmp_path / "a.py", """
        import os
        os.getenv("MSG", "hello world")
    """)
    scan = scan_project(tmp_path)
    out = build_env_example(scan)
    line = next(l for l in out.splitlines() if l.startswith("MSG="))
    assert line == 'MSG="hello world"'


def test_simple_value_not_quoted(tmp_path: Path) -> None:
    # don't quote things that don't need it — keeps the file readable
    write(tmp_path / "a.py", """
        import os
        os.getenv("PORT", "8000")
        os.getenv("DEBUG", "false")
    """)
    scan = scan_project(tmp_path)
    out = build_env_example(scan)
    assert "PORT=8000" in out
    assert "DEBUG=false" in out
    # no quotes around them
    assert 'PORT="' not in out
    assert 'DEBUG="' not in out


@pytest.mark.parametrize(
    "value",
    [
        "hello world",
        "fragment#part; next&other|last",
        "both 'single' and \"double\" quotes",
        r"C:\server\\share\folder",
        "first\nsecond\nthird",
        "$HOME",
        "$(printf should-not-run)",
        "`printf should-not-run`",
    ],
)
def test_defaults_round_trip_through_python_dotenv(value: str) -> None:
    out = build_env_example(scan_with_default(value))

    parsed = dotenv_values(stream=StringIO(out))

    assert parsed["VALUE"] == value


@pytest.mark.parametrize(
    "value",
    [
        "${HOME}",
        "cost is '$5'",
        "ends with $ and a backslash\\",
        "nul\x00byte",
    ],
)
def test_unportable_defaults_are_omitted_without_leaking_value(
    value: str, monkeypatch
) -> None:
    monkeypatch.setenv("HOME", "expanded-home")

    out = build_env_example(scan_with_default(value))
    parsed = dotenv_values(stream=StringIO(out))

    assert parsed["VALUE"] == ""
    assert "default omitted" in out
    assert value not in out


def test_none_default_is_not_treated_as_a_usable_example() -> None:
    out = build_env_example(scan_with_default(None))

    assert "VALUE=\n" in out
    assert "default omitted" not in out


def test_default_selection_is_stable_by_source_location() -> None:
    early = scan_with_default("alpha", file=Path("a.py")).usages[0]
    late = scan_with_default("zulu", file=Path("z.py")).usages[0]

    out1 = build_env_example(ScanResult(usages=[late, early]))
    out2 = build_env_example(ScanResult(usages=[early, late]))

    assert "VALUE=alpha" in out1
    assert out1 == out2


def test_default_selection_skips_unportable_literal() -> None:
    unsafe = scan_with_default("${HOME}", file=Path("a.py")).usages[0]
    fallback = scan_with_default("local", file=Path("b.py")).usages[0]

    out = build_env_example(ScanResult(usages=[unsafe, fallback]))

    assert "VALUE=local" in out
    assert "default omitted" not in out


@pytest.mark.parametrize(
    "name",
    ["", "1STARTS_WITH_NUMBER", "BAD=NAME", "BAD NAME", "BAD\nNAME", "ÜNICODE"],
)
def test_invalid_env_names_fail_generation(name: str) -> None:
    scan = scan_with_default("x", name=name)

    with pytest.raises(InvalidEnvNameError) as exc_info:
        build_env_example(scan)

    assert name in exc_info.value.names


def test_case_colliding_names_are_rejected_on_windows(monkeypatch) -> None:
    upper = scan_with_default("one", name="FOO").usages[0]
    lower = scan_with_default("two", name="foo").usages[0]
    monkeypatch.setattr(
        "envsleuth.generator._env_names_case_sensitive", lambda: False,
    )

    with pytest.raises(EnvNameCollisionError) as exc_info:
        build_env_example(ScanResult(usages=[upper, lower]))

    assert exc_info.value.groups == [["FOO", "foo"]]


def test_case_collision_message_order_is_stable(monkeypatch) -> None:
    usages = [
        scan_with_default("x", name=name).usages[0]
        for name in ["foo", "BAZ", "bar", "FOO", "baz", "BAR"]
    ]
    monkeypatch.setattr(
        "envsleuth.generator._env_names_case_sensitive", lambda: False,
    )

    with pytest.raises(EnvNameCollisionError) as exc_info:
        build_env_example(ScanResult(usages=usages))

    assert exc_info.value.groups == [
        ["BAR", "bar"], ["BAZ", "baz"], ["FOO", "foo"],
    ]


def test_scan_errors_fail_before_force_overwrite(tmp_path: Path) -> None:
    target = tmp_path / ".env.example"
    target.write_text("keep me\n", encoding="utf-8")
    scan = ScanResult(errors=[(tmp_path / "broken.py", "syntax error")])

    with pytest.raises(IncompleteScanError) as exc_info:
        write_env_example(scan, target, force=True)

    assert len(exc_info.value.errors) == 1
    assert target.read_text(encoding="utf-8") == "keep me\n"


def test_invalid_name_fails_before_force_overwrite(tmp_path: Path) -> None:
    target = tmp_path / ".env.example"
    target.write_text("keep me\n", encoding="utf-8")

    with pytest.raises(InvalidEnvNameError):
        write_env_example(scan_with_default("x", name="BAD NAME"), target, force=True)

    assert target.read_text(encoding="utf-8") == "keep me\n"


def test_dynamic_only_scan_has_an_explicit_warning(tmp_path: Path) -> None:
    write(tmp_path / "a.py", "import os\nname = 'X'\nos.getenv(name)\n")

    out = build_env_example(scan_project(tmp_path))

    assert "WARNING: 1 dynamic environment variable usage" in out
    assert "No statically resolved environment variables" in out
    assert "No environment variables found" not in out


def test_dynamic_warning_sanitizes_paths_and_expressions() -> None:
    usage = EnvUsage(
        name=None,
        file=Path("bad\nINJECTED=1.py"),
        line=7,
        raw_expr="name\nEVIL=1",
    )

    out = build_env_example(ScanResult(usages=[usage]))

    assert "\nINJECTED=1.py" not in out
    assert "\nEVIL=1" not in out
    assert "\\nINJECTED=1.py" in out
    assert "name\\nEVIL=1" in out


def test_write_uses_lf_for_shell_compatible_output(tmp_path: Path) -> None:
    target = tmp_path / ".env.example"

    write_env_example(scan_with_default("hello world"), target)

    assert b"\r\n" not in target.read_bytes()


def test_carriage_return_default_is_omitted(tmp_path: Path) -> None:
    target = tmp_path / ".env.example"

    write_env_example(scan_with_default("before\rafter"), target)

    content = target.read_text(encoding="utf-8")
    assert "default omitted" in content
    assert dotenv_values(target)["VALUE"] == ""


def test_lone_surrogate_default_is_omitted(tmp_path: Path) -> None:
    target = tmp_path / ".env.example"

    write_env_example(scan_with_default("bad\ud800value"), target)

    content = target.read_text(encoding="utf-8")
    assert "default omitted" in content
    assert "\ud800" not in content
    assert dotenv_values(target)["VALUE"] == ""


def test_trailing_backslash_cannot_consume_the_next_assignment() -> None:
    first = scan_with_default("ends\\", name="A").usages[0]
    second = scan_with_default("hello world", name="B").usages[0]

    out = build_env_example(ScanResult(usages=[first, second]))
    parsed = dotenv_values(stream=StringIO(out))

    assert parsed == {"A": "", "B": "hello world"}
    assert "default omitted" in out


def test_terminal_control_default_is_omitted_without_leaking_it() -> None:
    value = "safe\x1b[31mRED\x7f"

    out = build_env_example(scan_with_default(value))

    assert "\x1b" not in out
    assert "\x7f" not in out
    assert "default omitted" in out
    assert dotenv_values(stream=StringIO(out))["VALUE"] == ""


def test_replace_failure_keeps_existing_output(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / ".env.example"
    target.write_text("keep me\n", encoding="utf-8")

    def fail_replace(source, destination):
        raise OSError("replace failed")

    monkeypatch.setattr("envsleuth.generator.os.replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        write_env_example(scan_with_default("new value"), target, force=True)

    assert target.read_text(encoding="utf-8") == "keep me\n"
    assert list(tmp_path.glob(".envsleuth-*.tmp")) == []


def test_force_overwrite_preserves_existing_mode(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("POSIX permission bits are not available")
    target = tmp_path / ".env.example"
    target.write_text("old\n", encoding="utf-8")
    target.chmod(0o640)

    write_env_example(scan_with_default("new"), target, force=True)

    assert target.stat().st_mode & 0o777 == 0o640


def test_force_sets_private_mode_before_writing(
    tmp_path: Path, monkeypatch,
) -> None:
    if os.name == "nt" or not hasattr(os, "fchmod"):
        pytest.skip("descriptor chmod is not available")
    target = tmp_path / ".env.example"
    target.write_text("old\n", encoding="utf-8")
    target.chmod(0o600)
    real_open = Path.open
    modes_during_write = []

    class ObservedStream:
        def __init__(self, wrapped) -> None:
            self.wrapped = wrapped

        def __enter__(self):
            self.wrapped.__enter__()
            return self

        def __exit__(self, *args):
            return self.wrapped.__exit__(*args)

        def fileno(self):
            return self.wrapped.fileno()

        def flush(self):
            return self.wrapped.flush()

        def write(self, value):
            mode = stat.S_IMODE(os.fstat(self.fileno()).st_mode)
            modes_during_write.append(mode)
            return self.wrapped.write(value)

    def observed_open(path, *args, **kwargs):
        opened = real_open(path, *args, **kwargs)
        if path.name.startswith(".envsleuth-"):
            return ObservedStream(opened)
        return opened

    monkeypatch.setattr(Path, "open", observed_open)

    write_env_example(scan_with_default("private"), target, force=True)

    assert modes_during_write == [0o600]
    assert target.stat().st_mode & 0o777 == 0o600


def test_non_force_creation_does_not_use_replace(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / ".env.example"

    def should_not_replace(source, destination):
        raise AssertionError("non-force creation must use an exclusive open")

    monkeypatch.setattr("envsleuth.generator.os.replace", should_not_replace)

    write_env_example(scan_with_default("safe"), target)
    assert dotenv_values(target)["VALUE"] == "safe"


def test_cleanup_does_not_delete_replaced_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from envsleuth.generator import _unlink_if_same

    target = tmp_path / ".env.example"
    target.write_text("created\n", encoding="utf-8")
    created_stat = target.lstat()
    target.unlink()
    target.write_text("someone else's file\n", encoding="utf-8")

    # Make inode reuse deterministic on filesystems that normally allocate a
    # different one for the replacement.
    monkeypatch.setattr(
        "envsleuth.generator.os.path.samestat",
        lambda expected, current: True,
    )

    _unlink_if_same(target, created_stat)

    assert target.read_text(encoding="utf-8") == "someone else's file\n"


def test_cleanup_deletes_unchanged_created_path(tmp_path: Path) -> None:
    from envsleuth.generator import _unlink_if_same

    target = tmp_path / ".env.example"
    target.write_text("created\n", encoding="utf-8")

    _unlink_if_same(target, target.lstat())

    assert not target.exists()


def test_force_replaces_dangling_symlink(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("symlink creation usually needs extra Windows privileges")
    target = tmp_path / ".env.example"
    target.symlink_to(tmp_path / "missing-target")

    write_env_example(scan_with_default("safe"), target, force=True)

    assert target.is_symlink() is False
    assert dotenv_values(target)["VALUE"] == "safe"


def test_force_handles_long_valid_target_name(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("Windows path length policy varies by host")
    target = tmp_path / ("x" * 245)
    target.write_text("old\n", encoding="utf-8")

    write_env_example(scan_with_default("new"), target, force=True)

    assert dotenv_values(target)["VALUE"] == "new"


def test_huge_integer_default_does_not_crash_generation() -> None:
    node = ast.parse("0x" + "f" * 15000, mode="eval").body
    usage = EnvUsage(
        name="HUGE",
        file=Path("app.py"),
        line=1,
        has_default=True,
        default_node=node,
    )

    out = build_env_example(ScanResult(usages=[usage]))

    assert "HUGE=\n" in out
    assert "default omitted" in out


@pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX shell is unavailable")
def test_shell_source_does_not_execute_default_commands(tmp_path: Path) -> None:
    marker1 = tmp_path / "command-substitution-ran"
    marker2 = tmp_path / "backtick-ran"
    value = f'$(touch {marker1}); `touch {marker2}` & | $HOME "quoted"'
    target = tmp_path / ".env.example"
    write_env_example(scan_with_default(value), target)
    env = os.environ.copy()
    env["HOME"] = "expanded-home"

    result = subprocess.run(
        [shutil.which("sh"), "-c", '. "$1"\nprintf "%s" "$VALUE"', "sh", str(target)],
        check=True,
        capture_output=True,
        env=env,
    )

    assert result.stdout == value.encode()
    assert marker1.exists() is False
    assert marker2.exists() is False


# leftover from when I was thinking about ast.Name resolution — punted
# def test_default_from_module_constant(tmp_path):
#     # SOMETHING = "default-value"
#     # os.getenv("X", SOMETHING)  -> should it pull "default-value"?
#     pass
