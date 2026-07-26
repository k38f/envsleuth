"""Loading and validation for ``[tool.envsleuth]`` configuration."""

from __future__ import annotations

import os
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

import tomli


MAX_CONFIG_BYTES = 1024 * 1024
MAX_ENV_FILES = 64
FAIL_ON_CHOICES = frozenset({"missing", "extra", "dynamic"})
_ALLOWED_KEYS = frozenset({
    "path",
    "env",
    "envignore",
    "strict",
    "fail-on",
    "exclude",
    "extensions",
})


class ConfigError(ValueError):
    """Raised when an envsleuth configuration cannot be safely loaded."""

    def __init__(self, message: str, *, path: Optional[Path] = None) -> None:
        self.path = path
        self.message = message
        if path is None:
            super().__init__(message)
        else:
            super().__init__(f"{path}: {message}")


@dataclass(frozen=True)
class EnvSleuthConfig:
    """Validated values from one ``[tool.envsleuth]`` table."""

    source: Optional[Path] = None
    active: bool = False
    path: Optional[Path] = None
    env_files: Tuple[Path, ...] = ()
    envignore: Optional[Path] = None
    strict: Optional[bool] = None
    fail_on: Tuple[str, ...] = ()
    exclude: Tuple[str, ...] = ()
    extensions: Tuple[str, ...] = ()


def load_config(
    *,
    start: Optional[Path] = None,
    config_path: Optional[Path] = None,
    disabled: bool = False,
) -> EnvSleuthConfig:
    """Load an explicit config or discover the nearest ``pyproject.toml``.

    ``start`` controls automatic discovery only. An explicit ``config_path`` is
    resolved relative to the current working directory, matching normal CLI
    path behaviour.
    """

    if disabled:
        if config_path is not None:
            raise ConfigError(
                "an explicit config cannot be used together with --no-config",
            )
        return EnvSleuthConfig()

    if config_path is not None:
        source = _absolute_lexical_path(
            Path.cwd(), config_path, "config file",
        )
        return _load_from_file(
            source, table_required=True, restrict_paths=False,
        )

    source = discover_pyproject(start)
    if source is None:
        return EnvSleuthConfig()
    return _load_from_file(source, table_required=False, restrict_paths=True)


def discover_pyproject(start: Optional[Path] = None) -> Optional[Path]:
    """Return the nearest ``pyproject.toml``, without skipping parent files."""

    initial = Path.cwd() if start is None else Path(start)
    try:
        current = initial.resolve()
    except (OSError, RuntimeError) as exc:
        raise ConfigError(f"could not resolve config search path: {exc}") from exc

    try:
        if current.is_file():
            current = current.parent
    except OSError as exc:
        raise ConfigError(
            f"could not inspect config search path: {exc}", path=current,
        ) from exc

    while True:
        candidate = current / "pyproject.toml"
        try:
            candidate.stat()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ConfigError(
                f"could not inspect potential config file: {exc}",
                path=candidate,
            ) from exc
        else:
            return candidate

        parent = current.parent
        if parent == current:
            return None
        current = parent


def _load_from_file(
    source: Path,
    *,
    table_required: bool,
    restrict_paths: bool,
) -> EnvSleuthConfig:
    document = _read_toml(source)
    table = _find_table(document, source)
    if table is None:
        if table_required:
            raise ConfigError(
                "missing required [tool.envsleuth] table", path=source,
            )
        return EnvSleuthConfig(source=source)

    unknown = sorted(set(table) - _ALLOWED_KEYS)
    if unknown:
        rendered = ", ".join(repr(key) for key in unknown)
        raise ConfigError(
            f"unknown [tool.envsleuth] key(s): {rendered}", path=source,
        )

    base = source.parent
    confined_base = base if restrict_paths else None
    scan_path = _optional_path(
        table, "path", base, source, confined_base=confined_base,
    )
    env_files = _env_paths(
        table, base, source, confined_base=confined_base,
    )
    envignore = _optional_path(
        table, "envignore", base, source, confined_base=confined_base,
    )
    strict = _optional_bool(table, "strict", source)
    fail_on = _choice_list(table, "fail-on", source)
    exclude = _string_list(table, "exclude", source)
    extensions = _extension_list(table, source)

    return EnvSleuthConfig(
        source=source,
        active=True,
        path=scan_path,
        env_files=env_files,
        envignore=envignore,
        strict=strict,
        fail_on=fail_on,
        exclude=exclude,
        extensions=extensions,
    )


