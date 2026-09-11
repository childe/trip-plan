from decimal import Decimal

import pytest

from tripplan.agents.limits import LimitExceeded, SlotContext, SlotLimits
from tripplan.agents.runner import SchemaError, run_agent
from tripplan.llm.client import FakeLlm, LlmResponse, ToolCall, Usage
from tripplan.llm.config import Role

SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
}


def _text(payload: str, out_tokens: int = 10) -> LlmResponse:
    return LlmResponse("end_turn", payload, [], Usage(100, out_tokens))


def _tool(name: str, args: dict) -> LlmResponse:
    return LlmResponse("tool_use", "", [ToolCall("t1", name, args)], Usage(100, 5))


def _tool_batch(n: int, name: str = "lookup") -> LlmResponse:
    """一次响应里打包 n 个 tool_use 块（模型批量并行调用工具）。"""
    calls = [ToolCall(f"t{i}", name, {"q": str(i)}) for i in range(n)]
    return LlmResponse("tool_use", "", calls, Usage(100, 5))


def _run(llm, ctx=None, tool_impls=None, tools=None):
    return run_agent(
        system_prompt="sys",
        user_prompt="do it",
        tools=tools,
        output_schema=SCHEMA,
        role=Role.PLANNER,
        ctx=ctx or SlotContext(SlotLimits()),
        client=llm,
        tool_impls=tool_impls or {},
    )


def test_returns_parsed_structured_output():
    assert _run(FakeLlm([_text('{"answer": "ok"}')])) == {"answer": "ok"}


def test_strips_markdown_fence_around_json():
    llm = FakeLlm([_text('```json\n{"answer": "ok"}\n```')])
    assert _run(llm) == {"answer": "ok"}


def test_executes_tools_and_feeds_results_back():
    llm = FakeLlm([_tool("lookup", {"q": "京都"}), _text('{"answer": "ok"}')])
    seen = []

    def lookup(q):
        seen.append(q)
        return {"hits": 3}

    assert _run(llm, tool_impls={"lookup": lookup}) == {"answer": "ok"}
    assert seen == ["京都"]
    # 工具结果作为一条 user 消息回喂
    last_messages = llm.calls[-1].messages
    assert any("hits" in str(m) for m in last_messages)


def test_tool_error_is_reported_to_the_model_not_raised():
    """工具报错让模型自己换个方式，不该炸穿整条线。"""
    llm = FakeLlm([_tool("lookup", {"q": "x"}), _text('{"answer": "ok"}')])

    def lookup(q):
        raise RuntimeError("上游 500")

    assert _run(llm, tool_impls={"lookup": lookup}) == {"answer": "ok"}
    assert any("上游 500" in str(m) for m in llm.calls[-1].messages)


def test_unknown_tool_is_reported_to_the_model():
    llm = FakeLlm([_tool("nope", {}), _text('{"answer": "ok"}')])
    assert _run(llm) == {"answer": "ok"}
    assert any("nope" in str(m) for m in llm.calls[-1].messages)


def test_schema_violation_triggers_one_repair_round():
    llm = FakeLlm([_text('{"wrong": 1}'), _text('{"answer": "ok"}')])
    assert _run(llm) == {"answer": "ok"}
    assert len(llm.calls) == 2


def test_repair_attempts_are_capped():
    """「失败则重试」没有上限，就是一条安静吃掉整个预算的路径。"""
    llm = FakeLlm([_text('{"wrong": 1}')] * 10)
    ctx = SlotContext(SlotLimits(max_schema_repairs=2))
    with pytest.raises(LimitExceeded, match="schema"):
        _run(llm, ctx=ctx)
    assert len(llm.calls) == 3  # 首次 + 2 次修复


def test_invalid_json_also_counts_as_schema_failure():
    llm = FakeLlm([_text("这不是 JSON")] * 5)
    with pytest.raises(LimitExceeded):
        _run(llm, ctx=SlotContext(SlotLimits(max_schema_repairs=1)))


def test_output_tokens_are_charged_to_context():
    ctx = SlotContext(SlotLimits())
    _run(FakeLlm([_text('{"answer": "ok"}', out_tokens=42)]), ctx=ctx)
    assert ctx.spent.output_tokens == 42


def test_tool_calls_are_charged_to_context():
    llm = FakeLlm([_tool("lookup", {"q": "a"}), _text('{"answer": "ok"}')])
    ctx = SlotContext(SlotLimits())
    _run(llm, ctx=ctx, tool_impls={"lookup": lambda q: {}})
    assert ctx.tool_calls == 1


