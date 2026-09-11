"""状态机。不阻塞、不读 stdin、不 print。

核心不变量：advance 要么拒绝（状态与 revision 均不变），
要么修改（revision 恰好 +1）。递增只发生在一处，位于校验之后、返回之前——
依赖「所有路径碰巧都会走到某个暂停函数」是靠不住的。

第三种结局——外部依赖失败：collect 与 classify_feedback 直接调用 LLM，
一旦底层传输失败（Task 18 把这类故障统一归一为 ProviderError）或撞额度
（LimitExceeded），二者都不捕获，原样从 advance 里抛出去；调用方看到的是
异常，不是 Rejected/NeedInput。这是刻意的：模型不可达是环境故障，不是
规划结果，不该被编码成状态机里的一个 transition——那会逼着下游把"服务
临时挂了"解释成某种行程语义。调用方（driver/CLI）应该捕获 ProviderError，
告诉用户"模型暂时无法访问，进度已保存在 revision N，稍后重试"。

为了让这条"异常途中，state 不留半吊子改动"站得住，_apply 的每个分支都
排好了顺序：可能失败的调用（classify_feedback）必须先做，成功拿到返回值
之后才能把结果写回 state——反过来的顺序会在调用失败时留下一个已经写进去、
但 revision 没递增的改动，逃出了「拒绝/修改」这个二分律。

`pick_angles` 是例外：它没有 seed/候选可以退回，唯一的产出就是角度本身，
所以它的 ProviderError/LimitExceeded 与它自己那个"角度 key 重复"的裸
ValueError 一视同仁——都收敛成一个 FAILED 占位候选，让流程停在
AWAIT_CHOICE 而不是把异常甩给调用方；run_slot 系的三条候选线同理，一条
线撞见外部依赖失败不该拖累另外两条。
"""

from tripplan.agents.limits import LimitExceeded, SlotContext, SlotLimits
from tripplan.agents.steps import (
    Scale,
    apply_patch,
    classify_feedback,
    collect,
    pick_angles,
)
from tripplan.models.issue import Issue
from tripplan.models.itinerary import Angle
from tripplan.models.requirements import mark_all_confirmed, missing_required
from tripplan.providers.base import ProviderError
from tripplan.slot import run_slot
from tripplan.state import (
    ALLOWED_COMMANDS,
    AWAITING,
    AmendRequirements,
    CandidateSlot,
    ChooseCandidate,
    ConfirmRequirements,
    Done,
    GiveFeedback,
    InputKind,
    NeedInput,
    Rejected,
    RejectReason,
    SlotStatus,
    Stage,
)
from tripplan.validation.diversity import enforce_diversity
from tripplan.validation.resolver import resolve_timezone


def _noop(_event) -> None:
    pass


def advance(state, deps, cmd=None, emit=_noop):
    # ⓪ 终态幂等：已定稿的行程反复查询只回同一个答案，不改状态、不递增 revision。
    #    这个分支必须在 _apply 之前 —— ChooseCandidate 把 stage 推到 DONE 的那一次
    #    仍要走下面的正常路径并递增 revision（否则并发选择又能互相覆盖）。
    #    这里处理的只是「进来时就已经是 DONE」。
    if state.stage is Stage.DONE:
        return Done(state.chosen().itinerary)

    # ① 校验：所有拒绝与空查询都在这里返回 —— 不改状态、不递增 revision
    if state.stage in AWAITING:
        if cmd is None:
            return _pending(state)  # 空调用 = 重新问一遍
        if (bad := _validate(state, cmd)) is not None:
            return Rejected(bad, _pending(state))
    elif cmd is not None:
        return Rejected(RejectReason.WRONG_COMMAND_FOR_STAGE, _pending(state))

    # ② 过了这道线，状态必然改变
    if cmd is not None:
        _apply(state, cmd, deps, emit)  # 只有这里能改 stage
    outcome = _run_to_pause(state, deps, emit)
    state.revision += 1  # ★ 唯一的递增点
    return outcome


def _validate(state, cmd) -> RejectReason | None:
    if cmd.expected_revision != state.revision:
        return RejectReason.STALE_REVISION
    if type(cmd) not in ALLOWED_COMMANDS[state.stage]:
        return RejectReason.WRONG_COMMAND_FOR_STAGE
    if isinstance(cmd, ConfirmRequirements) and missing_required(
        state.requirements or _empty_requirements()
    ):
        return RejectReason.MISSING_REQUIRED
    return _check_candidate(state, cmd)


def _check_candidate(state, cmd) -> RejectReason | None:
    """ChooseCandidate / GiveFeedback 携带的 angle_key 必须真实且可用。"""
    if not isinstance(cmd, (ChooseCandidate, GiveFeedback)):
        return None
    slot = state.slot(cmd.angle_key)
    if slot is None:
        return RejectReason.UNKNOWN_CANDIDATE
    if slot.itinerary is None:  # FAILED 且没跑出任何东西
        return RejectReason.UNSELECTABLE_CANDIDATE
    return None


