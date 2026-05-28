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


# ============================================================== regression tests


def test_scan_works_when_root_path_contains_excluded_dir_name(tmp_path: Path) -> None:
    # bug: project sitting under ~/.venv/foo skipped itself because '.venv' is
    # in path.parts. now we only check parts relative to root.
    project = tmp_path / ".venv" / "myproj"
    write(project, "main.py", "import os\nos.getenv('NESTED')\n")
    result = scan_project(project)
    assert result.static_names == {"NESTED"}


def test_scan_still_excludes_nested_excluded_dirs(tmp_path: Path) -> None:
    # make sure the relative-parts fix didn't break the original behavior
    write(tmp_path, "good.py", "import os\nos.getenv('GOOD')\n")
    write(tmp_path, "venv/bad.py", "import os\nos.getenv('BAD')\n")
    result = scan_project(tmp_path)
    assert result.static_names == {"GOOD"}


def test_utf8_bom_is_handled(tmp_path: Path) -> None:
    # Windows Notepad saves utf-8 with BOM by default. before the fix this
    # raised SyntaxError on the U+FEFF character.
    p = tmp_path / "bom.py"
    p.write_bytes(b"\xef\xbb\xbfimport os\nos.getenv('FROM_BOM')\n")
    usages = scan_file(p)
    assert len(usages) == 1
    assert usages[0].name == "FROM_BOM"


