from __future__ import annotations

import json
from pathlib import Path

import envsleuth.sarif as sarif_module
from envsleuth.checker import CheckReport, VarReport
from envsleuth.sarif import (
    MAX_RESULTS,
    SARIF_SCHEMA,
    render_error_sarif,
    render_report_sarif,
)
from envsleuth.scanner import EnvUsage


def usage(
    file: Path,
    line: int,
    *,
    name: str | None = None,
    call_type: str = "getenv",
    raw_expr: str | None = None,
) -> EnvUsage:
    return EnvUsage(
        name=name,
        file=file,
        line=line,
        call_type=call_type,
        raw_expr=raw_expr,
    )


def missing(name: str, usages: list[EnvUsage]) -> VarReport:
    return VarReport(
        name=name,
        present_in_env=False,
        has_default_in_code=False,
        usages=usages,
    )


def payload(text: str) -> dict:
    return json.loads(text)


def test_minimal_structure_and_fixed_rules(tmp_path: Path) -> None:
    report = CheckReport(
        env_file=tmp_path / ".env",
        env_file_exists=True,
    )

    data = payload(render_report_sarif(report, tmp_path, "1.0.0"))
    run = data["runs"][0]

    assert data["$schema"] == SARIF_SCHEMA
    assert data["version"] == "2.1.0"
    assert run["tool"]["driver"]["name"] == "envsleuth"
    assert run["tool"]["driver"]["version"] == "1.0.0"
    assert run["results"] == []
    assert [rule["id"] for rule in run["tool"]["driver"]["rules"]] == [
        "envsleuth/missing-environment-variable",
        "envsleuth/extra-environment-variable",
        "envsleuth/dynamic-environment-variable",
        "envsleuth/scan-error",
        "envsleuth/operational-error",
    ]
    assert MAX_RESULTS == 25_000


def test_findings_have_expected_rules_and_missing_is_aggregated(
    tmp_path: Path,
) -> None:
    app = tmp_path / "src" / "app.py"
    report = CheckReport(
        variables=[missing("TOKEN", [
            usage(app, 12, name="TOKEN"),
            usage(app, 30, name="TOKEN"),
        ])],
        dynamic_usages=[
            usage(app, 20, call_type="getenv", raw_expr="prefix + name"),
        ],
        env_file=tmp_path / ".env.dev",
        env_file_exists=True,
        extra_in_env=["OLD_TOKEN"],
        errors=[(tmp_path / "broken.py", "syntax error")],
    )

    results = payload(
        render_report_sarif(report, tmp_path, "1.0.0")
    )["runs"][0]["results"]
    by_rule = {result["ruleId"]: result for result in results}

    assert len(results) == 4
    missing_result = by_rule["envsleuth/missing-environment-variable"]
    assert missing_result["ruleIndex"] == 0
    assert missing_result["level"] == "error"
    assert missing_result["locations"][0]["physicalLocation"] == {
        "artifactLocation": {"uri": "src/app.py"},
        "region": {"startLine": 12},
    }
    assert ".env.dev" in missing_result["message"]["text"]
    assert by_rule["envsleuth/extra-environment-variable"]["level"] == "note"
    assert by_rule["envsleuth/extra-environment-variable"]["locations"] == [{
        "physicalLocation": {
            "artifactLocation": {"uri": ".env.dev"},
        }
    }]
    assert by_rule["envsleuth/dynamic-environment-variable"]["level"] == (
        "warning"
    )
    assert by_rule["envsleuth/scan-error"]["locations"] == [{
        "physicalLocation": {
            "artifactLocation": {"uri": "broken.py"},
        }
    }]


def test_scan_error_message_redacts_absolute_path(tmp_path: Path) -> None:
    broken = tmp_path / "src" / "broken.py"
    report = CheckReport(
        env_file=tmp_path / ".env",
        env_file_exists=True,
        errors=[(
            broken,
            f"could not read {broken}: permission denied",
        )],
    )

    rendered = render_report_sarif(report, tmp_path, "1.0.0")
    result = payload(rendered)["runs"][0]["results"][0]

    assert str(tmp_path) not in rendered
    assert "src/broken.py" in result["message"]["text"]
    assert result["locations"][0]["physicalLocation"] == {
        "artifactLocation": {"uri": "src/broken.py"}
    }


