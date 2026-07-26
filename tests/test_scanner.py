"""Tests for envsleuth.scanner."""

from __future__ import annotations

import ast
import sys
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


def test_scan_project_excludes_virtualenv_with_a_custom_name(
    tmp_path: Path,
) -> None:
    write(tmp_path, "good.py", "import os\nos.getenv('GOOD')\n")
    write(tmp_path, "runtime/pyvenv.cfg", "home = /usr/bin\n")
    write(
        tmp_path, "runtime/lib/site-packages/dependency.py",
        "import os\nos.getenv('FROM_DEPENDENCY')\n",
    )

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


# ======================================================== scanner bugcheck


def test_environ_writes_and_deletes_are_not_reads(tmp_path: Path) -> None:
    f = write(tmp_path, "writes.py", """
        import os

        os.environ["WRITE_ONLY"] = "value"
        del os.environ["DELETE_ONLY"]
        os.environ["READ_AND_WRITE"] += "suffix"
        value = os.environ["READ_ONLY"]
    """)

    usages = scan_file(f)
    assert [usage.name for usage in usages] == ["READ_AND_WRITE", "READ_ONLY"]


def test_keyword_variable_names_are_supported(tmp_path: Path) -> None:
    f = write(tmp_path, "keywords.py", """
        import os
        import environ
        from os import getenv as ge
        from decouple import config

        env = environ.Env()
        os.getenv(key="OS_KEY")
        os.environ.get(key="MAPPING_KEY", default="fallback")
        ge(key="ALIASED_KEY")
        config(option="DECOUPLE_OPTION", default="x")
        env(var="DJANGO_VAR", cast=int)
        env.bool(var="DJANGO_BOOL", default=False)
    """)

    by_name = {usage.name: usage for usage in scan_file(f)}
    assert set(by_name) == {
        "OS_KEY", "MAPPING_KEY", "ALIASED_KEY", "DECOUPLE_OPTION",
        "DJANGO_VAR", "DJANGO_BOOL",
    }
    assert by_name["DJANGO_VAR"].has_default is False
    assert by_name["DJANGO_BOOL"].has_default is True


def test_django_positional_default_signatures(tmp_path: Path) -> None:
    f = write(tmp_path, "django_args.py", """
        import environ

        env = environ.Env()
        env("CALL_CAST", int)
        env("CALL_DEFAULT", int, 8000)
        env.list("LIST_CAST", str)
        env.list("LIST_DEFAULT", str, [])
        env.dict("DICT_CAST", dict)
        env.bool("BOOL_DEFAULT", False)
    """)

    by_name = {usage.name: usage for usage in scan_file(f)}
    assert by_name["CALL_CAST"].has_default is False
    assert by_name["CALL_DEFAULT"].has_default is True
    assert by_name["LIST_CAST"].has_default is False
    assert by_name["LIST_DEFAULT"].has_default is True
    assert by_name["DICT_CAST"].has_default is False
    assert by_name["BOOL_DEFAULT"].has_default is True


def test_aliases_stop_matching_after_shadow_or_rebind(tmp_path: Path) -> None:
    f = write(tmp_path, "shadowing.py", """
        import os

        os.getenv("BEFORE_REBIND")
        os = object()
        os.getenv("AFTER_REBIND")

        def takes_os(os):
            return os.getenv("PARAMETER")
    """)

    assert [usage.name for usage in scan_file(f)] == ["BEFORE_REBIND"]


def test_alias_assignment_lambda_and_comprehension_scopes(tmp_path: Path) -> None:
    f = write(tmp_path, "nested_scopes.py", """
        import os

        operating_system = os
        operating_system.getenv("PROPAGATED")
        fn = lambda os: os.getenv("LAMBDA_PARAMETER")
        hidden = [os.getenv("COMP_TARGET") for os in values]
        visible = [os.getenv("COMP_VISIBLE") for item in values]
        source = [item for os in [os.getenv("FIRST_ITERABLE")]]
    """)

    assert {usage.name for usage in scan_file(f)} == {
        "PROPAGATED", "COMP_VISIBLE", "FIRST_ITERABLE",
    }


def test_function_decorators_and_defaults_use_enclosing_scope(tmp_path: Path) -> None:
    f = write(tmp_path, "definition_time.py", """
        import os

        @decorate(os.getenv("DECORATOR_VALUE"))
        def configured(value=os.getenv("ARG_DEFAULT"), os=None):
            return os.getenv("PARAM_SHADOW")
    """)

    assert [usage.name for usage in scan_file(f)] == [
        "DECORATOR_VALUE", "ARG_DEFAULT",
    ]


def test_relative_os_import_is_not_stdlib(tmp_path: Path) -> None:
    f = write(tmp_path, "relative.py", """
        from .os import getenv, environ

        getenv("LOCAL_GETENV")
        environ["LOCAL_ENVIRON"]
    """)

    assert scan_file(f) == []


