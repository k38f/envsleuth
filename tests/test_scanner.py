"""Tests for envsleuth.scanner."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from envsleuth.scanner import (
    DEFAULT_EXCLUDES,
    ScanError,
    iter_python_files,
    scan_file,
    scan_project,
)


def write(tmp_path: Path, name: str, source: str) -> Path:
    """Write source code to a file and return the path."""
    f = tmp_path / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(textwrap.dedent(source), encoding="utf-8")
    return f


# ============================================================ basic patterns


def test_finds_os_getenv(tmp_path: Path) -> None:
    f = write(tmp_path, "a.py", """
        import os
        x = os.getenv("DATABASE_URL")
    """)
    usages = scan_file(f)
    assert len(usages) == 1
    assert usages[0].name == "DATABASE_URL"
    assert usages[0].call_type == "getenv"
    assert usages[0].has_default is False


def test_finds_os_environ_subscript(tmp_path: Path) -> None:
    f = write(tmp_path, "a.py", """
        import os
        x = os.environ["SECRET_KEY"]
    """)
    usages = scan_file(f)
    assert len(usages) == 1
    assert usages[0].name == "SECRET_KEY"
    assert usages[0].call_type == "environ_subscript"
    assert usages[0].has_default is False


def test_finds_os_environ_get(tmp_path: Path) -> None:
    f = write(tmp_path, "a.py", """
        import os
        x = os.environ.get("DEBUG")
    """)
    usages = scan_file(f)
    assert len(usages) == 1
    assert usages[0].name == "DEBUG"
    assert usages[0].call_type == "environ_get"


# =================================================================== defaults


def test_getenv_with_default(tmp_path: Path) -> None:
    f = write(tmp_path, "a.py", """
        import os
        x = os.getenv("PORT", "8000")
    """)
    usages = scan_file(f)
    assert usages[0].has_default is True


def test_environ_get_with_default(tmp_path: Path) -> None:
    f = write(tmp_path, "a.py", """
        import os
        x = os.environ.get("HOST", "localhost")
    """)
    usages = scan_file(f)
    assert usages[0].has_default is True


def test_environ_subscript_never_has_default(tmp_path: Path) -> None:
    """Subscript syntax raises KeyError — no default possible."""
    f = write(tmp_path, "a.py", """
        import os
        x = os.environ["X"]
    """)
    usages = scan_file(f)
    assert usages[0].has_default is False


def test_getenv_with_none_default(tmp_path: Path) -> None:
    """Passing None explicitly still counts as 'has_default'."""
    f = write(tmp_path, "a.py", """
        import os
        x = os.getenv("X", None)
    """)
    usages = scan_file(f)
    assert usages[0].has_default is True


# =============================================================== dynamic names


def test_dynamic_name_from_variable(tmp_path: Path) -> None:
    f = write(tmp_path, "a.py", """
        import os
        var_name = "DATABASE_URL"
        x = os.getenv(var_name)
    """)
    usages = scan_file(f)
    assert len(usages) == 1
    assert usages[0].is_dynamic
    assert usages[0].name is None
    assert usages[0].raw_expr == "var_name"


def test_dynamic_name_from_fstring(tmp_path: Path) -> None:
    f = write(tmp_path, "a.py", """
        import os
        prefix = "DB"
        x = os.getenv(f"{prefix}_URL")
    """)
    usages = scan_file(f)
    assert usages[0].is_dynamic


def test_dynamic_name_in_subscript(tmp_path: Path) -> None:
    f = write(tmp_path, "a.py", """
        import os
        key = "FOO"
        x = os.environ[key]
    """)
    usages = scan_file(f)
    assert len(usages) == 1
    assert usages[0].is_dynamic


# ================================================================ alias forms


def test_import_os_as_alias(tmp_path: Path) -> None:
    f = write(tmp_path, "a.py", """
        import os as operating_system
        x = operating_system.getenv("X")
        y = operating_system.environ["Y"]
    """)
    usages = scan_file(f)
    names = {u.name for u in usages}
    assert names == {"X", "Y"}


def test_from_os_import_getenv(tmp_path: Path) -> None:
    f = write(tmp_path, "a.py", """
        from os import getenv
        x = getenv("A")
        y = getenv("B", "default")
    """)
    usages = scan_file(f)
    names = {u.name for u in usages}
    assert names == {"A", "B"}
    b_usage = next(u for u in usages if u.name == "B")
    assert b_usage.has_default is True


def test_from_os_import_environ(tmp_path: Path) -> None:
    f = write(tmp_path, "a.py", """
        from os import environ
        x = environ["A"]
        y = environ.get("B")
    """)
    usages = scan_file(f)
    assert {u.name for u in usages} == {"A", "B"}


def test_from_os_import_getenv_as_alias(tmp_path: Path) -> None:
    f = write(tmp_path, "a.py", """
        from os import getenv as ge
        x = ge("FOO")
    """)
    usages = scan_file(f)
    assert len(usages) == 1
    assert usages[0].name == "FOO"


# ==================================================================== negatives


def test_does_not_match_unrelated_getenv(tmp_path: Path) -> None:
    """Someone else's .getenv() (not from os) should not be matched."""
    f = write(tmp_path, "a.py", """
        class Config:
            def getenv(self, k):
                return None
        c = Config()
        x = c.getenv("NOT_ENV")
    """)
    usages = scan_file(f)
    assert usages == []


