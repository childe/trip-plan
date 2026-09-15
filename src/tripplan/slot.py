"""单条候选线的打磨循环。GENERATE 与 REFINE 复用同一个函数。

任何一条线超限都不会拖垮另外两条：三种出路（收敛 / 撞上限 / 外部依赖失败）
都返回一个带 detail 的 CandidateSlot，绝不卡住、也绝不抛异常炸穿。
"""

from tripplan.agents._emit import safe_emit as _safe_emit
from tripplan.agents.limits import LimitExceeded, SlotContext, SlotLimits
from tripplan.agents.steps import generate, revise, run_llm_critic
from tripplan.models.issue import Severity, has_blocking
from tripplan.providers.base import ProviderError
from tripplan.state import CandidateSlot, SlotStatus
from tripplan.validation.resolver import resolve
from tripplan.validation.rules import run_rule_checks


def _noop(_event) -> None:
    pass


def run_slot(
    angle,
    seed,
    reqs,
    tz,
    deps,
    issues=(),
    limits: SlotLimits = SlotLimits(),
    emit=_noop,
    avoid_poi_ids=(),
) -> CandidateSlot:
    ctx = SlotContext(limits, emit=emit)
    itin, facts = seed, None
    issues = list(issues)
    revisions = 0  # 真正调用过 revise 的次数——轮 0 只校验首稿，不一定修订

    try:
        if itin is None:
            _safe_emit(emit, ("generating", angle.key))
            itin = generate(reqs, angle, deps, ctx, avoid_poi_ids=avoid_poi_ids)

        for rnd in range(limits.max_rounds):
            if issues:  # 有待办问题就先改
                _safe_emit(emit, ("revision", angle.key, rnd))
                itin = revise(itin, reqs, issues, deps, ctx)
                revisions += 1

            facts = resolve(itin, reqs, deps.provider, tz)  # 本轮唯一的 I/O
            issues = run_rule_checks(itin, reqs, facts)  # ① 纯函数，便宜
            if not has_blocking(issues):
                # ② 贵：硬伤清完才请 critic。点评一份时间都对不上的行程没意义。
                issues = issues + run_llm_critic(itin, reqs, deps, ctx)
            if not has_blocking(issues):
                itin.issues = issues
                return CandidateSlot(angle, itin, facts, SlotStatus.OK)

        itin.issues = issues
        blocking = sum(1 for i in issues if i.severity is Severity.BLOCKING)
        return CandidateSlot(
            angle,
            itin,
            facts,
            SlotStatus.EXHAUSTED,
            f"修订 {revisions} 次后仍有 {blocking} 个硬伤",
        )

    except LimitExceeded as e:
        if itin is not None:
            itin.issues = list(issues)
        return CandidateSlot(
            angle,
            itin,
            facts,
            SlotStatus.EXHAUSTED if itin is not None else SlotStatus.FAILED,
            f"资源超限：{e}",
        )
    except ProviderError as e:
        return CandidateSlot(
            angle, itin, facts, SlotStatus.FAILED, f"外部依赖失败：{e}"
        )