def test_oversized_file_is_skipped_with_error(tmp_path: Path) -> None:
    # vendored libs and minified .py files can be huge — don't bother parsing.
    # we report it as a scan error so the user knows we skipped it.
    from envsleuth.scanner import MAX_FILE_SIZE
    p = tmp_path / "huge.py"
    # write something just over the limit. need real bytes, not just zeros
    # because read might still work on sparse files
    p.write_bytes(b"x = 1\n" * ((MAX_FILE_SIZE // 6) + 100))
    result = scan_project(tmp_path)
    assert result.usages == []
    assert len(result.errors) == 1
    assert "larger than" in result.errors[0][1]


# ============================================================== scope handling


def test_alias_in_function_doesnt_leak_outside(tmp_path: Path) -> None:
    # before scope-awareness: ge from inside f() was treated as global,
    # so a stray ge("OUTSIDE") would also be matched (it's actually NameError)
    f = write(tmp_path, "a.py", """
        def f():
            from os import getenv as ge
            ge("INSIDE")

        ge("OUTSIDE")
    """)
    usages = scan_file(f)
    names = {u.name for u in usages}
    assert names == {"INSIDE"}


def test_inner_function_sees_outer_import(tmp_path: Path) -> None:
    # this is normal lexical scoping — inner can see outer
    f = write(tmp_path, "a.py", """
        import os

        def outer():
            def inner():
                return os.getenv("STILL_VISIBLE")
            return inner()
    """)
    usages = scan_file(f)
    assert {u.name for u in usages} == {"STILL_VISIBLE"}


def test_class_body_import_doesnt_leak_to_methods(tmp_path: Path) -> None:
    # this would actually NameError at runtime — class body imports don't
    # carry into methods. our scope handling now matches that.
    f = write(tmp_path, "a.py", """
        class Foo:
            from os import getenv
            X = getenv("CLASS_BODY")  # this works in class body

            def method(self):
                return getenv("BROKEN_AT_RUNTIME")  # NameError
    """)
    usages = scan_file(f)
    names = {u.name for u in usages}
    # CLASS_BODY is detected because class body sees the import.
    # BROKEN_AT_RUNTIME is NOT detected because it's actually broken Python.
    assert "CLASS_BODY" in names
    assert "BROKEN_AT_RUNTIME" not in names


def test_default_node_is_attached(tmp_path: Path) -> None:
    # generator needs default_node to render literal defaults without re-parsing
    f = write(tmp_path, "a.py", """
        import os
        os.getenv("PORT", "8000")
        os.getenv("HOST", default="localhost")
        os.getenv("NO_DEFAULT")
    """)
    usages = scan_file(f)
    by_name = {u.name: u for u in usages}
    assert by_name["PORT"].default_node is not None
    assert by_name["HOST"].default_node is not None
    assert by_name["NO_DEFAULT"].default_node is None
    # and has_default still works for keyword form
    assert by_name["HOST"].has_default is True


# ============================================================== django-environ


def test_django_environ_basic(tmp_path: Path) -> None:
    f = write(tmp_path, "settings.py", """
        import environ
        env = environ.Env()
        SECRET = env('SECRET_KEY')
        DEBUG = env.bool('DEBUG', default=False)
        PORT = env.int('PORT', default=8000)
        HOSTS = env.list('ALLOWED_HOSTS', default=[])
    """)
    usages = scan_file(f)
    names = {u.name for u in usages}
    assert names == {"SECRET_KEY", "DEBUG", "PORT", "ALLOWED_HOSTS"}

    by_name = {u.name: u for u in usages}
    assert by_name["SECRET_KEY"].call_type == "django_environ"
    assert by_name["DEBUG"].call_type == "django_environ.bool"
    assert by_name["PORT"].call_type == "django_environ.int"
    assert by_name["HOSTS" if "HOSTS" in by_name else "ALLOWED_HOSTS"].call_type == "django_environ.list"


def test_django_environ_db_helpers(tmp_path: Path) -> None:
    # env.db() / env.cache() / env.url() also take a var name as first arg
    f = write(tmp_path, "settings.py", """
        import environ
        env = environ.Env()
        DATABASES = {'default': env.db('DATABASE_URL')}
        CACHES = {'default': env.cache('REDIS_URL')}
    """)
    usages = scan_file(f)
    assert {u.name for u in usages} == {"DATABASE_URL", "REDIS_URL"}


def test_django_environ_aliased_module(tmp_path: Path) -> None:
    # `import environ as envlib` is unusual but valid
    f = write(tmp_path, "settings.py", """
        import environ as envlib
        env = envlib.Env()
        x = env('X')
    """)
    usages = scan_file(f)
    assert {u.name for u in usages} == {"X"}


def test_django_environ_multiple_instances(tmp_path: Path) -> None:
    # some projects use separate Env instances for different scopes
    f = write(tmp_path, "settings.py", """
        import environ
        env = environ.Env()
        env_dev = environ.Env()
        A = env('FROM_MAIN')
        B = env_dev('FROM_DEV')
    """)
    usages = scan_file(f)
    assert {u.name for u in usages} == {"FROM_MAIN", "FROM_DEV"}


def test_django_environ_only_tracked_after_assignment(tmp_path: Path) -> None:
    # without `env = environ.Env(...)`, a bare env(...) call shouldn't match
    f = write(tmp_path, "a.py", """
        import environ
        # never assigns environ.Env(), just imports — this env() is something else
        env = lambda x: x
        env('NOT_AN_ENV_VAR')
    """)
    usages = scan_file(f)
    assert usages == []  # `env` is not an Env instance


# ============================================================== python-decouple


def test_decouple_basic(tmp_path: Path) -> None:
    f = write(tmp_path, "conf.py", """
        from decouple import config, Csv
        SECRET = config('SECRET_KEY')
        DEBUG = config('DEBUG', default=False, cast=bool)
        HOSTS = config('ALLOWED_HOSTS', cast=Csv())
    """)
    usages = scan_file(f)
    names = {u.name for u in usages}
    assert names == {"SECRET_KEY", "DEBUG", "ALLOWED_HOSTS"}
    for u in usages:
        assert u.call_type == "decouple_config"


def test_decouple_aliased(tmp_path: Path) -> None:
    f = write(tmp_path, "conf.py", """
        from decouple import config as cfg
        KEY = cfg('SECRET_KEY')
    """)
    usages = scan_file(f)
    assert [u.name for u in usages] == ["SECRET_KEY"]


def test_decouple_default_extraction(tmp_path: Path) -> None:
    # default= keyword should be picked up just like os.getenv
    f = write(tmp_path, "a.py", """
        from decouple import config
        config('PORT', default='8000')
    """)
    usages = scan_file(f)
    assert usages[0].has_default is True
    assert usages[0].default_node is not None


def test_decouple_scoped_to_function(tmp_path: Path) -> None:
    # alias from inside a function shouldn't leak — same scope rules as os imports
    f = write(tmp_path, "a.py", """
        def f():
            from decouple import config
            config('INSIDE')

        config('OUTSIDE')  # NameError in real python
    """)
    usages = scan_file(f)
    assert [u.name for u in usages] == ["INSIDE"]


# ============================================================== v0.2 regressions


def test_has_default_only_true_for_actual_default_kw(tmp_path: Path) -> None:
    # critical bug fix: any keyword arg used to set has_default=True. now we
    # check that only `default=` (or args[1]) counts. cast=, subcast=, etc. don't.
    f = write(tmp_path, "a.py", """
        from decouple import config
        config('R', cast=bool)
        config('H', default='x', cast=bool)
        config('P', 'x')
        config('N')
    """)
    by_name = {u.name: u for u in scan_file(f)}
    assert by_name["R"].has_default is False, "cast=bool alone is NOT a default"
    assert by_name["H"].has_default is True
    assert by_name["P"].has_default is True  # positional default still works
    assert by_name["N"].has_default is False


def test_has_default_for_django_env_methods(tmp_path: Path) -> None:
    # same bug applies to env.list('X', subcast=str) — REQUIRED, no default
    f = write(tmp_path, "a.py", """
        import environ
        env = environ.Env()
        REQUIRED = env.list('R', subcast=str)
        WITH_DEFAULT = env.list('W', default=[])
    """)
    by_name = {u.name: u for u in scan_file(f)}
    assert by_name["R"].has_default is False
    assert by_name["W"].has_default is True


def test_django_environ_from_environ_import_env(tmp_path: Path) -> None:
    # bug fix: `from environ import Env; env = Env(...)` is a documented pattern
    # in django-environ tutorials. used to return zero usages.
    f = write(tmp_path, "settings.py", """
        from environ import Env
        env = Env()
        SECRET = env('SECRET_KEY')
        DEBUG = env.bool('DEBUG', default=False)
    """)
    usages = scan_file(f)
    names = {u.name for u in usages}
    assert names == {"SECRET_KEY", "DEBUG"}


def test_django_environ_from_environ_with_alias(tmp_path: Path) -> None:
    f = write(tmp_path, "a.py", """
        from environ import Env as Env_
        env = Env_()
        X = env('X')
    """)
    usages = scan_file(f)
    assert {u.name for u in usages} == {"X"}


# ============================================================== mixed