def test_missing_location_uses_only_unsatisfied_usage(tmp_path: Path) -> None:
    defaulted = usage(
        tmp_path / "a_defaulted.py", 2, name="TOKEN",
    )
    required = usage(
        tmp_path / "z_required.py", 2, name="TOKEN",
    )
    report = CheckReport(
        variables=[VarReport(
            name="TOKEN",
            present_in_env=False,
            has_default_in_code=False,
            usages=[defaulted, required],
            missing_usages=[required],
        )],
        env_file=tmp_path / ".env",
        env_file_exists=True,
    )

    result = payload(
        render_report_sarif(report, tmp_path, "1.0.0")
    )["runs"][0]["results"][0]

    assert result["locations"][0]["physicalLocation"] == {
        "artifactLocation": {"uri": "z_required.py"},
        "region": {"startLine": 2},
    }


def test_uri_encoding_and_untrusted_text_are_safe(tmp_path: Path) -> None:
    odd = tmp_path / "odd #?%é.py"
    raw_name = "TOKEN\nFORGED\x1b]8;;evil\x07CLICK\u202e"
    report = CheckReport(
        variables=[missing(raw_name, [usage(odd, 4, name=raw_name)])],
        env_file=tmp_path / ".env",
        env_file_exists=True,
    )

    rendered = render_report_sarif(report, tmp_path, "1.0.0")
    result = payload(rendered)["runs"][0]["results"][0]
    message = result["message"]["text"]
    uri = result["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]

    assert uri == "odd%20%23%3F%25%C3%A9.py"
    assert "\\nFORGED" in message
    assert "\nFORGED" not in message
    assert "\x1b" not in message
    assert "\x07" not in message
    assert "\u202e" not in message
    assert "\\u202e" in message
    assert all(ord(char) < 128 for char in rendered)


def test_output_is_deterministic_when_input_order_changes(
    tmp_path: Path,
) -> None:
    a = usage(tmp_path / "b.py", 8, name="TOKEN")
    b = usage(tmp_path / "a.py", 3, name="TOKEN")
    first = CheckReport(
        variables=[missing("TOKEN", [a, b])],
        dynamic_usages=[
            usage(tmp_path / "z.py", 2, raw_expr="name"),
            usage(tmp_path / "c.py", 9, raw_expr="other"),
        ],
        env_file=tmp_path / ".env",
        env_file_exists=True,
        extra_in_env=["ZED", "ALPHA"],
    )
    second = CheckReport(
        variables=[missing("TOKEN", [b, a])],
        dynamic_usages=list(reversed(first.dynamic_usages)),
        env_file=first.env_file,
        env_file_exists=True,
        extra_in_env=list(reversed(first.extra_in_env)),
    )

    one = render_report_sarif(first, tmp_path, "1.0.0")
    two = render_report_sarif(second, tmp_path, "1.0.0")

    assert one == two


def test_multiple_reports_distinguish_env_and_dedupe_global_findings(
    tmp_path: Path,
) -> None:
    app = tmp_path / "app.py"
    dynamic = usage(app, 7, raw_expr="name")
    error = (tmp_path / "bad.py", "syntax error")
    dev = CheckReport(
        variables=[missing("TOKEN", [usage(app, 2, name="TOKEN")])],
        dynamic_usages=[dynamic],
        env_file=tmp_path / ".env.dev",
        env_file_exists=True,
        errors=[error],
    )
    prod = CheckReport(
        variables=[missing("TOKEN", [usage(app, 2, name="TOKEN")])],
        dynamic_usages=[dynamic],
        env_file=tmp_path / ".env.prod",
        env_file_exists=True,
        errors=[error],
    )

    results = payload(
        render_report_sarif([prod, dev], tmp_path, "1.0.0")
    )["runs"][0]["results"]
    rule_ids = [result["ruleId"] for result in results]

    assert rule_ids.count("envsleuth/missing-environment-variable") == 2
    assert rule_ids.count("envsleuth/dynamic-environment-variable") == 1
    assert rule_ids.count("envsleuth/scan-error") == 1
    messages = [result["message"]["text"] for result in results]
    assert any(".env.dev" in message for message in messages)
    assert any(".env.prod" in message for message in messages)


def test_result_cap_adds_invocation_notification(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(sarif_module, "MAX_RESULTS", 2)
    report = CheckReport(
        env_file=tmp_path / ".env",
        env_file_exists=True,
        extra_in_env=["C", "A", "B"],
    )

    run = payload(
        render_report_sarif(report, tmp_path, "1.0.0")
    )["runs"][0]

    assert len(run["results"]) == 2
    notification = run["invocations"][0]["toolExecutionNotifications"][0]
    assert notification["level"] == "warning"
    assert "1 additional result was omitted" in notification["message"]["text"]


def test_result_cap_reserves_each_present_rule_category(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(sarif_module, "MAX_RESULTS", 3)
    app = tmp_path / "app.py"
    report = CheckReport(
        variables=[
            missing(f"MISSING_{index}", [
                usage(app, index + 1, name=f"MISSING_{index}")
            ])
            for index in range(10)
        ],
        dynamic_usages=[usage(app, 20, raw_expr="name")],
        env_file=tmp_path / ".env",
        env_file_exists=True,
        extra_in_env=["EXTRA"],
    )

    results = payload(
        render_report_sarif(report, tmp_path, "1.0.0")
    )["runs"][0]["results"]

    assert {result["ruleId"] for result in results} == {
        "envsleuth/missing-environment-variable",
        "envsleuth/extra-environment-variable",
        "envsleuth/dynamic-environment-variable",
    }


def test_outside_root_and_invalid_line_use_safe_fallback_locations(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    outside = tmp_path / "outside.py"
    report = CheckReport(
        variables=[missing("TOKEN", [usage(outside, 4, name="TOKEN")])],
        dynamic_usages=[usage(root / "inside.py", 0, raw_expr="name")],
        env_file=tmp_path / "external" / ".env",
        env_file_exists=True,
    )

    rendered = render_report_sarif(report, root, "1.0.0")
    results = payload(rendered)["runs"][0]["results"]

    by_rule = {result["ruleId"]: result for result in results}
    assert by_rule["envsleuth/missing-environment-variable"][
        "locations"
    ] == [{
        "physicalLocation": {"artifactLocation": {"uri": "."}}
    }]
    assert by_rule["envsleuth/dynamic-environment-variable"][
        "locations"
    ] == [{
        "physicalLocation": {
            "artifactLocation": {"uri": "inside.py"}
        }
    }]
    assert str(tmp_path) not in rendered
    assert "../" not in rendered


def test_operational_error_is_valid_sarif_and_truncates_text(
    tmp_path: Path,
) -> None:
    rendered = render_error_sarif(
        "bad\n" + "x" * 5000 + "\x1b",
        tmp_path,
        "1.0.0",
        path=tmp_path.parent / "secret" / ".env",
    )
    data = payload(rendered)
    result = data["runs"][0]["results"][0]

    assert result["ruleId"] == "envsleuth/operational-error"
    assert result["ruleIndex"] == 4
    assert result["level"] == "error"
    assert len(result["message"]["text"]) <= sarif_module.MAX_MESSAGE_LENGTH
    assert "\\n" in result["message"]["text"]
    assert "\x1b" not in result["message"]["text"]
    assert result["locations"] == [{
        "physicalLocation": {"artifactLocation": {"uri": "."}}
    }]
    assert str(tmp_path) not in rendered


def test_operational_error_redacts_external_path_from_message(
    tmp_path: Path,
) -> None:
    external = tmp_path.parent / "private" / "secrets.env"
    rendered = render_error_sarif(
        f"could not read environment file {external}: denied",
        tmp_path,
        "1.0.0",
        path=external,
    )

    result = payload(rendered)["runs"][0]["results"][0]

    assert str(external) not in rendered
    assert "secrets.env" in result["message"]["text"]


def test_missing_env_suppresses_derived_missing_findings(tmp_path: Path) -> None:
    app = tmp_path / "app.py"
    report = CheckReport(
        variables=[missing("TOKEN", [usage(app, 2, name="TOKEN")])],
        env_file=tmp_path / ".env",
        env_file_exists=False,
    )

    results = payload(
        render_report_sarif(report, tmp_path, "1.0.0")
    )["runs"][0]["results"]

    assert [item["ruleId"] for item in results] == [
        "envsleuth/operational-error"
    ]
    assert results[0]["locations"][0]["physicalLocation"] == {
        "artifactLocation": {"uri": ".env"}
    }


def test_long_names_do_not_collide_after_display_truncation(
    tmp_path: Path,
) -> None:
    prefix = "X" * (sarif_module.MAX_NAME_LENGTH + 20)
    report = CheckReport(
        env_file=tmp_path / ".env",
        env_file_exists=True,
        extra_in_env=[prefix + "A", prefix + "B"],
    )

    results = payload(
        render_report_sarif(report, tmp_path, "1.0.0")
    )["runs"][0]["results"]

    assert len(results) == 2


def test_unicode_line_separators_are_escaped(tmp_path: Path) -> None:
    report = CheckReport(
        variables=[missing(
            "TOKEN\u2028FORGED\u2029END",
            [usage(tmp_path / "app.py", 1, name="TOKEN")],
        )],
        env_file=tmp_path / ".env",
        env_file_exists=True,
    )

    result = payload(
        render_report_sarif(report, tmp_path, "1.0.0")
    )["runs"][0]["results"][0]

    assert "\u2028" not in result["message"]["text"]
    assert "\u2029" not in result["message"]["text"]
    assert "\\u2028" in result["message"]["text"]
    assert "\\u2029" in result["message"]["text"]
