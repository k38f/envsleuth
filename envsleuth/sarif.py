"""SARIF 2.1.0 output for envsleuth reports."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple, Union
from urllib.parse import quote_from_bytes

from envsleuth.checker import CheckReport


SARIF_SCHEMA = (
    "https://docs.oasis-open.org/sarif/sarif/v2.1.0/"
    "errata01/os/schemas/sarif-schema-2.1.0.json"
)
MAX_RESULTS = 25_000
MAX_NAME_LENGTH = 512
MAX_MESSAGE_LENGTH = 4096


_RULES = (
    {
        "id": "envsleuth/missing-environment-variable",
        "name": "MissingEnvironmentVariable",
        "shortDescription": {
            "text": "A required environment variable is missing."
        },
        "fullDescription": {
            "text": (
                "Code reads an environment variable without a usable default, "
                "but the selected environment file does not define it."
            )
        },
        "defaultConfiguration": {"level": "error"},
        "help": {
            "text": (
                "Define the variable in the selected environment file or "
                "provide an intentional default in every required lookup."
            )
        },
        "properties": {
            "precision": "very-high",
            "problem.severity": "error",
            "tags": ["configuration", "correctness"],
        },
    },
    {
        "id": "envsleuth/extra-environment-variable",
        "name": "ExtraEnvironmentVariable",
        "shortDescription": {
            "text": "An environment variable has no statically resolved use."
        },
        "fullDescription": {
            "text": (
                "The selected environment file defines a variable for which "
                "envsleuth found no statically resolved lookup."
            )
        },
        "defaultConfiguration": {"level": "note"},
        "help": {
            "text": (
                "Remove the variable if it is obsolete, or ignore it when it "
                "is consumed dynamically or by code outside the scan."
            )
        },
        "properties": {
            "precision": "medium",
            "problem.severity": "recommendation",
            "tags": ["configuration", "maintainability"],
        },
    },
    {
        "id": "envsleuth/dynamic-environment-variable",
        "name": "DynamicEnvironmentVariable",
        "shortDescription": {
            "text": "An environment-variable name could not be resolved."
        },
        "fullDescription": {
            "text": (
                "An environment-variable lookup computes its name dynamically, "
                "so envsleuth cannot compare it with an environment file."
            )
        },
        "defaultConfiguration": {"level": "warning"},
        "help": {
            "text": (
                "Check this lookup manually or use a statically resolvable "
                "environment-variable name where practical."
            )
        },
        "properties": {
            "precision": "high",
            "problem.severity": "warning",
            "tags": ["configuration", "correctness"],
        },
    },
    {
        "id": "envsleuth/scan-error",
        "name": "ScanError",
        "shortDescription": {"text": "A source file could not be scanned."},
        "fullDescription": {
            "text": (
                "envsleuth could not completely parse or inspect a source file, "
                "so the environment-variable report may be incomplete."
            )
        },
        "defaultConfiguration": {"level": "error"},
        "help": {
            "text": (
                "Fix the reported source, encoding, or filesystem problem and "
                "run envsleuth again."
            )
        },
        "properties": {
            "precision": "very-high",
            "problem.severity": "error",
            "tags": ["correctness"],
        },
    },
    {
        "id": "envsleuth/operational-error",
        "name": "OperationalError",
        "shortDescription": {"text": "envsleuth could not complete the check."},
        "fullDescription": {
            "text": (
                "An input, filesystem, or configuration problem prevented "
                "envsleuth from completing the requested check."
            )
        },
        "defaultConfiguration": {"level": "error"},
        "help": {
            "text": "Correct the reported input or filesystem problem and retry."
        },
        "properties": {
            "precision": "very-high",
            "problem.severity": "error",
            "tags": ["correctness"],
        },
    },
)

_LEVELS = ("error", "note", "warning", "error", "error")
_PRIORITY = {4: 0, 3: 1, 0: 2, 2: 3, 1: 4}


@dataclass(frozen=True)
class _Finding:
    rule_index: int
    message: str
    identity: Tuple[str, ...]
    path: Optional[Path] = None
    line: Optional[int] = None


def render_report_sarif(
    reports: Union[CheckReport, Iterable[CheckReport]],
    source_root: Path,
    version: str,
) -> str:
    """Render one or more check reports as one deterministic SARIF log."""
    report_list = _coerce_reports(reports)
    root = _source_root(source_root)
    findings = _collect_findings(report_list, root)
    return _render(findings, root, version)


def render_error_sarif(
    message: str,
    source_root: Path,
    version: str,
    path: Optional[Path] = None,
    line: Optional[int] = None,
) -> str:
    """Render an operational failure without corrupting machine output."""
    root = _source_root(source_root)
    display_message = _redact_path(message, path, root)
    safe_message = _safe_text(display_message, MAX_MESSAGE_LENGTH)
    finding = _Finding(
        rule_index=4,
        message=safe_message or "envsleuth could not complete the check.",
        identity=(
            "operational",
            _identity_text(message),
            _path_identity(path),
            str(line),
        ),
        path=path if _relative_uri(path, root) is not None else root,
        line=_valid_line(line),
    )
    return _render([finding], root, version)


def _coerce_reports(
    reports: Union[CheckReport, Iterable[CheckReport]],
) -> List[CheckReport]:
    if isinstance(reports, CheckReport):
        return [reports]
    result = list(reports)
    if not all(isinstance(report, CheckReport) for report in result):
        raise TypeError("reports must contain CheckReport instances")
    return result


def _collect_findings(
    reports: List[CheckReport],
    root: Path,
) -> List[_Finding]:
    missing: Dict[
        Tuple[str, str],
        Tuple[Optional[Path], str, List[object], str],
    ] = {}
    extras: Dict[
        Tuple[str, str], Tuple[Optional[Path], str, str]
    ] = {}
    dynamics: Dict[Tuple[str, ...], object] = {}
    errors: Dict[Tuple[str, str], Tuple[Path, str]] = {}
    missing_envs: Dict[str, Path] = {}

    for report in reports:
        env_key = _path_identity(report.env_file)
        env_label = _path_label(report.env_file, root)
        if report.env_file is not None and not report.env_file_exists:
            missing_envs.setdefault(env_key, report.env_file)

        for variable in report.missing if report.env_file_exists else ():
            name = _safe_text(variable.name, MAX_NAME_LENGTH)
            key = (env_key, _identity_text(variable.name))
            entry = missing.setdefault(
                key, (report.env_file, env_label, [], name)
            )
            entry[2].extend(
                variable.missing_usages or variable.usages
            )

        for raw_name in report.extra_in_env:
            name = _safe_text(raw_name, MAX_NAME_LENGTH)
            extras.setdefault(
                (env_key, _identity_text(raw_name)),
                (report.env_file, env_label, name),
            )

        for usage in report.dynamic_usages:
            key = (
                _path_identity(getattr(usage, "file", None)),
                str(_valid_line(getattr(usage, "line", None))),
                _identity_text(
                    getattr(usage, "call_type", "lookup"), MAX_NAME_LENGTH
                ),
                _identity_text(
                    getattr(usage, "raw_expr", None) or "?",
                ),
            )
            dynamics.setdefault(key, usage)

        for error_path, raw_message in report.errors:
            redacted = _redact_path(raw_message, error_path, root)
            message = _safe_text(redacted, MAX_MESSAGE_LENGTH)
            key = (_path_identity(error_path), _identity_text(redacted))
            errors.setdefault(key, (error_path, message))

    findings: List[_Finding] = []
    for (env_key, name_key), (
        env_path, env_label, usages, name,
    ) in missing.items():
        usage = _primary_usage(usages, root)
        path = (
            getattr(usage, "file", None)
            if usage is not None else env_path
        )
        line = (
            _valid_line(getattr(usage, "line", None))
            if usage is not None else None
        )
        findings.append(_Finding(
            rule_index=0,
            message=(
                f"Environment variable '{name}' is required by code but "
                f"missing from '{env_label}'."
            ),
            identity=("missing", env_key, name_key),
            path=path,
            line=line,
        ))

    for (env_key, name_key), (env_path, env_label, name) in extras.items():
        findings.append(_Finding(
            rule_index=1,
            message=(
                f"Environment variable '{name}' is defined in '{env_label}' "
                "but has no statically resolved use."
            ),
            identity=("extra", env_key, name_key),
            path=env_path,
        ))

    for key, usage in dynamics.items():
        call_type = _safe_text(
            getattr(usage, "call_type", "lookup"), MAX_NAME_LENGTH
        )
        expression = _safe_text(
            getattr(usage, "raw_expr", None) or "?", MAX_NAME_LENGTH
        )
        findings.append(_Finding(
            rule_index=2,
            message=(
                f"Dynamic environment-variable lookup "
                f"'{call_type}({expression})' could not be resolved statically."
            ),
            identity=("dynamic",) + key,
            path=getattr(usage, "file", None),
            line=_valid_line(getattr(usage, "line", None)),
        ))

    for (path_key, message_key), (path, message) in errors.items():
        findings.append(_Finding(
            rule_index=3,
            message=f"Could not scan this file: {message}",
            identity=("scan-error", path_key, message_key),
            path=path,
        ))

    for env_key, env_path in missing_envs.items():
        env_label = _path_label(env_path, root)
        findings.append(_Finding(
            rule_index=4,
            message=f"Environment file not found: '{env_label}'.",
            identity=("missing-env", env_key),
            path=env_path,
        ))
    return findings


def _primary_usage(usages: List[object], root: Path) -> Optional[object]:
    candidates = []
    for usage in usages:
        path = getattr(usage, "file", None)
        line = _valid_line(getattr(usage, "line", None))
        uri = _relative_uri(path, root)
        if uri is None or line is None:
            continue
        candidates.append((
            uri,
            line,
            _safe_text(getattr(usage, "call_type", ""), MAX_NAME_LENGTH),
            _safe_text(
                getattr(usage, "raw_expr", None) or "", MAX_NAME_LENGTH
            ),
            usage,
        ))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[:-1])
    return candidates[0][-1]


def _render(findings: List[_Finding], root: Path, version: str) -> str:
    unique = {(
        finding.rule_index,
        finding.identity,
    ): finding for finding in findings}
    ordered = sorted(unique.values(), key=lambda finding: _finding_key(
        finding, root
    ))
    omitted = max(0, len(ordered) - MAX_RESULTS)
    if omitted:
        reserved: List[_Finding] = []
        reserved_rules = set()
        for finding in ordered:
            if finding.rule_index in reserved_rules:
                continue
            reserved.append(finding)
            reserved_rules.add(finding.rule_index)
            if len(reserved) == MAX_RESULTS:
                break
        reserved_ids = {id(finding) for finding in reserved}
        selected = reserved + [
            finding for finding in ordered
            if id(finding) not in reserved_ids
        ][:MAX_RESULTS - len(reserved)]
        ordered = sorted(selected, key=lambda finding: _finding_key(
            finding, root
        ))

    run = {
        "tool": {
            "driver": {
                "name": "envsleuth",
                "version": _safe_text(version, MAX_NAME_LENGTH) or "unknown",
                "informationUri": "https://github.com/k38f/envsleuth",
                "rules": list(_RULES),
            }
        },
        "results": [_result(finding, root) for finding in ordered],
    }
    if omitted:
        run["invocations"] = [{
            "executionSuccessful": True,
            "toolExecutionNotifications": [{
                "level": "warning",
                "message": {
                    "text": (
                        f"SARIF output was limited to {MAX_RESULTS} results; "
                        f"{omitted} additional result"
                        f"{' was' if omitted == 1 else 's were'} omitted."
                    )
                },
            }],
        }]

    payload = {
        "$schema": SARIF_SCHEMA,
        "version": "2.1.0",
        "runs": [run],
    }
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True)


def _finding_key(finding: _Finding, root: Path) -> Tuple[object, ...]:
    return (
        _PRIORITY[finding.rule_index],
        _relative_uri(finding.path, root) or "",
        finding.line or 0,
        finding.identity,
    )


def _result(finding: _Finding, root: Path) -> dict:
    result = {
        "ruleId": _RULES[finding.rule_index]["id"],
        "ruleIndex": finding.rule_index,
        "level": _LEVELS[finding.rule_index],
        "message": {
            "text": _safe_text(finding.message, MAX_MESSAGE_LENGTH),
        },
    }
    location = _location(finding.path, finding.line, root)
    if location is None:
        location = _location(root, None, root)
    result["locations"] = [location]
    return result


def _location(
    path: Optional[Path],
    line: Optional[int],
    root: Path,
) -> Optional[dict]:
    uri = _relative_uri(path, root)
    if uri is None:
        return None
    physical = {"artifactLocation": {"uri": uri}}
    valid_line = _valid_line(line)
    if valid_line is not None:
        physical["region"] = {"startLine": valid_line}
    return {"physicalLocation": physical}


def _source_root(path: Path) -> Path:
    resolved = _resolve_path(path)
    if resolved is None:
        return Path.cwd()
    try:
        if resolved.is_file():
            return resolved.parent
    except OSError:
        pass
    return resolved


def _relative_uri(path: Optional[Path], root: Path) -> Optional[str]:
    resolved = _resolve_path(path)
    if resolved is None:
        return None
    try:
        relative = resolved.relative_to(root)
    except (TypeError, ValueError):
        return None
    if not relative.parts:
        return "."
    posix = relative.as_posix()
    return quote_from_bytes(os.fsencode(posix), safe="/-._~")


def _resolve_path(path: Optional[Path]) -> Optional[Path]:
    if path is None:
        return None
    try:
        return Path(path).resolve(strict=False)
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def _path_identity(path: Optional[Path]) -> str:
    resolved = _resolve_path(path)
    if resolved is None:
        return ""
    return os.path.normcase(str(resolved))


def _path_label(path: Optional[Path], root: Path) -> str:
    if path is None:
        return ".env"
    uri = _relative_uri(path, root)
    if uri is not None:
        return _safe_text(uri, MAX_NAME_LENGTH)
    return _safe_text(Path(path).name or ".env", MAX_NAME_LENGTH)


def _valid_line(value: object) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 1 or value > 2_147_483_647:
        return None
    return value


def _safe_text(value: object, limit: int) -> str:
    try:
        text = str(value)
    except Exception:
        text = "<unprintable>"

    pieces: List[str] = []
    length = 0
    truncated = False
    for char in text:
        if char == "\r":
            piece = "\\r"
        elif char == "\n":
            piece = "\\n"
        elif char == "\t":
            piece = "\\t"
        elif not char.isprintable():
            codepoint = ord(char)
            piece = (
                f"\\u{codepoint:04x}"
                if codepoint <= 0xFFFF else f"\\U{codepoint:08x}"
            )
        else:
            piece = char

        if length + len(piece) > limit:
            truncated = True
            break
        pieces.append(piece)
        length += len(piece)
    result = "".join(pieces)
    if truncated and limit >= 3:
        while pieces and length > limit - 3:
            length -= len(pieces.pop())
        result = "".join(pieces) + "..."
    return result


def _identity_text(value: object, _limit: Optional[int] = None) -> str:
    try:
        return str(value)
    except Exception:
        return "<unprintable>"


def _redact_path(
    message: object,
    path: Optional[Path],
    root: Path,
) -> str:
    text = _identity_text(message)
    if path is None:
        return text

    label = _path_label(path, root)
    candidates = {str(path)}
    resolved = _resolve_path(path)
    if resolved is not None:
        candidates.add(str(resolved))
    for candidate in sorted(candidates, key=len, reverse=True):
        if candidate:
            text = text.replace(candidate, label)
    return text
