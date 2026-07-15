"""Integration tests for the command-line interface."""

from __future__ import annotations

import io
import json
from pathlib import Path

from click.testing import CliRunner

import envsleuth.cli as cli_module
from envsleuth.cli import cli


def write(root: Path, name: str, content: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def invoke(*args: str):
    return CliRunner().invoke(cli, list(args))


def test_scan_clean_project(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "app.py", "import os\nos.getenv('TOKEN')\n")
    write(tmp_path, ".env", "TOKEN=secret\n")

    result = invoke("scan", "--no-color", "--no-update-check")

    assert result.exit_code == 0, result.output
    assert "TOKEN" in result.output
    assert "1 ok" in result.output


def test_scan_reuses_precomputed_file_list(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "app.py", "value = 1\n")
    write(tmp_path, ".env", "")
    real_iter = cli_module.iter_python_files
    real_scan = cli_module.scan_project
    calls = {"walk": 0}

    def counted_iter(*args, **kwargs):
        calls["walk"] += 1
        return real_iter(*args, **kwargs)

    def checked_scan(*args, **kwargs):
        assert kwargs["files"] == [tmp_path / "app.py"]
        return real_scan(*args, **kwargs)

    monkeypatch.setattr(cli_module, "iter_python_files", counted_iter)
    monkeypatch.setattr(cli_module, "scan_project", checked_scan)

    result = invoke("scan", "--output", "json")

    assert result.exit_code == 0, result.output
    assert calls["walk"] == 1


def test_strict_missing_variable_exits_one(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "app.py", "import os\nos.getenv('TOKEN')\n")
    write(tmp_path, ".env", "")

    result = invoke(
        "scan", "--strict", "--no-color", "--no-update-check",
    )

    assert result.exit_code == 1
    assert "TOKEN" in result.output
    assert "1 missing" in result.output


def test_missing_env_json_is_structured_operational_error(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "app.py", "value = 1\n")

    result = invoke(
        "scan", "--strict", "--output", "json", "--env", "missing.env",
    )

    assert result.exit_code == 2, result.output
    payload = json.loads(result.output)
    assert payload["env_file_exists"] is False
    assert payload["summary"]["missing"] == 0


def test_missing_env_github_is_error_not_notice(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "app.py", "value = 1\n")

    result = invoke(
        "scan", "--strict", "--output", "github", "--env", "missing.env",
    )

    assert result.exit_code == 2
    assert "::error" in result.output
    assert "Environment file not found" in result.output
    assert "all required env vars defined" not in result.output


def test_malformed_env_keeps_json_output_machine_readable(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "app.py", "import os\nos.getenv('TOKEN')\n")
    env = write(tmp_path, ".env", "TOKEN='unterminated\n")

    result = invoke("scan", "--output", "json")

    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["error"]["file"] == str(env)
    assert "invalid syntax" in payload["error"]["message"]


def test_malformed_env_is_a_github_error_command(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "app.py", "import os\nos.getenv('TOKEN')\n")
    write(tmp_path, ".env", "TOKEN='unterminated\n")

    result = invoke("scan", "--output", "github")

    assert result.exit_code == 2
    assert result.output.startswith("::error")
    assert "Could not read environment file" in result.output
    assert "invalid syntax" in result.output


def test_scan_errors_exit_two_without_strict(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "broken.py", "def nope(:\n")
    write(tmp_path, ".env", "")

    result = invoke("scan", "--output", "json")

    assert result.exit_code == 2, result.output
    payload = json.loads(result.output)
    assert payload["summary"]["errors"] == 1
    assert "syntax error" in payload["errors"][0]["error"]


def test_malformed_env_is_redacted_operational_error(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "app.py", "value = 1\n")
    write(tmp_path, ".env", 'API_KEY="sk-do-not-print-this\n')

    result = invoke("scan", "--output", "json")

    assert result.exit_code == 2
    assert "invalid syntax at line 1" in result.output
    assert "sk-do-not-print-this" not in result.output
    assert "Traceback" not in result.output


def test_nul_in_env_is_operational_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "app.py", "import os\nos.getenv('TOKEN')\n")
    write(tmp_path, ".env", "TOKEN=secret\0suffix\n")

    result = invoke("scan", "--no-update-check")

    assert result.exit_code == 2
    assert "NUL byte at line 1" in result.output
    assert "secret" not in result.output


