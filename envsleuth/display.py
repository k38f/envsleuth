"""Terminal output with ANSI colors. No rich dep — just escape codes."""

from __future__ import annotations

import json
import os
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
    lines.append(s.bold(f"Found {total} variable{'s' if total != 1 else ''} in code"))
    if report.env_file is not None:
        env_label = f"checking against {report.env_file}"
        if not report.env_file_exists:
            env_label += s.red("  (not found)")
        lines.append(s.dim(env_label))
    lines.append("")

    # per-var list
    for var in report.variables:
        if var.status == "present":
            icon = s.green("✅")
            line = f"{icon} {var.name}"
            if verbose and var.usages:
                line += s.dim(f"  ({len(var.usages)} usage{'s' if len(var.usages) != 1 else ''})")
        elif var.status == "missing":
            icon = s.red("❌")
            line = f"{icon} {s.bold(var.name)} {s.dim('— missing from')} {report.env_file}"
        elif var.status == "default":
            icon = s.yellow("⚠️ ")
            line = (
                f"{icon} {var.name} "
                f"{s.dim('— not in .env but has default in code (probably ok)')}"
            )
        elif var.status == "ignored":
            icon = s.gray("·")
            line = s.gray(f"{icon} {var.name}  (ignored by .envignore)")
        else:
            # shouldn't happen but don't crash
            line = f"? {var.name}"

        lines.append(line)

        # show where it's used when verbose, or always for missing (helpful context)
        if verbose or var.status == "missing":
            for u in var.usages[:MAX_USAGES_SHOWN]:
                try:
                    rel = u.file.relative_to(Path.cwd())
                except ValueError:
                    rel = u.file
                lines.append(s.dim(f"     at {rel}:{u.line}"))
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
            expr = u.raw_expr or "?"
            lines.append(s.dim(f"     {rel}:{u.line}  →  {u.call_type}({expr})"))

    # extras in .env (not fatal, just informational)
    if report.extra_in_env:
        lines.append("")
        lines.append(s.dim(
            f"ℹ  {len(report.extra_in_env)} variable"
            f"{'s' if len(report.extra_in_env) != 1 else ''} "
            f"in .env not referenced in code: {', '.join(report.extra_in_env)}"
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
            lines.append(s.red(f"  {rel}: {msg}"))

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

    parts = []
    if n_present:
        parts.append(s.green(f"{n_present} ok"))
    if n_default:
        parts.append(s.yellow(f"{n_default} with default"))
    if n_missing:
        parts.append(s.red(f"{n_missing} missing"))
    if n_ignored:
        parts.append(s.gray(f"{n_ignored} ignored"))

    if not parts:
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
        if n_missing:
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
        s.red(s.bold(f"Error: {env_path} not found.")),
        "",
    ]

    # filter out the one we were looking for
    others = [p for p in nearby if p != env_path]

    if others:
        lines.append("Found other env-like files in this directory:")
        for p in others:
            lines.append(s.cyan(f"  {p.name}"))
        lines.append("")
        lines.append(
            f"Try:  envsleuth scan --env {others[0].name}"
        )
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

    for var in report.missing:
        # one annotation per usage so each occurrence gets pinned in the PR
        for u in var.usages:
            file_path = _gha_path(u.file)
            msg = _gha_escape(
                f"Missing env var: {var.name} is used here but not defined in "
                f"{report.env_file.name if report.env_file else '.env'}"
            )
            lines.append(
                f"::error file={file_path},line={u.line},"
                f"title=Missing env var::{msg}"
            )

    # dynamic usages — warn, can't statically resolve so might be a bug
    for u in report.dynamic_usages:
        file_path = _gha_path(u.file)
        expr = u.raw_expr or "?"
        msg = _gha_escape(
            f"Dynamic env var lookup: {u.call_type}({expr}). "
            f"envsleuth can't check this statically."
        )
        lines.append(
            f"::warning file={file_path},line={u.line},"
            f"title=Dynamic env var::{msg}"
        )

    # scan errors get their own annotation too
    for path, err in report.errors:
        file_path = _gha_path(path)
        msg = _gha_escape(f"Scan error: {err}")
        lines.append(f"::error file={file_path},title=Scan error::{msg}")

    # a final summary line so the run log isn't empty when everything is green
    n_missing = len(report.missing)
    n_dynamic = len(report.dynamic_usages)
    n_errors = len(report.errors)
    if n_missing == 0 and n_errors == 0:
        if n_dynamic:
            lines.append(f"::notice::envsleuth: all required vars defined "
                         f"({n_dynamic} dynamic lookup{'s' if n_dynamic != 1 else ''} skipped)")
        else:
            lines.append("::notice::envsleuth: all required env vars defined")

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


def _gha_escape(text: str) -> str:
    # workflow commands have a small set of chars that need percent-encoding
    # see "Sending values to the post action" in the GHA docs
    return (text.replace("%", "%25")
                .replace("\r", "%0D")
                .replace("\n", "%0A")
                .replace(":", "%3A")
                .replace(",", "%2C"))


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
