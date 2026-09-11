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


def test_cancel_stops_the_slot():
    ctx = SlotContext(SlotLimits())
    ctx.cancel()
    with pytest.raises(LimitExceeded, match="已取消"):
        ctx.check()


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
