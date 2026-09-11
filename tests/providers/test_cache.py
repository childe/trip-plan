from pathlib import Path

from tripplan.providers.cache import DiskCache


def test_put_then_get_roundtrips(tmp_path: Path):
    c = DiskCache(tmp_path, ttl_days=7)
    c.put("k1", {"a": 1})
    assert c.get("k1") == {"a": 1}


def test_get_missing_returns_none(tmp_path: Path):
    assert DiskCache(tmp_path, ttl_days=7).get("nope") is None


def test_entry_expires_after_ttl(tmp_path: Path):
    c = DiskCache(tmp_path, ttl_days=7)
    c.put("k1", {"a": 1})
    stale = DiskCache(tmp_path, ttl_days=0)
    assert stale.get("k1") is None


def test_keys_with_slashes_do_not_escape_the_cache_dir(tmp_path: Path):
    c = DiskCache(tmp_path, ttl_days=7)
    c.put("../../evil", {"a": 1})
    assert c.get("../../evil") == {"a": 1}
    assert not (tmp_path.parent.parent / "evil").exists()


def test_corrupt_entry_is_treated_as_miss(tmp_path: Path):
    c = DiskCache(tmp_path, ttl_days=7)
    c.put("k1", {"a": 1})
    next(tmp_path.glob("*.json")).write_text("{ not json")
    assert c.get("k1") is None
