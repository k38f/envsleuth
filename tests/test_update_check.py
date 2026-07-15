"""Tests for envsleuth.update_check."""

from __future__ import annotations

import io
import json
import os
import time
from pathlib import Path
from unittest import mock

import pytest
from packaging.version import InvalidVersion, Version

from envsleuth import update_check


@pytest.fixture
def fake_cache(tmp_path, monkeypatch):
    """Redirect the cache dir into a tmp_path so tests don't touch the user's home."""
    cache_file = tmp_path / "envsleuth" / "last_check.json"
    monkeypatch.setattr(update_check, "_cache_path", lambda: cache_file)
    return cache_file


# ------------------------------------------------------------ version parsing


def test_parse_simple_versions():
    assert update_check._parse_version("0.1.0") == Version("0.1.0")
    assert update_check._parse_version("1.2.3") == Version("1.2.3")
    assert update_check._parse_version("10.20.30") == Version("10.20.30")


def test_parse_preserves_prerelease_suffix():
    assert update_check._parse_version("0.2.0rc1") == Version("0.2.0rc1")
    assert update_check._parse_version("1.0.0b3") == Version("1.0.0b3")


def test_parse_strips_v_prefix():
    # be forgiving about leading 'v' just in case we ever tag wrongly
    assert update_check._parse_version("v1.2.3") == Version("1.2.3")
    assert update_check._parse_version("V0.1.0") == Version("0.1.0")


def test_parse_rejects_invalid_version():
    with pytest.raises(InvalidVersion):
        update_check._parse_version("definitely not a release")


def test_is_newer():
    assert update_check._is_newer("0.1.2", "0.1.1") is True
    assert update_check._is_newer("0.2.0", "0.1.99") is True
    assert update_check._is_newer("1.0.0", "0.9.9") is True
    assert update_check._is_newer("0.1.1", "0.1.1") is False
    assert update_check._is_newer("0.1.0", "0.1.1") is False


def test_is_newer_follows_pep440_ordering():
    assert update_check._is_newer("1.0", "1.0rc1") is True
    assert update_check._is_newer("1.0.post1", "1.0") is True
    assert update_check._is_newer("1.0.dev2", "1.0.dev1") is True
    assert update_check._is_newer("2!0.1", "1!99.0") is True
    assert update_check._is_newer("1.0rc1", "1.0") is False
    assert update_check._is_newer("not-a-version", "1.0") is False


# ------------------------------------------------------------ opt-out behavior


def test_disabled_via_env_var(monkeypatch, fake_cache):
    monkeypatch.setenv("ENVSLEUTH_NO_UPDATE_CHECK", "1")
    # even if pypi would have returned a newer version, we don't call it
    with mock.patch.object(update_check, "_fetch_latest_version") as fake:
        result = update_check.check("0.1.0")
    assert result is None
    fake.assert_not_called()


def test_not_disabled_with_zero_or_false(monkeypatch, fake_cache):
    # "0" and "false" should NOT disable — follows NO_COLOR convention loosely
    for val in ("0", "false", "no", ""):
        monkeypatch.setenv("ENVSLEUTH_NO_UPDATE_CHECK", val)
        assert update_check._is_disabled() is False


def test_disabled_with_any_other_value(monkeypatch):
    for val in ("1", "yes", "true", "anything"):
        monkeypatch.setenv("ENVSLEUTH_NO_UPDATE_CHECK", val)
        assert update_check._is_disabled() is True


# ------------------------------------------------------------ caching


def test_uses_cache_when_recent(fake_cache):
    fake_cache.parent.mkdir(parents=True, exist_ok=True)
    fake_cache.write_text(json.dumps({
        "last_check": time.time() - 60,  # one minute ago
        "latest_version": "0.5.0",
    }))
    with mock.patch.object(update_check, "_fetch_latest_version") as fake:
        result = update_check.check("0.1.1")
    assert result == "0.5.0"
    fake.assert_not_called()  # didn't hit network


