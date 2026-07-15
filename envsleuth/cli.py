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
    _display_field,
    render_error_github,
    render_error_json,
    render_env_not_found_error,
    render_report,
    render_report_github,
    render_report_json,
    should_use_color,
    stream_safe_text,
)
from envsleuth.generator import GenerationError, write_env_example
from envsleuth.scanner import ScanError, ScanResult, iter_python_files, scan_project


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
    type=click.Path(
        exists=True, file_okay=True, dir_okay=True,
        readable=True, path_type=Path,
    ),
    default=".",
    help="Directory (or file) to scan. Defaults to current directory.",
)
@click.option(
    "--env", "env_file",
    type=click.Path(file_okay=True, dir_okay=False, readable=True, path_type=Path),
    default=None,
    help=f"Path to the .env file. Defaults to ./{DEFAULT_ENV_FILE}",
)
@click.option(
    "--envignore",
    type=click.Path(
        exists=True, file_okay=True, dir_okay=False,
        readable=True, path_type=Path,
    ),
    default=None,
    help=f"Path to .envignore. Defaults to ./{DEFAULT_ENVIGNORE_FILE} if present.",
)
@click.option(
    "--strict", is_flag=True,
    help="Exit with code 1 if any vars are missing (for CI).",
)
@click.option(
    "--json", "as_json", is_flag=True,
    help="Emit machine-readable JSON instead of the human report. "
         "Equivalent to --output json.",
)
@click.option(
    "--output", "-o", "output_format",
    type=click.Choice(["text", "json", "github"], case_sensitive=False),
    default=None,
    help="Output format. 'github' emits GitHub Actions workflow commands so "
         "missing vars show as PR annotations. Default: text.",
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
@click.option(
    "--no-update-check", "no_update_check", is_flag=True,
    help="Skip the weekly check for new envsleuth releases on PyPI.",
)
def scan(
    path: Path,
    env_file: Optional[Path],
    envignore: Optional[Path],
    strict: bool,
    as_json: bool,
    output_format: Optional[str],
    no_color: bool,
    exclude: tuple,
    ext: tuple,
    verbose: bool,
    no_update_check: bool,
) -> None:
    """Scan a project for env var usages and check against .env."""

    # --json is an alias for --output json. if both given, --output wins
    if output_format is None:
        output_format = "json" if as_json else "text"
    output_format = output_format.lower()

    # resolve paths up front so error messages look sensible
    try:
        root = path.resolve()
    except OSError as exc:
        _fail_for_output(
            f"could not resolve scan path {path}: {exc}",
            output_format, path=path, title="Invalid scan path",
        )

    if env_file is None:
        env_file = Path.cwd() / DEFAULT_ENV_FILE
    # always make absolute, even if file doesn't exist — otherwise the
    # "not found" message shows just the bare name
    try:
        env_file = env_file.absolute()
    except OSError as exc:
        _fail_for_output(
            f"could not resolve environment file {env_file}: {exc}",
            output_format, path=env_file, title="Invalid environment file",
        )

    _validate_optional_file(env_file, "environment file", output_format)

    if envignore is None:
        candidate = Path.cwd() / DEFAULT_ENVIGNORE_FILE
        try:
            envignore = candidate if candidate.exists() else None
        except OSError as exc:
            _fail_for_output(
                f"could not inspect ignore file {candidate}: {exc}",
                output_format, path=candidate, title="Invalid ignore file",
            )
        if envignore is not None:
            _validate_required_file(envignore, "ignore file", output_format)

    use_color = should_use_color(force=False if no_color else None)
    # machine-readable formats get no color
    if output_format in ("json", "github"):
        use_color = False

    # env file missing — bail out with a helpful message (unless machine format
    # where we still want to emit a structured report for CI to consume)
    try:
        env_exists = env_file.exists()
    except OSError as exc:
        _fail_for_output(
            f"could not inspect environment file {env_file}: {exc}",
            output_format, path=env_file, title="Invalid environment file",
        )

    if not env_exists and output_format == "text":
        # look for .env* files in the same directory the user pointed at
        try:
            parent_exists = env_file.parent.exists()
        except OSError:
            parent_exists = False
        search_root = env_file.parent if parent_exists else Path.cwd()
        try:
            nearby = find_nearby_env_files(search_root)
        except OSError:
            nearby = []
        _echo(render_env_not_found_error(
            env_file, nearby, use_color=use_color,
        ), err=True)
        raise click.exceptions.Exit(2)

    # build the sets for scan_project
    exts: Optional[Set[str]] = None
    if ext:
        # include default .py plus the user-supplied ones, normalise the dot
        exts = {".py"} | {e if e.startswith(".") else f".{e}" for e in ext}
    extra_excl: Optional[Set[str]] = set(exclude) if exclude else None

    # Enumerate once: the same list sizes the progress bar and feeds the scan.
    try:
        files = iter_python_files(
            root, extensions=exts, extra_excludes=extra_excl,
        )
    except (ScanError, OSError) as exc:
        result = ScanResult(errors=[(root, str(exc))])
    else:
        progress_stream = sys.stderr
        use_progress = (
            output_format == "text"
            and len(files) >= PROGRESS_THRESHOLD
            and use_color
            and _stream_is_tty(progress_stream)
        )

        if use_progress:
            from flashbar import Bar  # imported lazily so --json path stays lean
            bar_options = _progress_bar_options(progress_stream)
            with Bar(
                len(files), label="Scanning", show_eta=True, show_speed=True,
                file=progress_stream, **bar_options,
            ) as bar:
                def _tick(_f: Path) -> None:
                    bar.update()

                try:
                    result = scan_project(
                        root, extensions=exts, extra_excludes=extra_excl,
                        on_file=_tick, files=files,
                    )
                except OSError as exc:
                    _fail_for_output(
                        f"could not scan {root}: {exc}", output_format,
                        path=root, title="Scan failed",
                    )
        else:
            try:
                result = scan_project(
                    root, extensions=exts, extra_excludes=extra_excl,
                    files=files,
                )
            except OSError as exc:
                _fail_for_output(
                    f"could not scan {root}: {exc}", output_format,
                    path=root, title="Scan failed",
                )

    # .envignore patterns
    try:
        patterns = (
            load_ignore_patterns(envignore, required=True)
            if envignore else []
        )
    except (OSError, UnicodeError) as exc:
        _fail_for_output(
            f"could not read ignore file {envignore}: {exc}",
            output_format, path=envignore, title="Could not read ignore file",
        )

    try:
        report = check(result, env_file, ignore_patterns=patterns)
    except (OSError, UnicodeError) as exc:
        _fail_for_output(
            f"could not read environment file {env_file}: {exc}",
            output_format, path=env_file,
            title="Could not read environment file",
        )

    if output_format == "json":
        _echo(render_report_json(report))
    elif output_format == "github":
        _echo(render_report_github(report))
    else:
        _echo(render_report(report, use_color=use_color, verbose=verbose))

    # Missing input and incomplete scans are operational failures, not lint
    # findings. Keep their exit code stable even without --strict.
    if not report.env_file_exists or report.errors:
        raise click.exceptions.Exit(2)

    # quietly check pypi for newer versions — never output in machine formats
    # since it would corrupt the JSON or break the GHA annotation parser
    if output_format == "text" and not no_update_check:
        from envsleuth.update_check import maybe_notify
        maybe_notify(__version__)

    if strict and report.has_issues:
        raise click.exceptions.Exit(1)


# ------------------------------------------------------------------- generate

@cli.command()
@click.option(
    "--path", "-p",
    type=click.Path(
        exists=True, file_okay=True, dir_okay=True,
        readable=True, path_type=Path,
    ),
    default=".",
)
@click.option(
    "--output", "-o",
    type=click.Path(file_okay=True, dir_okay=False, writable=True, path_type=Path),
    default=None,
    help="Where to write the example file. Defaults to ./.env.example",
)
@click.option("--force", "-f", is_flag=True, help="Overwrite if output file exists.")
@click.option("--no-color", is_flag=True, help="Disable ANSI colors in the success message.")
@click.option("--exclude", multiple=True, help="Extra directory name to skip. Can be repeated.")
@click.option("--ext", multiple=True, help="Extra file extension to scan (e.g. .pyi). Can be repeated.")
@click.option(
    "--no-update-check", "no_update_check", is_flag=True,
    help="Skip the weekly check for new envsleuth releases on PyPI.",
)
def generate(
    path: Path,
    output: Optional[Path],
    force: bool,
    no_color: bool,
    exclude: tuple,
    ext: tuple,
    no_update_check: bool,
) -> None:
    """Generate a .env.example file from scanned code."""

    try:
        root = path.resolve()
    except OSError as exc:
        _fail(f"could not resolve scan path {path}: {exc}")

    if output is None:
        output = Path.cwd() / ".env.example"
    output = output.absolute()

    use_color = should_use_color(force=False if no_color else None)

    exts: Optional[Set[str]] = None
    if ext:
        exts = {".py"} | {e if e.startswith(".") else f".{e}" for e in ext}
    extra_excl: Optional[Set[str]] = set(exclude) if exclude else None

    try:
        result = scan_project(root, extensions=exts, extra_excludes=extra_excl)
    except OSError as exc:
        _fail(f"could not scan {root}: {exc}")

    if result.errors:
        lines = ["Error: scan incomplete; .env.example was not written."]
        for file_path, message in result.errors:
            lines.append(
                f"  {_display_field(file_path)}: {_display_field(message)}"
            )
        _echo("\n".join(lines), err=True)
        raise click.exceptions.Exit(2)

    try:
        write_env_example(result, output, force=force)
    except (GenerationError, OSError, UnicodeError) as exc:
        _fail(str(exc))

    n = len(result.static_names)
    msg = (
        f"Wrote {n} variable{'s' if n != 1 else ''} "
        f"to {_display_field(output)}"
    )
    n_dynamic = len(result.dynamic_usages)
    if n_dynamic:
        msg += (
            f"; skipped {n_dynamic} dynamic lookup"
            f"{'s' if n_dynamic != 1 else ''}"
        )
    if use_color:
        msg = f"\033[32m✓\033[0m {msg}"
    _echo(msg)

    if not no_update_check:
        from envsleuth.update_check import maybe_notify
        maybe_notify(__version__)


def _echo(message: str, err: bool = False) -> None:
    stream = sys.stderr if err else sys.stdout
    click.echo(stream_safe_text(message, stream), file=stream)


def _fail(message: str) -> None:
    _echo(f"Error: {_display_field(message)}", err=True)
    raise click.exceptions.Exit(2)


def _fail_for_output(
    message: str,
    output_format: str,
    path: Optional[Path] = None,
    title: str = "envsleuth error",
) -> None:
    if output_format == "json":
        _echo(render_error_json(message, path))
    elif output_format == "github":
        _echo(render_error_github(message, path=path, title=title))
    else:
        _echo(f"Error: {_display_field(message)}", err=True)
    raise click.exceptions.Exit(2)


def _stream_is_tty(stream) -> bool:
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def _progress_bar_options(stream) -> dict:
    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        "█░▏▎▍▌▋▊▉".encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return {"theme": "retro", "smooth": False}
    return {}


def _validate_optional_file(
    path: Path, label: str, output_format: str = "text",
) -> None:
    try:
        if path.exists() and not path.is_file():
            _fail_for_output(
                f"{label} is not a file: {path}", output_format,
                path=path, title=f"Invalid {label}",
            )
    except OSError as exc:
        _fail_for_output(
            f"could not inspect {label} {path}: {exc}", output_format,
            path=path, title=f"Invalid {label}",
        )


def _validate_required_file(
    path: Path, label: str, output_format: str = "text",
) -> None:
    try:
        if not path.exists():
            _fail_for_output(
                f"{label} not found: {path}", output_format,
                path=path, title=f"Invalid {label}",
            )
        if not path.is_file():
            _fail_for_output(
                f"{label} is not a file: {path}", output_format,
                path=path, title=f"Invalid {label}",
            )
    except OSError as exc:
        _fail_for_output(
            f"could not inspect {label} {path}: {exc}", output_format,
            path=path, title=f"Invalid {label}",
        )


if __name__ == "__main__":
    cli()
