"""Terminal output with ANSI colors. No rich dep — just escape codes."""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path
from typing import List, Optional, TextIO

from envsleuth.checker import CheckReport


# ANSI escape codes. Keeping these as plain constants — no fancy Color class,
# it's 5 colors, not a design system.
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
CYAN = "\033[36m"
GRAY = "\033[90m"

MAX_USAGES_SHOWN = 5
_WINDOWS = os.name == "nt"

_ASCII_FALLBACKS = (
    ("⚠️", "[warning]"),
    ("✅", "[ok]"),
    ("❌", "[missing]"),
    ("ℹ", "[info]"),
    ("→", "->"),
    ("—", "-"),
    ("·", "-"),
    ("✓", "OK"),
)


def _display_field(value: object) -> str:
    """Make an untrusted value safe to place on one terminal line."""
    text = str(value)
    pieces: List[str] = []
    for char in text:
        if char == "\r":
            pieces.append("\\r")
        elif char == "\n":
            pieces.append("\\n")
        elif char == "\t":
            pieces.append("\\t")
        elif char.isprintable():
            pieces.append(char)
        else:
            pieces.append(ascii(char)[1:-1])
    return "".join(pieces)


def _gha_field(value: object) -> str:
    """Keep command delimiters escapable while neutralising terminal controls."""
    text = str(value)
    pieces: List[str] = []
    for char in text:
        if char in "\r\n" or char.isprintable():
            pieces.append(char)
        else:
            pieces.append(ascii(char)[1:-1])
    return "".join(pieces)