def test_annotated_django_instances_and_schema_defaults(tmp_path: Path) -> None:
    f = write(tmp_path, "schema.py", """
        import environ

        env: environ.Env = environ.Env(
            DEBUG=(bool, False),
            OPTIONAL=(str, None),
            REQUIRED=str,
        )
        alias: environ.Env = env
        alias("DEBUG")
        alias("OPTIONAL")
        alias("REQUIRED")
    """)

    by_name = {usage.name: usage for usage in scan_file(f)}
    assert by_name["DEBUG"].has_default is True
    assert ast.literal_eval(by_name["DEBUG"].default_node) is False
    assert by_name["OPTIONAL"].has_default is True
    assert ast.literal_eval(by_name["OPTIONAL"].default_node) is None
    assert by_name["REQUIRED"].has_default is False


def test_file_aware_env_and_current_helper_methods(tmp_path: Path) -> None:
    f = write(tmp_path, "file_aware.py", """
        import environ
        from environ import FileAwareEnv as SecretEnv

        env = environ.FileAwareEnv()
        other = SecretEnv()
        env("SECRET_KEY")
        other.bytes("CERTIFICATE")
        env.db()
        env.cache_url()
        env.email()
        env.channels()
        env.get_value("CAST_ONLY", str)
        env.get_value("WITH_DEFAULT", str, "fallback")
    """)

    by_name = {usage.name: usage for usage in scan_file(f)}
    assert set(by_name) == {
        "SECRET_KEY", "CERTIFICATE", "DATABASE_URL", "CACHE_URL",
        "EMAIL_URL", "CHANNELS_URL", "CAST_ONLY", "WITH_DEFAULT",
    }
    assert by_name["CAST_ONLY"].has_default is False
    assert by_name["WITH_DEFAULT"].has_default is True


def test_configured_env_scheme_is_propagated(tmp_path: Path) -> None:
    f = write(tmp_path, "configured.py", """
        import environ

        env = environ.Env.configured(
            scheme={"DEBUG": (bool, False), "REQUIRED": str},
        )
        env.bool("DEBUG")
        env("REQUIRED")
    """)

    by_name = {usage.name: usage for usage in scan_file(f)}
    assert by_name["DEBUG"].has_default is True
    assert by_name["REQUIRED"].has_default is False


def test_function_local_bindings_shadow_import_for_whole_scope(tmp_path: Path) -> None:
    f = write(tmp_path, "late_bindings.py", """
        import os

        def rebound_later():
            os.getenv("LOCAL_BEFORE_ASSIGNMENT")
            os = object()

        def imported_later():
            os.getenv("LOCAL_BEFORE_IMPORT")
            import os
            os.getenv("LOCAL_AFTER_IMPORT")
    """)

    assert [usage.name for usage in scan_file(f)] == ["LOCAL_AFTER_IMPORT"]


def test_class_name_is_bound_after_class_body_runs(tmp_path: Path) -> None:
    f = write(tmp_path, "class_binding.py", """
        import os

        class os:
            value = os.getenv("CLASS_BODY_VALUE")

        os.getenv("AFTER_CLASS")
    """)

    assert [usage.name for usage in scan_file(f)] == ["CLASS_BODY_VALUE"]


def test_import_os_path_binds_os_module(tmp_path: Path) -> None:
    f = write(tmp_path, "submodule.py", """
        import os.path

        os.getenv("FROM_SUBMODULE_IMPORT")
    """)

    assert [usage.name for usage in scan_file(f)] == ["FROM_SUBMODULE_IMPORT"]


def test_os_wildcard_import_tracks_exported_env_helpers(tmp_path: Path) -> None:
    f = write(tmp_path, "wildcard.py", """
        from os import *

        getenv("WILDCARD_GETENV")
        environ["WILDCARD_ENVIRON"]
    """)

    assert {usage.name for usage in scan_file(f)} == {
        "WILDCARD_GETENV", "WILDCARD_ENVIRON",
    }


def test_configured_env_accepts_positional_scheme(tmp_path: Path) -> None:
    f = write(tmp_path, "configured_positional.py", """
        import environ

        env = environ.Env.configured(None, {"PORT": (int, 8000)})
        env.int("PORT")
    """)

    usage = scan_file(f)[0]
    assert usage.name == "PORT"
    assert usage.has_default is True
    assert ast.literal_eval(usage.default_node) == 8000


def test_mutually_exclusive_branches_keep_possible_aliases(tmp_path: Path) -> None:
    f = write(tmp_path, "branches.py", """
        import os

        if enabled:
            os = object()
        else:
            os.getenv("ELSE_BRANCH")

        os.getenv("AFTER_BRANCH")
    """)

    assert {usage.name for usage in scan_file(f)} == {
        "ELSE_BRANCH", "AFTER_BRANCH",
    }


def test_try_handlers_see_intermediate_but_not_else_bindings(tmp_path: Path) -> None:
    f = write(tmp_path, "try_flow.py", """
        import os

        try:
            alias = os
            risky()
            alias = object()
        except Exception:
            alias.getenv("HANDLER_INTERMEDIATE")

        try:
            pass
        except Exception:
            late.getenv("ELSE_BINDING")
        else:
            import os as late
    """)

    assert [usage.name for usage in scan_file(f)] == ["HANDLER_INTERMEDIATE"]


