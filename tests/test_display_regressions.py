"""Regression tests for v0.2 display behavior."""

from __future__ import annotations

import textwrap
from pathlib import Path

from envsleuth.checker import check
from envsleuth.display import _render_summary, render_report, Styler
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


def test_panel_used_when_color_enabled(tmp_path: Path) -> None:
    # but when colors ARE on, we still want the pretty box
    write(tmp_path, "a.py", """
        import os
        os.getenv('X')
    """)
    env = write(tmp_path, ".env", "X=1\n")
    scan = scan_project(tmp_path)
    rep = check(scan, env)

    fancy = _render_summary(rep, Styler(enabled=True))
    # the box drawing chars should be present
    assert "╭" in fancy or "┌" in fancy, f"no box: {fancy!r}"


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
