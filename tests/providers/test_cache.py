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


def test_entry_truncated_mid_multibyte_character_is_a_miss(tmp_path: Path):
    """写入被从多字节字符中间截断（比如崩溃在 fsync 之前）也不能让
    UnicodeDecodeError 逃出 get()——"缓存坏了就当没命中"要对所有坏法成立。"""
    c = DiskCache(tmp_path, ttl_days=7)
    c.put("k1", {"name": "清水寺"})
    path = next(tmp_path.glob("*.json"))
    raw = path.read_bytes()
    # "清" 的 UTF-8 编码占 3 字节，从中间切一刀，制造无法解码的半个字符
    cut = raw.index("清".encode("utf-8")) + 1
    path.write_bytes(raw[:cut])
    assert c.get("k1") is None


def test_put_leaves_no_stray_temp_file(tmp_path: Path):
    c = DiskCache(tmp_path, ttl_days=7)
    c.put("k1", {"a": 1})
    assert list(tmp_path.glob("*.tmp")) == []