@pytest.mark.skipif(sys.version_info < (3, 10), reason="match requires Python 3.10")
def test_irrefutable_match_capture_rebinds_alias(tmp_path: Path) -> None:
    f = write(tmp_path, "match_capture.py", """
        import os

        match value:
            case os:
                pass

        os.getenv("AFTER_CAPTURE")
    """)

    assert scan_file(f) == []


@pytest.mark.skipif(sys.version_info < (3, 12), reason="type aliases require Python 3.12")
def test_pep695_type_alias_rebinds_name(tmp_path: Path) -> None:
    f = write(tmp_path, "type_alias.py", """
        import os
        type os = int
        os.getenv("AFTER_TYPE_ALIAS")
    """)

    assert scan_file(f) == []


def test_attribute_aliases_are_propagated(tmp_path: Path) -> None:
    f = write(tmp_path, "attribute_aliases.py", """
        import os
        import environ

        getter = os.getenv
        mapping = os.environ
        EnvClass = environ.Env
        env = EnvClass()

        getter("GETTER_ALIAS")
        mapping.get("MAPPING_GET")
        mapping["MAPPING_ITEM"]
        env("ENV_CLASS_ALIAS")
    """)

    assert {usage.name for usage in scan_file(f)} == {
        "GETTER_ALIAS", "MAPPING_GET", "MAPPING_ITEM", "ENV_CLASS_ALIAS",
    }


def test_django_prefix_and_alias_state_are_tracked(tmp_path: Path) -> None:
    f = write(tmp_path, "prefix.py", """
        import environ

        env = environ.Env(DJANGO_DEBUG=(bool, False))
        alias = env
        env.prefix = "DJANGO_"
        alias.bool("DEBUG")
        alias.db()

        configured = environ.Env.configured(
            None,
            {"APP_PORT": (int, 8000)},
            prefix="APP_",
        )
        configured.int("PORT")
    """)

    by_name = {usage.name: usage for usage in scan_file(f)}
    assert set(by_name) == {
        "DJANGO_DEBUG", "DJANGO_DATABASE_URL", "APP_PORT",
    }
    assert by_name["DJANGO_DEBUG"].has_default is True
    assert by_name["APP_PORT"].has_default is True


def test_dynamic_django_prefix_becomes_dynamic_usage(tmp_path: Path) -> None:
    f = write(tmp_path, "dynamic_prefix.py", """
        import environ

        env = environ.Env()
        env.prefix = get_prefix()
        env("TOKEN")
    """)

    usage = scan_file(f)[0]
    assert usage.is_dynamic is True
    assert usage.raw_expr == "env.prefix + ('TOKEN')"


def test_function_prefix_mutation_does_not_leak_to_module_state(tmp_path: Path) -> None:
    f = write(tmp_path, "function_prefix.py", """
        import environ

        env = environ.Env()

        def configure():
            env.prefix = "INNER_"
            env("TOKEN")

        env("OUTER")
    """)

    assert [usage.name for usage in scan_file(f)] == ["INNER_TOKEN", "OUTER"]


def test_comprehension_walrus_keeps_zero_iteration_path(tmp_path: Path) -> None:
    f = write(tmp_path, "walrus.py", """
        import os

        [(os := object()) for item in values]
        os.getenv("MAY_STILL_BE_OS")
    """)

    assert [usage.name for usage in scan_file(f)] == ["MAY_STILL_BE_OS"]


def test_pep263_source_encoding_is_respected(tmp_path: Path) -> None:
    path = tmp_path / "latin1.py"
    path.write_bytes(
        b"# -*- coding: latin-1 -*-\n# caf\xe9\nimport os\nos.getenv('LATIN1')\n"
    )

    assert [usage.name for usage in scan_file(path)] == ["LATIN1"]


def test_deep_ast_becomes_scan_error(tmp_path: Path) -> None:
    expression = " + ".join("1" for _ in range(2500))
    path = write(tmp_path, "deep.py", f"value = {expression}\n")

    with pytest.raises(ScanError, match="deeply nested"):
        scan_file(path)

    result = scan_project(tmp_path)
    assert len(result.errors) == 1
    assert result.errors[0][0] == path


def test_project_walk_prunes_case_variants_of_excludes(tmp_path: Path) -> None:
    write(tmp_path, "main.PY", "import os\nos.getenv('MAIN')\n")
    write(tmp_path, "VENV/ignored.py", "import os\nos.getenv('IGNORED')\n")
    write(tmp_path, "Node_Modules/ignored.py", "import os\nos.getenv('IGNORED2')\n")

    files = iter_python_files(tmp_path)
    assert [path.name for path in files] == ["main.PY"]
    assert scan_project(tmp_path).static_names == {"MAIN"}


