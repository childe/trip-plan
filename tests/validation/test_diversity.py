from datetime import date

from tripplan.models.itinerary import Angle, Category
from tripplan.state import CandidateSlot, SlotStatus
from tripplan.validation.diversity import (
    enforce_diversity,
    jaccard,
    poi_signature,
    too_similar,
)

D1 = date(2026, 10, 1)


def _slot(mk, key, poi_ids, categories=None):
    cats = categories or [Category.SIGHT] * len(poi_ids)
    acts = [
        mk.act(f"{key}a{i}", "d1", f"{9 + i}:00", f"{10 + i}:00", category=cats[i - 1])
        for i in range(1, len(poi_ids) + 1)
    ]
    itin = mk.itin([mk.day("d1", D1, acts)])
    itin.angle = Angle(key, f"方案{key}", "")
    facts = mk.facts(
        poi_by_activity={a.id: mk.resolved(pid) for a, pid in zip(acts, poi_ids)}
    )
    return CandidateSlot(
        angle=itin.angle, itinerary=itin, facts=facts, status=SlotStatus.OK
    )


def test_signature_collects_resolved_sight_poi_ids(mk):
    slot = _slot(mk, "A", ["B1", "B2", "B3"])
    assert poi_signature(slot.itinerary, slot.facts) == {"B1", "B2", "B3"}


def test_signature_ignores_non_sight_categories(mk):
    slot = _slot(mk, "A", ["B1", "B2"], categories=[Category.SIGHT, Category.MEAL])
    assert poi_signature(slot.itinerary, slot.facts) == {"B1"}


def test_signature_ignores_unresolved_activities(mk):
    slot = _slot(mk, "A", ["B1", "B2"])
    slot.facts.poi_by_activity.pop("Aa2")
    assert poi_signature(slot.itinerary, slot.facts) == {"B1"}


def test_jaccard_basics():
    assert jaccard(frozenset(), frozenset()) == 0.0
    assert jaccard(frozenset({"a"}), frozenset({"a"})) == 1.0
    assert jaccard(frozenset({"a", "b"}), frozenset({"b", "c"})) == 1 / 3


def test_too_similar_finds_the_overlapping_pair(mk):
    slots = [
        _slot(mk, "A", ["B1", "B2", "B3"]),
        _slot(mk, "B", ["B1", "B2", "B3"]),  # 完全相同
        _slot(mk, "C", ["B7", "B8", "B9"]),
    ]
    assert too_similar(slots, threshold=0.6) == [(0, 1)]


def test_too_similar_empty_when_all_distinct(mk):
    slots = [
        _slot(mk, "A", ["B1", "B2"]),
        _slot(mk, "B", ["B3", "B4"]),
        _slot(mk, "C", ["B5", "B6"]),
    ]
    assert too_similar(slots, threshold=0.6) == []


def test_too_similar_skips_slots_without_itinerary(mk):
    slots = [
        _slot(mk, "A", ["B1"]),
        CandidateSlot(angle=_slot(mk, "B", ["B1"]).angle, status=SlotStatus.FAILED),
    ]
    assert too_similar(slots, threshold=0.6) == []


def test_enforce_regenerates_only_the_later_duplicate(mk):
    slots = [
        _slot(mk, "A", ["B1", "B2", "B3"]),
        _slot(mk, "B", ["B1", "B2", "B3"]),
        _slot(mk, "C", ["B7", "B8"]),
    ]
    called = []

    def regenerate(slot, avoid_poi_ids):
        called.append((slot.angle.key, sorted(avoid_poi_ids)))
        return _slot(mk, slot.angle.key, ["B4", "B5"])

    out = enforce_diversity(slots, regenerate, threshold=0.6)
    assert [k for k, _ in called] == ["B"]  # 只重跑靠后那份
    assert called[0][1] == ["B1", "B2", "B3"]  # 重合 POI 作为 avoid
    assert poi_signature(out[1].itinerary, out[1].facts) == {"B4", "B5"}


