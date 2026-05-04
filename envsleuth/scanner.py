"""AST-based scanner for environment variable usages in Python code.

Detects three patterns:
    os.getenv("VAR")              -> static usage
    os.getenv("VAR", "default")   -> static usage with default
    os.environ["VAR"]             -> static usage
    os.environ.get("VAR")         -> static usage
    os.environ.get("VAR", "def")  -> static usage with default
    os.getenv(var_name)           -> dynamic usage (can't resolve)
    os.getenv(f"PREFIX_{x}")      -> dynamic usage (can't resolve)

The scanner also tracks aliased imports:
    from os import getenv; getenv("X")            -> detected
    from os import environ; environ["X"]          -> detected
    import os as operating_system                  -> detected
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Set, Tuple


@dataclass
class EnvUsage:
    """A single occurrence of an env var lookup in source code."""

    name: Optional[str]
    """Variable name if it could be resolved statically, else None."""

    file: Path
    """File where the usage was found."""

    line: int
    """1-based line number."""

    has_default: bool = False
    """True if a default value was provided (e.g. os.getenv('X', 'fallback'))."""

    call_type: str = "getenv"
    """One of: 'getenv', 'environ_subscript', 'environ_get'."""

    raw_expr: Optional[str] = None
    """For dynamic usages: text of the unresolved expression (for diagnostics)."""

    default_node: Optional[ast.AST] = None
    """The AST node of the default value, when has_default is True. Lets the
    generator pull literal defaults without re-parsing the file."""

    @property
    def is_dynamic(self) -> bool:
        return self.name is None


@dataclass
class ScanResult:
    """Aggregated result of scanning one or more files."""

    usages: List[EnvUsage] = field(default_factory=list)
    scanned_files: List[Path] = field(default_factory=list)
    errors: List[Tuple[Path, str]] = field(default_factory=list)

    @property
    def static_names(self) -> Set[str]:
        """Unique set of statically-resolved variable names."""
        return {u.name for u in self.usages if u.name is not None}

    @property
    def dynamic_usages(self) -> List[EnvUsage]:
        return [u for u in self.usages if u.is_dynamic]

    @property
    def names_with_defaults(self) -> Set[str]:
        """Names that have at least one usage with a default value provided."""
        return {u.name for u in self.usages if u.name is not None and u.has_default}


class ScanError(Exception):
    """Raised when a single file cannot be scanned."""


class _EnvVisitor(ast.NodeVisitor):
    """AST visitor that collects env var usages from a single module."""

    def __init__(self, file_path: Path) -> None:
        self.file_path = file_path
        self.usages: List[EnvUsage] = []

        # Scope stack: each entry has its own alias sets. Nested scopes can see
        # outer ones (lexical scoping), but outer scopes can't see inner.
        # That way `def f(): from os import getenv as ge; ge("X")` doesn't
        # make a stray `ge("Y")` outside the function look like an env usage.
        # Each entry: {"kind": "module"|"function"|"class", "os":..., ...}
        self.scopes: List[dict] = [self._fresh_scope("module")]

    @staticmethod
    def _fresh_scope(kind: str) -> dict:
        return {"kind": kind, "os": set(), "getenv": set(), "environ": set()}

    def _push_scope(self, kind: str) -> None:
        self.scopes.append(self._fresh_scope(kind))

    def _pop_scope(self) -> None:
        self.scopes.pop()

    def _all_aliases(self, kind: str) -> Set[str]:
        # python scoping: function bodies don't see class-level names from
        # enclosing classes (only module + enclosing functions). but a call
        # site inside a class body itself does see the class's own imports.
        top = self.scopes[-1]
        out: Set[str] = set()
        if top["kind"] == "class":
            # class body — see everything in the chain
            for s in self.scopes:
                out |= s[kind]
        else:
            # function or module — skip class scopes (they don't leak into methods)
            for s in self.scopes:
                if s["kind"] == "class":
                    continue
                out |= s[kind]
        return out

    # -------------------------------------------------------------- scoping

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._push_scope("function")
        try:
            self.generic_visit(node)
        finally:
            self._pop_scope()

    def visit_AsyncFunctionDef(self, node) -> None:
        self._push_scope("function")
        try:
            self.generic_visit(node)
        finally:
            self._pop_scope()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        # class bodies have their own namespace. `import os` in a class body
        # doesn't leak into methods (those would NameError). pushing a scope
        # mirrors that.
        self._push_scope("class")
        try:
            self.generic_visit(node)
        finally:
            self._pop_scope()

    # ------------------------------------------------------------------ imports

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name == "os":
                self.scopes[-1]["os"].add(alias.asname or "os")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module == "os":
            for alias in node.names:
                bound = alias.asname or alias.name
                if alias.name == "getenv":
                    self.scopes[-1]["getenv"].add(bound)
                elif alias.name == "environ":
                    self.scopes[-1]["environ"].add(bound)
        self.generic_visit(node)

    # ------------------------------------------------------------------- calls

    def visit_Call(self, node: ast.Call) -> None:
        # order matters here — environ.get is also a Call so we check getenv first
        if self._is_getenv_call(node):
            self._record_call(node, call_type="getenv")
        elif self._is_environ_get_call(node):
            self._record_call(node, call_type="environ_get")
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if self._is_environ_subscript(node):
            self._record_subscript(node)
        self.generic_visit(node)

    # ---------------------------------------------------------------- matchers

    def _is_getenv_call(self, node: ast.Call) -> bool:
        func = node.func
        # os.getenv(...)
        if isinstance(func, ast.Attribute) and func.attr == "getenv":
            return isinstance(func.value, ast.Name) and func.value.id in self._all_aliases("os")
        # getenv(...) via `from os import getenv`
        if isinstance(func, ast.Name):
            return func.id in self._all_aliases("getenv")
        return False

    def _is_environ_get_call(self, node: ast.Call) -> bool:
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "get":
            return False
        inner = func.value
        # os.environ.get(...)
        if isinstance(inner, ast.Attribute) and inner.attr == "environ":
            return isinstance(inner.value, ast.Name) and inner.value.id in self._all_aliases("os")
        # environ.get(...) via `from os import environ`
        if isinstance(inner, ast.Name) and inner.id in self._all_aliases("environ"):
            return True
        return False

    def _is_environ_subscript(self, node: ast.Subscript) -> bool:
        value = node.value
        # os.environ["X"]
        if isinstance(value, ast.Attribute) and value.attr == "environ":
            return isinstance(value.value, ast.Name) and value.value.id in self._all_aliases("os")
        # environ["X"] via `from os import environ`
        if isinstance(value, ast.Name) and value.id in self._all_aliases("environ"):
            return True
        return False

    # ----------------------------------------------------------------- recording

    def _record_call(self, node: ast.Call, call_type: str) -> None:
        if not node.args:
            return
        name_arg = node.args[0]

        # find default arg — could be positional (args[1]) or keyword (default=...)
        default_node: Optional[ast.AST] = None
        if len(node.args) >= 2:
            default_node = node.args[1]
        else:
            for kw in node.keywords:
                if kw.arg == "default":
                    default_node = kw.value
                    break

        has_default = default_node is not None or bool(node.keywords)
        name = _extract_string(name_arg)
        self.usages.append(
            EnvUsage(
                name=name,
                file=self.file_path,
                line=node.lineno,
                has_default=has_default,
                call_type=call_type,
                raw_expr=None if name is not None else _unparse(name_arg),
                default_node=default_node,
            )
        )

    def _record_subscript(self, node: ast.Subscript) -> None:
        key_node = _subscript_key(node)
        name = _extract_string(key_node) if key_node is not None else None
        raw = None if name is not None else (_unparse(key_node) if key_node is not None else "?")
        self.usages.append(
            EnvUsage(
                name=name,
                file=self.file_path,
                line=node.lineno,
                has_default=False,
                call_type="environ_subscript",
                raw_expr=raw,
            )
        )


# ------------------------------------------------------------------ helpers


def _extract_string(node: Optional[ast.AST]) -> Optional[str]:
    """Return the string value if node is a constant string literal, else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _subscript_key(node: ast.Subscript) -> Optional[ast.AST]:
    """Get the index node from a Subscript, handling py3.8 ast.Index wrapper."""
    key = node.slice
    # On Python 3.8, Subscript.slice is an ast.Index wrapping the actual node.
    Index = getattr(ast, "Index", None)
    if Index is not None and isinstance(key, Index):
        return key.value  # type: ignore[attr-defined]
    return key


