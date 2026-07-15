"""Compare scanned env vars against an actual .env file."""

from __future__ import annotations

import ast
import fnmatch
import os
import stat
import string
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from dotenv import dotenv_values
from dotenv.parser import parse_stream

from envsleuth.scanner import EnvUsage, ScanResult


DEFAULT_ENV_FILE = ".env"
DEFAULT_ENVIGNORE_FILE = ".envignore"


class EnvFileParseError(OSError):
    """Raised when python-dotenv cannot parse a line in an environment file."""


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
        env_is_missing = self.env_file is not None and not self.env_file_exists
        return env_is_missing or bool(self.missing) or bool(self.errors)


# --------------------------------------------------------------------- helpers


def _parse_env_text(path: Path, text: str) -> Dict[str, Optional[str]]:
    stream = StringIO(text)
    for binding in parse_stream(stream):
        if binding.error:
            raise EnvFileParseError(
                f"{path}: invalid syntax at line {binding.original.line}"
            )
        key_has_nul = binding.key is not None and "\0" in binding.key
        value_has_nul = binding.value is not None and "\0" in binding.value
        if key_has_nul or value_has_nul:
            raise EnvFileParseError(
                f"{path}: NUL byte at line {binding.original.line}"
            )

    stream.seek(0)
    return dict(dotenv_values(stream=stream))


def _load_env_file_snapshot(
    path: Path,
) -> Tuple[bool, Dict[str, Optional[str]]]:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return False, {}

    try:
        file_info = os.fstat(descriptor)
        if not stat.S_ISREG(file_info.st_mode):
            raise OSError(f"{path}: not a regular file")
        with os.fdopen(descriptor, "r", encoding="utf-8-sig") as source:
            descriptor = -1
            text = source.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    return True, _parse_env_text(path, text)


def load_env_file(path: Path) -> Dict[str, Optional[str]]:
    return _load_env_file_snapshot(path)[1]


def load_ignore_patterns(path: Path, *, required: bool = False) -> List[str]:
    """Read .envignore — one glob pattern per line. Blank lines and '#' are ignored."""
    if not required and not path.is_file():
        return []
    patterns: List[str] = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(line)
    return patterns


def _env_names_case_sensitive() -> bool:
    return os.name != "nt"


def _name_key(name: str, case_sensitive: bool) -> str:
    return name if case_sensitive else name.casefold()


def _matches_any(
    name: str,
    patterns: List[str],
    case_sensitive: bool = True,
) -> bool:
    if case_sensitive:
        return any(fnmatch.fnmatchcase(name, p) for p in patterns)
    folded = name.casefold()
    return any(fnmatch.fnmatchcase(folded, p.casefold()) for p in patterns)


_DynamicPattern = Tuple[Optional[str], ...]
_DYNAMIC_ANCHOR_LENGTH = 8


@dataclass
class _PatternTrieNode:
    children: Dict[str, int] = field(default_factory=dict)
    patterns: List[_DynamicPattern] = field(default_factory=list)


@dataclass
class _DynamicPatternIndex:
    case_sensitive: bool
    prefixes: List[_PatternTrieNode] = field(
        default_factory=lambda: [_PatternTrieNode()]
    )
    suffixes: List[_PatternTrieNode] = field(
        default_factory=lambda: [_PatternTrieNode()]
    )
    contains: Dict[str, List[_DynamicPattern]] = field(default_factory=dict)
    contains_lengths: Set[int] = field(default_factory=set)


def _trie_add(
    nodes: List[_PatternTrieNode],
    text: str,
    pattern: _DynamicPattern,
) -> None:
    state = 0
    for char in text:
        child = nodes[state].children.get(char)
        if child is None:
            child = len(nodes)
            nodes[state].children[char] = child
            nodes.append(_PatternTrieNode())
        state = child
    nodes[state].patterns.append(pattern)


