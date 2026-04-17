"""Tests for envsleuth.checker."""

from __future__ import annotations

import textwrap
from pathlib import Path

from envsleuth.checker import (
    check,
    find_nearby_env_files,
    load_env_file,
    load_ignore_patterns,
)
from envsleuth.scanner import scan_project


def write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    return path


# --------------------------------------------------------------- load_env_file


def test_load_env_file_basic(tmp_path: Path) -> None:
    env = write(tmp_path / ".env", """
        A=1
        B=hello
        C=
    """)
    values = load_env_file(env)
    assert values["A"] == "1"
    assert values["B"] == "hello"
    assert "C" in values


def test_load_env_file_missing(tmp_path: Path) -> None:
    assert load_env_file(tmp_path / "nope.env") == {}


def test_load_env_file_ignores_comments(tmp_path: Path) -> None:
    env = write(tmp_path / ".env", """
        # comment
        A=1
    """)
    values = load_env_file(env)
    assert "A" in values
    # dotenv doesn't yield the comment as a key.
    assert not any(k.startswith("#") for k in values)


# --------------------------------------------------------- load_ignore_patterns


def test_load_ignore_patterns(tmp_path: Path) -> None:
    ign = write(tmp_path / ".envignore", """
        # skip test vars
        TEST_*
        DEBUG_TOOL

        LEGACY_*
    """)
    patterns = load_ignore_patterns(ign)
    assert patterns == ["TEST_*", "DEBUG_TOOL", "LEGACY_*"]


def test_load_ignore_patterns_missing(tmp_path: Path) -> None:
    assert load_ignore_patterns(tmp_path / ".envignore") == []


# ========================================================================= check


def test_check_detects_missing(tmp_path: Path) -> None:
    code = tmp_path / "src"
    write(code / "a.py", """
        import os
        os.getenv("PRESENT")
        os.getenv("MISSING")
    """)
    env = write(tmp_path / ".env", "PRESENT=yes\n")

    scan = scan_project(code)
    report = check(scan, env)

    missing = [v.name for v in report.missing]
    present = [v.name for v in report.present]
    assert missing == ["MISSING"]
    assert present == ["PRESENT"]


def test_check_marks_defaults_as_default_status(tmp_path: Path) -> None:
    code = tmp_path / "src"
    write(code / "a.py", """
        import os
        os.getenv("OPTIONAL", "fallback")
    """)
    env = write(tmp_path / ".env", "")

    scan = scan_project(code)
    report = check(scan, env)

    # Not present in .env but has default in code -> status 'default', not 'missing'.
    assert [v.name for v in report.with_default] == ["OPTIONAL"]
    assert report.missing == []


def test_check_default_still_wins_over_present(tmp_path: Path) -> None:
    """If var is in .env AND has default, it's 'present' (the env value is used)."""
    code = tmp_path / "src"
    write(code / "a.py", """
        import os
        os.getenv("VAR", "fallback")
    """)
    env = write(tmp_path / ".env", "VAR=real\n")

    scan = scan_project(code)
    report = check(scan, env)

    assert [v.name for v in report.present] == ["VAR"]
    assert report.with_default == []


def test_check_ignore_patterns_exclude_vars(tmp_path: Path) -> None:
    code = tmp_path / "src"
    write(code / "a.py", """
        import os
        os.getenv("REAL_THING")
        os.getenv("TEST_A")
        os.getenv("TEST_B")
    """)
    env = write(tmp_path / ".env", "REAL_THING=1\n")

    scan = scan_project(code)
    report = check(scan, env, ignore_patterns=["TEST_*"])

    ignored = {v.name for v in report.ignored}
    assert ignored == {"TEST_A", "TEST_B"}
    assert report.missing == []


def test_check_ignore_exact_match(tmp_path: Path) -> None:
    code = tmp_path / "src"
    write(code / "a.py", """
        import os
        os.getenv("SPECIAL")
    """)
    env = write(tmp_path / ".env", "")

    scan = scan_project(code)
    report = check(scan, env, ignore_patterns=["SPECIAL"])

    assert [v.name for v in report.ignored] == ["SPECIAL"]


