"""Coverage for `_load_strategies_toml` mtime caching.

The cache lives at module scope, so the first call after import warms
it and every subsequent call is zero-IO until the underlying file
changes (mtime bump). These tests pin both behaviors:

- repeated calls do NOT re-read the file (zero-IO path)
- a stat mtime change DOES invalidate the cache (dev hot-reload path)
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from trading.api import db as apidb


@pytest.fixture
def toml_file(tmp_path: Path) -> Path:
    p = tmp_path / "staging.toml"
    p.write_text(
        '[strategies.alpha]\nenabled = true\nparams_file = "alpha.yaml"\n'
        '[strategies.beta]\nenabled = false\nparams_file = "beta.yaml"\n'
    )
    return p


@pytest.fixture(autouse=True)
def _clear_cache():
    apidb._TOML_CACHE.clear()
    yield
    apidb._TOML_CACHE.clear()


def test_loads_and_parses_toml(toml_file: Path) -> None:
    cfg = apidb._load_strategies_toml(toml_file)
    assert "strategies" in cfg
    assert cfg["strategies"]["alpha"]["enabled"] is True
    assert cfg["strategies"]["beta"]["enabled"] is False


def test_repeated_calls_skip_disk_read(toml_file: Path, monkeypatch) -> None:
    # Warm the cache, then poison Path.read_text to fail loud — any
    # subsequent disk read means caching is broken.
    apidb._load_strategies_toml(toml_file)
    reads: list[str] = []
    real_read_text = Path.read_text

    def _spy(self, *a, **kw):
        reads.append(str(self))
        return real_read_text(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", _spy)

    for _ in range(20):
        cfg = apidb._load_strategies_toml(toml_file)
        assert "strategies" in cfg

    assert reads == [], f"expected zero re-reads, got: {reads}"


def test_mtime_change_invalidates_cache(toml_file: Path) -> None:
    # First load
    cfg1 = apidb._load_strategies_toml(toml_file)
    assert "alpha" in cfg1["strategies"]
    # Edit + bump mtime forward (mtime resolution on most FS is 1 s, so
    # we explicitly set it to "now + 1" to avoid a false-cache-hit on
    # filesystems where edit-and-stat happen within the same second).
    toml_file.write_text('[strategies.alpha]\nenabled = false\nparams_file = "alpha-v2.yaml"\n')
    new_mtime = time.time() + 1
    os.utime(toml_file, (new_mtime, new_mtime))

    cfg2 = apidb._load_strategies_toml(toml_file)
    assert cfg2["strategies"]["alpha"]["enabled"] is False
    assert cfg2["strategies"]["alpha"]["params_file"] == "alpha-v2.yaml"
    assert "beta" not in cfg2["strategies"]