def _unparse(node: Optional[ast.AST]) -> str:
    """Best-effort textual representation of an AST node for diagnostics."""
    if node is None:
        return "?"
    unparse = getattr(ast, "unparse", None)
    if unparse is not None:
        try:
            return unparse(node)
        except Exception:
            pass
    return f"<expr at line {getattr(node, 'lineno', '?')}>"


# ----------------------------------------------------------------- public api


def scan_file(path: Path) -> List[EnvUsage]:
    # bail out early on huge files — typically vendored libs or minified output
    # 2MB is generous for a real source file
    try:
        if path.stat().st_size > MAX_FILE_SIZE:
            raise ScanError(f"skipped {path}: file is larger than {MAX_FILE_SIZE} bytes")
    except OSError as exc:
        raise ScanError(f"could not stat {path}: {exc}") from exc

    try:
        # utf-8-sig handles files saved with BOM (Notepad does this by default
        # on Windows). Plain utf-8 files still read fine since utf-8-sig is a superset.
        source = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise ScanError(f"could not read {path}: {exc}") from exc
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        # might break on files with encoding declaratrions in weird places, haven't hit it yet
        raise ScanError(f"syntax error in {path}: {exc}") from exc
    visitor = _EnvVisitor(path)
    visitor.visit(tree)
    return visitor.usages