def test_enforce_retries_at_most_once(mk):
    """仍然重合就如实展示——为差异硬凑一个更差的方案不划算。"""
    slots = [_slot(mk, "A", ["B1", "B2"]), _slot(mk, "B", ["B1", "B2"])]
    calls = []

    def regenerate(slot, avoid_poi_ids):
        calls.append(slot.angle.key)
        return _slot(mk, slot.angle.key, ["B1", "B2"])  # 依然一样

    out = enforce_diversity(slots, regenerate, threshold=0.6)
    assert len(calls) == 1
    assert len(out) == 2


def test_enforce_is_noop_when_already_diverse(mk):
    def must_not_be_called(*_):
        raise AssertionError("已经足够不同，不该触发重跑")

    slots = [_slot(mk, "A", ["B1"]), _slot(mk, "B", ["B2"])]
    assert enforce_diversity(slots, must_not_be_called, threshold=0.6) == slots


def test_too_similar_excludes_exact_threshold_equality(mk):
    """严格 `>`：恰好等于阈值不算「过于相似」。误改成 `>=` 会被此用例捕获。"""
    slots = [
        _slot(mk, "A", ["P1", "P2", "P3"]),
        _slot(mk, "B", ["P1", "P2", "P3", "P4", "P5"]),
    ]
    sig_a = poi_signature(slots[0].itinerary, slots[0].facts)
    sig_b = poi_signature(slots[1].itinerary, slots[1].facts)
    assert jaccard(sig_a, sig_b) == 0.6
    assert too_similar(slots, threshold=0.6) == []


def test_enforce_avoid_set_for_chained_retry_uses_original_signatures(mk):
    """链式重叠（A≈B、B≈C，A 与 C 不相似）时，重跑 C 用的 avoid 集必须来自
    原始的 B∩C，而不是 B 已经被重跑替换之后的新签名——否则 C 的重跑毫无
    指导，等于白跑一次还骗过了『已处理』的假象。"""
    slots = [
        _slot(mk, "A", ["X", "Y"]),  # sig={X,Y}
        _slot(mk, "B", ["X", "Y", "Z"]),  # sig={X,Y,Z}
        _slot(mk, "C", ["Y", "Z"]),  # sig={Y,Z}
    ]
    # jaccard(A,B)=2/3, jaccard(B,C)=2/3, jaccard(A,C)=1/3 —— 与复现报告一致的拓扑。
    assert too_similar(slots, threshold=0.6) == [(0, 1), (1, 2)]

    calls = []

    def regenerate(slot, avoid_poi_ids):
        calls.append((slot.angle.key, sorted(avoid_poi_ids)))
        if slot.angle.key == "B":
            return _slot(mk, "B", ["Q1", "Q2"])  # 与 C 毫无关系的新方案
        return _slot(mk, slot.angle.key, ["Q3", "Q4"])

    enforce_diversity(slots, regenerate, threshold=0.6)

    assert [k for k, _ in calls] == ["B", "C"]
    assert calls[0][1] == ["X", "Y"]  # 真正的 A∩B
    assert calls[1][1] == ["Y", "Z"]  # 真正的 B∩C——不是重跑后的空集


def test_enforce_retries_a_slot_flagged_by_two_pairs_only_once(mk):
    """同一个下标同时是两对重叠中靠后的那个（j 相同）时，也只重跑一次——
    覆盖 `if j in retried: continue` 这条此前没有用例跑到的分支。"""
    slots = [
        _slot(mk, "A", ["P1", "P2"]),  # sig={P1,P2}
        _slot(mk, "B", ["P2", "P3"]),  # sig={P2,P3}
        _slot(mk, "C", ["P1", "P2", "P3"]),  # sig={P1,P2,P3}
    ]
    # jaccard(A,B)=1/3（不触发），jaccard(A,C)=2/3，jaccard(B,C)=2/3 —— C 被两对同时命中。
    assert too_similar(slots, threshold=0.6) == [(0, 2), (1, 2)]

    calls = []

    def regenerate(slot, avoid_poi_ids):
        calls.append(slot.angle.key)
        return _slot(mk, slot.angle.key, ["Q1", "Q2"])

    out = enforce_diversity(slots, regenerate, threshold=0.6)
    assert calls == ["C"]
    assert poi_signature(out[2].itinerary, out[2].facts) == {"Q1", "Q2"}