def stream_safe_text(text: str, stream: Optional[TextIO] = None) -> str:
    """Return text that can be written to a narrow Windows console."""
    target = stream or sys.stdout
    encoding = getattr(target, "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
        return text
    except (LookupError, UnicodeEncodeError):
        pass

    for symbol, replacement in _ASCII_FALLBACKS:
        text = text.replace(symbol, replacement)

    try:
        return text.encode(encoding, errors="replace").decode(encoding)
    except LookupError:
        return text.encode("ascii", errors="replace").decode("ascii")


def should_use_color(stream: Optional[TextIO] = None, force: Optional[bool] = None) -> bool:
    """Decide whether to emit ANSI codes.

    force=True/False overrides detection. NO_COLOR env var disables colors.
    See https://no-color.org
    """
    if force is not None:
        return force
    if os.environ.get("NO_COLOR"):
        return False
    s = stream or sys.stdout
    # not all streams have isatty (e.g. some test runners)
    try:
        return s.isatty()
    except Exception:
        return False


class Styler:
    """Wraps a bool — either returns text with ANSI codes or untouched."""

    def __init__(self, enabled: bool):
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        if not self.enabled:
            return text
        return f"{code}{text}{RESET}"

    def red(self, t: str) -> str:    return self._wrap(RED, t)
    def green(self, t: str) -> str:  return self._wrap(GREEN, t)
    def yellow(self, t: str) -> str: return self._wrap(YELLOW, t)
    def blue(self, t: str) -> str:   return self._wrap(BLUE, t)
    def cyan(self, t: str) -> str:   return self._wrap(CYAN, t)
    def gray(self, t: str) -> str:   return self._wrap(GRAY, t)
    def bold(self, t: str) -> str:   return self._wrap(BOLD, t)
    def dim(self, t: str) -> str:    return self._wrap(DIM, t)


# ---------------------------------------------------------------- text output


def render_report(report: CheckReport, use_color: bool = True, verbose: bool = False) -> str:
    """Build a human-readable report as a single string."""
    s = Styler(use_color)
    lines: List[str] = []

    # header
    total = len(report.variables)
    qualifier = " statically resolved" if report.dynamic_usages else ""
    lines.append(s.bold(
        f"Found {total}{qualifier} variable"
        f"{'s' if total != 1 else ''} in code"
    ))
    if report.env_file is not None:
        env_label = f"checking against {_display_field(report.env_file)}"
        if not report.env_file_exists:
            env_label += s.red("  (not found)")
        lines.append(s.dim(env_label))
    lines.append("")

    # per-var list
    for var in report.variables:
        name = _display_field(var.name)
        if var.status == "present":
            icon = s.green("✅")
            line = f"{icon} {name}"
            if verbose and var.usages:
                line += s.dim(f"  ({len(var.usages)} usage{'s' if len(var.usages) != 1 else ''})")
        elif var.status == "missing":
            icon = s.red("❌")
            line = (
                f"{icon} {s.bold(name)} {s.dim('— missing from')} "
                f"{_display_field(report.env_file)}"
            )
        elif var.status == "default":
            icon = s.yellow("⚠️ ")
            line = (
                f"{icon} {name} "
                f"{s.dim('— not in .env but has default in code (probably ok)')}"
            )
        elif var.status == "ignored":
            icon = s.gray("·")
            line = s.gray(f"{icon} {name}  (ignored by .envignore)")
        else:
            # shouldn't happen but don't crash
            line = f"? {name}"

        lines.append(line)

        # show where it's used when verbose, or always for missing (helpful context)
        if verbose or var.status == "missing":
            for u in var.usages[:MAX_USAGES_SHOWN]:
                try:
                    rel = u.file.relative_to(Path.cwd())
                except ValueError:
                    rel = u.file
                lines.append(s.dim(
                    f"     at {_display_field(rel)}:{u.line}"
                ))
            if len(var.usages) > MAX_USAGES_SHOWN:
                extra = len(var.usages) - MAX_USAGES_SHOWN
                lines.append(s.dim(f"     ... and {extra} more"))

    # dynamic warnings
    if report.dynamic_usages:
        lines.append("")
        lines.append(s.yellow(
            f"⚠️  {len(report.dynamic_usages)} dynamic usage"
            f"{'s' if len(report.dynamic_usages) != 1 else ''} "
            f"(variable name computed at runtime, can't check statically)"
        ))
        for u in report.dynamic_usages:
            try:
                rel = u.file.relative_to(Path.cwd())
            except ValueError:
                rel = u.file
            expr = _display_field(u.raw_expr or "?")
            call_type = _display_field(u.call_type)
            lines.append(s.dim(
                f"     {_display_field(rel)}:{u.line}  →  {call_type}({expr})"
            ))

    # extras in .env (not fatal, just informational)
    if report.extra_in_env:
        lines.append("")
        lines.append(s.dim(
            f"ℹ  {len(report.extra_in_env)} variable"
            f"{'s' if len(report.extra_in_env) != 1 else ''} "
            "in .env not referenced in code: "
            f"{', '.join(_display_field(name) for name in report.extra_in_env)}"
        ))

    # scan errors — these are real problems, surface them at the end
    if report.errors:
        lines.append("")
        lines.append(s.red(f"Scan errors ({len(report.errors)}):"))
        for path, msg in report.errors:
            try:
                rel = path.relative_to(Path.cwd())
            except ValueError:
                rel = path
            lines.append(s.red(
                f"  {_display_field(rel)}: {_display_field(msg)}"
            ))

    # summary footer
    lines.append("")
    summary = _render_summary(report, s)
    lines.append(summary)

    return "\n".join(lines)


def _render_summary(report: CheckReport, s: Styler) -> str:
    n_missing = len(report.missing)
    n_present = len(report.present)
    n_default = len(report.with_default)
    n_ignored = len(report.ignored)
    n_dynamic = len(report.dynamic_usages)
    n_errors = len(report.errors)

    parts = []
    if n_present:
        parts.append(s.green(f"{n_present} ok"))
    if n_default:
        parts.append(s.yellow(f"{n_default} with default"))
    if n_missing:
        parts.append(s.red(f"{n_missing} missing"))
    if n_ignored:
        parts.append(s.gray(f"{n_ignored} ignored"))
    if n_errors:
        parts.append(s.red(
            f"{n_errors} scan error{'s' if n_errors != 1 else ''}"
        ))

    if not parts:
        if n_dynamic:
            return s.yellow(
                "No statically resolved env vars; "
                f"{n_dynamic} dynamic lookup{'s' if n_dynamic != 1 else ''} skipped."
            )
        return s.dim("No env vars found in code.")

    line = "  ".join(parts)

    # only use the fancy box when colors are on. flashbar.panel() returns ANSI
    # codes regardless of stdout state (their TTY autodetect lives in print_panel,
    # not panel itself), so we'd otherwise leak escape codes into pipes and CI logs.
    if not s.enabled:
        return line

    try:
        from flashbar import panel
        # pick a color based on whether there are issues
        if n_missing or n_errors:
            color = "red"
        elif n_default:
            color = "yellow"
        else:
            color = "green"
        return panel(line, title="Summary", color=color)
    except Exception:
        # if flashbar is missing or broken somehow, fall back to plain line.
        # don't kill the whole report over fancy box drawing
        return line


# -------------------------------------------------------------------- errors


def render_env_not_found_error(
    env_path: Path,
    nearby: List[Path],
    use_color: bool = True,
) -> str:
    """Nice error message when the user's .env file doesn't exist."""
    s = Styler(use_color)
    lines = [
        s.red(s.bold(f"Error: {_display_field(env_path)} not found.")),
        "",
    ]

    # filter out the one we were looking for
    others = [p for p in nearby if p != env_path]

    if others:
        lines.append("Found other env-like files in this directory:")
        for p in others:
            lines.append(s.cyan(f"  {_display_field(p.name)}"))
        lines.append("")
        if _WINDOWS:
            # cmd.exe and PowerShell do not share one safe quoting syntax.
            lines.append("Try the nearby file by passing this path as --env:")
            lines.append(f"  {s.cyan(_display_field(others[0]))}")
        else:
            quoted_path = _display_field(shlex.quote(str(others[0])))
            lines.append(f"Try:  envsleuth scan --env {quoted_path}")
    else:
        lines.append(s.dim("No .env files found in this directory."))
        lines.append("")
        lines.append("If your .env lives elsewhere, use:")
        lines.append(f"  {s.cyan('envsleuth scan --env path/to/.env')}")

    return "\n".join(lines)


# ----------------------------------------------------------- github actions


def render_report_github(report: CheckReport) -> str:
    """Emit GitHub Actions workflow commands so missing vars show up as
    annotations in the PR/run UI, right next to the source line.

    Format docs: https://docs.github.com/en/actions/using-workflows/workflow-commands-for-github-actions
    """
    lines: List[str] = []

    if report.env_file is not None and not report.env_file_exists:
        lines.append(_gha_command(
            "error",
            f"Environment file not found: {report.env_file}",
            title="Environment file not found",
        ))

    for var in report.missing:
        # one annotation per usage so each occurrence gets pinned in the PR
        for u in var.usages:
            file_path = _gha_path(u.file)
            msg = (
                f"Missing env var: {var.name} is used here but not defined in "
                f"{report.env_file.name if report.env_file else '.env'}"
            )
            lines.append(_gha_command(
                "error", msg, file=file_path, line=u.line,
                title="Missing env var",
            ))

    # dynamic usages — warn, can't statically resolve so might be a bug
    for u in report.dynamic_usages:
        file_path = _gha_path(u.file)
        expr = u.raw_expr or "?"
        msg = (
            f"Dynamic env var lookup: {u.call_type}({expr}). "
            f"envsleuth can't check this statically."
        )
        lines.append(_gha_command(
            "warning", msg, file=file_path, line=u.line,
            title="Dynamic env var",
        ))

    # scan errors get their own annotation too
    for path, err in report.errors:
        file_path = _gha_path(path)
        lines.append(_gha_command(
            "error", f"Scan error: {err}", file=file_path,
            title="Scan error",
        ))

    # a final summary line so the run log isn't empty when everything is green
    n_missing = len(report.missing)
    n_dynamic = len(report.dynamic_usages)
    n_errors = len(report.errors)
    if n_missing == 0 and n_errors == 0 and report.env_file_exists:
        if n_dynamic:
            lines.append(_gha_command(
                "notice",
                f"envsleuth: all required vars defined "
                f"({n_dynamic} dynamic lookup{'s' if n_dynamic != 1 else ''} skipped)",
            ))
        else:
            lines.append(_gha_command(
                "notice", "envsleuth: all required env vars defined"
            ))

    return "\n".join(lines)


def _gha_path(p: Path) -> str:
    # GitHub Actions wants forward-slash paths relative to the repo root.
    # inside a workflow run, GITHUB_WORKSPACE points exactly to the checkout —
    # use it when available so paths line up with the PR diff even if the
    # scanner was launched from a subdirectory.
    workspace = os.environ.get("GITHUB_WORKSPACE")
    if workspace:
        try:
            return p.relative_to(workspace).as_posix()
        except ValueError:
            pass
    # local runs / non-GHA CI: fall back to cwd
    try:
        return p.relative_to(Path.cwd()).as_posix()
    except ValueError:
        return p.as_posix()


def _gha_escape_data(text: str) -> str:
    # Matches actions/toolkit's escapeData. Colons and commas are valid data.
    return (text.replace("%", "%25")
                .replace("\r", "%0D")
                .replace("\n", "%0A"))


def _gha_escape_property(text: str) -> str:
    return (_gha_escape_data(text)
            .replace(":", "%3A")
            .replace(",", "%2C"))


def _gha_command(command: str, message: str, **properties) -> str:
    props = ",".join(
        f"{key}={_gha_escape_property(_gha_field(value))}"
        for key, value in properties.items()
        if value is not None and value != ""
    )
    prefix = f"::{command}"
    if props:
        prefix += f" {props}"
    return f"{prefix}::{_gha_escape_data(_gha_field(message))}"


def render_error_github(
    message: str,
    path: Optional[Path] = None,
    title: str = "envsleuth error",
) -> str:
    """Render one operational failure as a GitHub Actions command."""
    properties = {"title": title}
    if path is not None:
        properties["file"] = _gha_path(path)
    return _gha_command("error", message, **properties)


# ------------------------------------------------------------------- json out


def render_report_json(report: CheckReport) -> str:
    """Machine-readable output for CI pipelines."""
    def _usage(u) -> dict:
        return {
            "file": str(u.file),
            "line": u.line,
            "call_type": u.call_type,
            "has_default": u.has_default,
        }

    data = {
        "env_file": str(report.env_file) if report.env_file else None,
        "env_file_exists": report.env_file_exists,
        "ignore_patterns": list(report.ignore_patterns),
        "summary": {
            "total": len(report.variables),
            "present": len(report.present),
            "missing": len(report.missing),
            "with_default": len(report.with_default),
            "ignored": len(report.ignored),
            "dynamic": len(report.dynamic_usages),
            "errors": len(report.errors),
        },
        "variables": [
            {
                "name": v.name,
                "status": v.status,
                "usages": [_usage(u) for u in v.usages],
            }
            for v in report.variables
        ],
        "dynamic_usages": [
            {
                "file": str(u.file),
                "line": u.line,
                "expression": u.raw_expr,
                "call_type": u.call_type,
            }
            for u in report.dynamic_usages
        ],
        "extra_in_env": report.extra_in_env,
        "errors": [{"file": str(p), "error": m} for p, m in report.errors],
    }
    return json.dumps(data, indent=2)


def render_error_json(message: str, path: Optional[Path] = None) -> str:
    """Render an operational failure without breaking JSON consumers."""
    error = {"message": message}
    if path is not None:
        error["file"] = str(path)
    return json.dumps({"error": error}, indent=2)
