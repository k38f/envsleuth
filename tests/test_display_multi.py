from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from envsleuth.checker import CheckReport, VarReport
from envsleuth.display import (
    render_reports,
    render_reports_github,
    render_reports_json,
)
from envsleuth.scanner import EnvUsage


def _usage(name: Optional[str], line: int = 2) -> EnvUsage:
    return EnvUsage(
        name=name,
        file=Path("src/app.py"),
        line=line,
        call_type="getenv",
        raw_expr="name" if name is None else None,
    )


def _report(env: str, *, present: bool) -> CheckReport:
    usage = _usage("TOKEN")
    return CheckReport(
        variables=[
            VarReport(
                name="TOKEN",
                present_in_env=present,
                has_default_in_code=False,
                usages=[usage],
            )
        ],
        dynamic_usages=[_usage(None, 4)],
        env_file=Path(env),
        env_file_exists=True,
        errors=[(Path("broken.py"), "syntax error")],
    )


def test_one_report_keeps_legacy_json_shape() -> None:
    payload = json.loads(render_reports_json([_report(".env", present=True)]))

    assert payload["env_file"] == ".env"
    assert "reports" not in payload


def test_multi_json_has_ordered_profiles_and_global_findings_once() -> None:
    payload = json.loads(render_reports_json([
        _report(".env.dev", present=True),
        _report(".env.prod", present=False),
    ]))

    assert payload["env_files"] == [".env.dev", ".env.prod"]
    assert payload["summary"]["missing"] == 1
    assert len(payload["dynamic_usages"]) == 1
    assert len(payload["errors"]) == 1
    assert "dynamic_usages" not in payload["reports"][0]
    assert "errors" not in payload["reports"][1]
    assert "dynamic" not in payload["reports"][0]["summary"]


def test_multi_text_labels_profiles_and_does_not_repeat_scan_error() -> None:
    text = render_reports([
        _report(".env.dev", present=True),
        _report(".env.prod", present=False),
    ], use_color=False)

    assert "Environment 1/2: .env.dev" in text
    assert "Environment 2/2: .env.prod" in text
    assert text.count("syntax error") == 1


def test_multi_github_does_not_duplicate_global_annotations() -> None:
    text = render_reports_github([
        _report(".env.dev", present=True),
        _report(".env.prod", present=False),
    ])

    assert text.count("Dynamic env var") == 2  # title and message
    assert text.count("Scan error") == 2
    assert ".env.prod" in text


def test_multi_github_does_not_claim_success_after_shared_error() -> None:
    text = render_reports_github([
        _report(".env.dev", present=True),
        _report(".env.prod", present=True),
    ])

    assert "Scan error" in text
    assert "all required env vars defined" not in text


def test_json_does_not_echo_ignore_pattern_contents(tmp_path: Path) -> None:
    report = CheckReport(
        env_file=tmp_path / ".env",
        env_file_exists=True,
        ignore_patterns=["SECRET_MATERIAL_*"],
    )

    rendered = render_reports_json([report])
    payload = json.loads(rendered)

    assert payload["ignore_pattern_count"] == 1
    assert "ignore_patterns" not in payload
    assert "SECRET_MATERIAL" not in rendered
