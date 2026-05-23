"""Security regressions for src/tools/_vendor_helpers.py.

`get_cached_oauth_token` writes to `.data/vendor_tokens/<vendor>__<key>.json`
— both halves of the filename come from caller-controlled input, so a
slash or `..` in either is a path-traversal vector. We whitelist both.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def patched_tokens_dir(tmp_path, monkeypatch):
    from src.tools import _vendor_helpers
    monkeypatch.setattr(_vendor_helpers, "_TOKENS_DIR", tmp_path)
    return tmp_path


@pytest.mark.parametrize("vendor,key", [
    ("../etc", "passwd"),
    ("avito", "../traversal"),
    ("avito", "key/with/slash"),
    ("avito", "key\\with\\backslash"),
    ("avito", "key with space"),
    ("avito", ""),
    ("avito", "x" * 100),  # over length cap
    ("avito", "."),
    ("avito", ".."),
    ("avito$", "ok"),
])
def test_get_cached_oauth_token_bypasses_cache_on_unsafe_key(
    patched_tokens_dir, vendor, key
):
    """SEC sweep: on a malformed key, the helper must NOT write to disk.
    It still calls `fetch_fn()` so the upstream auth call still happens
    (fail-open auth path) but no cache file is created in any directory."""
    from src.tools import _vendor_helpers

    call_count = {"n": 0}

    def fetch_fn():
        call_count["n"] += 1
        return {"ok": True, "data": {"access_token": "tok", "expires_in": 3600}}

    out = _vendor_helpers.get_cached_oauth_token(vendor, key, fetch_fn)
    assert out["ok"] is True
    assert call_count["n"] == 1
    # No file landed in the tmp tokens dir
    assert list(patched_tokens_dir.iterdir()) == []
    # And no file landed in the parent (the path-traversal target)
    assert not list(patched_tokens_dir.parent.glob("**/passwd*"))
    assert not list(patched_tokens_dir.parent.glob("**/traversal*"))


def test_get_cached_oauth_token_writes_when_key_safe(patched_tokens_dir):
    from src.tools import _vendor_helpers

    def fetch_fn():
        return {"ok": True, "data": {"access_token": "tok", "expires_in": 3600}}

    _vendor_helpers.get_cached_oauth_token("avito", "abc123_def", fetch_fn)
    assert (patched_tokens_dir / "avito__abc123_def.json").exists()


def test_invalidate_oauth_cache_rejects_unsafe_key(patched_tokens_dir):
    from src.tools import _vendor_helpers
    out = _vendor_helpers.invalidate_oauth_cache("../etc", "passwd")
    assert out["ok"] is False
    assert out["error_kind"] == "bad_input"