def test_scan_project_reuses_precomputed_file_list(tmp_path: Path, monkeypatch) -> None:
    path = write(tmp_path, "main.py", "import os\nos.getenv('ONE_PASS')\n")

    def should_not_walk(*args, **kwargs):
        raise AssertionError("iter_python_files was called twice")

    monkeypatch.setattr("envsleuth.scanner.iter_python_files", should_not_walk)
    result = scan_project(tmp_path, files=[path])
    assert result.static_names == {"ONE_PASS"}
    assert result.scanned_files == [path]


def test_walk_errors_are_reported(tmp_path: Path, monkeypatch) -> None:
    def broken_walk(root, topdown=True, onerror=None):
        assert onerror is not None
        onerror(PermissionError("blocked directory"))
        yield root, [], []

    monkeypatch.setattr("envsleuth.scanner.os.walk", broken_walk)
    with pytest.raises(ScanError, match="blocked directory"):
        iter_python_files(tmp_path)

    result = scan_project(tmp_path)
    assert len(result.errors) == 1
    assert "blocked directory" in result.errors[0][1]


def test_scan_caps_file_and_usage_counts(
    tmp_path: Path, monkeypatch,
) -> None:
    write(tmp_path, "one.py", "import os\nos.getenv('A')\nos.getenv('B')\n")
    write(tmp_path, "two.py", "value = 2\n")
    write(tmp_path, "three.py", "value = 3\n")

    monkeypatch.setattr("envsleuth.scanner.MAX_SCAN_FILES", 2)
    with pytest.raises(ScanError, match="more than 2 files"):
        iter_python_files(tmp_path)

    monkeypatch.setattr("envsleuth.scanner.MAX_SCAN_FILES", 50_000)
    monkeypatch.setattr("envsleuth.scanner.MAX_SCAN_USAGES", 1)
    result = scan_project(tmp_path)

    assert len(result.usages) == 1
    assert len(result.errors) == 1
    assert "more than 1 environment-variable usages" in result.errors[0][1]


def test_finally_sees_partial_try_else_and_handler_states(tmp_path: Path) -> None:
    f = write(tmp_path, "finally_flow.py", """
        import os

        alias = object()
        try:
            alias = os
            risky()
            alias = object()
        finally:
            alias.getenv("TRY_PARTIAL")

        alias = object()
        try:
            risky()
        except Exception:
            alias = os
            risky_again()
            alias = object()
        finally:
            alias.getenv("HANDLER_PARTIAL")

        alias = object()
        try:
            pass
        except Exception:
            pass
        else:
            alias = os
            risky_in_else()
            alias = object()
        finally:
            alias.getenv("ELSE_PARTIAL")
    """)

    names = [usage.name for usage in scan_file(f)]
    assert names == ["TRY_PARTIAL", "HANDLER_PARTIAL", "ELSE_PARTIAL"]


def test_except_type_side_effects_feed_later_handlers(tmp_path: Path) -> None:
    f = write(tmp_path, "handler_types.py", """
        import os

        try:
            risky()
        except ((getter := os.getenv) and ValueError):
            pass
        except TypeError:
            getter("SEQUENTIAL_HANDLER")
    """)

    assert [usage.name for usage in scan_file(f)] == ["SEQUENTIAL_HANDLER"]


@pytest.mark.skipif(sys.version_info < (3, 11), reason="except* requires Python 3.11")
def test_except_star_clauses_share_type_and_handler_body_effects(
    tmp_path: Path,
) -> None:
    f = write(tmp_path, "handler_groups.py", """
        import os

        try:
            risky_group()
        except* ((getter := os.getenv) and ValueError):
            pass
        except* TypeError:
            getter("STAR_TYPE_EFFECT")

        try:
            another_group()
        except* ValueError:
            getter2 = os.getenv
            risky_handler()
            getter2 = object()
        except* TypeError:
            getter2("STAR_BODY_EFFECT")
    """)

    assert [usage.name for usage in scan_file(f)] == [
        "STAR_TYPE_EFFECT", "STAR_BODY_EFFECT",
    ]


def test_loop_analysis_reaches_fixpoint_without_duplicate_usages(
    tmp_path: Path,
) -> None:
    f = write(tmp_path, "loop_fixpoint.py", """
        import os

        getter = fallback
        for item in items:
            getter("FOR_REPEAT")
            getter = os.getenv

        getter = fallback
        while condition:
            getter("WHILE_REPEAT")
            getter = os.getenv

        getter = fallback
        [
            (getter("COMP_REPEAT"), (getter := os.getenv))
            for item in items
        ]

        first = fallback
        second = fallback
        third = os.getenv
        for item in items:
            first("THREE_PASSES")
            first = second
            second = third
    """)

    names = [usage.name for usage in scan_file(f)]
    assert names == ["FOR_REPEAT", "WHILE_REPEAT", "COMP_REPEAT", "THREE_PASSES"]