def _pending(state) -> NeedInput:
    """当前等待态对应的 NeedInput —— 纯函数，可反复调用。"""
    if state.stage is Stage.AWAIT_REQ_CONFIRM:
        return NeedInput(
            InputKind.CONFIRM_REQUIREMENTS, state.requirements, state.revision
        )
    return NeedInput(
        InputKind.CHOOSE_OR_FEEDBACK, list(state.candidates), state.revision
    )


def _empty_requirements():
    from tripplan.models.requirements import Requirements

    return Requirements()


def _step_ctx(emit) -> SlotContext:
    """给 slot 之外的一次性 LLM 步骤（collect / angle / classify）用的额度。

    这些步骤不属于任何候选线，但 run_agent 仍然要一个能记账、能超时的 ctx——
    传 None 会在 ctx.check() 处直接崩。额度按单次调用给，不跨步骤累积。
    """
    return SlotContext(
        SlotLimits(max_tool_calls=0, max_output_tokens=20_000, deadline_s=120),
        emit=emit,
    )


def _apply(state, cmd, deps, emit) -> None:
    """只有这里能改 stage。调用前 advance 已校验 revision、阶段与候选 key。"""
    match cmd:
        case ConfirmRequirements():
            state.requirements = mark_all_confirmed(state.requirements)
            state.stage = Stage.GENERATE

        case AmendRequirements(text=text):
            if state.stage is Stage.AWAIT_REQ_CONFIRM:
                state.raw_request += f"\n用户补充：{text}"
                state.stage = Stage.COLLECT
            else:  # 定稿前改需求
                delta = classify_feedback(
                    text, state.requirements, deps, _step_ctx(emit)
                )
                if delta.patches_requirements and delta.patch:
                    _patch_requirements(state, delta, emit)
                # 否则：classify_feedback 判定「patches_requirements=True 但
                # patch={}」不是真正可执行的变更——按它走 REWRITE/清候选只会
                # 白白丢掉已经跑出来的三个候选。当作没有发生需求变更，原地
                # 停在当前暂停态（_run_to_pause 会把它当成 no-op 直接返回）。

        case ChooseCandidate(angle_key=key):
            state.chosen_key = key
            state.stage = Stage.DONE

        case GiveFeedback(angle_key=key, text=text):
            # ★ 先调用可能失败的 classify_feedback，成功后才落 chosen_key。
            # 反过来的顺序（先选定再分类）会在 classify_feedback 抛
            # ProviderError/LimitExceeded 时，把 chosen_key 已经写进去、
            # revision 却没有递增的半吊子改动留在 state 里——逃出了
            # advance「拒绝/修改」的二分律。同时也只分类这一次：两次结果
            # 可能不一致，状态会就此走歪且无人报错。
            delta = classify_feedback(text, state.requirements, deps, _step_ctx(emit))
            state.chosen_key = key  # 提意见即选定
            if delta.patches_requirements and delta.patch:
                _patch_requirements(state, delta, emit)
            else:
                state.issues = [Issue.from_human(text)]
                state.stage = Stage.REFINE


def _patch_requirements(state, delta, emit) -> None:
    """delta 由调用方传入 —— 不在这里重新分类。调用前已确认 delta.patch 非空。"""
    state.requirements = apply_patch(state.requirements, delta.patch)
    emit(("requirements_patched", delta.patch))  # 非阻塞提示，不拦流程
    state.issues = []  # 旧 issue 基于旧需求，作废

    if "destination" in delta.patch:
        state.trip_timezone = None  # 置空 → 下轮重解析

    if delta.scale is Scale.REWRITE:  # 旧行程整体作废
        state.seeds = {}
        for slot in state.candidates:
            slot.itinerary = None  # 逼 run_slot 重新 generate
            slot.facts = None
            slot.status = SlotStatus.PENDING
    elif state.chosen_key is None:  # 增量 + 尚未选定
        state.seeds = {
            c.angle.key: c.itinerary for c in state.candidates if c.itinerary
        }

    state.stage = Stage.GENERATE if state.chosen_key is None else Stage.REFINE


def _ensure_timezone(state, deps) -> str:
    """幂等：destination 变更时由 _patch_requirements 置空，这里按需重解析。"""
    if state.trip_timezone is None:
        state.trip_timezone = resolve_timezone(state.requirements, deps.provider)
    return state.trip_timezone


