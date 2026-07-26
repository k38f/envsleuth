"""Tests for envsleuth.checker."""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

import envsleuth.checker as checker_module
from envsleuth.checker import (
    EnvFileParseError,
    check,
    find_nearby_env_files,
    load_env_file,
    load_ignore_patterns,
)
from envsleuth.scanner import EnvUsage, ScanResult, scan_project


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


def test_load_env_file_rejects_non_regular_path(tmp_path: Path) -> None:
    env_dir = tmp_path / ".env"
    env_dir.mkdir()

    with pytest.raises(OSError):
        load_env_file(env_dir)


def test_load_env_file_ignores_comments(tmp_path: Path) -> None:
    env = write(tmp_path / ".env", """
        # comment
        A=1
    """)
    values = load_env_file(env)
    assert "A" in values
    # dotenv doesn't yield the comment as a key.
    assert not any(k.startswith("#") for k in values)


def test_load_env_file_strips_utf8_bom(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("\ufeffFIRST=one\nSECOND=two\n", encoding="utf-8")

    values = load_env_file(env)

    assert values == {"FIRST": "one", "SECOND": "two"}


def test_load_env_file_distinguishes_bare_key_from_empty_value(tmp_path: Path) -> None:
    env = write(tmp_path / ".env", "BARE\nEMPTY=\n")

    values = load_env_file(env)

    assert values["BARE"] is None
    assert values["EMPTY"] == ""


def test_load_env_file_keeps_valid_dotenv_syntax(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        'export EXPORTED="hello world"\n'
        'MULTILINE="first line\nsecond line"\n'
        "QUOTED='# is part of the value'\n"
        "BARE\n",
        encoding="utf-8",
    )

    values = load_env_file(env)

    assert values == {
        "EXPORTED": "hello world",
        "MULTILINE": "first line\nsecond line",
        "QUOTED": "# is part of the value",
        "BARE": None,
    }


def test_load_env_file_rejects_malformed_syntax(tmp_path: Path) -> None:
    env = write(
        tmp_path / ".env",
        'API_SECRET="do-not-leak-this-value\nVALID=1\n',
    )

    with pytest.raises(EnvFileParseError) as raised:
        load_env_file(env)

    message = str(raised.value)
    assert str(env) in message
    assert "invalid syntax at line 1" in message
    assert "do-not-leak-this-value" not in message


@pytest.mark.parametrize(
    "content",
    ["NORMAL=secret\0suffix\n", "BAD\0KEY=secret\n"],
)
def test_load_env_file_rejects_nul_in_key_or_value(
    tmp_path: Path, content: str
) -> None:
    env = write(tmp_path / ".env", content)

    with pytest.raises(EnvFileParseError) as raised:
        load_env_file(env)

    message = str(raised.value)
    assert str(env) in message
    assert "NUL byte at line 1" in message
    assert "secret" not in message
    assert "\0" not in message


def test_load_env_file_uses_one_snapshot(tmp_path: Path, monkeypatch) -> None:
    env = write(tmp_path / ".env", "FIRST=original\n")
    real_dotenv_values = checker_module.dotenv_values

    def replace_file_before_dotenv_parse(*args, **kwargs):
        env.write_text("SECOND=changed\n", encoding="utf-8")
        return real_dotenv_values(*args, **kwargs)

    monkeypatch.setattr(
        checker_module, "dotenv_values", replace_file_before_dotenv_parse
    )

    values = load_env_file(env)

    assert values == {"FIRST": "original"}


def test_load_env_file_rejects_oversized_input(
    tmp_path: Path, monkeypatch,
) -> None:
    env = write(tmp_path / ".env", "TOKEN=value\n")
    monkeypatch.setattr(checker_module, "MAX_ENV_FILE_SIZE", 4)

    with pytest.raises(OSError, match="larger than 4 bytes"):
        load_env_file(env)


def test_load_env_file_caps_lines_and_bindings(
    tmp_path: Path, monkeypatch,
) -> None:
    too_many_bindings = write(tmp_path / "bindings.env", "A=1\nB=2\nC=3\n")
    monkeypatch.setattr(checker_module, "MAX_ENV_BINDINGS", 2)

    with pytest.raises(EnvFileParseError, match="more than 2 bindings"):
        load_env_file(too_many_bindings)

    too_many_lines = write(tmp_path / "lines.env", "# one\n# two\n# three\n")
    monkeypatch.setattr(checker_module, "MAX_ENV_LINES", 2)

    with pytest.raises(EnvFileParseError, match="more than 2 lines"):
        load_env_file(too_many_lines)


def test_load_env_file_checks_bytes_read_after_fstat(
    tmp_path: Path, monkeypatch,
) -> None:
    env = write(tmp_path / ".env", "TOKEN=value\n")
    real_fstat = checker_module.os.fstat

    def stale_fstat(descriptor: int):
        current = real_fstat(descriptor)
        return SimpleNamespace(st_mode=current.st_mode, st_size=0)

    monkeypatch.setattr(checker_module, "MAX_ENV_FILE_SIZE", 4)
    monkeypatch.setattr(checker_module.os, "fstat", stale_fstat)

    with pytest.raises(OSError, match="larger than 4 bytes"):
        load_env_file(env)


def test_load_env_file_does_not_interpolate_process_environment(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("HUGE_SECRET", "do-not-expand")
    env = write(tmp_path / ".env", "TOKEN=${HUGE_SECRET}\n")

    assert load_env_file(env) == {"TOKEN": "${HUGE_SECRET}"}


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


def test_required_ignore_file_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_ignore_patterns(tmp_path / ".envignore", required=True)


def test_load_ignore_patterns_strips_utf8_bom(tmp_path: Path) -> None:
    ignore = tmp_path / ".envignore"
    ignore.write_text("\ufeffTEST_*\nLOCAL_*\n", encoding="utf-8")

    assert load_ignore_patterns(ignore) == ["TEST_*", "LOCAL_*"]


def test_load_ignore_patterns_rejects_non_regular_path(tmp_path: Path) -> None:
    ignore = tmp_path / ".envignore"
    ignore.mkdir()

    with pytest.raises(OSError, match="not a regular file"):
        load_ignore_patterns(ignore, required=True)


def test_load_ignore_patterns_rejects_oversized_input(
    tmp_path: Path, monkeypatch,
) -> None:
    ignore = write(tmp_path / ".envignore", "TOKEN\n")
    monkeypatch.setattr(checker_module, "MAX_IGNORE_FILE_SIZE", 3)

    with pytest.raises(OSError, match="larger than 3 bytes"):
        load_ignore_patterns(ignore, required=True)


def test_load_ignore_patterns_checks_bytes_read_after_fstat(
    tmp_path: Path, monkeypatch,
) -> None:
    ignore = write(tmp_path / ".envignore", "TOKEN\n")
    real_fstat = checker_module.os.fstat

    def stale_fstat(descriptor: int):
        current = real_fstat(descriptor)
        return SimpleNamespace(st_mode=current.st_mode, st_size=0)

    monkeypatch.setattr(checker_module, "MAX_IGNORE_FILE_SIZE", 3)
    monkeypatch.setattr(checker_module.os, "fstat", stale_fstat)

    with pytest.raises(OSError, match="larger than 3 bytes"):
        load_ignore_patterns(ignore, required=True)


def test_load_ignore_patterns_caps_count_and_length(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(checker_module, "MAX_IGNORE_PATTERNS", 2)
    too_many = write(tmp_path / ".envignore", "A\nB\nC\n")

    with pytest.raises(OSError, match="more than 2 patterns"):
        load_ignore_patterns(too_many, required=True)

    monkeypatch.setattr(checker_module, "MAX_IGNORE_PATTERN_LENGTH", 2)
    too_long = write(tmp_path / ".envignore", "LONG\n")

    with pytest.raises(OSError, match="longer than 2"):
        load_ignore_patterns(too_long, required=True)


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


def test_check_bare_dotenv_key_is_still_missing(tmp_path: Path) -> None:
    code = tmp_path / "src"
    write(code / "a.py", "import os\nos.getenv('BARE')\nos.getenv('EMPTY')\n")
    env = write(tmp_path / ".env", "BARE\nEMPTY=\n")

    report = check(scan_project(code), env)

    assert [v.name for v in report.missing] == ["BARE"]
    assert [v.name for v in report.present] == ["EMPTY"]
    assert report.extra_in_env == []


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


def test_missing_group_tracks_only_unsatisfied_usages(tmp_path: Path) -> None:
    code = tmp_path / "src"
    write(code / "a_defaulted.py", """
        import os
        os.getenv("TOKEN", "fallback")
    """)
    write(code / "z_required.py", """
        import os
        os.environ["TOKEN"]
    """)
    env = write(tmp_path / ".env", "")

    report = check(scan_project(code), env)
    variable = report.missing[0]

    assert len(variable.usages) == 2
    assert [usage.file.name for usage in variable.missing_usages] == [
        "z_required.py"
    ]


def test_default_none_does_not_satisfy_strict_check(tmp_path: Path) -> None:
    code = tmp_path / "src"
    write(code / "a.py", "import os\nos.getenv('REQUIRED', None)\n")
    env = write(tmp_path / ".env", "")

    report = check(scan_project(code), env)

    assert [v.name for v in report.missing] == ["REQUIRED"]
    assert report.with_default == []
    assert report.has_issues is True


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


def test_check_extra_in_env_respects_ignore_patterns(tmp_path: Path) -> None:
    # if user has TEST_* in .envignore they don't want TEST_FOO in .env to show up
    # as 'extra' either — be consistent
    code = tmp_path / "src"
    write(code / "a.py", """
        import os
        os.getenv("USED")
    """)
    env = write(tmp_path / ".env", """
        USED=1
        TEST_FOO=2
        TEST_BAR=3
        REGULAR_UNUSED=4
    """)

    scan = scan_project(code)
    report = check(scan, env, ignore_patterns=["TEST_*"])

    assert report.extra_in_env == ["REGULAR_UNUSED"]


def test_check_extra_in_env_respects_dynamic_usages(tmp_path: Path) -> None:
    # bug fix: if code has os.getenv(f"PREFIX_{x}"), then PREFIX_FOO in .env
    # is not really "unused" — it might be the runtime target. don't pester user.
    code = tmp_path / "src"
    write(code / "a.py", """
        import os
        for x in ["FOO", "BAR"]:
            os.getenv(f"PREFIX_{x}")
        os.getenv("REAL_VAR")
    """)
    env = write(tmp_path / ".env", """
        REAL_VAR=1
        PREFIX_FOO=2
        PREFIX_BAR=3
        TOTALLY_UNRELATED=4
    """)

    scan = scan_project(code)
    report = check(scan, env)

    # PREFIX_* matches the dynamic expression, only TOTALLY_UNRELATED is a real extra
    assert report.extra_in_env == ["TOTALLY_UNRELATED"]


def test_dynamic_extra_heuristic_honors_prefix_suffix_and_format(tmp_path: Path) -> None:
    code = tmp_path / "src"
    write(code / "a.py", """
        import os
        region = "EU"
        os.getenv(f"{region}_TOKEN")
        os.getenv(f"APP_{region}_SECRET")
        os.getenv("REGION_{}_URL".format(region))
        os.getenv(region + "_CERT")
    """)
    env = write(tmp_path / ".env", """
        EU_TOKEN=1
        APP_EU_SECRET=2
        REGION_EU_URL=3
        EU_CERT=4
        TOKEN_EU=5
        APP_EU_OTHER=6
    """)

    report = check(scan_project(code), env)

    assert report.extra_in_env == ["APP_EU_OTHER", "TOKEN_EU"]


def test_dynamic_extra_heuristic_supports_percent_formatting(tmp_path: Path) -> None:
    code = tmp_path / "src"
    write(code / "a.py", """
        import os
        region = "EU"
        kind = "URL"
        os.getenv("PREFIX_%s" % region)
        os.getenv("%s_TOKEN" % region)
        os.getenv("APP_%s_%s" % (region, kind))
        os.getenv("RATE_%%_%s" % kind)
    """)
    env = write(tmp_path / ".env", """
        PREFIX_EU=1
        EU_TOKEN=2
        APP_EU_URL=3
        RATE_%_URL=4
        OTHER=5
    """)

    report = check(scan_project(code), env)

    assert report.extra_in_env == ["OTHER"]


def test_dynamic_index_handles_unanchored_literal_fragments(tmp_path: Path) -> None:
    code = tmp_path / "src"
    write(code / "a.py", """
        import os
        os.getenv(f"{left}_MID_{right}")
        os.getenv(f"{left}A{middle}B{right}")
    """)
    env = write(tmp_path / ".env", """
        LEFT_MID_RIGHT=1
        xAyBz=2
        LEFT_OTHER_RIGHT=3
        xAyCz=4
    """)

    report = check(scan_project(code), env)

    assert report.extra_in_env == ["LEFT_OTHER_RIGHT", "xAyCz"]


def test_dynamic_parts_match_large_ambiguous_input_without_regex() -> None:
    parts = [None]
    for _ in range(2_000):
        parts.extend(["A", None])
    parts.append("B")

    assert checker_module._matches_dynamic_parts(
        "A" * 2_000 + "B", parts, case_sensitive=True
    )
    assert not checker_module._matches_dynamic_parts(
        "A" * 2_000 + "C", parts, case_sensitive=True
    )


def test_dynamic_expressions_are_parsed_once_per_check(
    tmp_path: Path, monkeypatch
) -> None:
    env_path = write(
        tmp_path / ".env",
        "".join(f"EXTRA_{index}=1\n" for index in range(80)),
    )
    usages = [
        EnvUsage(
            name=None,
            file=tmp_path / "app.py",
            line=index + 1,
            raw_expr=f"'PREFIX_{index}_' + suffix",
        )
        for index in range(60)
    ]

    real_parse = ast.parse
    calls = 0

    def counting_parse(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real_parse(*args, **kwargs)

    monkeypatch.setattr("envsleuth.checker.ast.parse", counting_parse)

    check(ScanResult(usages=usages), env_path)
    assert calls == len(usages)


def test_dynamic_candidate_index_avoids_cartesian_matching(
    tmp_path: Path, monkeypatch,
) -> None:
    count = 300
    env_path = write(
        tmp_path / ".env",
        "".join(f"EXTRA_{index}=1\n" for index in range(count)),
    )
    usages = [
        EnvUsage(
            name=None,
            file=tmp_path / "app.py",
            line=index + 1,
            raw_expr=f"'PREFIX_{index}_' + suffix",
        )
        for index in range(count)
    ]
    real_match = checker_module._matches_dynamic_parts
    calls = 0

    def counting_match(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real_match(*args, **kwargs)

    monkeypatch.setattr(checker_module, "_matches_dynamic_parts", counting_match)

    report = check(ScanResult(usages=usages), env_path)

    assert len(report.extra_in_env) == count
    assert calls <= count


def test_dynamic_index_prefers_a_selective_internal_literal(monkeypatch) -> None:
    count = 300
    patterns = [
        ["P", None, f"UNIQUE_{index}_", None]
        for index in range(count)
    ]
    index = checker_module._build_dynamic_pattern_index(patterns, True)
    real_match = checker_module._matches_dynamic_parts
    calls = 0

    def counting_match(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real_match(*args, **kwargs)

    monkeypatch.setattr(checker_module, "_matches_dynamic_parts", counting_match)

    assert not any(
        checker_module._looks_like_dynamic_match(f"P_EXTRA_{i}", index)
        for i in range(count)
    )
    assert calls <= count


def test_dynamic_index_caps_memory_for_a_long_literal() -> None:
    prefix = "A" * 100_000
    parts = [[prefix, None]]

    index = checker_module._build_dynamic_pattern_index(parts, True)

    assert len(index.prefixes) <= checker_module._DYNAMIC_ANCHOR_LENGTH + 1
    assert checker_module._looks_like_dynamic_match(prefix + "tail", index)
    assert not checker_module._looks_like_dynamic_match("A" * 99_999, index)


def test_check_uses_env_existence_from_the_loaded_snapshot(
    tmp_path: Path, monkeypatch,
) -> None:
    env_path = tmp_path / ".env"

    def missing_snapshot(path: Path):
        path.write_text("LATE=1\n", encoding="utf-8")
        return False, {}

    monkeypatch.setattr(
        checker_module, "_load_env_file_snapshot", missing_snapshot,
    )

    report = check(ScanResult(), env_path)

    assert env_path.is_file()
    assert report.env_file_exists is False
    assert report.has_issues is True


def test_dynamic_extra_heuristic_does_not_hide_weak_matches(tmp_path: Path) -> None:
    code = tmp_path / "src"
    write(code / "a.py", """
        import os
        os.getenv(name)
        os.getenv(f"{left}_{right}")
    """)
    env = write(tmp_path / ".env", "ANY_NAME=1\nOTHER=2\n")

    report = check(scan_project(code), env)

    assert report.extra_in_env == ["ANY_NAME", "OTHER"]


def test_has_default_requires_all_usages_to_have_default(tmp_path: Path) -> None:
    # behavior change: previously `any()` of usages had default → counted as default.
    # now we require all usages to have default. otherwise the no-default usage
    # site would silently fail at runtime when the env var is missing.
    code = tmp_path / "src"
    write(code / "a.py", """
        import os
        os.getenv("MAYBE_BAD")          # no default — would return None
        os.getenv("MAYBE_BAD", "x")     # has default
    """)
    env = write(tmp_path / ".env", "")  # empty .env

    scan = scan_project(code)
    report = check(scan, env)

    # since one site has no default, treat as required (missing)
    assert [v.name for v in report.missing] == ["MAYBE_BAD"]
    assert report.with_default == []


def test_has_default_when_all_usages_have_default(tmp_path: Path) -> None:
    code = tmp_path / "src"
    write(code / "a.py", """
        import os
        os.getenv("OK", "a")
        os.getenv("OK", "b")
    """)
    env = write(tmp_path / ".env", "")

    scan = scan_project(code)
    report = check(scan, env)
    assert [v.name for v in report.with_default] == ["OK"]


def test_check_env_file_missing(tmp_path: Path) -> None:
    code = tmp_path / "src"
    write(code / "a.py", "import os\nos.getenv('X')\n")
    missing_env = tmp_path / "nope.env"

    scan = scan_project(code)
    report = check(scan, missing_env)

    assert report.env_file_exists is False
    # All vars are 'missing' because no .env means none are present.
    assert [v.name for v in report.missing] == ["X"]
    assert report.has_issues is True


def test_missing_env_is_an_issue_even_without_required_vars(tmp_path: Path) -> None:
    scan = scan_project(tmp_path)

    report = check(scan, tmp_path / "missing.env")

    assert report.missing == []
    assert report.has_issues is True


def test_windows_env_names_are_compared_case_insensitively(
    tmp_path: Path, monkeypatch
) -> None:
    code = tmp_path / "src"
    write(code / "a.py", "import os\nos.getenv('Mixed_Case')\n")
    env = write(tmp_path / ".env", "MIXED_CASE=value\n")
    monkeypatch.setattr(checker_module, "_env_names_case_sensitive", lambda: False)

    report = check(scan_project(code), env)

    assert [v.name for v in report.present] == ["Mixed_Case"]
    assert report.extra_in_env == []


def test_posix_env_names_remain_case_sensitive(tmp_path: Path, monkeypatch) -> None:
    code = tmp_path / "src"
    write(code / "a.py", "import os\nos.getenv('Mixed_Case')\n")
    env = write(tmp_path / ".env", "MIXED_CASE=value\n")
    monkeypatch.setattr(checker_module, "_env_names_case_sensitive", lambda: True)

    report = check(scan_project(code), env)

    assert [v.name for v in report.missing] == ["Mixed_Case"]
    assert report.extra_in_env == ["MIXED_CASE"]


def test_pydantic_usage_can_match_env_name_case_insensitively(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(
        checker_module, "_env_names_case_sensitive", lambda: True,
    )
    env = write(tmp_path / ".env", "API_KEY=value\n")
    usage = EnvUsage(
        name="api_key",
        file=tmp_path / "settings.py",
        line=4,
        call_type="pydantic_settings",
        accepted_names=("api_key",),
        case_sensitive=False,
    )

    report = check(ScanResult(usages=[usage]), env)

    assert [item.name for item in report.present] == ["api_key"]
    assert report.missing == []
    assert report.extra_in_env == []


def test_same_name_with_mixed_case_modes_is_one_requirement(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(
        checker_module, "_env_names_case_sensitive", lambda: True,
    )
    env = write(tmp_path / ".env", "TOKEN=value\n")
    usages = [
        EnvUsage(
            name="TOKEN", file=tmp_path / "app.py", line=1,
            call_type="getenv",
        ),
        EnvUsage(
            name="TOKEN", file=tmp_path / "settings.py", line=2,
            call_type="pydantic_settings", case_sensitive=False,
        ),
    ]

    report = check(ScanResult(usages=usages), env)

    assert len(report.variables) == 1
    assert report.variables[0].status == "present"
    assert len(report.variables[0].usages) == 2


def test_case_sensitive_dotenv_keeps_case_variants_on_windows(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(
        checker_module, "_env_names_case_sensitive", lambda: False,
    )
    env = write(tmp_path / ".env", "token=lower\nTOKEN=upper\n")
    usage = EnvUsage(
        name="token",
        file=tmp_path / "settings.py",
        line=2,
        call_type="pydantic_settings",
        case_sensitive=True,
    )

    report = check(ScanResult(usages=[usage]), env)

    assert [item.name for item in report.present] == ["token"]
    assert report.extra_in_env == ["TOKEN"]


def test_dynamic_pydantic_pattern_uses_its_case_mode(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(
        checker_module, "_env_names_case_sensitive", lambda: True,
    )
    env = write(tmp_path / ".env", "APP_TOKEN=value\n")
    usage = EnvUsage(
        name=None,
        file=tmp_path / "settings.py",
        line=2,
        call_type="pydantic_settings",
        raw_expr='f"app_{field}"',
        case_sensitive=False,
    )

    report = check(ScanResult(usages=[usage]), env)

    assert report.extra_in_env == []


def test_pydantic_none_default_is_not_required(tmp_path: Path) -> None:
    env = write(tmp_path / ".env", "")
    usage = EnvUsage(
        name="optional_token",
        file=tmp_path / "settings.py",
        line=4,
        has_default=True,
        default_node=ast.Constant(value=None),
        call_type="pydantic_settings",
        case_sensitive=False,
    )

    report = check(ScanResult(usages=[usage]), env)

    assert [item.name for item in report.with_default] == ["optional_token"]
    assert report.missing == []


def test_pydantic_scanner_and_checker_integration(tmp_path: Path) -> None:
    code = tmp_path / "src"
    write(code / "settings.py", """
        from pydantic import AliasChoices, Field
        from pydantic_settings import BaseSettings, SettingsConfigDict

        class Settings(BaseSettings):
            model_config = SettingsConfigDict(env_prefix="APP_")
            required: str
            generated: str = Field(default_factory=lambda: "fallback")
            token: str = Field(
                validation_alias=AliasChoices("TOKEN", "OLD_TOKEN")
            )
    """)
    env = write(
        tmp_path / ".env",
        "APP_REQUIRED=value\nOLD_TOKEN=legacy\n",
    )

    report = check(scan_project(code), env)

    assert report.missing == []
    assert [item.name for item in report.present] == [
        "APP_required", "TOKEN",
    ]
    assert [item.name for item in report.with_default] == ["APP_generated"]
    assert report.extra_in_env == []


def test_alias_choices_are_one_requirement_and_all_aliases_are_known(
    tmp_path: Path,
) -> None:
    env = write(tmp_path / ".env", "LEGACY_TOKEN=value\nTOKEN=also-valid\n")
    usage = EnvUsage(
        name="TOKEN",
        file=tmp_path / "settings.py",
        line=5,
        call_type="pydantic_settings",
        accepted_names=("TOKEN", "LEGACY_TOKEN"),
        case_sensitive=False,
    )

    report = check(ScanResult(usages=[usage]), env)

    assert [item.name for item in report.present] == ["TOKEN"]
    assert report.extra_in_env == []


def test_alias_choice_does_not_mask_an_unsatisfied_plain_usage(
    tmp_path: Path,
) -> None:
    env = write(tmp_path / ".env", "LEGACY_TOKEN=value\n")
    settings = tmp_path / "settings.py"
    choices = EnvUsage(
        name="TOKEN",
        file=settings,
        line=5,
        call_type="pydantic_settings",
        accepted_names=("TOKEN", "LEGACY_TOKEN"),
        case_sensitive=False,
    )
    plain = EnvUsage(
        name="TOKEN",
        file=settings,
        line=9,
        call_type="getenv",
        case_sensitive=False,
    )

    report = check(ScanResult(usages=[choices, plain]), env)

    assert [item.name for item in report.missing] == ["TOKEN"]


def test_default_only_needs_to_cover_unsatisfied_usage(tmp_path: Path) -> None:
    env = write(tmp_path / ".env", "LEGACY_TOKEN=value\n")
    settings = tmp_path / "settings.py"
    choices = EnvUsage(
        name="TOKEN",
        file=settings,
        line=5,
        accepted_names=("TOKEN", "LEGACY_TOKEN"),
        case_sensitive=False,
    )
    default_node = ast.Constant(value="fallback")
    plain = EnvUsage(
        name="TOKEN",
        file=settings,
        line=9,
        has_default=True,
        default_node=default_node,
        case_sensitive=False,
    )

    report = check(ScanResult(usages=[choices, plain]), env)

    assert [item.name for item in report.with_default] == ["TOKEN"]
    assert report.missing == []


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
