# Changelog

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