def test_check_extra_in_env_is_reported(tmp_path: Path) -> None:
    """Vars in .env but not used in code are reported as informational."""
    code = tmp_path / "src"
    write(code / "a.py", """
        import os
        os.getenv("USED")
    """)
    env = write(tmp_path / ".env", """
        USED=1
        UNUSED=2
        ALSO_UNUSED=3
    """)

    scan = scan_project(code)
    report = check(scan, env)

    assert report.extra_in_env == ["ALSO_UNUSED", "UNUSED"]


def test_check_env_file_missing(tmp_path: Path) -> None:
    code = tmp_path / "src"
    write(code / "a.py", "import os\nos.getenv('X')\n")
    missing_env = tmp_path / "nope.env"

    scan = scan_project(code)
    report = check(scan, missing_env)

    assert report.env_file_exists is False
    # All vars are 'missing' because no .env means none are present.
    assert [v.name for v in report.missing] == ["X"]


def test_check_dynamic_usages_separated(tmp_path: Path) -> None:
    code = tmp_path / "src"
    write(code / "a.py", """
        import os
        os.getenv("STATIC")
        name = "X"
        os.getenv(name)
    """)
    env = write(tmp_path / ".env", "STATIC=1\n")

    scan = scan_project(code)
    report = check(scan, env)

    # Dynamic usages don't appear in variables list.
    assert {v.name for v in report.variables} == {"STATIC"}
    assert len(report.dynamic_usages) == 1


def test_check_propagates_scan_errors(tmp_path: Path) -> None:
    code = tmp_path / "src"
    write(code / "good.py", "import os\nos.getenv('GOOD')\n")
    write(code / "broken.py", "not valid python!!!")
    env = write(tmp_path / ".env", "")

    scan = scan_project(code)
    report = check(scan, env)

    assert len(report.errors) == 1
    assert report.has_issues is True


def test_check_has_issues_false_on_clean_project(tmp_path: Path) -> None:
    code = tmp_path / "src"
    write(code / "a.py", "import os\nos.getenv('X')\n")
    env = write(tmp_path / ".env", "X=1\n")

    scan = scan_project(code)
    report = check(scan, env)

    assert report.has_issues is False


def test_check_vars_are_sorted(tmp_path: Path) -> None:
    code = tmp_path / "src"
    write(code / "a.py", """
        import os
        os.getenv("ZEBRA")
        os.getenv("ALPHA")
        os.getenv("MIDDLE")
    """)
    env = write(tmp_path / ".env", "")

    scan = scan_project(code)
    report = check(scan, env)

    names = [v.name for v in report.variables]
    assert names == sorted(names)


def test_check_usages_attached(tmp_path: Path) -> None:
    """Each VarReport should carry all EnvUsage records for that variable."""
    code = tmp_path / "src"
    write(code / "a.py", """
        import os
        os.getenv("X")
    """)
    write(code / "b.py", """
        import os
        os.getenv("X")
    """)
    env = write(tmp_path / ".env", "")

    scan = scan_project(code)
    report = check(scan, env)

    x = next(v for v in report.variables if v.name == "X")
    assert len(x.usages) == 2


# ========================================================== find_nearby_env_files


def test_find_nearby_env_files(tmp_path: Path) -> None:
    (tmp_path / ".env").touch()
    (tmp_path / ".env.production").touch()
    (tmp_path / ".env.local").touch()
    (tmp_path / "regular.txt").touch()

    found = find_nearby_env_files(tmp_path)
    names = [p.name for p in found]
    assert ".env" in names
    assert ".env.production" in names
    assert ".env.local" in names
    assert "regular.txt" not in names


def test_find_nearby_env_files_empty(tmp_path: Path) -> None:
    assert find_nearby_env_files(tmp_path) == []