def test_does_not_match_unrelated_environ(tmp_path: Path) -> None:
    f = write(tmp_path, "a.py", """
        environ = {"X": "1"}  # shadowed, not from os
        x = environ["X"]
    """)
    usages = scan_file(f)
    assert usages == []


def test_does_not_match_without_import(tmp_path: Path) -> None:
    """If os isn't imported, `os.getenv(...)` is someone else's symbol."""
    f = write(tmp_path, "a.py", """
        os = SomeFakeModule()
        x = os.getenv("X")
    """)
    usages = scan_file(f)
    assert usages == []


def test_getenv_no_args_is_ignored(tmp_path: Path) -> None:
    f = write(tmp_path, "a.py", """
        import os
        os.getenv()
    """)
    usages = scan_file(f)
    assert usages == []


# =================================================================== metadata


def test_line_numbers_are_tracked(tmp_path: Path) -> None:
    f = write(tmp_path, "a.py", """
        import os

        x = os.getenv("A")
        y = os.getenv("B")
    """)
    usages = scan_file(f)
    by_name = {u.name: u.line for u in usages}
    # After dedent the file starts with a blank line from the triple-quoted string,
    # so line 1 is blank, line 2 is "import os", line 3 is blank, line 4 has A.
    assert by_name["A"] == 4
    assert by_name["B"] == 5


def test_file_path_is_recorded(tmp_path: Path) -> None:
    f = write(tmp_path, "some/nested/module.py", """
        import os
        x = os.getenv("X")
    """)
    usages = scan_file(f)
    assert usages[0].file == f


# ============================================================= ScanResult api


def test_static_names_deduplicates(tmp_path: Path) -> None:
    f = write(tmp_path, "a.py", """
        import os
        a = os.getenv("DUP")
        b = os.getenv("DUP")
        c = os.getenv("UNIQUE")
    """)
    result = scan_project(tmp_path)
    assert result.static_names == {"DUP", "UNIQUE"}


def test_names_with_defaults(tmp_path: Path) -> None:
    f = write(tmp_path, "a.py", """
        import os
        a = os.getenv("WITH_DEF", "x")
        b = os.getenv("NO_DEF")
    """)
    result = scan_project(tmp_path)
    assert result.names_with_defaults == {"WITH_DEF"}


def test_dynamic_usages_isolated(tmp_path: Path) -> None:
    f = write(tmp_path, "a.py", """
        import os
        a = os.getenv("STATIC")
        name = "X"
        b = os.getenv(name)
    """)
    result = scan_project(tmp_path)
    assert len(result.dynamic_usages) == 1
    assert result.static_names == {"STATIC"}


# ============================================================= project walking


def test_scan_project_recursive(tmp_path: Path) -> None:
    write(tmp_path, "a.py", "import os\nos.getenv('A')\n")
    write(tmp_path, "sub/b.py", "import os\nos.getenv('B')\n")
    write(tmp_path, "sub/deep/c.py", "import os\nos.getenv('C')\n")

    result = scan_project(tmp_path)
    assert result.static_names == {"A", "B", "C"}


def test_scan_project_excludes_default_dirs(tmp_path: Path) -> None:
    write(tmp_path, "good.py", "import os\nos.getenv('GOOD')\n")
    write(tmp_path, "venv/bad.py", "import os\nos.getenv('BAD1')\n")
    write(tmp_path, ".venv/bad.py", "import os\nos.getenv('BAD2')\n")
    write(tmp_path, "node_modules/bad.py", "import os\nos.getenv('BAD3')\n")
    write(tmp_path, "__pycache__/bad.py", "import os\nos.getenv('BAD4')\n")

    result = scan_project(tmp_path)
    assert result.static_names == {"GOOD"}


def test_scan_project_custom_excludes(tmp_path: Path) -> None:
    write(tmp_path, "src/good.py", "import os\nos.getenv('GOOD')\n")
    write(tmp_path, "migrations/skip.py", "import os\nos.getenv('SKIP')\n")

    result = scan_project(tmp_path, extra_excludes={"migrations"})
    assert result.static_names == {"GOOD"}


def test_scan_project_custom_extensions(tmp_path: Path) -> None:
    write(tmp_path, "a.py", "import os\nos.getenv('PY')\n")
    write(tmp_path, "b.pyi", "import os\nos.getenv('PYI')\n")

    result = scan_project(tmp_path, extensions={".py", ".pyi"})
    assert result.static_names == {"PY", "PYI"}


def test_scan_project_handles_syntax_errors(tmp_path: Path) -> None:
    write(tmp_path, "good.py", "import os\nos.getenv('GOOD')\n")
    write(tmp_path, "broken.py", "this is not valid python !!!")

    result = scan_project(tmp_path)
    assert result.static_names == {"GOOD"}
    assert len(result.errors) == 1
    assert result.errors[0][0].name == "broken.py"


def test_on_file_callback(tmp_path: Path) -> None:
    write(tmp_path, "a.py", "import os\nos.getenv('A')\n")
    write(tmp_path, "b.py", "import os\nos.getenv('B')\n")

    seen = []
    scan_project(tmp_path, on_file=lambda f: seen.append(f))
    assert len(seen) == 2


def test_default_excludes_set_is_frozen() -> None:
    """Sanity check — make sure common dirs are excluded by default."""
    for expected in ["venv", ".venv", "__pycache__", "node_modules", ".git"]:
        assert expected in DEFAULT_EXCLUDES
