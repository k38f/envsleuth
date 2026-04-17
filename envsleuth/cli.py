"""CLI entry point for envsleuth."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional, Set

import click

from envsleuth import __version__
from envsleuth.checker import (
    DEFAULT_ENV_FILE,
    DEFAULT_ENVIGNORE_FILE,
    check,
    find_nearby_env_files,
    load_ignore_patterns,
)
from envsleuth.display import (
    render_env_not_found_error,
    render_report,
    render_report_json,
    should_use_color,
)
from envsleuth.generator import write_env_example
from envsleuth.scanner import scan_project


# show flashbar only if the project is big enough that a blink of progress is useful
PROGRESS_THRESHOLD = 20


@click.group()
@click.version_option(version=__version__, prog_name="envsleuth")
def cli() -> None:
    """envsleuth — find env vars in your code and check them against .env."""
    pass


# --------------------------------------------------------------------- scan

@cli.command()
@click.option(
    "--path", "-p",
    type=click.Path(exists=True, file_okay=True, dir_okay=True, path_type=Path),
    default=".",
    help="Directory (or file) to scan. Defaults to current directory.",
)
@click.option(
    "--env", "env_file",
    type=click.Path(path_type=Path),
    default=None,
    help=f"Path to the .env file. Defaults to ./{DEFAULT_ENV_FILE}",
)
@click.option(
    "--envignore",
    type=click.Path(path_type=Path),
    default=None,
    help=f"Path to .envignore. Defaults to ./{DEFAULT_ENVIGNORE_FILE} if present.",
)
@click.option(
    "--strict", is_flag=True,
    help="Exit with code 1 if any vars are missing (for CI).",
)
@click.option(
    "--json", "as_json", is_flag=True,
    help="Emit machine-readable JSON instead of the human report.",
)
@click.option(
    "--no-color", is_flag=True,
    help="Disable ANSI colors. Also set NO_COLOR=1 in your env.",
)
@click.option(
    "--exclude", multiple=True,
    help="Extra directory name to skip. Can be repeated.",
)
@click.option(
    "--ext", multiple=True,
    help="Extra file extension to scan (e.g. .pyi). Can be repeated.",
)
@click.option(
    "--verbose", "-v", is_flag=True,
    help="Show usage locations for every variable, not just missing ones.",
)
def scan(
    path: Path,
    env_file: Optional[Path],
    envignore: Optional[Path],
    strict: bool,
    as_json: bool,
    no_color: bool,
    exclude: tuple,
    ext: tuple,
    verbose: bool,
) -> None:
    """Scan a project for env var usages and check against .env."""

    # resolve paths up front so error messages look sensible
    root = path.resolve()
    if env_file is None:
        env_file = Path.cwd() / DEFAULT_ENV_FILE
    env_file = env_file.resolve() if env_file.exists() else env_file

    if envignore is None:
        candidate = Path.cwd() / DEFAULT_ENVIGNORE_FILE
        envignore = candidate if candidate.exists() else None

    use_color = should_use_color(force=False if no_color else None)
    # --json means no color regardless
    if as_json:
        use_color = False

    # env file missing — bail out with a helpful message (unless JSON mode where
    # we still want to emit a report for CI to consume)
    if not env_file.exists() and not as_json:
        # look for .env* files in the same directory the user pointed at
        search_root = env_file.parent if env_file.parent.exists() else Path.cwd()
        nearby = find_nearby_env_files(search_root)
        click.echo(render_env_not_found_error(env_file, nearby, use_color=use_color), err=True)
        sys.exit(2)

    # build the sets for scan_project
    exts: Optional[Set[str]] = None
    if ext:
        # include default .py plus the user-supplied ones, normalise the dot
        exts = {".py"} | {e if e.startswith(".") else f".{e}" for e in ext}
    extra_excl: Optional[Set[str]] = set(exclude) if exclude else None

    # flashbar progress, but only if the project is big enough to justify it
    files_preview = _count_files(root, exts, extra_excl)
    use_progress = (not as_json) and files_preview >= PROGRESS_THRESHOLD and sys.stdout.isatty()

    if use_progress:
        from flashbar import Bar  # imported lazily so --json path stays lean
        bar = Bar(files_preview, label="Scanning", show_eta=True, show_speed=True)
        def _tick(_f: Path) -> None:
            bar.update()
        result = scan_project(root, extensions=exts, extra_excludes=extra_excl, on_file=_tick)
    else:
        result = scan_project(root, extensions=exts, extra_excludes=extra_excl)

    # .envignore patterns
    patterns = load_ignore_patterns(envignore) if envignore else []

    report = check(result, env_file, ignore_patterns=patterns)

    if as_json:
        click.echo(render_report_json(report))
    else:
        click.echo(render_report(report, use_color=use_color, verbose=verbose))

    if strict and report.has_issues:
        sys.exit(1)


def _count_files(root: Path, exts, excludes) -> int:
    """Pre-count files so flashbar has a real total. Cheap compared to parsing."""
    # TODO: scan_project already walks twice effectively — merge these eventually
    from envsleuth.scanner import iter_python_files
    return len(iter_python_files(root, extensions=exts, extra_excludes=excludes))


# ------------------------------------------------------------------- generate

@cli.command()
@click.option(
    "--path", "-p",
    type=click.Path(exists=True, file_okay=True, dir_okay=True, path_type=Path),
    default=".",
)
@click.option(
    "--output", "-o",
    type=click.Path(path_type=Path),
    default=None,
    help="Where to write the example file. Defaults to ./.env.example",
)
@click.option("--force", "-f", is_flag=True, help="Overwrite if output file exists.")
@click.option("--no-color", is_flag=True)
@click.option("--exclude", multiple=True)
@click.option("--ext", multiple=True)
def generate(
    path: Path,
    output: Optional[Path],
    force: bool,
    no_color: bool,
    exclude: tuple,
    ext: tuple,
) -> None:
    """Generate a .env.example file from scanned code."""

    root = path.resolve()
    if output is None:
        output = Path.cwd() / ".env.example"

    use_color = should_use_color(force=False if no_color else None)

    exts: Optional[Set[str]] = None
    if ext:
        exts = {".py"} | {e if e.startswith(".") else f".{e}" for e in ext}
    extra_excl: Optional[Set[str]] = set(exclude) if exclude else None

    result = scan_project(root, extensions=exts, extra_excludes=extra_excl)

    try:
        write_env_example(result, output, force=force)
    except FileExistsError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(2)

    n = len(result.static_names)
    msg = f"Wrote {n} variable{'s' if n != 1 else ''} to {output}"
    if use_color:
        msg = f"\033[32m✓\033[0m {msg}"
    click.echo(msg)


if __name__ == "__main__":
    cli()