def _read_toml(path: Path) -> Dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise ConfigError("config file does not exist", path=path) from exc
    except PermissionError as exc:
        try:
            non_regular = path.exists() and not path.is_file()
        except OSError:
            non_regular = False
        if non_regular:
            raise ConfigError(
                "config path is not a regular file", path=path,
            ) from None
        raise ConfigError(f"could not open config file: {exc}", path=path) from exc
    except OSError as exc:
        raise ConfigError(f"could not open config file: {exc}", path=path) from exc

    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ConfigError(
                "config path is not a regular file", path=path,
            )
        if file_stat.st_size > MAX_CONFIG_BYTES:
            raise ConfigError(
                f"config file exceeds the {MAX_CONFIG_BYTES}-byte limit",
                path=path,
            )
        with os.fdopen(descriptor, "rb") as file:
            descriptor = -1
            raw = file.read(MAX_CONFIG_BYTES + 1)
    except ConfigError:
        raise
    except OSError as exc:
        raise ConfigError(
            f"could not read config file: {exc}", path=path,
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    if len(raw) > MAX_CONFIG_BYTES:
        raise ConfigError(
            f"config file exceeds the {MAX_CONFIG_BYTES}-byte limit", path=path,
        )
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ConfigError("config file is not valid UTF-8", path=path) from exc

    bad = next(
        (
            char for char in text
            if _is_control(char) and char not in "\t\n\r"
        ),
        None,
    )
    if bad is not None:
        raise ConfigError(
            f"config file contains a disallowed control character "
            f"(U+{ord(bad):04X})",
            path=path,
        )

    try:
        parsed = tomli.loads(text)
    except tomli.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML: {exc}", path=path) from exc
    except (MemoryError, OverflowError, RecursionError) as exc:
        raise ConfigError(
            "TOML structure is too complex", path=path,
        ) from exc
    if not isinstance(parsed, dict):  # defensive: both TOML parsers return dict
        raise ConfigError("TOML root must be a table", path=path)
    return parsed


def _find_table(
    document: Dict[str, Any], source: Path,
) -> Optional[Dict[str, Any]]:
    if "tool" not in document:
        return None
    tool = document["tool"]
    if not isinstance(tool, dict):
        return None
    if "envsleuth" not in tool:
        return None

    table = tool["envsleuth"]
    if not isinstance(table, dict):
        raise ConfigError("[tool.envsleuth] must be a table", path=source)
    return table


def _optional_path(
    table: Dict[str, Any],
    key: str,
    base: Path,
    source: Path,
    *,
    confined_base: Optional[Path],
) -> Optional[Path]:
    if key not in table:
        return None
    value = _string_value(table[key], key, source)
    return _resolve_path(
        base, value, key, source=source, confined_base=confined_base,
    )


def _env_paths(
    table: Dict[str, Any],
    base: Path,
    source: Path,
    *,
    confined_base: Optional[Path],
) -> Tuple[Path, ...]:
    if "env" not in table:
        return ()

    raw = table["env"]
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, list):
        if not raw:
            raise ConfigError("'env' must not be an empty list", path=source)
        if len(raw) > MAX_ENV_FILES:
            raise ConfigError(
                f"'env' has more than {MAX_ENV_FILES} entries", path=source,
            )
        values = [
            _string_value(value, f"env[{index}]", source)
            for index, value in enumerate(raw)
        ]
    else:
        raise ConfigError(
            "'env' must be a string or a non-empty list of strings", path=source,
        )

    resolved = [
        _resolve_path(
            base,
            value,
            "env",
            source=source,
            confined_base=confined_base,
        )
        for value in values
    ]
    return tuple(_stable_unique(resolved, key=_path_key))