def test_long_loop_alias_chain_uses_safe_widening(tmp_path: Path) -> None:
    count = 40
    lines = ["import os"]
    lines.extend(f"alias_{index} = fallback" for index in range(count))
    lines.extend(["tail = os.getenv", "for item in items:", '    alias_0("LONG_CHAIN")'])
    lines.extend(
        f"    alias_{index} = alias_{index + 1}"
        for index in range(count - 1)
    )
    lines.append(f"    alias_{count - 1} = tail")
    f = write(tmp_path, "long_chain.py", "\n".join(lines))

    assert [usage.name for usage in scan_file(f)] == ["LONG_CHAIN"]


@pytest.mark.skipif(sys.version_info < (3, 10), reason="match requires Python 3.10")
def test_match_failed_guards_feed_later_cases_and_keep_captures(
    tmp_path: Path,
) -> None:
    f = write(tmp_path, "match_guards.py", """
        import os

        alias = object()
        match value:
            case 1 if ((alias := os) and False):
                pass
            case _:
                alias.getenv("FAILED_GUARD_STATE")

        match other:
            case os if False:
                pass
        os.getenv("CAPTURE_ALREADY_REBOUND")
    """)

    assert [usage.name for usage in scan_file(f)] == ["FAILED_GUARD_STATE"]


def test_conditional_values_keep_all_possible_alias_kinds(tmp_path: Path) -> None:
    f = write(tmp_path, "conditional_aliases.py", """
        import os
        import environ

        env_a = environ.Env()
        env_a.prefix = "A_"
        env_b = environ.Env()
        env_b.prefix = "B_"

        getter = os.getenv if enabled else fallback
        mapping = os.environ if enabled else fallback_mapping
        selected = env_a if enabled else env_b
        mixed = os.getenv if enabled else env_a

        getter("CONDITIONAL_GETTER")
        mapping.get("CONDITIONAL_MAPPING")
        selected("TOKEN")
        mixed("MIXED_CALL")
    """)

    usages = scan_file(f)
    assert {usage.name for usage in usages if usage.name is not None} == {
        "CONDITIONAL_GETTER", "CONDITIONAL_MAPPING", "MIXED_CALL",
    }
    dynamic = [usage for usage in usages if usage.is_dynamic]
    assert len(dynamic) == 1
    assert dynamic[0].raw_expr == "selected.prefix + ('TOKEN')"


@pytest.mark.skipif(sys.version_info < (3, 12), reason="type params require Python 3.12")
def test_pep695_alias_annotation_scope_shadows_target_and_type_params(
    tmp_path: Path,
) -> None:
    f = write(tmp_path, "type_alias_scopes.py", """
        import os

        type Alias[os] = os.getenv("TYPE_PARAMETER")
        type RealAlias = os.getenv("REAL_ALIAS")
        type os = os.getenv("SELF_REFERENCE")
    """)

    assert [usage.name for usage in scan_file(f)] == ["REAL_ALIAS"]


@pytest.mark.skipif(sys.version_info < (3, 12), reason="type params require Python 3.12")
def test_generic_definition_scopes_only_cover_annotations_and_bodies(
    tmp_path: Path,
) -> None:
    f = write(tmp_path, "generic_scopes.py", """
        import os

        @decorate(os.getenv("FUNCTION_DECORATOR"))
        def configured[os](
            value: os.getenv("PARAMETER_ANNOTATION") = os.getenv("ARG_DEFAULT"),
        ) -> os.getenv("RETURN_ANNOTATION"):
            os.getenv("FUNCTION_BODY")

        @decorate(os.getenv("CLASS_DECORATOR"))
        class Generic[os](os.getenv("GENERIC_BASE")):
            value = os.getenv("CLASS_BODY")

        os.getenv("AFTER_GENERIC_DEFINITIONS")
    """)

    assert [usage.name for usage in scan_file(f)] == [
        "FUNCTION_DECORATOR",
        "ARG_DEFAULT",
        "CLASS_DECORATOR",
        "AFTER_GENERIC_DEFINITIONS",
    ]


# ====================================================== pydantic-settings


def test_pydantic_settings_fields_and_defaults(tmp_path: Path) -> None:
    f = write(tmp_path, "settings.py", """
        from typing import Optional
        from pydantic import Field
        from pydantic_settings import BaseSettings

        class Settings(BaseSettings):
            required: str
            optional_type_only: Optional[str]
            assigned: int = 3
            none_default: Optional[str] = None
            field_required: str = Field(...)
            field_default: str = Field(default="fallback")
            generated: list[str] = Field(default_factory=list)
            _private: str
    """)

    usages = {
        usage.name: usage for usage in scan_file(f)
        if usage.call_type == "pydantic_settings"
    }

    assert set(usages) == {
        "required",
        "optional_type_only",
        "assigned",
        "none_default",
        "field_required",
        "field_default",
        "generated",
    }
    assert usages["required"].has_default is False
    assert usages["optional_type_only"].has_default is False
    assert usages["field_required"].has_default is False
    assert usages["assigned"].has_default is True
    assert usages["none_default"].has_default is True
    assert usages["field_default"].has_default is True
    assert usages["generated"].has_default is True
    assert usages["generated"].default_node is None
    assert all(usage.case_sensitive is False for usage in usages.values())


