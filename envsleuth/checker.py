"""Compare scanned env vars against an actual .env file."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from dotenv import dotenv_values

from envsleuth.scanner import EnvUsage, ScanResult


DEFAULT_ENV_FILE = ".env"
DEFAULT_ENVIGNORE_FILE = ".envignore"


@dataclass
class VarReport:
    """Per-variable status after comparing scan results with .env."""

    name: str
    present_in_env: bool
    has_default_in_code: bool
    usages: List[EnvUsage] = field(default_factory=list)
    ignored: bool = False

    @property
    def status(self) -> str:
        """One of: 'present', 'missing', 'default', 'ignored'."""
        if self.ignored:
            return "ignored"
        if self.present_in_env:
            return "present"
        if self.has_default_in_code:
            return "default"
        return "missing"


@dataclass
class CheckReport:
    """Full comparison report."""

    variables: List[VarReport] = field(default_factory=list)
    dynamic_usages: List[EnvUsage] = field(default_factory=list)
    env_file: Optional[Path] = None
    env_file_exists: bool = False
    extra_in_env: List[str] = field(default_factory=list)
    ignore_patterns: List[str] = field(default_factory=list)
    errors: List[Tuple[Path, str]] = field(default_factory=list)

    @property
    def missing(self) -> List[VarReport]:
        return [v for v in self.variables if v.status == "missing"]

    @property
    def present(self) -> List[VarReport]:
        return [v for v in self.variables if v.status == "present"]

    @property
    def with_default(self) -> List[VarReport]:
        return [v for v in self.variables if v.status == "default"]

    @property
    def ignored(self) -> List[VarReport]:
        return [v for v in self.variables if v.status == "ignored"]

    @property
    def has_issues(self) -> bool:
        """True if anything requires user attention (missing vars or scan errors)."""
        return bool(self.missing) or bool(self.errors)


# --------------------------------------------------------------------- helpers


def load_env_file(path: Path) -> Dict[str, Optional[str]]:
    if not path.exists():
        return {}
    return dict(dotenv_values(path))


def load_ignore_patterns(path: Path) -> List[str]:
    """Read .envignore — one glob pattern per line. Blank lines and '#' are ignored."""
    if not path.exists():
        return []
    patterns: List[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(line)
    return patterns


def _matches_any(name: str, patterns: List[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


# -------------------------------------------------------------------- main api


def check(
    scan: ScanResult,
    env_path: Path,
    ignore_patterns: Optional[List[str]] = None,
) -> CheckReport:
    """Compare scan results against the given .env file and return a report."""
    patterns = ignore_patterns or []
    env_values = load_env_file(env_path)
    env_keys = set(env_values.keys())

    by_name: Dict[str, List[EnvUsage]] = {}
    for u in scan.usages:
        if u.name is None:
            continue
        by_name.setdefault(u.name, []).append(u)

    variables: List[VarReport] = []
    for name in sorted(by_name):
        usages = by_name[name]
        has_default = any(u.has_default for u in usages)
        ignored = _matches_any(name, patterns)
        variables.append(
            VarReport(
                name=name,
                present_in_env=name in env_keys,
                has_default_in_code=has_default,
                usages=usages,
                ignored=ignored,
            )
        )

    code_names = set(by_name.keys())
    extra_in_env = sorted(env_keys - code_names)

    return CheckReport(
        variables=variables,
        dynamic_usages=scan.dynamic_usages,
        env_file=env_path,
        env_file_exists=env_path.exists(),
        extra_in_env=extra_in_env,
        ignore_patterns=patterns,
        errors=list(scan.errors),
    )


def find_nearby_env_files(root: Path) -> List[Path]:
    """Look for .env.* files in `root` to help the user when .env is missing."""
    if not root.is_dir():
        return []
    candidates: List[Path] = []
    for p in sorted(root.iterdir()):
        if not p.is_file():
            continue
        name = p.name
        if name == ".env" or name.startswith(".env."):
            candidates.append(p)
    return candidates
