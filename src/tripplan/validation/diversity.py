"""候选差异度。角度命名自由，但产出必须真的不同。"""

from itertools import combinations
from typing import Callable

from tripplan.models.facts import FactSnapshot
from tripplan.models.itinerary import Category, Itinerary
from tripplan.state import CandidateSlot

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


def _signature_of(slot: CandidateSlot) -> frozenset[str] | None:
    if slot.itinerary is None or slot.facts is None:
        return None
    return poi_signature(slot.itinerary, slot.facts)


def too_similar(
    slots: list[CandidateSlot], threshold: float = DIVERSITY_THRESHOLD
) -> list[tuple[int, int]]:
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
    slots: list[CandidateSlot],
    regenerate: Callable[[CandidateSlot, frozenset[str]], CandidateSlot],
    emit=_noop,
    threshold: float = DIVERSITY_THRESHOLD,
) -> list[CandidateSlot]:
    """重合度超阈值时重跑靠后的那一份，至多一次。

    每一对的 avoid 集合要在任何重跑发生之前、从原始签名一次性算好并快照。
    重跑会替换 slots；如果中途现读 result[i]/result[j]，链式重叠（同一个下标
    先当 j 被替换、随后又当 i 参与下一对比较）时，后面那次算出的 avoid 集
    会读到已经被替换掉的邻居，而不是原本真正重叠的那份——重跑等于没给指导，
    还会被误当作「已处理」。

    regenerate 是回调（slot, 需要避开的 POI id 集合）-> 新 slot，
    因此本模块不依赖 LLM 层。
    """
    result = list(slots)
    overlaps: list[tuple[int, int, frozenset[str]]] = []
    for i, j in too_similar(result, threshold):
        si = _signature_of(result[i]) or frozenset()
        sj = _signature_of(result[j]) or frozenset()
        overlaps.append((i, j, si & sj))

    retried: set[int] = set()
    for i, j, overlap in overlaps:
        if j in retried:
            continue
        emit(("diversity_retry", result[j].angle.key, sorted(overlap)))
        retried.add(j)
        result[j] = regenerate(result[j], overlap)
    return result