def _build_dynamic_pattern_index(
    patterns: List[List[Optional[str]]],
    case_sensitive: bool,
) -> _DynamicPatternIndex:
    index = _DynamicPatternIndex(case_sensitive=case_sensitive)
    unique: Set[_DynamicPattern] = set()
    for parts in patterns:
        normalized = tuple(
            part if case_sensitive or part is None else part.casefold()
            for part in parts
        )
        if normalized in unique:
            continue
        unique.add(normalized)

    candidates: Dict[_DynamicPattern, List[Tuple[str, str]]] = {}
    frequencies: Dict[Tuple[str, str], int] = {}
    for pattern in unique:
        anchors: List[Tuple[str, str]] = []
        last_index = len(pattern) - 1
        for position, literal in enumerate(pattern):
            if not literal:
                continue
            if position == 0:
                anchor = ("prefix", literal[:_DYNAMIC_ANCHOR_LENGTH])
            elif position == last_index:
                anchor = ("suffix", literal[-_DYNAMIC_ANCHOR_LENGTH:])
            else:
                anchor = ("contains", literal[:_DYNAMIC_ANCHOR_LENGTH])
            if anchor not in anchors:
                anchors.append(anchor)
        if not anchors:
            continue
        candidates[pattern] = anchors
        for anchor in anchors:
            frequencies[anchor] = frequencies.get(anchor, 0) + 1

    for pattern, anchors in candidates.items():
        kind, anchor = max(
            anchors,
            key=lambda item: (
                len(item[1]),
                -frequencies[item],
                item[0] != "contains",
                item[1],
            ),
        )
        if kind == "prefix":
            _trie_add(index.prefixes, anchor, pattern)
        elif kind == "suffix":
            _trie_add(index.suffixes, anchor[::-1], pattern)
        else:
            index.contains.setdefault(anchor, []).append(pattern)
            index.contains_lengths.add(len(anchor))
    return index


def _trie_matches(
    traversal_name: str,
    match_name: str,
    nodes: List[_PatternTrieNode],
) -> bool:
    state = 0
    for char in traversal_name:
        child = nodes[state].children.get(char)
        if child is None:
            return False
        state = child
        for parts in nodes[state].patterns:
            if _matches_dynamic_parts(match_name, list(parts), True):
                return True
    return False


def _looks_like_dynamic_match(name: str, index: _DynamicPatternIndex) -> bool:
    """Heuristic: does this env var name plausibly match any of the dynamic
    expressions we couldn't statically resolve?

    Common pattern: os.getenv(f"PREFIX_{x}") — we don't know x, but if the .env
    has PREFIX_FOO and PREFIX_BAR, those are probably the targets.
    """
    normalized = name if index.case_sensitive else name.casefold()
    if _trie_matches(normalized, normalized, index.prefixes):
        return True
    if _trie_matches(normalized[::-1], normalized, index.suffixes):
        return True

    for length in index.contains_lengths:
        seen_keys: Set[str] = set()
        for offset in range(len(normalized) - length + 1):
            probe = normalized[offset:offset + length]
            if probe in seen_keys or probe not in index.contains:
                continue
            seen_keys.add(probe)
            for parts in index.contains[probe]:
                if _matches_dynamic_parts(normalized, list(parts), True):
                    return True
    return False


def _dynamic_name_parts(expr: str) -> Optional[List[Optional[str]]]:
    try:
        node = ast.parse(expr, mode="eval").body
    except (SyntaxError, ValueError):
        return None

    parts = _dynamic_parts(node)
    literals = "".join(p for p in parts if p is not None)
    if len(literals) < 2 or not any(c.isalnum() for c in literals):
        return None
    return parts


def _matches_dynamic_parts(
    name: str,
    parts: List[Optional[str]],
    case_sensitive: bool,
) -> bool:
    """Match literal/dynamic pieces without regex backtracking."""
    if not case_sensitive:
        name = name.casefold()
        parts = [part.casefold() if part is not None else None for part in parts]

    position = 0
    last_index = len(parts) - 1
    for index, part in enumerate(parts):
        if part is None:
            continue

        if index == 0:
            if not name.startswith(part):
                return False
            position = len(part)
            continue

        if index == last_index:
            suffix_at = len(name) - len(part)
            return suffix_at >= position and name.endswith(part)

        found_at = name.find(part, position)
        if found_at < 0:
            return False
        position = found_at + len(part)

    return parts[-1] is None or position == len(name)


def _dynamic_parts(node: ast.AST) -> List[Optional[str]]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.FormattedValue):
        return [None]
    if isinstance(node, ast.JoinedStr):
        res: List[Optional[str]] = []
        for value in node.values:
            res.extend(_dynamic_parts(value))
        return _merge_dynamic_parts(res)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _merge_dynamic_parts(
            _dynamic_parts(node.left) + _dynamic_parts(node.right)
        )
    if (
        isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Mod)
        and isinstance(node.left, ast.Constant)
        and isinstance(node.left.value, str)
    ):
        return _percent_format_parts(node.left.value)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "format"
        and isinstance(node.func.value, ast.Constant)
        and isinstance(node.func.value.value, str)
    ):
        return _format_string_parts(node.func.value.value)
    return [None]


def _format_string_parts(template: str) -> List[Optional[str]]:
    parts: List[Optional[str]] = []
    try:
        parsed = string.Formatter().parse(template)
        for literal, field_name, _format_spec, _conversion in parsed:
            if literal:
                parts.append(literal)
            if field_name is not None:
                parts.append(None)
    except ValueError:
        return [None]
    return _merge_dynamic_parts(parts)