def test_pydantic_prefix_applies_even_when_config_follows_fields(
    tmp_path: Path,
) -> None:
    f = write(tmp_path, "settings.py", """
        from pydantic_settings import BaseSettings, SettingsConfigDict

        class Settings(BaseSettings):
            token: str
            model_config = SettingsConfigDict(env_prefix="APP_")
    """)

    usage = scan_file(f)[0]

    assert usage.name == "APP_token"
    assert usage.accepted_names == ("APP_token",)


def test_pydantic_aliases_override_prefix_and_choices_are_one_of(
    tmp_path: Path,
) -> None:
    f = write(tmp_path, "settings.py", """
        from pydantic import AliasChoices, Field
        from pydantic_settings import BaseSettings, SettingsConfigDict

        class Settings(BaseSettings):
            model_config = SettingsConfigDict(env_prefix="APP_")
            plain: str
            aliased: str = Field(alias="DIRECT")
            chosen: str = Field(
                alias="IGNORED",
                validation_alias=AliasChoices("TOKEN", "LEGACY_TOKEN"),
            )
            output_name: str = Field(serialization_alias="SERIALIZED")
    """)

    usages = {usage.name: usage for usage in scan_file(f)}

    assert set(usages) == {
        "APP_plain", "DIRECT", "TOKEN", "APP_output_name",
    }
    assert usages["TOKEN"].accepted_names == ("TOKEN", "LEGACY_TOKEN")


def test_pydantic_module_and_assignment_aliases(tmp_path: Path) -> None:
    f = write(tmp_path, "settings.py", """
        import pydantic as pd
        import pydantic_settings as ps

        Base = ps.BaseSettings
        Config = ps.SettingsConfigDict
        F = pd.Field
        Choices = pd.AliasChoices

        class Settings(Base):
            model_config = Config(env_prefix="SERVICE_")
            endpoint: str
            token: str = F(validation_alias=Choices("TOKEN", "OLD_TOKEN"))
    """)

    usages = {usage.name: usage for usage in scan_file(f)}

    assert set(usages) == {"SERVICE_endpoint", "TOKEN"}
    assert usages["TOKEN"].accepted_names == ("TOKEN", "OLD_TOKEN")


def test_pydantic_annotated_field_and_classvar(tmp_path: Path) -> None:
    f = write(tmp_path, "settings.py", """
        from typing import Annotated, ClassVar
        from pydantic import Field
        from pydantic_settings import BaseSettings

        class Settings(BaseSettings):
            token: Annotated[str, Field(validation_alias="TOKEN")]
            metadata: ClassVar[str] = "not an env field"
    """)

    usages = scan_file(f)

    assert [usage.name for usage in usages] == ["TOKEN"]


def test_pydantic_class_keywords_and_inherited_config(tmp_path: Path) -> None:
    f = write(tmp_path, "settings.py", """
        from pydantic_settings import BaseSettings

        class Base(BaseSettings, env_prefix="BASE_", case_sensitive=True):
            parent: str

        class Child(Base):
            child: str
    """)

    usages = {usage.name: usage for usage in scan_file(f)}

    assert set(usages) == {"BASE_parent", "BASE_child"}
    assert all(usage.case_sensitive is True for usage in usages.values())


def test_pydantic_multiple_bases_follow_base_settings_config_merge(
    tmp_path: Path,
) -> None:
    f = write(tmp_path, "settings.py", """
        from pydantic_settings import BaseSettings, SettingsConfigDict

        class Prefixed(BaseSettings):
            model_config = SettingsConfigDict(env_prefix="APP_")

        class Sensitive(BaseSettings):
            model_config = SettingsConfigDict(case_sensitive=True)

        class Settings(Prefixed, Sensitive):
            token: str
    """)

    usage = scan_file(f)[0]

    assert usage.name == "token"
    assert usage.case_sensitive is True


def test_dynamic_pydantic_config_is_reported_not_guessed(
    tmp_path: Path,
) -> None:
    f = write(tmp_path, "settings.py", """
        from pydantic import Field
        from pydantic_settings import BaseSettings, SettingsConfigDict

        class Settings(BaseSettings):
            model_config = SettingsConfigDict(env_prefix=get_prefix())
            token: str
            selected: str = Field(validation_alias=get_alias())
    """)

    usages = scan_file(f)

    assert len(usages) == 2
    assert all(usage.is_dynamic for usage in usages)
    assert {usage.raw_expr for usage in usages} == {
        "model_config['env_prefix'] + 'token'",
        "get_alias()",
    }


def test_pydantic_case_sensitivity_can_be_enabled_with_plain_dict(
    tmp_path: Path,
) -> None:
    f = write(tmp_path, "settings.py", """
        from pydantic_settings import BaseSettings

        class Settings(BaseSettings):
            model_config = {
                "env_prefix": "Exact_",
                "case_sensitive": True,
            }
            token: str
    """)

    usage = scan_file(f)[0]

    assert usage.name == "Exact_token"
    assert usage.case_sensitive is True


