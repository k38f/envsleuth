"""Regression tests for terminal display behavior."""

from __future__ import annotations

import shlex
import textwrap
from pathlib import Path

import envsleuth.display as display_module
from envsleuth.checker import check
from envsleuth.display import (
    Styler,
    _render_summary,
    render_env_not_found_error,
    render_report,
    stream_safe_text,
)
from envsleuth.scanner import scan_project


def write(tmp_path: Path, name: str, source: str) -> Path:
    f = tmp_path / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(textwrap.dedent(source), encoding="utf-8")
    return f


def test_panel_strips_ansi_when_color_disabled(tmp_path: Path) -> None:
    # bug fix: flashbar.panel() returns ANSI escape codes regardless of TTY state.
    # we'd leak them into pipes / CI logs. now we skip panel when use_color is off.
    write(tmp_path, "a.py", """
        import os
        os.getenv('X')
    """)
    env = write(tmp_path, ".env", "X=1\n")
    scan = scan_project(tmp_path)
    rep = check(scan, env)

    plain = _render_summary(rep, Styler(enabled=False))
    assert "\x1b[" not in plain, f"summary contains ANSI codes: {plain!r}"


def test_panel_used_when_color_enabled(tmp_path: Path, monkeypatch) -> None:
    # but when colors ARE on, we still want the pretty box
    panel_call = {}

    def fake_panel(text, title, color):
        panel_call.update(text=text, title=title, color=color)
        return f"<panel>{text}</panel>"

    monkeypatch.setattr("flashbar.panel", fake_panel)
    write(tmp_path, "a.py", """
        import os
        os.getenv('X')
    """)
    env = write(tmp_path, ".env", "X=1\n")
    scan = scan_project(tmp_path)
    rep = check(scan, env)

    fancy = _render_summary(rep, Styler(enabled=True))
    assert fancy == f"<panel>{panel_call['text']}</panel>"
    assert "1 ok" in panel_call["text"]
    assert panel_call["title"] == "Summary"
    assert panel_call["color"] == "green"


def test_full_render_with_color_off_is_clean(tmp_path: Path) -> None:
    # end-to-end: render_report with use_color=False produces no ANSI
    write(tmp_path, "a.py", """
        import os
        os.getenv('PRESENT')
        os.getenv('MISSING')
    """)
    env = write(tmp_path, ".env", "PRESENT=1\n")
    scan = scan_project(tmp_path)
    rep = check(scan, env)

    out = render_report(rep, use_color=False)
    assert "\x1b[" not in out, f"render_report leaked ANSI: {out!r}"


def test_report_falls_back_for_narrow_windows_encoding(tmp_path: Path) -> None:
    write(tmp_path, "a.py", """
        import os
        os.getenv('MISSING')
    """)
    env = write(tmp_path, ".env", "")
    rep = check(scan_project(tmp_path), env)

    class Cp1251Stream:
        encoding = "cp1251"

    out = stream_safe_text(
        render_report(rep, use_color=False), Cp1251Stream(),
    )

    assert "[missing]" in out
    assert " - missing from" in out
    assert out.encode("cp1251").decode("cp1251") == out


def test_report_escapes_terminal_controls_from_source(tmp_path: Path) -> None:
    write(tmp_path, "a.py", r"""
        import os
        os.getenv("TOKEN\nFORGED\x1b]8;;https://evil.test\x07CLICK")
    """)
    env = write(tmp_path, ".env", "")
    rep = check(scan_project(tmp_path), env)

    out = render_report(rep, use_color=False)

    assert "\x1b" not in out
    assert "\x07" not in out
    assert r"TOKEN\nFORGED\x1b]8;;https://evil.test\x07CLICK" in out
    assert "\nFORGED" not in out


def test_summary_includes_scan_errors(tmp_path: Path) -> None:
    write(tmp_path, "ok.py", """
        import os
        os.getenv("TOKEN")
    """)
    write(tmp_path, "broken.py", "def nope(:\n")
    env = write(tmp_path, ".env", "TOKEN=value\n")
    rep = check(scan_project(tmp_path), env)

    out = render_report(rep, use_color=False)

    assert "1 ok" in out
    assert "1 scan error" in out
    assert out.rstrip().endswith("1 ok  1 scan error")


def test_dynamic_only_report_says_names_were_not_resolved(
    tmp_path: Path,
) -> None:
    write(tmp_path, "a.py", """
        import os
        os.getenv(name)
    """)
    env = write(tmp_path, ".env", "")
    rep = check(scan_project(tmp_path), env)

    out = render_report(rep, use_color=False)

    assert "Found 0 statically resolved variables in code" in out
    assert "No statically resolved env vars; 1 dynamic lookup skipped." in out
    assert "No env vars found in code." not in out


def test_posix_env_suggestion_keeps_path_and_quotes_it(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(display_module, "_WINDOWS", False)
    env_path = tmp_path / "config" / ".env"
    nearby = tmp_path / "config" / ".env.prod; echo owned"

    out = render_env_not_found_error(
        env_path, [nearby], use_color=False,
    )

    expected = f"envsleuth scan --env {shlex.quote(str(nearby))}"
    assert expected in out
    assert "envsleuth scan --env .env.prod; echo owned" not in out


def test_windows_env_suggestion_avoids_shell_specific_command(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(display_module, "_WINDOWS", True)
    env_path = tmp_path / "config" / ".env"
    nearby = tmp_path / "config" / ".env.prod & echo owned"

    out = render_env_not_found_error(
        env_path, [nearby], use_color=False,
    )

    assert "passing this path as --env" in out
    assert str(nearby) in out
    assert "envsleuth scan --env" not in out
