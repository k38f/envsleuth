# Changelog

## 1.0.0 - 2026-07-26

First stable release.

### Configuration and CI

- Added safe, nearest-project discovery of `[tool.envsleuth]` in
  `pyproject.toml`, with `--config` and `--no-config` overrides. Paths from the
  table resolve relative to the config file. Auto-discovered paths are confined
  to that project root, while explicit config and CLI paths remain opt-in
  escape hatches.
- `--env` is repeatable. Multiple env files are checked independently after
  one source scan, so development, test, and production profiles cannot mask
  one another.
- Added repeatable `--fail-on missing|extra|dynamic` policies.
  `--strict` remains fully supported as the backwards-compatible spelling for
  `--fail-on missing`.
- Added deterministic SARIF 2.1.0 output for code-scanning integrations.
  Missing, extra, dynamic, scan, and operational findings use stable rule IDs,
  safe repository-relative URIs, bounded messages, and a 25,000-result limit.
- Multi-profile text, JSON, and GitHub Actions output identifies the selected
  environment without repeating global scan diagnostics.

### Scanner

- Added static support for `pydantic-settings` v2 `BaseSettings` classes,
  including aliased imports, `SettingsConfigDict(env_prefix=...)`, class
  config keywords, required/default fields, `Field(alias=...)`,
  `Field(validation_alias=...)`, `AliasChoices`, `env_prefix_target`, and
  local inheritance with subclass config overrides.
- Pydantic's default case-insensitive environment matching and one-of alias
  behavior are represented explicitly instead of treating alternative names
  as separate required variables.
- Pydantic and inspected application modules are never imported or executed;
  computed prefixes, unpacked config, and alias generators remain visible as
  dynamic findings.

### Hardening and compatibility

- Config, `.env`, and `.envignore` inputs must be regular UTF-8 files and have
  bounded sizes and entry counts. Scans also cap source-file and usage counts.
  Invalid types, unknown config keys, excessive profiles or bindings, malformed
  TOML/dotenv syntax, control characters, and unsafe extensions fail with
  redacted code-2 errors.
- Machine output reports only the number of active ignore patterns, not their
  raw contents, so a misconfigured ignore path cannot echo file data into CI.
- Operational errors continue to take precedence over lint policies, and every
  machine format remains a single valid document even when the command exits
  nonzero.
- Direct, build, development, and release-tool dependencies are restricted to
  tested major/minor branches while allowing patch updates. The same `tomli`
  parser is used on every supported Python version so config behavior does not
  drift across the CI matrix.
- The minimum supported version is now Python 3.10. Python 3.10 through 3.14
  and Windows remain in the CI matrix, allowing current dependency branches
  without per-interpreter dependency splits.

## 0.3.1 - 2026-07-21

Patch release focused on predictable dependency updates.

- Runtime dependencies now stay within a tested major/minor branch while still
  accepting patch releases. The selected branches support the full Python
  3.9-3.14 matrix.
- Build and development dependencies use the same compatible-release policy,
  preventing an unrelated feature release from unexpectedly changing CI or
  package builds.
- Update notifications now recommend
  `python -m pip install --upgrade envsleuth`, ensuring pip runs through the
  selected Python interpreter.

## 0.3.0 - 2026-07-15

Full bug-audit pass focused on correctness, safe failure modes, and cross-platform
CLI behavior.

### Scanner and checking

- Fixed AST scope and import tracking around rebinding, function-local names,
  lambdas and comprehensions, class bodies, relative imports, `import os.path`,
  and `from os import *`. Aliases now stop matching when Python would shadow
  them, without hiding valid lookups that run before a class name is bound.
- Control-flow alias tracking now reaches a bounded fixpoint across loops and
  comprehensions, preserves intermediate `try`/`except`/`except*`/`finally`
  states, models guarded `match` cases, and respects PEP 695 type-parameter
  scopes without quadratic behavior on long alias chains.
