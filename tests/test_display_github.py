"""Tests for display.render_report_github — GitHub Actions annotations."""

from __future__ import annotations

import textwrap
from pathlib import Path

from envsleuth.checker import CheckReport, VarReport, check
from envsleuth.display import (
    _gha_escape_data,
    _gha_escape_property,
    render_report_github,
)
from envsleuth.scanner import EnvUsage, scan_project


def write(tmp_path: Path, name: str, source: str) -> Path:
    f = tmp_path / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(textwrap.dedent(source), encoding="utf-8")
    return f


def test_emits_error_for_missing_var(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "src/app.py", """
        import os
        os.getenv('MISSING_VAR')
    """)
    env = write(tmp_path, ".env", "")
    scan = scan_project(Path("src"))
    report = check(scan, env)

    out = render_report_github(report)
    # one annotation per usage
    assert "::error" in out
    assert "MISSING_VAR" in out
    assert "src/app.py" in out
    assert "line=3" in out  # textwrap.dedent leaves a leading blank line
    assert "title=Missing env var" in out


def test_emits_warning_for_dynamic(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "src/app.py", """
        import os
        for k in ['A', 'B']:
            os.getenv(f"PREFIX_{k}")
    """)
    env = write(tmp_path, ".env", "")
    scan = scan_project(Path("src"))
    report = check(scan, env)

    out = render_report_github(report)
    assert "::warning" in out
    assert "Dynamic env var" in out


def test_notice_when_all_green(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "src/app.py", """
        import os
        os.getenv('A')
    """)
    env = write(tmp_path, ".env", "A=1\n")
    scan = scan_project(Path("src"))
    report = check(scan, env)

    out = render_report_github(report)
    assert "::notice" in out
    assert "all required env vars defined" in out


def test_message_uses_actions_toolkit_escape_data(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "src/app.py", """
        import os
        os.getenv('VAR_WITH_X')
    """)
    env = write(tmp_path, ".env", "")
    scan = scan_project(Path("src"))
    report = check(scan, env)

    out = render_report_github(report)
    lines = [l for l in out.splitlines() if l.startswith("::error")]
    assert lines, "expected at least one ::error line"
    for line in lines:
        msg = line.split("::", 2)[2]
        assert "Missing env var: VAR_WITH_X" in msg
        assert "%3A" not in msg
        assert "%2C" not in msg


def test_escape_data_and_property_match_actions_toolkit() -> None:
    raw = "a%b:c,d\r\ne"
    assert _gha_escape_data(raw) == "a%25b:c,d%0D%0Ae"
    assert _gha_escape_property(raw) == "a%25b%3Ac%2Cd%0D%0Ae"


def test_filename_property_cannot_inject_workflow_command() -> None:
    usage = EnvUsage(
        name="SECRET", file=Path("src/a,b%name.py\n::notice::owned"), line=7,
    )
    report = CheckReport(
        variables=[VarReport(
            name="SECRET", present_in_env=False,
            has_default_in_code=False, usages=[usage],
        )],
        env_file=Path(".env"),
        env_file_exists=True,
    )

    out = render_report_github(report)

    assert len(out.splitlines()) == 1
    assert "file=src/a%2Cb%25name.py%0A%3A%3Anotice%3A%3Aowned" in out
    assert "\n::notice::owned" not in out


def test_message_neutralises_terminal_control_sequences() -> None:
    name = "SECRET\x1b]8;;https://evil.test\x07CLICK"
    usage = EnvUsage(name=name, file=Path("src/app.py"), line=7)
    report = CheckReport(
        variables=[VarReport(
            name=name, present_in_env=False,
            has_default_in_code=False, usages=[usage],
        )],
        env_file=Path(".env"),
        env_file_exists=True,
    )

    out = render_report_github(report)

    assert "\x1b" not in out
    assert "\x07" not in out
    assert r"\x1b]8;;https://evil.test\x07CLICK" in out


def test_missing_env_file_is_an_error_not_green_notice() -> None:
    report = CheckReport(env_file=Path("missing.env"), env_file_exists=False)

    out = render_report_github(report)

    assert out.startswith("::error")
    assert "Environment file not found" in out
    assert "all required env vars defined" not in out


def test_paths_are_posix(tmp_path: Path, monkeypatch) -> None:
    # GHA wants forward slashes even on Windows
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "deep/nested/path/app.py", """
        import os
        os.getenv('X')
    """)
    env = write(tmp_path, ".env", "")
    scan = scan_project(Path("deep"))
    report = check(scan, env)

    out = render_report_github(report)
    assert "deep/nested/path/app.py" in out
    assert "\\" not in out  # no windows backslashes


def test_uses_github_workspace_when_available(tmp_path: Path, monkeypatch) -> None:
    # inside a real GHA run, $GITHUB_WORKSPACE = checkout root. honoring it lets
    # paths line up with the PR diff even if envsleuth is run from a subdirectory.
    write(tmp_path, "src/app.py", """
        import os
        os.getenv('X')
    """)
    env = write(tmp_path, ".env", "")
    # simulate being invoked from a subdir while GITHUB_WORKSPACE points at the root
    subdir = tmp_path / "src"
    monkeypatch.chdir(subdir)
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))

    scan = scan_project(subdir)
    report = check(scan, env)
    out = render_report_github(report)

    # path should be relative to workspace, not subdir
    assert "src/app.py" in out


# small note for myself: scan errors get their own annotation too. covered
# implicitly when MAX_FILE_SIZE skip kicks in but no dedicated test yet
# def test_scan_errors_get_annotated():
#     pass