def test_loop_stops_when_tool_budget_exhausted():
    """裸 while True 的核心风险：模型一直调工具，永远不收敛。"""
    llm = FakeLlm([_tool("lookup", {"q": "a"})] * 50)
    ctx = SlotContext(SlotLimits(max_tool_calls=3))
    with pytest.raises(LimitExceeded, match="工具调用"):
        _run(llm, ctx=ctx, tool_impls={"lookup": lambda q: {}})


def test_loop_stops_when_deadline_passes():
    class Clock:
        def __init__(self):
            self.t = 0.0

        def __call__(self):
            self.t += 30  # 每次 check 走 30 秒
            return self.t

    llm = FakeLlm([_tool("lookup", {"q": "a"})] * 50)
    ctx = SlotContext(SlotLimits(deadline_s=60), clock=Clock())
    with pytest.raises(LimitExceeded, match="超时"):
        _run(llm, ctx=ctx, tool_impls={"lookup": lambda q: {}})


def test_fence_with_preamble_still_parses():
    """模型在围栏前加一句客套话，答案本身仍然是合法 JSON——不该被当成 schema 失败。"""
    llm = FakeLlm([_text('这是你要的 JSON：\n```json\n{"answer": "ok"}\n```')])
    ctx = SlotContext(SlotLimits(max_schema_repairs=0))
    assert _run(llm, ctx=ctx) == {"answer": "ok"}


def test_fence_with_trailing_text_still_parses():
    """围栏后面多一句话，同样不该判定为 schema 失败。"""
    llm = FakeLlm([_text('```json\n{"answer": "ok"}\n```\n还需要什么告诉我')])
    ctx = SlotContext(SlotLimits(max_schema_repairs=0))
    assert _run(llm, ctx=ctx) == {"answer": "ok"}


def test_two_consecutive_preamble_wrapped_responses_converge_not_exhaust():
    """两条都是合法 JSON（只是围栏前带了闲聊），应该第一条就收敛返回，
    而不是被误判为连续两次 schema 失败、吃光修复预算后报错。"""
    llm = FakeLlm(
        [
            _text('好的，这是结果：\n```json\n{"answer": "a"}\n```'),
            _text('再说一遍：\n```json\n{"answer": "b"}\n```'),
        ]
    )
    ctx = SlotContext(SlotLimits(max_schema_repairs=1))
    assert _run(llm, ctx=ctx) == {"answer": "a"}
    assert len(llm.calls) == 1


def test_tool_call_cap_stops_execution_at_exactly_the_cap():
    """check-then-act 的竞态：额度打穿的那一轮工具不该被执行——
    上限 3、每轮 1 个工具，应该恰好执行 3 次，而不是 4 次。"""
    executed = []

    def lookup(q):
        executed.append(q)
        return {}

    llm = FakeLlm([_tool("lookup", {"q": "a"})] * 50)
    ctx = SlotContext(SlotLimits(max_tool_calls=3))
    with pytest.raises(LimitExceeded, match="工具调用"):
        _run(llm, ctx=ctx, tool_impls={"lookup": lookup})
    assert len(executed) == 3


def test_batched_tool_calls_over_budget_execute_none():
    """一次响应批量打包 5 个工具调用，上限只有 3——这一整轮应该一个都不执行，
    而不是先把 5 个全部执行完再在下一轮才发现超限。"""
    executed = []

    def lookup(q):
        executed.append(q)
        return {}

    llm = FakeLlm([_tool_batch(5)])
    ctx = SlotContext(SlotLimits(max_tool_calls=3))
    with pytest.raises(LimitExceeded, match="工具调用"):
        _run(llm, ctx=ctx, tool_impls={"lookup": lookup})
    assert executed == []


def test_tool_result_containing_decimal_is_serialised_not_swallowed_as_error():
    """项目里金额一律用 Decimal；工具结果里带 Decimal 不该被
    json.dumps 的 TypeError 伪装成"工具报错"回喂给模型。"""

    def price(x):
        return {"total": Decimal("12.50")}

    llm = FakeLlm([_tool("price", {"x": 1}), _text('{"answer": "ok"}')])
    assert _run(llm, tool_impls={"price": price}) == {"answer": "ok"}
    last_messages = llm.calls[-1].messages
    assert any("12.50" in str(m) for m in last_messages)
    assert not any("错误" in str(m) for m in last_messages)