def test_rebound_or_relative_pydantic_names_do_not_match(
    tmp_path: Path,
) -> None:
    f = write(tmp_path, "settings.py", """
        from .pydantic_settings import BaseSettings as RelativeBase
        from pydantic_settings import BaseSettings

        BaseSettings = object

        class One(BaseSettings):
            token: str

        class Two(RelativeBase):
            other: str
    """)

    assert scan_file(f) == []


def test_pydantic_default_factory_is_never_executed(tmp_path: Path) -> None:
    marker = tmp_path / "SHOULD_NOT_EXIST"
    f = write(tmp_path, "settings.py", f"""
        from pydantic import Field
        from pydantic_settings import BaseSettings

        class Settings(BaseSettings):
            token: str = Field(
                default_factory=lambda: open({str(marker)!r}, "w").write("bad")
            )
    """)

    usage = scan_file(f)[0]

    assert usage.has_default is True
    assert not marker.exists()


def test_pydantic_env_prefix_target_controls_aliases_and_variables(
    tmp_path: Path,
) -> None:
    f = write(tmp_path, "settings.py", """
        from pydantic import Field
        from pydantic_settings import BaseSettings, SettingsConfigDict

        class DefaultTarget(BaseSettings):
            model_config = SettingsConfigDict(env_prefix="DEF_")
            plain_default: str
            named_default: str = Field(alias="DEFAULT_ALIAS")

        class AllTarget(BaseSettings):
            model_config = SettingsConfigDict(
                env_prefix="ALL_",
                env_prefix_target="all",
            )
            plain_all: str
            named_all: str = Field(alias="ALL_ALIAS")

        class AliasTarget(
            BaseSettings,
            env_prefix="ONLY_ALIAS_",
            env_prefix_target="alias",
        ):
            plain_alias_target: str
            named_alias_target: str = Field(alias="NAMED")
    """)

    usages = {usage.name: usage for usage in scan_file(f)}

    assert set(usages) == {
        "DEF_plain_default",
        "DEFAULT_ALIAS",
        "ALL_plain_all",
        "ALL_ALL_ALIAS",
        "plain_alias_target",
        "ONLY_ALIAS_NAMED",
    }


def test_pydantic_subclass_reapplies_config_to_inherited_fields(
    tmp_path: Path,
) -> None:
    f = write(tmp_path, "settings.py", """
        from pydantic import Field
        from pydantic_settings import BaseSettings, SettingsConfigDict

        class Common(BaseSettings):
            token: str
            endpoint: str = Field(alias="ENDPOINT")

        class Production(Common):
            model_config = SettingsConfigDict(
                env_prefix="PROD_",
                env_prefix_target="all",
                case_sensitive=True,
            )
    """)

    usages = scan_file(f)
    by_name = {usage.name: usage for usage in usages}

    assert set(by_name) == {
        "token",
        "ENDPOINT",
        "PROD_token",
        "PROD_ENDPOINT",
    }
    assert by_name["token"].case_sensitive is False
    assert by_name["PROD_token"].case_sensitive is True
    assert by_name["PROD_ENDPOINT"].case_sensitive is True


def test_pydantic_unpacked_config_is_dynamic_instead_of_guessed(
    tmp_path: Path,
) -> None:
    f = write(tmp_path, "settings.py", """
        from pydantic_settings import BaseSettings, SettingsConfigDict

        CONFIG = {"env_prefix": "APP_"}

        class KeywordUnpack(BaseSettings):
            model_config = SettingsConfigDict(**CONFIG)
            token: str

        class DictUnpack(BaseSettings):
            model_config = {**CONFIG}
            endpoint: str

        class PositionalMapping(BaseSettings):
            model_config = SettingsConfigDict(CONFIG)
            host: str
    """)

    usages = scan_file(f)

    assert len(usages) == 3
    assert all(usage.is_dynamic for usage in usages)
    assert {usage.raw_expr for usage in usages} == {"model_config"}


def test_pydantic_model_config_mutations_are_dynamic(tmp_path: Path) -> None:
    f = write(tmp_path, "settings.py", """
        from pydantic_settings import BaseSettings, SettingsConfigDict

        class ItemAssignment(BaseSettings):
            model_config = SettingsConfigDict()
            model_config["env_prefix"] = "APP_"
            token: str

        class MethodMutation(BaseSettings):
            model_config = SettingsConfigDict()
            model_config.update(case_sensitive=True)
            endpoint: str

        class EscapedConfig(BaseSettings):
            model_config = SettingsConfigDict()
            configure(model_config)
            host: str

        class AliasedConfig(BaseSettings):
            model_config = SettingsConfigDict()
            cfg = model_config
            cfg.update({"env_prefix": "ALIAS_"})
            port: int
    """)

    usages = scan_file(f)

    assert len(usages) == 4
    assert all(usage.is_dynamic for usage in usages)
    assert {usage.raw_expr for usage in usages} == {"model_config"}