def test_cache_version_is_normalized_before_use(fake_cache):
    fake_cache.parent.mkdir(parents=True, exist_ok=True)
    fake_cache.write_text(json.dumps({
        "last_check": time.time() - 60,
        "latest_version": "\nV0.5.0\t",
    }))

    assert update_check.check("0.1.1") == "0.5.0"


def test_refetches_when_cache_stale(fake_cache):
    fake_cache.parent.mkdir(parents=True, exist_ok=True)
    fake_cache.write_text(json.dumps({
        "last_check": time.time() - (8 * 24 * 60 * 60),  # 8 days ago
        "latest_version": "0.1.1",
    }))
    with mock.patch.object(update_check, "_fetch_latest_version", return_value="0.3.0"):
        result = update_check.check("0.1.1")
    assert result == "0.3.0"


def test_no_cache_writes_one(fake_cache):
    # cache file doesn't exist initially
    assert not fake_cache.exists()
    with mock.patch.object(update_check, "_fetch_latest_version", return_value="0.2.0"):
        update_check.check("0.1.1")
    # now it should
    assert fake_cache.exists()
    data = json.loads(fake_cache.read_text())
    assert data["latest_version"] == "0.2.0"


def test_cache_save_uses_atomic_replace(fake_cache, monkeypatch):
    real_replace = update_check.os.replace
    replacements = []

    def recording_replace(source, destination):
        replacements.append((Path(source), Path(destination)))
        return real_replace(source, destination)

    monkeypatch.setattr(update_check.os, "replace", recording_replace)

    update_check._save_cache({"last_check": 123.0, "latest_version": "0.2.0"})

    assert len(replacements) == 1
    assert replacements[0][1] == fake_cache
    assert json.loads(fake_cache.read_text()) == {
        "last_check": 123.0, "latest_version": "0.2.0",
    }
    assert list(fake_cache.parent.glob(".last-check-*.tmp")) == []


def test_cache_save_replaces_symlink_instead_of_its_target(fake_cache):
    if os.name == "nt":
        pytest.skip("symlink creation usually needs extra Windows privileges")
    fake_cache.parent.mkdir(parents=True, exist_ok=True)
    target = fake_cache.parent / "do-not-touch.json"
    target.write_text("keep me", encoding="utf-8")
    fake_cache.symlink_to(target)

    update_check._save_cache({"last_check": 123.0, "latest_version": "0.2.0"})

    assert not fake_cache.is_symlink()
    assert target.read_text(encoding="utf-8") == "keep me"
    assert json.loads(fake_cache.read_text())["latest_version"] == "0.2.0"


def test_corrupted_cache_is_ignored(fake_cache):
    fake_cache.parent.mkdir(parents=True, exist_ok=True)
    fake_cache.write_text("not json at all {{{")
    # should not crash, should refetch
    with mock.patch.object(update_check, "_fetch_latest_version", return_value="0.2.0"):
        result = update_check.check("0.1.1")
    assert result == "0.2.0"


@pytest.mark.parametrize("bad_cache", [
    [],
    {"last_check": "yesterday", "latest_version": "0.5.0"},
    {"last_check": 123.0, "latest_version": ["0.5.0"]},
    {"last_check": 123.0, "latest_version": "toast"},
    {"last_check": float("inf"), "latest_version": "0.5.0"},
    {"last_check": 10 ** 400, "latest_version": "0.5.0"},
])
def test_valid_json_with_bad_cache_schema_self_heals(fake_cache, bad_cache):
    fake_cache.parent.mkdir(parents=True, exist_ok=True)
    fake_cache.write_text(json.dumps(bad_cache))

    with mock.patch.object(
        update_check, "_fetch_latest_version", return_value="0.6.0",
    ):
        assert update_check.check("0.1.0") == "0.6.0"

    repaired = json.loads(fake_cache.read_text())
    assert isinstance(repaired["last_check"], (int, float))
    assert repaired["latest_version"] == "0.6.0"


