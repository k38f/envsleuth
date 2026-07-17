"""Lightweight check for new envsleuth releases on PyPI.

Design choices worth noting:
  - urllib/json for the HTTP request, with packaging.version for correct PEP 440
    comparisons; no requests/httpx dependency just for one tiny request
  - cached in ~/.cache/envsleuth/last_check.json, checks at most once a week
  - silent on any error: network down, PyPI 503, broken JSON — we just don't
    show anything. update check failure must never kill the user's actual command.
  - opt-out via ENVSLEUTH_NO_UPDATE_CHECK env var or --no-update-check flag
"""

from __future__ import annotations

import json
import math
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from packaging.version import InvalidVersion, Version

from envsleuth.display import stream_safe_text


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
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None  # corrupted cache — treat as no cache

    if not isinstance(data, dict):
        return None

    last_check = data.get("last_check")
    if isinstance(last_check, bool) or not isinstance(last_check, (int, float)):
        return None
    try:
        normalized_last_check = float(last_check)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(normalized_last_check) or normalized_last_check < 0:
        return None

    latest = data.get("latest_version")
    if latest is not None:
        if not isinstance(latest, str) or not latest.strip():
            return None
        try:
            latest = str(_parse_version(latest))
        except InvalidVersion:
            return None

    return {"last_check": normalized_last_check, "latest_version": latest}


def _save_cache(data: dict) -> None:
    p = _cache_path()
    temp_path: Optional[Path] = None
    temp_stat: Optional[os.stat_result] = None
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(10):
            candidate = p.parent / f".last-check-{secrets.token_hex(8)}.tmp"
            try:
                stream = candidate.open("x", encoding="utf-8", newline="\n")
            except FileExistsError:
                continue
            temp_path = candidate
            break
        else:
            return

        with stream:
            temp_stat = os.fstat(stream.fileno())
            stream.write(json.dumps(data))
        if not os.path.samestat(temp_stat, temp_path.lstat()):
            return
        os.replace(temp_path, p)
        temp_path = None
    except OSError:
        pass  # can't write cache — fine, we just check again next time
    finally:
        if temp_path is not None and temp_stat is not None:
            try:
                current = temp_path.lstat()
                if os.path.samestat(temp_stat, current):
                    temp_path.unlink()
            except OSError:
                pass


def _fetch_latest_version() -> Optional[str]:
    try:
        req = urllib.request.Request(
            PYPI_URL,
            headers={"User-Agent": "envsleuth-update-check"},
        )
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            latest = payload.get("info", {}).get("version")
            if not isinstance(latest, str) or not latest.strip():
                return None
            return str(_parse_version(latest))
    except (urllib.error.URLError, urllib.error.HTTPError,
            TimeoutError, InvalidVersion, ValueError, OSError, AttributeError,
            UnicodeError):
        return None


def _parse_version(value: str) -> Version:
    """Parse and normalize a release using the PEP 440 reference library."""
    return Version(value)


def _is_newer(latest: str, current: str) -> bool:
    try:
        return _parse_version(latest) > _parse_version(current)
    except (InvalidVersion, TypeError):
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
        age = now - last_check
        if 0 <= age < CHECK_INTERVAL:
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
            latest_label = str(_parse_version(latest))
            current_label = str(_parse_version(current_version))
            # quiet, one line, points to the fix
            message = (
                f"ℹ  envsleuth {latest_label} is available "
                f"(you have {current_label}). "
                f"Run: python -m pip install --upgrade envsleuth\n"
            )
            stream.write(stream_safe_text(message, stream))
    except Exception:
        # belt and suspenders — check() already swallows errors, but if
        # anything sneaks out we kill it here
        pass
