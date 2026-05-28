"""Tests for display.render_report_github — GitHub Actions annotations."""

from __future__ import annotations

import textwrap
from pathlib import Path

from envsleuth.checker import check
from envsleuth.display import render_report_github
from envsleuth.scanner import scan_project


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


def test_special_chars_in_message_are_escaped(tmp_path: Path, monkeypatch) -> None:
    # GHA workflow commands need : and , percent-encoded
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "src/app.py", """
        import os
        os.getenv('VAR_WITH_X')
    """)
    env = write(tmp_path, ".env", "")
    scan = scan_project(Path("src"))
    report = check(scan, env)

    out = render_report_github(report)
    # the colon in "Missing env var: VAR..." must be encoded inside the message
    # the format itself has unescaped colons (::error file=...::msg)
    lines = [l for l in out.splitlines() if l.startswith("::error")]
    assert lines, "expected at least one ::error line"
    for line in lines:
        # split off the leading ::error and trailing ::message
        prefix, _, message = line.partition("::error ")
        _, _, msg = message.partition("::")
        # the message part should NOT contain literal : or ,
        assert ":" not in msg, f"unescaped : in message: {msg!r}"
        assert "," not in msg, f"unescaped , in message: {msg!r}"


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
