"""Tests for envsleuth.generator.

Coverage was thin here, only adding what's needed to lock in the relative-path
fix and a couple of obvious things while we're at it.
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

from envsleuth.generator import build_env_example, write_env_example
from envsleuth.scanner import scan_project


def write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    return path


def test_basic_output(tmp_path: Path) -> None:
    write(tmp_path / "a.py", """
        import os
        os.getenv("DATABASE_URL")
        os.getenv("PORT", "8000")
    """)
    scan = scan_project(tmp_path)
    out = build_env_example(scan)
    assert "DATABASE_URL=" in out
    assert "PORT=8000" in out


def test_paths_are_relative_to_cwd(tmp_path: Path, monkeypatch) -> None:
    # bug fix: locations in the generated file used to be absolute paths,
    # which leaked the author's home dir into committed .env.example files.
    write(tmp_path / "src" / "main.py", """
        import os
        os.getenv("DB")
    """)
    monkeypatch.chdir(tmp_path)
    scan = scan_project(Path("src"))
    out = build_env_example(scan)

    # should mention src/main.py, not /tmp/.../src/main.py
    assert "src/main.py" in out or "src\\main.py" in out  # windows path separators
    assert str(tmp_path) not in out


def test_empty_project(tmp_path: Path) -> None:
    scan = scan_project(tmp_path)
    out = build_env_example(scan)
    assert "No environment variables" in out


def test_write_env_example_creates_file(tmp_path: Path) -> None:
    write(tmp_path / "a.py", "import os\nos.getenv('X')\n")
    scan = scan_project(tmp_path)
    target = tmp_path / ".env.example"
    write_env_example(scan, target)
    assert target.exists()
    assert "X=" in target.read_text()


def test_write_env_example_refuses_overwrite(tmp_path: Path) -> None:
    write(tmp_path / "a.py", "import os\nos.getenv('X')\n")
    scan = scan_project(tmp_path)
    target = tmp_path / ".env.example"
    target.write_text("hand written\n")
    try:
        write_env_example(scan, target)
        assert False, "should have raised FileExistsError"
    except FileExistsError:
        pass
    assert target.read_text() == "hand written\n"


def test_write_env_example_force_overwrites(tmp_path: Path) -> None:
    write(tmp_path / "a.py", "import os\nos.getenv('FORCED')\n")
    scan = scan_project(tmp_path)
    target = tmp_path / ".env.example"
    target.write_text("old content\n")
    write_env_example(scan, target, force=True)
    assert "FORCED=" in target.read_text()


def test_keyword_default_is_extracted(tmp_path: Path) -> None:
    # bug #2: previously generator only looked at positional args[1] for default,
    # so os.getenv("X", default="foo") wrote `X=` instead of `X=foo`.
    write(tmp_path / "a.py", """
        import os
        os.getenv("PORT", default="8000")
        os.getenv("HOST", default="localhost")
    """)
    scan = scan_project(tmp_path)
    out = build_env_example(scan)
    assert "PORT=8000" in out
    assert "HOST=localhost" in out


def test_value_with_hash_is_quoted(tmp_path: Path) -> None:
    # bug #10: bare `URL=https://x.com#anchor` — bash and JS dotenv parsers cut
    # at the '#'. wrap in quotes when value has dangerous chars.
    write(tmp_path / "a.py", """
        import os
        os.getenv("URL", "https://example.com/path#fragment")
    """)
    scan = scan_project(tmp_path)
    out = build_env_example(scan)
    # should be quoted. exact form: URL="https://example.com/path#fragment"
    line = next(l for l in out.splitlines() if l.startswith("URL="))
    assert line.startswith('URL="') and line.endswith('"')
    assert "#fragment" in line


def test_value_with_spaces_is_quoted(tmp_path: Path) -> None:
    write(tmp_path / "a.py", """
        import os
        os.getenv("MSG", "hello world")
    """)
    scan = scan_project(tmp_path)
    out = build_env_example(scan)
    line = next(l for l in out.splitlines() if l.startswith("MSG="))
    assert line == 'MSG="hello world"'


def test_simple_value_not_quoted(tmp_path: Path) -> None:
    # don't quote things that don't need it — keeps the file readable
    write(tmp_path / "a.py", """
        import os
        os.getenv("PORT", "8000")
        os.getenv("DEBUG", "false")
    """)
    scan = scan_project(tmp_path)
    out = build_env_example(scan)
    assert "PORT=8000" in out
    assert "DEBUG=false" in out
    # no quotes around them
    assert 'PORT="' not in out
    assert 'DEBUG="' not in out


# leftover from when I was thinking about ast.Name resolution — punted
# def test_default_from_module_constant(tmp_path):
#     # SOMETHING = "default-value"
#     # os.getenv("X", SOMETHING)  -> should it pull "default-value"?
#     pass