# Default directories to skip when walking a project tree.
DEFAULT_EXCLUDES = frozenset({
    "venv", ".venv", "env",
    "__pycache__", ".git", ".hg", ".svn",
    "node_modules",
    ".tox", ".nox",
    "build", "dist",
    ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".eggs",
})

DEFAULT_EXTENSIONS = frozenset({".py"})

MAX_FILE_SIZE = 2 * 1024 * 1024  # 2MB


def iter_python_files(
    root: Path,
    extensions: Optional[Set[str]] = None,
    extra_excludes: Optional[Set[str]] = None,
) -> List[Path]:
    """Walk `root` and yield all files matching the given extensions.

    Directories named in DEFAULT_EXCLUDES (plus any in extra_excludes) are skipped.
    """
    exts = extensions or DEFAULT_EXTENSIONS
    excludes = set(DEFAULT_EXCLUDES)
    if extra_excludes:
        excludes |= set(extra_excludes)

    files: List[Path] = []
    if root.is_file():
        if root.suffix in exts:
            files.append(root)
        return files

    for path in root.rglob("*"):
        # check parts relative to root, not absolute — otherwise a project sitting
        # inside ~/.venv/foo/ skips itself because '.venv' is in path.parts
        try:
            rel_parts = path.relative_to(root).parts
        except ValueError:
            # shouldn't normally happen since path comes from root.rglob, but
            # symlinks pointing outside the tree can do this
            rel_parts = path.parts
        if any(part in excludes for part in rel_parts):
            continue
        if path.is_file() and path.suffix in exts:
            files.append(path)
    return sorted(files)


def scan_project(
    root: Path,
    extensions: Optional[Set[str]] = None,
    extra_excludes: Optional[Set[str]] = None,
    on_file: Optional[Callable[[Path], None]] = None,
) -> ScanResult:
    """Scan every Python file under `root` and return aggregated results.

    `on_file` is an optional callback invoked after each file is processed
    (useful for wiring up a progress bar from the CLI layer).
    """
    result = ScanResult()
    files = iter_python_files(root, extensions=extensions, extra_excludes=extra_excludes)
    result.scanned_files = files

    for f in files:
        try:
            result.usages.extend(scan_file(f))
        except ScanError as exc:
            result.errors.append((f, str(exc)))
        if on_file is not None:
            on_file(f)

    return result
