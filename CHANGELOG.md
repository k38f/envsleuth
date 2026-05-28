# Changelog

## 0.2.0

Feature release. Adds support for popular Django/config libraries, GitHub
Actions integration, pre-commit hook, and a quiet weekly check for new
releases.

### New features

- **django-environ support.** Recognizes `env = environ.Env(...)` instances
  and all the typed accessors (`env('X')`, `env.bool('X')`, `env.int('X')`,
  `env.list('X')`, `env.db('X')`, `env.cache('X')`, etc).
- **python-decouple support.** Recognizes `config('X')` calls after
  `from decouple import config`, including aliased imports.
- **GitHub Actions output.** New `--output github` mode emits workflow
  commands (`::error file=...,line=...::Missing var: X`) so missing env vars
  show up as annotations next to the source line in the PR/run UI.
- **pre-commit hook.** Drop the following into your `.pre-commit-config.yaml`:
  ```yaml
  - repo: https://github.com/k38f/envsleuth
    rev: v0.2.0
    hooks:
      - id: envsleuth
  ```
  Runs `envsleuth scan --strict` on commit when .py files change.
- **Auto-update check.** Like pip, envsleuth now occasionally tells you
  when a newer release is available. Checks PyPI at most once per week,
  cached in `~/.cache/envsleuth/`. Opt out via `--no-update-check` flag
  or `ENVSLEUTH_NO_UPDATE_CHECK=1` env var. Never shown in `--json` or
  `--output github` (would corrupt machine-readable output).

### Bug fixes caught during pre-release audit

Found and fixed before tagging v0.2.0:

- `has_default` no longer flips to `True` for any keyword arg — only when
  there's an actual `default=` value (or positional `args[1]`). The old logic
  flagged `config('X', cast=bool)` as defaulted, which silently passed
  `--strict` mode on required vars.
- `from environ import Env; env = Env(...)` (documented django-environ pattern)
  is now recognized. Previously this returned zero usages because we only
  tracked `import environ; env = environ.Env(...)`.
- Summary panel no longer leaks ANSI escape codes when colors are disabled
  (e.g. `envsleuth scan > log.txt` or in CI without TTY). `flashbar.panel()`
  always returns colored output regardless of stdout state, so we now skip the
  box entirely when `use_color` is off.
- `update_check` no longer caches a network failure for the full 7-day
  interval. A failed PyPI fetch now retries after 1 hour instead, so transient
  outages don't hide new releases for a week.

### Internal

- Migrated progress bar to `flashbar>=1.2` context manager API. Cleaner
  cleanup on exception, no more `getattr` fallback dance.
- Summary footer now uses `flashbar.panel()` for a proper boxed look in
  TTY, automatically falls back to plain text in pipes/CI.
- Test count: 49 → 108.

### Compatibility

- Minimum `flashbar` version is now `>=1.2` (was `>=1.0`).
- `--json` still works as an alias for `--output json`.
- No breaking changes to existing scan/generate output formats.


## 0.1.1

Bugfix release. Found 16 issues during a code audit and fixed all of them.

### Bug fixes

- **Scanning under a path with an excluded directory name in it.** Projects
  living in `~/.venv/foo/`, `~/build/old/` etc. used to return zero results
  because the scanner checked absolute parts of the path instead of parts
  relative to the scan root.
- **UTF-8 BOM in source files.** Files saved by Windows Notepad (which adds
  a BOM by default) crashed the scanner with `SyntaxError: invalid non-printable
  character U+FEFF`. Now read with `utf-8-sig` which handles both BOM and
  no-BOM files transparently.
- **`extra_in_env` ignored `.envignore`.** If `.envignore` had `TEST_*`, but
  `.env` had `TEST_FOO`, envsleuth still flagged it as unused. Now the same
  patterns filter the "extras" list.
- **`extra_in_env` ignored dynamic usages.** If your code does
  `os.getenv(f"PREFIX_{x}")` and `.env` has `PREFIX_FOO`/`PREFIX_BAR`,
  envsleuth no longer reports those as unused — they're plausible runtime
  matches for the dynamic expression.
- **Generated `.env.example` used absolute paths in comments.** Now relative
  to cwd. No more leaking `/home/youname/...` into commits.
- **`envsleuth generate` lost defaults given as keyword arguments.**
  `os.getenv("PORT", default="8000")` now correctly writes `PORT=8000`
  (previously wrote `PORT=`).
- **Default values containing `#`, spaces, quotes etc. now get quoted in
  `.env.example`.** `python-dotenv` was already lenient with these, but
  `bash source .env` and JS `dotenv` would mis-parse `URL=https://x.com#frag`.
  Now written as `URL="https://x.com#frag"`.
- **Files larger than 2MB are now skipped with a scan error** instead of
  trying to parse vendored libs / minified output.
- **Progress bar is closed cleanly** even if scanning raises.
- **`--env` path is always made absolute** so the "not found" error shows
  the full path, not just the bare filename.
- **JSON output includes the applied `ignore_patterns`** for CI debugging.

### Behavior changes

- **A variable is only treated as "has default" when *every* usage provides
  a default.** Previously a single `os.getenv("X", "fb")` would mark `X`
  as optional even if other call sites used `os.getenv("X")` (which would
  silently return `None`). The old behavior could mask real missing-var
  bugs.
- **The AST scanner is now scope-aware.** Aliases imported inside a
  function (`def f(): from os import getenv as ge`) no longer leak into
  module scope. Class-body imports correctly don't leak into methods.
  This matches Python's actual lexical scoping rules.

### Internal

- `EnvUsage` now carries `default_node` (the AST node of the default value).
  Removed the old `_first_literal_default` hack in `generator.py` which
  re-parsed source files to find defaults.
- Added `tests/test_generator.py` (was missing).

## 0.1.0

Initial release.
