"""候选差异度。角度命名自由，但产出必须真的不同。"""

from itertools import combinations
from typing import Callable

from tripplan.models.facts import FactSnapshot
from tripplan.models.itinerary import Category, Itinerary

DIVERSITY_THRESHOLD = 0.6


def _noop(_event) -> None:
    pass


def poi_signature(itin: Itinerary, facts: FactSnapshot) -> frozenset[str]:
    """一份行程的「核心 POI」指纹：已解析的观光类 POI id 集合。

    未解析的活动不参与比较——拿猜的身份去算相似度只会得到噪声。
    """
    ids = set()
    for act in itin.all_activities():
        if act.category is not Category.SIGHT:
            continue
        poi_id = facts.poi_id_for(act.id)
        if poi_id is not None:
            ids.add(poi_id)
    return frozenset(ids)


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def _signature_of(slot) -> frozenset[str] | None:
    if slot.itinerary is None or slot.facts is None:
        return None
    return poi_signature(slot.itinerary, slot.facts)


def too_similar(slots, threshold: float = DIVERSITY_THRESHOLD):
    """返回重合度超阈值的下标对，靠后的那个是待重跑的。"""
    pairs = []
    for i, j in combinations(range(len(slots)), 2):
        si, sj = _signature_of(slots[i]), _signature_of(slots[j])
        if si is None or sj is None:
            continue
        if jaccard(si, sj) > threshold:
            pairs.append((i, j))
    return pairs


def enforce_diversity(
    slots: list,
    regenerate: Callable[[object, frozenset[str]], object],
    emit=_noop,
    threshold: float = DIVERSITY_THRESHOLD,
) -> list:
    """重合度超阈值时重跑靠后的那一份，至多一次。

    regenerate 是回调（slot, 需要避开的 POI id 集合）-> 新 slot，
    因此本模块不依赖 LLM 层。
    """
    result = list(slots)
    retried: set[int] = set()
    for i, j in too_similar(result, threshold):
        if j in retried:
            continue
        overlap = (_signature_of(result[i]) or frozenset()) & (
            _signature_of(result[j]) or frozenset()
        )
        emit(("diversity_retry", result[j].angle.key, sorted(overlap)))
        retried.add(j)
        result[j] = regenerate(result[j], overlap)
    return result
