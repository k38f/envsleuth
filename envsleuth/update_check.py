"""Lightweight check for new envsleuth releases on PyPI.

Design choices worth noting:
  - stdlib only (urllib, json, no requests/httpx) — envsleuth's whole point is
    being a small dev tool, can't bloat deps for this
  - cached in ~/.cache/envsleuth/last_check.json, checks at most once a week
  - silent on any error: network down, PyPI 503, broken JSON — we just don't
    show anything. update check failure must never kill the user's actual command.
  - opt-out via ENVSLEUTH_NO_UPDATE_CHECK env var or --no-update-check flag
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional


PYPI_URL = "https://pypi.org/pypi/envsleuth/json"
CHECK_INTERVAL = 7 * 24 * 60 * 60  # 7 days in seconds
RETRY_AFTER_FAILURE = 60 * 60  # try again in an hour if PyPI was unreachable
REQUEST_TIMEOUT = 2.0  # seconds — keep low so we don't slow CLI down


def _cache_path() -> Path:
    # XDG_CACHE_HOME first, fall back to ~/.cache. on windows the latter still
    # works (python's expanduser does the right thing). worst case we put it
    # somewhere reasonable and it just keeps being a fresh check
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "envsleuth" / "last_check.json"


def _is_disabled() -> bool:
    val = os.environ.get("ENVSLEUTH_NO_UPDATE_CHECK", "").strip().lower()
    # accept the usual truthy values, but really any non-empty thing means opt out.
    # follows the NO_COLOR convention
    return val not in ("", "0", "false", "no")


def _load_cache() -> Optional[dict]:
    p = _cache_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None  # corrupted cache — treat as no cache


def _save_cache(data: dict) -> None:
    p = _cache_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass  # can't write cache — fine, we just check again next time


def _fetch_latest_version() -> Optional[str]:
    try:
        req = urllib.request.Request(
            PYPI_URL,
            headers={"User-Agent": "envsleuth-update-check"},
        )
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            return payload.get("info", {}).get("version")
    except (urllib.error.URLError, urllib.error.HTTPError,
            TimeoutError, ValueError, OSError):
        return None


def _parse_version(v: str) -> tuple:
    """Very loose version parsing — good enough for 'is X newer than Y' on our
    own releases. We control the format (0.1.1, 0.2.0, etc) so don't need
    packaging.version.parse here.
    """
    # be forgiving about a leading 'v' — pypi shouldn't return one but if we
    # ever fat-finger our own tag/version we don't want sort to break silently
    v = v.lstrip("vV")
    parts = []
    for chunk in v.split("."):
        # strip any pre-release suffix like '0.2.0rc1' — just take leading digits
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        if digits:
            parts.append(int(digits))
    return tuple(parts)


def _is_newer(latest: str, current: str) -> bool:
    try:
        return _parse_version(latest) > _parse_version(current)
    except Exception:
        return False


def check(current_version: str, force: bool = False) -> Optional[str]:
    """Check PyPI for a newer release. Returns the new version string if there
    is one available and we should tell the user about it, otherwise None.

    force=True skips the cache (useful for testing).
    """
    if _is_disabled():
        return None

    now = time.time()
    cache = _load_cache()

    if not force and cache:
        last_check = cache.get("last_check", 0)
        if now - last_check < CHECK_INTERVAL:
            # still within the week, use cached result
            latest = cache.get("latest_version")
            if latest and _is_newer(latest, current_version):
                return latest
            return None

    latest = _fetch_latest_version()
    if latest is None:
        # network failed — don't pretend we got a fresh check. set last_check
        # to (now - CHECK_INTERVAL + RETRY_AFTER_FAILURE) so the cache "expires"
        # in an hour rather than a full week. otherwise a transient PyPI 503
        # would hide new releases from the user for 7 days.
        retry_anchor = now - CHECK_INTERVAL + RETRY_AFTER_FAILURE
        _save_cache({
            "last_check": retry_anchor,
            "latest_version": cache.get("latest_version") if cache else None,
        })
        return None

    _save_cache({"last_check": now, "latest_version": latest})

    if _is_newer(latest, current_version):
        return latest
    return None


def maybe_notify(current_version: str, stream=None) -> None:
    """Run a check and print a one-liner to stderr if a new version exists.

    Catches every exception — this should NEVER raise into the caller.
    """
    if stream is None:
        stream = sys.stderr
    try:
        latest = check(current_version)
        if latest:
            # quiet, one line, points to the fix
            stream.write(
                f"ℹ  envsleuth {latest} is available "
                f"(you have {current_version}). "
                f"Run: pip install -U envsleuth\n"
            )
    except Exception:
        # belt and suspenders — check() already swallows errors, but if
        # anything sneaks out we kill it here
        pass