- Environment writes and deletes are no longer reported as reads;
  augmented assignment still counts because it reads the existing value.
- Added keyword-name support for stdlib, django-environ, and python-decouple
  calls, and corrected django-environ positional default signatures.
- Expanded django-environ handling for constructor schemas, `FileAwareEnv`,
  `Env.configured(...)`, current helper methods, and helpers with implicit names
  such as `env.db()` and `env.cache_url()`.
- Python encoding cookies are now honored. Deep AST recursion, file traversal
  failures, and inaccessible paths become regular scan errors instead of
  uncaught exceptions. Excluded directories and extensions are matched without
  case sensitivity while walking the project, and virtual environments are
  pruned by their `pyvenv.cfg` marker even when they use a custom directory name.
- `.env` and `.envignore` accept a UTF-8 BOM. A bare `NAME` line is no longer
  treated as defined (unlike `NAME=`), and an explicit `default=None` no longer
  satisfies a required-variable check.
- Malformed `.env` syntax and embedded NUL bytes now fail as redacted
  operational errors instead of producing a warning-only false green.
- Environment names and ignore patterns follow Windows' case-insensitive
  behavior on Windows while remaining case-sensitive on POSIX. Dynamic-name
  matching for extra `.env` entries now understands prefixes, suffixes,
  concatenation, f-strings, `.format()`, and `%` formatting without regex
  backtracking, repeated parsing, or a Cartesian extra-key × pattern scan.
- `.env` existence is carried from the same opened snapshot as its values, so
  a concurrently created or replaced file cannot produce a false-green report.

### Generation and CLI

- `.env.example` generation now fails closed on scan errors or non-portable
  variable names. Validation happens before opening the target, so even
  `--force` cannot overwrite a good file with incomplete output.
- Generated defaults are quoted only when they round-trip through both
  `python-dotenv` and POSIX shell parsing. Defaults that cannot be represented
  safely are omitted without leaking their contents, and output uses stable
  source ordering and LF newlines. This includes terminal-control values and
  trailing backslashes that could otherwise consume the next assignment.
- Case-colliding names such as `FOO` and `foo` are rejected on Windows, where
  the process environment cannot represent them independently.
- Output replacement is atomic when overwriting an existing example; write
  failures preserve the previous file and temporary files are cleaned up
  without deleting a concurrently replaced path. Existing private POSIX modes
  are applied to the temporary descriptor before any content is written.
- Dynamic-only and mixed scans leave explicit warning comments in generated
  files, and the CLI reports how many dynamic lookups were skipped.
- A missing `.env` or any incomplete scan is now an operational error with exit
  code 2 in every output mode, even without `--strict`. JSON and GitHub modes
  still emit their structured reports before exiting.
- CLI paths and file types are validated up front, read/write/traversal errors
  are reported without tracebacks, and project enumeration is reused by the
  progress bar instead of walking the tree twice.
- Output falls back to ASCII-safe symbols on narrow Windows consoles. GitHub
  Actions commands now escape message data and property fields according to
  the toolkit rules, terminal control sequences are neutralized, and a missing
  environment file emits an error annotation. Operational failures also stay
  structured in JSON and GitHub output modes.

### Integrations and maintenance

- Update checks now use `packaging.version.Version` for PEP 440 ordering and
  reject malformed remote versions. Invalid, stale, or future-dated cache data
  is ignored and repaired on the next successful request. Cache writes are
  atomic, huge timestamps and non-UTF-8 replies are harmless, cached versions
  are normalized, and notifications fall back on narrow console encodings.
- The pre-commit scan hook now reruns for `.env`, `.env.*`, and `.envignore`
  changes as well as Python changes; the manual generate hook runs even when
  pre-commit receives no Python filenames.
- CI now includes Windows tests, manifest validation, sdist/wheel builds, a
  manual-hook smoke test, and a wheel smoke test outside the source checkout.
- Package metadata now uses the standardized SPDX license expression and
  explicitly includes the license file in built distributions.

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