def test_pydantic_method_body_does_not_mutate_outer_config(
    tmp_path: Path,
) -> None:
    f = write(tmp_path, "settings.py", """
        from pydantic_settings import BaseSettings

        class Settings(BaseSettings):
            token: str

            @staticmethod
            def merge(model_config):
                model_config.update({"env_prefix": "NOT_OUTER_"})
    """)

    usage = scan_file(f)[0]

    assert usage.name == "token"
    assert usage.is_dynamic is False


def test_pydantic_only_last_model_config_assignment_is_effective(
    tmp_path: Path,
) -> None:
    f = write(tmp_path, "settings.py", """
        from pydantic_settings import BaseSettings, SettingsConfigDict

        class Reassigned(BaseSettings):
            model_config = SettingsConfigDict(env_prefix="OLD_")
            model_config = SettingsConfigDict(case_sensitive=True)
            token: str

        class Conditional(BaseSettings):
            if enabled:
                model_config = SettingsConfigDict(env_prefix="MAYBE_")
            endpoint: str

        class Recovered(BaseSettings):
            if enabled:
                model_config = SettingsConfigDict(env_prefix="MAYBE_")
            model_config = SettingsConfigDict(env_prefix="FINAL_")
            host: str
    """)

    usages = scan_file(f)
    static = {usage.name: usage for usage in usages if usage.name is not None}
    dynamic = [usage for usage in usages if usage.is_dynamic]

    assert set(static) == {"token", "FINAL_host"}
    assert static["token"].case_sensitive is True
    assert len(dynamic) == 1
    assert dynamic[0].raw_expr == "model_config"


def test_pydantic_bare_field_annotation_does_not_shadow_import(
    tmp_path: Path,
) -> None:
    f = write(tmp_path, "settings.py", """
        from pydantic import Field
        from pydantic_settings import BaseSettings

        class Settings(BaseSettings):
            Field: str
            token: str = Field(alias="TOKEN")
    """)

    usages = scan_file(f)

    assert [usage.name for usage in usages] == ["Field", "TOKEN"]


def test_pydantic_alias_generator_makes_unaliased_fields_dynamic(
    tmp_path: Path,
) -> None:
    f = write(tmp_path, "settings.py", """
        from pydantic import Field
        from pydantic_settings import BaseSettings, SettingsConfigDict

        class Settings(BaseSettings):
            model_config = SettingsConfigDict(alias_generator=to_camel)
            api_key: str
            explicit: str = Field(alias="EXPLICIT")
    """)

    usages = scan_file(f)
    dynamic = [usage for usage in usages if usage.is_dynamic]

    assert len(dynamic) == 1
    assert dynamic[0].raw_expr == "alias_generator('api_key')"
    assert [usage.name for usage in usages if not usage.is_dynamic] == [
        "EXPLICIT"
    ]


def test_dynamic_pydantic_case_mode_does_not_guess_name_matching(
    tmp_path: Path,
) -> None:
    f = write(tmp_path, "settings.py", """
        from pydantic_settings import BaseSettings, SettingsConfigDict

        class Settings(BaseSettings):
            model_config = SettingsConfigDict(
                env_prefix="APP_",
                case_sensitive=get_case_mode(),
            )
            token: str
    """)

    usage = scan_file(f)[0]

    assert usage.is_dynamic
    assert usage.raw_expr == "model_config['case_sensitive']"
    assert usage.case_sensitive is None


def test_pydantic_name_semantics_we_do_not_model_are_dynamic(
    tmp_path: Path,
) -> None:
    f = write(tmp_path, "settings.py", """
        from pydantic import Field
        from pydantic_settings import BaseSettings, SettingsConfigDict

        class Nested(BaseSettings):
            model_config = SettingsConfigDict(env_nested_delimiter="__")
            database: dict

        class EmptyIgnored(BaseSettings):
            model_config = SettingsConfigDict(env_ignore_empty=True)
            token: str

        class NameFallback(BaseSettings):
            model_config = SettingsConfigDict(populate_by_name=True)
            endpoint: str = Field(alias="ENDPOINT")
    """)

    usages = scan_file(f)

    assert len(usages) == 3
    assert all(usage.is_dynamic for usage in usages)
    assert all(
        usage.raw_expr is not None
        and usage.raw_expr.startswith("model_config")
        for usage in usages
    )


def test_pydantic_custom_sources_are_not_guessed(tmp_path: Path) -> None:
    f = write(tmp_path, "settings.py", """
        from pydantic_settings import BaseSettings

        class Settings(BaseSettings):
            token: str

            @classmethod
            def settings_customise_sources(
                cls, settings_cls, init, env, dotenv, secrets
            ):
                return (custom_source,)
    """)

    usage = scan_file(f)[0]

    assert usage.is_dynamic
    assert usage.raw_expr == "model_config"


# ============================================================== mixed