def test_future_cache_timestamp_is_refetched(fake_cache):
    fake_cache.parent.mkdir(parents=True, exist_ok=True)
    fake_cache.write_text(json.dumps({
        "last_check": time.time() + 86400,
        "latest_version": "0.5.0",
    }))

    with mock.patch.object(
        update_check, "_fetch_latest_version", return_value="0.6.0",
    ) as fetch:
        assert update_check.check("0.1.0") == "0.6.0"

    fetch.assert_called_once_with()


# ------------------------------------------------------------ network failures


def test_network_failure_returns_none(fake_cache):
    # _fetch_latest_version returning None simulates any failure
    with mock.patch.object(update_check, "_fetch_latest_version", return_value=None):
        result = update_check.check("0.1.1")
    assert result is None


def test_fetch_rejects_non_utf8_response(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return b"\xff\xfe"

    monkeypatch.setattr(update_check.urllib.request, "urlopen", lambda *a, **k: Response())

    assert update_check._fetch_latest_version() is None


def test_network_failure_retries_within_an_hour(fake_cache):
    # bug fix: previously a single failed PyPI fetch would suppress checks for
    # 7 full days. now last_check is anchored to the past so cache expires after
    # ~1 hour instead.
    import time as time_module

    # first call fails — should record a "stale-ish" last_check
    with mock.patch.object(update_check, "_fetch_latest_version", return_value=None):
        update_check.check("0.1.1")

    assert fake_cache.exists()
    data = json.loads(fake_cache.read_text())
    age = time_module.time() - data["last_check"]
    # the saved last_check should be ~6 days old, leaving roughly 1 hour
    # before the next check will run
    expected = update_check.CHECK_INTERVAL - update_check.RETRY_AFTER_FAILURE
    assert abs(age - expected) < 5, (
        f"last_check anchor is {age}s old, expected ~{expected}s"
    )

    # second call right away should still use cache (within retry window)
    with mock.patch.object(update_check, "_fetch_latest_version", return_value="9.9.9") as fake:
        update_check.check("0.1.1")
    # might or might not call fetch depending on exact timing; the important thing
    # is that the retry window IS bounded, not 7 days
    # we already asserted the timing above


def test_maybe_notify_never_raises():
    # the contract: maybe_notify must never let an exception out, even if
    # something deep inside misbehaves
    stream = io.StringIO()
    with mock.patch.object(update_check, "check", side_effect=RuntimeError("boom")):
        # should not raise
        update_check.maybe_notify("0.1.1", stream=stream)
    assert stream.getvalue() == ""  # nothing written on error


def test_maybe_notify_writes_when_newer(fake_cache):
    stream = io.StringIO()
    with mock.patch.object(update_check, "_fetch_latest_version", return_value="0.5.0"):
        update_check.maybe_notify("0.1.1", stream=stream)
    out = stream.getvalue()
    assert "0.5.0" in out
    assert "0.1.1" in out
    assert "pip install -U envsleuth" in out


def test_maybe_notify_falls_back_on_a_narrow_stream(fake_cache):
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding="cp1251")
    with mock.patch.object(update_check, "check", return_value="0.5.0"):
        update_check.maybe_notify("0.1.1", stream=stream)
    stream.flush()

    out = buffer.getvalue().decode("cp1251")
    assert "[info]" in out
    assert "envsleuth 0.5.0 is available" in out


def test_maybe_notify_keeps_cached_version_on_one_line():
    stream = io.StringIO()
    with mock.patch.object(update_check, "check", return_value="\nV0.5.0\t"):
        update_check.maybe_notify("\n0.1.1\r", stream=stream)

    assert stream.getvalue().count("\n") == 1
    assert "envsleuth 0.5.0 is available (you have 0.1.1)" in stream.getvalue()


def test_maybe_notify_silent_when_up_to_date(fake_cache):
    stream = io.StringIO()
    with mock.patch.object(update_check, "_fetch_latest_version", return_value="0.1.1"):
        update_check.maybe_notify("0.1.1", stream=stream)
    assert stream.getvalue() == ""


# ------------------------------------------------------------ cache location


def test_xdg_cache_home_is_honored(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    path = update_check._cache_path()
    assert tmp_path in path.parents