def test_env_directory_is_rejected_without_traceback(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").mkdir()
    write(tmp_path, "app.py", "value = 1\n")

    result = invoke("scan", "--env", ".env", "--output", "json")

    assert result.exit_code == 2
    assert "file" in result.output.lower()
    assert "Traceback" not in result.output


def test_missing_envignore_is_a_click_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "app.py", "value = 1\n")
    write(tmp_path, ".env", "")

    result = invoke("scan", "--envignore", "does-not-exist")

    assert result.exit_code == 2
    assert "does not exist" in result.output
    assert "Traceback" not in result.output


def test_envignore_directory_is_rejected(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "app.py", "value = 1\n")
    write(tmp_path, ".env", "")
    (tmp_path / ".envignore").mkdir()

    result = invoke("scan")

    assert result.exit_code == 2
    assert "ignore file is not a file" in result.output
    assert "Traceback" not in result.output


def test_envignore_removed_during_scan_is_not_silently_ignored(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "app.py", "value = 1\n")
    write(tmp_path, ".env", "")
    ignore = write(tmp_path, ".envignore", "TOKEN\n")
    real_iter = cli_module.iter_python_files

    def removing_iter(*args, **kwargs):
        ignore.unlink()
        return real_iter(*args, **kwargs)

    monkeypatch.setattr(cli_module, "iter_python_files", removing_iter)
    result = invoke(
        "scan", "--envignore", str(ignore), "--output", "json",
    )

    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["error"]["file"] == str(ignore)
    assert "could not read ignore file" in payload["error"]["message"]


def test_missing_scan_path_is_a_click_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    result = invoke("scan", "--path", "missing-project")

    assert result.exit_code == 2
    assert "does not exist" in result.output
    assert "Traceback" not in result.output


def test_traversal_oserror_is_friendly(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "app.py", "value = 1\n")
    write(tmp_path, ".env", "")

    def denied(*args, **kwargs):
        raise PermissionError("access denied")

    monkeypatch.setattr(cli_module, "scan_project", denied)
    result = invoke("scan", "--output", "json")

    assert result.exit_code == 2
    assert "could not scan" in result.output
    assert "access denied" in result.output
    assert "Traceback" not in result.output


def test_traversal_scanerror_stays_structured(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    write(tmp_path, ".env", "")

    def broken_walk(*args, **kwargs):
        raise cli_module.ScanError("could not walk project: access denied")

    monkeypatch.setattr(cli_module, "iter_python_files", broken_walk)
    result = invoke("scan", "--output", "json")

    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["summary"]["errors"] == 1
    assert "access denied" in payload["errors"][0]["error"]


def test_generate_does_not_overwrite_after_scan_error(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "broken.py", "def nope(:\n")
    target = write(tmp_path, ".env.example", "KEEP_ME=1\n")

    result = invoke(
        "generate", "--force", "--no-color", "--no-update-check",
    )

    assert result.exit_code == 2, result.output
    assert "scan incomplete" in result.output
    assert target.read_text(encoding="utf-8") == "KEEP_ME=1\n"


def test_generate_reports_dynamic_lookups(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "app.py", "import os\nos.getenv(name)\n")

    result = invoke(
        "generate", "--no-color", "--no-update-check",
    )

    assert result.exit_code == 0, result.output
    assert "skipped 1 dynamic lookup" in result.output
    generated = (tmp_path / ".env.example").read_text(encoding="utf-8")
    assert "dynamic" in generated.lower()
    assert "app.py:2" in generated


def test_generate_handles_generation_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "app.py", "import os\nos.getenv('BAD-NAME')\n")

    result = invoke(
        "generate", "--no-color", "--no-update-check",
    )

    assert result.exit_code == 2
    assert "cannot generate portable assignments" in result.output
    assert not (tmp_path / ".env.example").exists()