def _optional_bool(
    table: Dict[str, Any], key: str, source: Path,
) -> Optional[bool]:
    if key not in table:
        return None
    value = table[key]
    if not isinstance(value, bool):
        raise ConfigError(f"'{key}' must be a boolean", path=source)
    return value


def _choice_list(
    table: Dict[str, Any], key: str, source: Path,
) -> Tuple[str, ...]:
    if key not in table:
        return ()
    values = _require_list(table[key], key, source)
    result = []
    for index, value in enumerate(values):
        item = _string_value(value, f"{key}[{index}]", source)
        if item not in FAIL_ON_CHOICES:
            choices = ", ".join(sorted(FAIL_ON_CHOICES))
            raise ConfigError(
                f"'{key}' entry {item!r} is invalid; choose from: {choices}",
                path=source,
            )
        result.append(item)
    return tuple(_stable_unique(result))


def _string_list(
    table: Dict[str, Any], key: str, source: Path,
) -> Tuple[str, ...]:
    if key not in table:
        return ()
    values = _require_list(table[key], key, source)
    result = [
        _string_value(value, f"{key}[{index}]", source)
        for index, value in enumerate(values)
    ]
    return tuple(_stable_unique(result, key=str.casefold))


def _extension_list(
    table: Dict[str, Any], source: Path,
) -> Tuple[str, ...]:
    if "extensions" not in table:
        return ()
    values = _require_list(table["extensions"], "extensions", source)
    result = []
    for index, value in enumerate(values):
        item = _string_value(value, f"extensions[{index}]", source)
        body = item[1:] if item.startswith(".") else item
        if (
            not body
            or "." in body
            or any(char.isspace() for char in body)
            or any(char in body for char in ("/", "\\", ":"))
        ):
            raise ConfigError(
                f"'extensions' entry {item!r} is not a file extension",
                path=source,
            )
        result.append(f".{body}")
    return tuple(_stable_unique(result, key=str.casefold))


def _require_list(value: Any, key: str, source: Path) -> list:
    if not isinstance(value, list):
        raise ConfigError(f"'{key}' must be a list", path=source)
    return value


def _string_value(value: Any, key: str, source: Path) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"'{key}' must be a string", path=source)
    if not value or not value.strip():
        raise ConfigError(f"'{key}' must not be empty", path=source)
    bad = next((char for char in value if _is_control(char)), None)
    if bad is not None:
        raise ConfigError(
            f"'{key}' contains a disallowed control character "
            f"(U+{ord(bad):04X})",
            path=source,
        )
    return value


def _resolve_path(
    base: Path,
    value: Any,
    label: str,
    *,
    source: Optional[Path] = None,
    confined_base: Optional[Path] = None,
) -> Path:
    try:
        path = Path(value)
        if not path.is_absolute():
            path = base / path
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ConfigError(
            f"could not resolve '{label}' path {value!r}: {exc}", path=source,
        ) from exc
    if confined_base is not None:
        try:
            resolved.relative_to(confined_base.resolve(strict=False))
        except (OSError, RuntimeError, ValueError) as exc:
            raise ConfigError(
                f"'{label}' path must stay inside the directory containing "
                "an auto-discovered pyproject.toml; use --config to opt in "
                "to an external path",
                path=source,
            ) from exc
    return resolved


def _absolute_lexical_path(
    base: Path,
    value: Any,
    label: str,
) -> Path:
    try:
        path = Path(value)
        if not path.is_absolute():
            path = base / path
        return Path(os.path.abspath(path))
    except (OSError, TypeError, ValueError) as exc:
        raise ConfigError(
            f"could not resolve '{label}' path {value!r}: {exc}",
        ) from exc


def _stable_unique(
    values: Iterable[Any],
    *,
    key=lambda value: value,
) -> list:
    seen = set()
    result = []
    for value in values:
        marker = key(value)
        if marker in seen:
            continue
        seen.add(marker)
        result.append(value)
    return result


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path))


def _is_control(char: str) -> bool:
    return unicodedata.category(char) in {"Cc", "Cf", "Cs"}
