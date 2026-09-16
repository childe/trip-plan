import pytest

from tripplan.agents.limits import LimitExceeded, SlotContext, SlotLimits
from tripplan.llm.client import Usage


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def test_fresh_context_passes_check():
    SlotContext(SlotLimits()).check()  # 不抛


def test_output_token_budget_is_enforced():
    ctx = SlotContext(SlotLimits(max_output_tokens=100))
    ctx.charge(Usage(input_tokens=9999, output_tokens=60))
    ctx.check()
    ctx.charge(Usage(input_tokens=0, output_tokens=50))
    with pytest.raises(LimitExceeded, match="输出 token"):
        ctx.check()


def test_input_tokens_do_not_count_against_output_budget():
    ctx = SlotContext(SlotLimits(max_output_tokens=100))
    ctx.charge(Usage(input_tokens=1_000_000, output_tokens=1))
    ctx.check()


def test_tool_call_budget_is_enforced():
    ctx = SlotContext(SlotLimits(max_tool_calls=3))
    ctx.charge_tool_calls(3)
    ctx.check()
    ctx.charge_tool_calls(1)
    with pytest.raises(LimitExceeded, match="工具调用"):
        ctx.check()


def test_deadline_is_enforced():
    clock = _Clock()
    ctx = SlotContext(SlotLimits(deadline_s=60), clock=clock)
    clock.advance(59)
    ctx.check()
    clock.advance(2)
    with pytest.raises(LimitExceeded, match="超时"):
        ctx.check()


def test_cancel_raises_cancelled_not_limit_exceeded():
    """取消必须是独立信号。继承或复用 LimitExceeded 会让它在 slot.py:76
    被转成一个 EXHAUSTED 候选，用户按了「取消」却被写进盘里一份
    「候选全部生成失败」的行程（spec §4.3）。"""
    from tripplan.agents.limits import Cancelled

    ctx = SlotContext(SlotLimits())
    ctx.cancel()
    with pytest.raises(Cancelled, match="已取消"):
        ctx.check()


def test_cancelled_is_not_a_limit_exceeded_subclass():
    """这条不是重复：上一条用 pytest.raises(Cancelled) 断言类型，
    而 Cancelled 如果继承了 LimitExceeded，上一条照样通过。"""
    from tripplan.agents.limits import Cancelled

    assert not issubclass(Cancelled, LimitExceeded)


def test_external_cancel_token_stops_the_slot():
    """Web 层拿不到 SlotContext 的句柄（它在 orchestrator 内部现场创建），
    所以取消必须靠一个从外面传进来的令牌（spec §4.3）。"""
    import threading

    from tripplan.agents.limits import Cancelled

    token = threading.Event()
    ctx = SlotContext(SlotLimits(), cancel=token)
    ctx.check()  # 未取消：不抛
    token.set()
    with pytest.raises(Cancelled):
        ctx.check()


def test_budget_limits_still_raise_limit_exceeded_when_a_token_is_present():
    """反证：带取消令牌不会把额度错误也改成 Cancelled。"""
    import threading

    from tripplan.agents.limits import Cancelled

    ctx = SlotContext(SlotLimits(max_output_tokens=100), cancel=threading.Event())
    ctx.charge(Usage(0, 500))
    with pytest.raises(LimitExceeded) as exc:
        ctx.check()
    assert not isinstance(exc.value, Cancelled)


def test_raise_if_cancelled_tolerates_a_missing_token():
    """None 表示「没有取消通道」——CLI 与现有测试都走这条路，不能崩。"""
    import threading

    from tripplan.agents.limits import Cancelled, raise_if_cancelled

    raise_if_cancelled(None)  # 不抛
    raise_if_cancelled(threading.Event())  # 未 set：不抛
    token = threading.Event()
    token.set()
    with pytest.raises(Cancelled):
        raise_if_cancelled(token)


def test_spent_reports_accumulated_usage():
    ctx = SlotContext(SlotLimits())
    ctx.charge(Usage(10, 20))
    ctx.charge(Usage(1, 2))
    assert ctx.spent == Usage(11, 22)


def test_budget_is_shared_across_multiple_run_agent_calls():
    """一条线里 generate + revise×N + critic 共享同一份额度。"""
    ctx = SlotContext(SlotLimits(max_output_tokens=100))
    for _ in range(3):
        ctx.charge(Usage(0, 40))
    with pytest.raises(LimitExceeded):
        ctx.check()
