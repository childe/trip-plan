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