def _percent_format_parts(template: str) -> List[Optional[str]]:
    parts: List[Optional[str]] = []
    literal: List[str] = []
    index = 0

    def flush_literal() -> None:
        if literal:
            parts.append("".join(literal))
            literal.clear()

    while index < len(template):
        if template[index] != "%":
            literal.append(template[index])
            index += 1
            continue

        if index + 1 < len(template) and template[index + 1] == "%":
            literal.append("%")
            index += 2
            continue

        cursor = index + 1
        if cursor < len(template) and template[cursor] == "(":
            closing = template.find(")", cursor + 1)
            if closing < 0 or closing == cursor + 1:
                return [None]
            cursor = closing + 1

        while cursor < len(template) and template[cursor] in "#0- +":
            cursor += 1
        if cursor < len(template) and template[cursor] == "*":
            cursor += 1
        else:
            while cursor < len(template) and template[cursor].isdigit():
                cursor += 1
        if cursor < len(template) and template[cursor] == ".":
            cursor += 1
            if cursor < len(template) and template[cursor] == "*":
                cursor += 1
            else:
                while cursor < len(template) and template[cursor].isdigit():
                    cursor += 1
        if cursor < len(template) and template[cursor] in "hlL":
            cursor += 1
        if cursor >= len(template) or template[cursor] not in "diouxXeEfFgGcrsa":
            return [None]

        flush_literal()
        parts.append(None)
        index = cursor + 1

    flush_literal()
    return _merge_dynamic_parts(parts)


def _merge_dynamic_parts(parts: List[Optional[str]]) -> List[Optional[str]]:
    merged: List[Optional[str]] = []
    for part in parts:
        if part is None:
            if not merged or merged[-1] is not None:
                merged.append(None)
        elif part:
            if merged and merged[-1] is not None:
                merged[-1] += part
            else:
                merged.append(part)
    return merged or [None]


def _has_usable_default(usage: EnvUsage) -> bool:
    if not usage.has_default or usage.default_node is None:
        return False
    return not (
        isinstance(usage.default_node, ast.Constant)
        and usage.default_node.value is None
    )


# -------------------------------------------------------------------- main api


def check(
    scan: ScanResult,
    env_path: Path,
    ignore_patterns: Optional[List[str]] = None,
) -> CheckReport:
    """Compare scan results against the given .env file and return a report."""
    patterns = ignore_patterns or []
    case_sensitive = _env_names_case_sensitive()
    env_file_exists, env_values = _load_env_file_snapshot(env_path)
    env_names: Dict[str, str] = {}
    for name, value in env_values.items():
        # A bare `NAME` entry is ignored by load_dotenv, unlike `NAME=`.
        if value is not None:
            env_names[_name_key(name, case_sensitive)] = name
    env_keys = set(env_names)

    by_name: Dict[str, List[EnvUsage]] = {}
    for u in scan.usages:
        if u.name is None:
            continue
        by_name.setdefault(_name_key(u.name, case_sensitive), []).append(u)

    variables: List[VarReport] = []
    for name_key in sorted(by_name):
        usages = by_name[name_key]
        names = {u.name for u in usages if u.name is not None}
        name = sorted(names, key=lambda item: (item.casefold(), item))[0]
        # bug fix: was `any()` before — meant a single defaulted usage masked
        # other call sites that would actually crash on missing var. now we
        # only call it 'has default' if every usage provides one.
        has_default = bool(usages) and all(_has_usable_default(u) for u in usages)
        ignored = _matches_any(name, patterns, case_sensitive)
        variables.append(
            VarReport(
                name=name,
                present_in_env=name_key in env_keys,
                has_default_in_code=has_default,
                usages=usages,
                ignored=ignored,
            )
        )

    code_names = set(by_name.keys())
    dynamic_patterns = [
        parts
        for usage in scan.dynamic_usages
        if (parts := _dynamic_name_parts(usage.raw_expr or "")) is not None
    ]
    dynamic_index = _build_dynamic_pattern_index(
        dynamic_patterns, case_sensitive
    )
    # if user told us to ignore TEST_*, they probably don't want TEST_FOO in .env
    # showing up as "unused" either
    extras_raw = env_keys - code_names
    extra_in_env = sorted(
        env_names[n] for n in extras_raw
        if not _matches_any(env_names[n], patterns, case_sensitive)
        and not _looks_like_dynamic_match(env_names[n], dynamic_index)
    )

    return CheckReport(
        variables=variables,
        dynamic_usages=scan.dynamic_usages,
        env_file=env_path,
        env_file_exists=env_file_exists,
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