def test_generate_output_directory_is_rejected(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "app.py", "value = 1\n")
    (tmp_path / "output").mkdir()

    result = invoke("generate", "--output", "output", "--no-update-check")

    assert result.exit_code == 2
    assert "file" in result.output.lower()
    assert "Traceback" not in result.output


def test_generate_write_oserror_is_friendly(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "app.py", "value = 1\n")

    result = invoke(
        "generate", "--output", "missing/out.env", "--no-update-check",
    )

    assert result.exit_code == 2
    assert "Error:" in result.output
    assert "Traceback" not in result.output


def test_generate_scan_errors_escape_terminal_controls(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    bad_path = Path("bad\x1b]8;;evil.test\x07CLICK.py")
    scan_result = cli_module.ScanResult(errors=[
        (bad_path, "syntax failed\x1b[2J\nFORGED"),
    ])
    monkeypatch.setattr(
        cli_module, "scan_project", lambda *args, **kwargs: scan_result,
    )

    result = invoke("generate", "--no-color", "--no-update-check")

    assert result.exit_code == 2
    assert "\x1b" not in result.output
    assert "\x07" not in result.output
    assert r"bad\x1b]8;;evil.test\x07CLICK.py" in result.output
    assert r"syntax failed\x1b[2J\nFORGED" in result.output


def test_generate_success_escapes_controls_in_output_path(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        cli_module, "write_env_example", lambda *args, **kwargs: None,
    )
    stdout = io.StringIO()
    stderr = io.StringIO()
    monkeypatch.setattr(cli_module.sys, "stdout", stdout)
    monkeypatch.setattr(cli_module.sys, "stderr", stderr)
    output = tmp_path / "out\x1b]8;;evil.test\x07CLICK.env"

    cli_module.generate.callback(
        path=tmp_path,
        output=output,
        force=False,
        no_color=True,
        exclude=(),
        ext=(),
        no_update_check=True,
    )

    text = stdout.getvalue()
    assert "\x1b" not in text
    assert "\x07" not in text
    assert r"out\x1b]8;;evil.test\x07CLICK.env" in text


def test_generation_error_message_escapes_terminal_controls(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    def bad_write(*args, **kwargs):
        raise cli_module.GenerationError("failed\x1b[2J\x07\nFORGED")

    monkeypatch.setattr(cli_module, "write_env_example", bad_write)
    result = invoke("generate", "--no-color", "--no-update-check")

    assert result.exit_code == 2
    assert "\x1b" not in result.output
    assert "\x07" not in result.output
    assert r"failed\x1b[2J\x07\nFORGED" in result.output


def test_progress_bar_uses_ascii_on_a_narrow_tty(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    for index in range(cli_module.PROGRESS_THRESHOLD):
        write(tmp_path, f"file_{index}.py", "value = 1\n")
    env = write(tmp_path, ".env", "")

    class EncodedStream:
        def __init__(self, encoding: str, tty: bool) -> None:
            self.encoding = encoding
            self.tty = tty
            self.parts = []

        def isatty(self) -> bool:
            return self.tty

        def write(self, value: str) -> int:
            value.encode(self.encoding)
            self.parts.append(value)
            return len(value)

        def flush(self) -> None:
            pass

    stdout = EncodedStream("utf-8", True)
    stderr = EncodedStream("cp1251", True)
    monkeypatch.setattr(cli_module.sys, "stdout", stdout)
    monkeypatch.setattr(cli_module.sys, "stderr", stderr)

    cli_module.scan.callback(
        path=tmp_path,
        env_file=env,
        envignore=None,
        strict=False,
        as_json=False,
        output_format="text",
        no_color=False,
        exclude=(),
        ext=(),
        verbose=False,
        no_update_check=True,
    )

    progress = "".join(stderr.parts)
    assert "Scanning" in progress
    assert "#" in progress
    assert "█" not in progress