def _safe_slot(angle, seed, reqs, tz, deps, emit, issues=(), avoid_poi_ids=()):
    """run_slot 自己只兜 LimitExceeded / ProviderError（已知的两种失败模式）。

    设计要求候选并发容错覆盖"任何"异常，不止这两种——否则 run_slot 内部
    以后新长出的一种失败模式，或者一个纯粹的 bug，照样能把另外两条已经跑
    完的候选一起拖垮。GENERATE 的候选列表推导、diversity 重跑回调、REFINE
    的单候选刷新，三处都经过这里，行为统一。

    写 Task 21+ 的测试时注意：这层 except Exception 是故意宽泛的，连
    FakeLlm 脚本用尽时抛出的 AssertionError 也会被吞成一个 FAILED 候选，
    而不是让"脚本条数不够"以它本来的样子炸出来——调用方看到的只是一个
    "失败了"的候选，不是"脚本写少了一条"的提示。写脚本化测试时自己把
    每条候选线预期消耗的轮次数对齐，不要指望这里的兜底替你发现脚本不够
    长（生产环境不受影响：兜底不收窄，这正是它本来的目的）。
    """
    try:
        return run_slot(
            angle=angle,
            seed=seed,
            reqs=reqs,
            tz=tz,
            deps=deps,
            issues=issues,
            emit=emit,
            avoid_poi_ids=avoid_poi_ids,
        )
    except Exception as e:  # noqa: BLE001 — 故意兜底：详见上面的说明
        return CandidateSlot(
            angle,
            None,
            None,
            SlotStatus.FAILED,
            f"候选线出现未处理异常：{type(e).__name__}: {e}",
        )


def _run_to_pause(state, deps, emit):
    """工作态：一路向前，直到再次需要人或结束。不碰 revision。"""
    while True:
        match state.stage:
            case Stage.COLLECT:
                state.requirements = collect(state.raw_request, deps, _step_ctx(emit))
                state.stage = Stage.AWAIT_REQ_CONFIRM
                return _pending(state)

            case Stage.GENERATE:
                tz = _ensure_timezone(state, deps)
                try:
                    angles = pick_angles(state.requirements, deps, _step_ctx(emit))
                except (ValueError, ProviderError, LimitExceeded) as e:
                    # 三种失败一视同仁地收敛：ValueError 是"一个能解析的
                    # 角度都没有"或"key 重复"（没有安全的默认值可退，不像
                    # 单个字段解析失败那样能当没给）；ProviderError 是模型
                    # 传输层故障（Task 18 的归一化）；LimitExceeded 是撞了
                    # schema 修复/超时/token 额度。三者都不能任它们逃出
                    # advance 变成一截原始 traceback——pick_angles 没有 seed
                    # 或候选可以退回，唯一的产出就是角度本身，所以收敛成一个
                    # FAILED 占位候选，流程仍然停在一个合法的暂停态：用户
                    # 看到的是"生成失败"而不是程序崩溃，还能用
                    # AmendRequirements 补充信息再试一次（若补充触发了真正
                    # 的需求 patch，会重新走到这里重试）。
                    emit(("angle_generation_failed", str(e)))
                    state.candidates = [
                        CandidateSlot(
                            Angle("_error", "角度生成失败", ""),
                            None,
                            None,
                            SlotStatus.FAILED,
                            f"角度生成失败：{e}",
                        )
                    ]
                    state.stage = Stage.AWAIT_CHOICE
                    return _pending(state)

                state.candidates = [
                    _safe_slot(
                        a, state.seeds.get(a.key), state.requirements, tz, deps, emit
                    )
                    for a in angles
                ]
                state.candidates = enforce_diversity(
                    state.candidates,
                    lambda slot, avoid: _safe_slot(
                        slot.angle,
                        None,
                        state.requirements,
                        tz,
                        deps,
                        emit,
                        avoid_poi_ids=avoid,
                    ),
                    emit=emit,
                )
                state.seeds = {}
                state.stage = Stage.AWAIT_CHOICE
                return _pending(state)

            case Stage.REFINE:
                tz = _ensure_timezone(state, deps)
                slot = state.chosen()
                refreshed = _safe_slot(
                    slot.angle,
                    slot.itinerary,
                    state.requirements,
                    tz,
                    deps,
                    emit,
                    issues=state.issues,
                )
                state.candidates = [refreshed]
                state.issues = []
                state.stage = Stage.AWAIT_CHOICE
                return _pending(state)

            case Stage.DONE:
                # 仍然可达，且必须保留：ChooseCandidate 在 _apply 里把 stage 推到
                # DONE，然后落到这里返回——那一次要经过 advance 的递增点。
                # advance 顶部的 DONE 早返回只拦「进来时就已经是 DONE」。
                return Done(state.chosen().itinerary)

            case Stage.AWAIT_CHOICE | Stage.AWAIT_REQ_CONFIRM:
                # 正常不会带着暂停态进到这个循环：_apply 要么把 stage 推向
                # 下一个工作态，要么（AmendRequirements 被分类为"没有可执行
                # 补丁"时）刻意不推。后一种情况属于"这次命令被接受了，但没
                # 有产生任何可执行的变化"——原地返回当前暂停态即可，不能让
                # while True 在一个没有对应 case 的 stage 上悄悄空转下去。
                return _pending(state)
