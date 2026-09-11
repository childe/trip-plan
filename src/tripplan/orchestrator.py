"""状态机。不阻塞、不读 stdin、不 print。

核心不变量：advance 要么拒绝（状态与 revision 均不变），
要么修改（revision 恰好 +1）。递增只发生在一处，位于校验之后、返回之前——
依赖「所有路径碰巧都会走到某个暂停函数」是靠不住的。
"""

from tripplan.models.requirements import missing_required
from tripplan.state import (
    ALLOWED_COMMANDS,
    AWAITING,
    ChooseCandidate,
    ConfirmRequirements,
    Done,
    GiveFeedback,
    InputKind,
    NeedInput,
    Rejected,
    RejectReason,
    Stage,
)


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


# ---- 以下两个函数在 Task 20 补全 ----


def _apply(state, cmd, deps, emit) -> None:
    raise NotImplementedError


def _run_to_pause(state, deps, emit):
    raise NotImplementedError
