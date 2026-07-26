"""CLI entry point for envsleuth."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Iterable, Optional, Set, Tuple

import click

from envsleuth import __version__
from envsleuth.checker import (
    DEFAULT_ENV_FILE,
    DEFAULT_ENVIGNORE_FILE,
    check,
    find_nearby_env_files,
    load_ignore_patterns,
)
from envsleuth.config import (
    MAX_ENV_FILES,
    ConfigError,
    load_config,
)
from envsleuth.display import (
    _display_field,
    render_error_github,
    render_error_json,
    render_env_not_found_error,
    render_reports,
    render_reports_github,
    render_reports_json,
    should_use_color,
    stream_safe_text,
)
from envsleuth.generator import GenerationError, write_env_example
from envsleuth.sarif import render_error_sarif, render_report_sarif
from envsleuth.scanner import ScanError, ScanResult, iter_python_files, scan_project


# show flashbar only if the project is big enough that a blink of progress is useful
PROGRESS_THRESHOLD = 20
MAX_TOTAL_ENV_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_ENV_BINDINGS = 50_000
MAX_TOTAL_REPORT_VARIABLES = 100_000


@click.group()
@click.version_option(version=__version__, prog_name="envsleuth")
def cli() -> None:
    """envsleuth - find env vars in your code and check them against .env."""
    pass


# --------------------------------------------------------------------- scan

@cli.command()
@click.option(
    "--path", "-p",
    type=click.Path(
        file_okay=True, dir_okay=True, path_type=Path,
    ),
    default=None,
    help="Directory (or file) to scan. Overrides [tool.envsleuth].path.",
)
@click.option(
    "--env", "env_file",
    type=click.Path(path_type=Path),
    multiple=True,
    help="Path to an env file. Repeat to check independent profiles. "
         f"Defaults to ./{DEFAULT_ENV_FILE}",
)
@click.option(
    "--envignore",
    type=click.Path(path_type=Path),
    default=None,
    help=f"Path to .envignore. Defaults to ./{DEFAULT_ENVIGNORE_FILE} if present.",
)
@click.option(
    "--strict/--no-strict", default=None,
    help="Exit with code 1 if any vars are missing (for CI).",
)
@click.option(
    "--fail-on",
    type=click.Choice(["missing", "extra", "dynamic"], case_sensitive=False),
    multiple=True,
    help="Exit with code 1 for this finding category. Can be repeated.",
)
@click.option(
    "--no-fail-on", is_flag=True,
    help="Clear fail-on policies loaded from pyproject.toml.",
)
@click.option(
    "--json", "as_json", is_flag=True,
    help="Emit machine-readable JSON instead of the human report. "
         "Equivalent to --output json.",
)
@click.option(
    "--output", "-o", "output_format",
    type=click.Choice(
        ["text", "json", "github", "sarif"], case_sensitive=False,
    ),
    default=None,
    help="Output format: text, json, github, or SARIF 2.1.0. Default: text.",
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
@click.option(
    "--config", "config_file",
    type=click.Path(path_type=Path),
    default=None,
    help="Read [tool.envsleuth] from this TOML file.",
)
@click.option(
    "--no-config", is_flag=True,
    help="Do not discover or load pyproject.toml.",
)
def scan(
    path: Optional[Path],
    env_file,
    envignore: Optional[Path],
    strict: Optional[bool],
    as_json: bool,
    output_format: Optional[str],
    no_color: bool,
    exclude: tuple,
    ext: tuple,
    verbose: bool,
    no_update_check: bool,
    fail_on: tuple = (),
    no_fail_on: bool = False,
    config_file: Optional[Path] = None,
    no_config: bool = False,
) -> None:
    """Scan a project for env var usages and check against .env."""

    # --json is an alias for --output json. if both given, --output wins
    if output_format is None:
        output_format = "json" if as_json else "text"
    output_format = output_format.lower()

    try:
        config = load_config(
            start=Path.cwd(), config_path=config_file, disabled=no_config,
        )
    except ConfigError as exc:
        _fail_for_output(
            str(exc), output_format, path=exc.path,
            title="Invalid envsleuth config",
        )

    if no_fail_on and fail_on:
        _fail_for_output(
            "--fail-on cannot be used together with --no-fail-on",
            output_format, title="Invalid fail policy",
        )

    config_root = (
        config.source.parent
        if config.active and config.source is not None
        else Path.cwd()
    )
    chosen_path = path
    if chosen_path is None:
        chosen_path = config.path if config.active else None
    if chosen_path is None:
        chosen_path = config_root if config.active else Path(".")

    # resolve paths up front so error messages look sensible
    try:
        root = chosen_path.resolve()
    except (OSError, RuntimeError) as exc:
        _fail_for_output(
            f"could not resolve scan path {chosen_path}: {exc}",
            output_format, path=chosen_path, title="Invalid scan path",
        )
    _validate_scan_path(root, output_format)
    sarif_root = _sarif_location_root(root, config_root)

    cli_env_files = _coerce_env_files(env_file)
    if cli_env_files:
        selected_env_files = cli_env_files
    elif config.active and config.env_files:
        selected_env_files = config.env_files
    else:
        selected_env_files = (config_root / DEFAULT_ENV_FILE,)
    env_files = _absolute_unique_paths(
        selected_env_files, "environment file", output_format,
    )
    if len(env_files) > MAX_ENV_FILES:
        _fail_for_output(
            f"too many environment files (maximum {MAX_ENV_FILES})",
            output_format, title="Too many environment files",
        )
    for selected_env in env_files:
        _validate_optional_file(
            selected_env, "environment file", output_format,
        )
    _validate_total_env_size(env_files, output_format)

    if envignore is None:
        if config.active and config.envignore is not None:
            envignore = config.envignore
        else:
            candidate = config_root / DEFAULT_ENVIGNORE_FILE
            try:
                envignore = candidate if candidate.exists() else None
            except OSError as exc:
                _fail_for_output(
                    f"could not inspect ignore file {candidate}: {exc}",
                    output_format, path=candidate, title="Invalid ignore file",
                )
    if envignore is not None:
        try:
            envignore = envignore.absolute()
        except (OSError, RuntimeError) as exc:
            _fail_for_output(
                f"could not resolve ignore file {envignore}: {exc}",
                output_format, path=envignore, title="Invalid ignore file",
            )
        _validate_required_file(envignore, "ignore file", output_format)

    use_color = should_use_color(force=False if no_color else None)
    # machine-readable formats get no color
    if output_format in ("json", "github", "sarif"):
        use_color = False

    missing_env_files = _missing_paths(
        env_files, "environment file", output_format,
    )
    if missing_env_files and output_format == "text":
        if len(env_files) == 1:
            _show_env_not_found(missing_env_files[0], use_color)
        lines = ["Error: environment files not found:"]
        lines.extend(
            f"  {_display_field(missing)}" for missing in missing_env_files
        )
        _echo("\n".join(lines), err=True)
        raise click.exceptions.Exit(2)

    # build the sets for scan_project
    configured_ext = config.extensions if config.active else ()
    configured_exclude = config.exclude if config.active else ()
    merged_ext = _stable_unique_strings((*configured_ext, *ext))
    merged_exclude = _stable_unique_strings(
        (*configured_exclude, *exclude), casefold=True,
    )
    try:
        exts = _extension_set(merged_ext)
        extra_excl = _exclude_set(merged_exclude)
    except ValueError as exc:
        _fail_for_output(
            str(exc), output_format, title="Invalid scan option",
            source_root=sarif_root,
        )

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
                        source_root=sarif_root,
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
                    source_root=sarif_root,
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
            source_root=sarif_root,
        )

    reports = []
    total_env_bindings = 0
    for selected_env in env_files:
        try:
            report = check(result, selected_env, ignore_patterns=patterns)
        except (OSError, UnicodeError) as exc:
            _fail_for_output(
                f"could not read environment file {selected_env}: {exc}",
                output_format, path=selected_env,
                title="Could not read environment file",
                source_root=sarif_root,
            )
        if (
            not reports
            and len(report.variables) * len(env_files)
            > MAX_TOTAL_REPORT_VARIABLES
        ):
            _fail_for_output(
                "environment profiles would create more than "
                f"{MAX_TOTAL_REPORT_VARIABLES} variable reports",
                output_format,
                title="Too many variable reports",
                source_root=sarif_root,
            )
        reports.append(report)
        total_env_bindings += report.env_binding_count
        if total_env_bindings > MAX_TOTAL_ENV_BINDINGS:
            _fail_for_output(
                "environment profiles contain more than "
                f"{MAX_TOTAL_ENV_BINDINGS} unique bindings in total",
                output_format,
                title="Too many environment bindings",
                source_root=sarif_root,
            )

    if output_format == "json":
        _echo(render_reports_json(reports))
    elif output_format == "github":
        _echo(render_reports_github(reports))
    elif output_format == "sarif":
        _echo(render_report_sarif(reports, sarif_root, __version__))
    else:
        _echo(render_reports(
            reports, use_color=use_color, verbose=verbose,
        ))

    # Missing input and incomplete scans are operational failures, not lint
    # findings. Keep their exit code stable even without --strict.
    if any(not report.env_file_exists for report in reports) or result.errors:
        raise click.exceptions.Exit(2)

    configured_fail_on = config.fail_on if config.active else ()
    if no_fail_on:
        effective_fail_on = set()
    elif fail_on:
        effective_fail_on = {choice.lower() for choice in fail_on}
    else:
        effective_fail_on = set(configured_fail_on)

    effective_strict = (
        strict
        if strict is not None
        else bool(config.strict) if config.active else False
    )
    if effective_strict:
        effective_fail_on.add("missing")

    failed = (
        ("missing" in effective_fail_on and any(r.missing for r in reports))
        or (
            "extra" in effective_fail_on
            and any(r.extra_in_env for r in reports)
        )
        or (
            "dynamic" in effective_fail_on
            and bool(result.dynamic_usages)
        )
    )
    if failed:
        raise click.exceptions.Exit(1)

    # Never append update notices to machine-readable output.
    if output_format == "text" and not no_update_check:
        from envsleuth.update_check import maybe_notify
        maybe_notify(__version__)


# ------------------------------------------------------------------- generate

@cli.command()
@click.option(
    "--path", "-p",
    type=click.Path(
        exists=True, file_okay=True, dir_okay=True,
        readable=True, path_type=Path,
    ),
    default=None,
    help="Directory (or file) to scan. Overrides [tool.envsleuth].path.",
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
@click.option(
    "--config", "config_file",
    type=click.Path(file_okay=True, dir_okay=False, path_type=Path),
    default=None,
    help="Read [tool.envsleuth] from this TOML file.",
)
@click.option("--no-config", is_flag=True, help="Do not load pyproject.toml.")
def generate(
    path: Optional[Path],
    output: Optional[Path],
    force: bool,
    no_color: bool,
    exclude: tuple,
    ext: tuple,
    no_update_check: bool,
    config_file: Optional[Path] = None,
    no_config: bool = False,
) -> None:
    """Generate a .env.example file from scanned code."""

    try:
        config = load_config(
            start=Path.cwd(), config_path=config_file, disabled=no_config,
        )
    except ConfigError as exc:
        _fail(str(exc))

    config_root = (
        config.source.parent
        if config.active and config.source is not None
        else Path.cwd()
    )
    chosen_path = path
    if chosen_path is None:
        chosen_path = config.path if config.active else None
    if chosen_path is None:
        chosen_path = config_root if config.active else Path(".")

    try:
        root = chosen_path.resolve()
    except (OSError, RuntimeError) as exc:
        _fail(f"could not resolve scan path {chosen_path}: {exc}")
    _validate_scan_path(root)

    if output is None:
        output = Path.cwd() / ".env.example"
    try:
        output = output.absolute()
    except (OSError, RuntimeError) as exc:
        _fail(f"could not resolve output path {output}: {exc}")

    use_color = should_use_color(force=False if no_color else None)

    configured_ext = config.extensions if config.active else ()
    configured_exclude = config.exclude if config.active else ()
    merged_ext = _stable_unique_strings((*configured_ext, *ext))
    merged_exclude = _stable_unique_strings(
        (*configured_exclude, *exclude), casefold=True,
    )
    try:
        exts = _extension_set(merged_ext)
        extra_excl = _exclude_set(merged_exclude)
    except ValueError as exc:
        _fail(str(exc))

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


def _coerce_env_files(value) -> Tuple[Path, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, Path)):
        return (Path(value),)
    return tuple(Path(item) for item in value)


def _absolute_unique_paths(
    paths: Iterable[Path],
    label: str,
    output_format: str,
) -> Tuple[Path, ...]:
    result = []
    seen = set()
    for raw_path in paths:
        try:
            path = Path(raw_path).absolute()
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            _fail_for_output(
                f"could not resolve {label} {raw_path}: {exc}",
                output_format, path=Path(raw_path),
                title=f"Invalid {label}",
            )
        key = os.path.normcase(str(path))
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return tuple(result)


def _missing_paths(
    paths: Iterable[Path],
    label: str,
    output_format: str,
) -> Tuple[Path, ...]:
    missing = []
    for path in paths:
        try:
            if not path.exists():
                missing.append(path)
        except OSError as exc:
            _fail_for_output(
                f"could not inspect {label} {path}: {exc}",
                output_format, path=path, title=f"Invalid {label}",
            )
    return tuple(missing)


def _validate_total_env_size(
    paths: Iterable[Path],
    output_format: str,
) -> None:
    total = 0
    for path in paths:
        try:
            if not path.exists():
                continue
            total += path.stat().st_size
        except OSError as exc:
            _fail_for_output(
                f"could not inspect environment file {path}: {exc}",
                output_format, path=path,
                title="Invalid environment file",
            )
        if total > MAX_TOTAL_ENV_FILE_BYTES:
            _fail_for_output(
                "environment files exceed the cumulative "
                f"{MAX_TOTAL_ENV_FILE_BYTES}-byte limit",
                output_format, title="Environment files are too large",
            )


def _show_env_not_found(env_file: Path, use_color: bool) -> None:
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


def _stable_unique_strings(
    values: Iterable[str], *, casefold: bool = False,
) -> tuple:
    result = []
    seen = set()
    for value in values:
        marker = value.casefold() if casefold else value
        if marker in seen:
            continue
        seen.add(marker)
        result.append(value)
    return tuple(result)


def _extension_set(values: Iterable[str]) -> Optional[Set[str]]:
    values = tuple(values)
    if len(values) > 128:
        raise ValueError("too many extra file extensions (maximum 128)")
    if not values:
        return None

    result = {".py"}
    for value in values:
        if not isinstance(value, str):
            raise ValueError("file extensions must be strings")
        body = value[1:] if value.startswith(".") else value
        invalid = (
            not body
            or len(body) > 32
            or "." in body
            or any(char.isspace() or not char.isprintable() for char in body)
            or any(char in body for char in ("/", "\\", ":"))
        )
        if invalid:
            raise ValueError(f"invalid file extension: {_display_field(value)}")
        result.add(f".{body}")
    return result


def _exclude_set(values: Iterable[str]) -> Optional[Set[str]]:
    values = tuple(values)
    if len(values) > 256:
        raise ValueError("too many excluded directory names (maximum 256)")
    if not values:
        return None

    result = set()
    for value in values:
        invalid = (
            not isinstance(value, str)
            or not value
            or value in (".", "..")
            or len(value) > 255
            or any(char in value for char in ("/", "\\"))
            or any(not char.isprintable() for char in value)
        )
        if invalid:
            raise ValueError(
                f"invalid excluded directory name: {_display_field(value)}"
            )
        result.add(value)
    return result


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
    source_root: Optional[Path] = None,
) -> None:
    if output_format == "json":
        _echo(render_error_json(message, path))
    elif output_format == "github":
        _echo(render_error_github(message, path=path, title=title))
    elif output_format == "sarif":
        _echo(render_error_sarif(
            message,
            source_root or Path.cwd(),
            __version__,
            path=path,
        ))
    else:
        _echo(f"Error: {_display_field(message)}", err=True)
    raise click.exceptions.Exit(2)


def _sarif_location_root(scan_root: Path, config_root: Path) -> Path:
    candidates = []
    workspace = os.environ.get("GITHUB_WORKSPACE")
    if workspace:
        candidates.append(Path(workspace))
    candidates.extend((Path.cwd(), config_root))

    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=False)
            scan_root.relative_to(resolved)
        except (OSError, RuntimeError, ValueError):
            continue
        return resolved

    try:
        return scan_root.parent if scan_root.is_file() else scan_root
    except OSError:
        return scan_root


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


def _validate_scan_path(
    path: Path, output_format: str = "text",
) -> None:
    try:
        if not path.exists():
            _fail_for_output(
                f"scan path not found: {path}", output_format,
                path=path, title="Invalid scan path",
            )
        if not (path.is_file() or path.is_dir()):
            _fail_for_output(
                f"scan path is not a regular file or directory: {path}",
                output_format, path=path, title="Invalid scan path",
            )
    except OSError as exc:
        _fail_for_output(
            f"could not inspect scan path {path}: {exc}", output_format,
            path=path, title="Invalid scan path",
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
