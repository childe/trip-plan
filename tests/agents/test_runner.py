import json
from decimal import Decimal

import pytest

from tripplan.agents.limits import LimitExceeded, SlotContext, SlotLimits
from tripplan.agents.runner import SchemaError, _parse_and_validate, run_agent
from tripplan.agents.schemas import ANGLES_SCHEMA, CRITIQUE_SCHEMA, ITINERARY_SCHEMA
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


# ---------- 最终评审 C1/I1：按 schema 声明的 type 递归校验 ----------
#
# 这一组守的是评审那条裁定：schema 里**本来就写着**每一个被忽略掉的类型。
# 不按它执行的后果不是"校验不够严"，而是一个 null/数字/对象标量能干净地
# 穿过解析、穿过领域对象、穿过 state.json 往返，最后在 escape() 里炸成一截
# AttributeError——而且 `trip render` 会永远复现，itinerary.html 再也出不来。


def _act(**over):
    base = {
        "poi_query": "清水寺",
        "start": "09:00",
        "end": "10:00",
        "category": "SIGHT",
        "cost": {"amount": 400, "currency": "JPY"},
        "indoor": False,
        "note": "",
    }
    base.update(over)
    return base


def _plan(day_over=None, **act_over):
    day = {"date": "2026-10-01", "lodging": "京都塔", "activities": [_act(**act_over)]}
    day.update(day_over or {})
    return {"days": [day]}


#: 评审 C1 点名的五种毒数据，全部满足各自 schema 的 required，
#: 全部在旧实现下"零 issue 地"通过校验，全部只炸 HTML 不炸 Markdown。
_POISON = [
    pytest.param(
        _plan(cost={"amount": 400, "currency": None}),
        ITINERARY_SCHEMA,
        "cost.currency",
        id="cost.currency=null",
    ),
    pytest.param(_plan(poi_query=None), ITINERARY_SCHEMA, "poi_query", id="poi=null"),
    pytest.param(_plan(note=3), ITINERARY_SCHEMA, "note", id="note=number"),
    pytest.param(
        _plan(day_over={"lodging": {"name": "京都塔酒店"}}),
        ITINERARY_SCHEMA,
        "lodging",
        id="lodging=object",
    ),
    pytest.param(
        {"issues": [{"severity": "BLOCKING", "message": None}]},
        CRITIQUE_SCHEMA,
        "message",
        id="critic.message=null",
    ),
]


@pytest.mark.parametrize("payload,schema,where", _POISON)
def test_non_string_scalar_is_rejected_by_the_declared_type(payload, schema, where):
    with pytest.raises(SchemaError) as exc:
        _parse_and_validate(json.dumps(payload, ensure_ascii=False), schema)
    assert where in str(exc.value)  # 报错要指到具体位置，模型才改得动


@pytest.mark.parametrize("payload,schema,where", _POISON)
def test_type_violation_engages_the_repair_loop_rather_than_failing_outright(
    payload, schema, where
):
    """关键在"打回重写"而不是"直接失败"：修在 run_agent 这一层，模型还能
    把字段改对，用户拿回的是数据；修在渲染器里加 isinstance 只能把数据丢掉。"""
    good = {"days": []} if schema is ITINERARY_SCHEMA else {"issues": []}
    llm = FakeLlm(
        [
            _text(json.dumps(payload, ensure_ascii=False)),
            _text(json.dumps(good, ensure_ascii=False)),
        ]
    )
    out = run_agent(
        system_prompt="sys",
        user_prompt="do it",
        tools=None,
        output_schema=schema,
        role=Role.PLANNER,
        ctx=SlotContext(SlotLimits(max_schema_repairs=1)),
        client=llm,
        tool_impls={},
    )
    assert out == good
    assert len(llm.calls) == 2
    # 修复提示里必须带上出错的位置，否则模型只能瞎猜
    assert any(where in str(m) for m in llm.calls[-1].messages)


def test_angle_key_of_the_wrong_type_is_rejected():
    """评审 I6：{"key": []} 会让 pick_angles 在 len(set(keys)) 处抛
    TypeError（unhashable），那个类型不在 orchestrator 的 except 元组里，
    直接变成一截裸 traceback。key 声明的是 string，照着执行就没这回事。"""
    payload = {"angles": [{"key": [], "title": "T"}]}
    with pytest.raises(SchemaError, match="key"):
        _parse_and_validate(json.dumps(payload), ANGLES_SCHEMA)


# ---------- 校验器本身的行为 ----------


def test_nested_array_items_are_checked_elementwise():
    schema = {
        "type": "object",
        "properties": {"xs": {"type": "array", "items": {"type": "string"}}},
    }
    with pytest.raises(SchemaError, match=r"xs\[1\]"):
        _parse_and_validate('{"xs": ["a", 2, "c"]}', schema)


def test_union_type_accepts_every_listed_member():
    schema = {
        "type": "object",
        "properties": {"x": {"type": ["string", "null"]}},
    }
    assert _parse_and_validate('{"x": null}', schema) == {"x": None}
    assert _parse_and_validate('{"x": "s"}', schema) == {"x": "s"}


def test_boolean_does_not_satisfy_number_even_though_python_says_bool_is_int():
    """JSON 里 true 不是数字；Python 里 bool 是 int 的子类。不显式排除的话，
    {"amount": true} 会被当成合法金额，然后 Decimal(str(True)) 才炸。"""
    schema = {"type": "object", "properties": {"n": {"type": "number"}}}
    with pytest.raises(SchemaError, match="n"):
        _parse_and_validate('{"n": true}', schema)
    assert _parse_and_validate('{"n": 1}', schema) == {"n": 1}


def test_number_does_not_satisfy_boolean_either():
    schema = {"type": "object", "properties": {"b": {"type": "boolean"}}}
    with pytest.raises(SchemaError, match="b"):
        _parse_and_validate('{"b": 1}', schema)


def test_absent_optional_property_is_not_type_checked():
    """缺字段的语义归 steps.py 的三级兜底管（跳过这一项、留一条 WARNING）；
    校验器只管"给了的东西类型对不对"，不能顺手把嵌套 required 也接管过来——
    那会把"30 个活动里有 1 个缺 poi_query"升级成"整份行程作废重来"。"""
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
        "required": ["a"],
    }
    assert _parse_and_validate('{"a": "x"}', schema) == {"a": "x"}


def test_missing_nested_required_key_still_passes_validation():
    payload = {"angles": [{"key": "A"}]}  # 缺 title
    assert _parse_and_validate(json.dumps(payload), ANGLES_SCHEMA) == payload


def test_property_without_a_declared_type_accepts_anything():
    """_FIELD 里的 "value": {} 是刻意不声明类型的——十二个字段的 value
    形状各不相同（字符串/对象/数组/枚举），交给 _PARSERS 去解释。"""
    schema = {"type": "object", "properties": {"value": {}}}
    for raw in ('{"value": null}', '{"value": 3}', '{"value": {"a": 1}}'):
        _parse_and_validate(raw, schema)  # 不抛


def test_unknown_type_keyword_is_ignored_rather_than_rejecting_everything():
    schema = {"type": "object", "properties": {"x": {"type": "date-time"}}}
    assert _parse_and_validate('{"x": "2026-10-01"}', schema) == {"x": "2026-10-01"}


# ---------- §13：推理预算耗尽的诊断谓词 ----------
#
# 谓词必须精确，否则会复制它本要修的那个毛病——诊断与真实原因无关。


def test_hint_added_when_every_non_tool_round_is_blank():
    llm = FakeLlm(
        [
            LlmResponse("end_turn", "", [], Usage(1, 1)),
            LlmResponse("end_turn", "", [], Usage(1, 1)),
            LlmResponse("end_turn", "", [], Usage(1, 1)),
        ]
    )
    ctx = SlotContext(SlotLimits(max_schema_repairs=1), emit=lambda *a, **k: None)
    with pytest.raises(LimitExceeded) as exc:
        run_agent(
            system_prompt="s",
            user_prompt="u",
            tools=None,
            output_schema={"type": "object", "required": []},
            role=Role.CLASSIFIER,
            ctx=ctx,
            client=llm,
            tool_impls={},
        )
    message = str(exc.value)
    assert "classifier" in message
    assert "推理预算" in message
    # 不带具体数值——run_agent 这一层拿不到 per-role max_tokens
    assert "1000" not in message


def test_no_hint_when_exhausted_by_tool_calls():
    """工具轮误报。OpenAI 推理模型发起工具调用时 content 恒为 None，经
    归一化成 ""，于是「每一轮 text 都空」为真——但真因是工具空转烧穿
    max_tool_calls，与 max_tokens 毫无关系。"""
    llm = FakeLlm(
        [
            LlmResponse("tool_use", "", [ToolCall("c", "probe", {})], Usage(1, 1))
            for _ in range(5)
        ]
    )
    ctx = SlotContext(SlotLimits(max_tool_calls=2), emit=lambda *a, **k: None)
    with pytest.raises(LimitExceeded) as exc:
        run_agent(
            system_prompt="s",
            user_prompt="u",
            tools=None,
            output_schema={"type": "object", "required": []},
            role=Role.PLANNER,
            ctx=ctx,
            client=llm,
            tool_impls={"probe": lambda: {}},
        )
    assert "推理预算" not in str(exc.value)


def test_no_hint_when_interrupted_before_any_round():
    """零轮真空为真。runner.py:129 的 ctx.check() 在 client.chat 之前，
    而 ctx 的作用域是整条候选线——第二、三次 run_agent 可能一次 chat 都
    没发出就被 deadline 打断。"""
    llm = FakeLlm([])
    ctx = SlotContext(SlotLimits(max_output_tokens=0), emit=lambda *a, **k: None)
    ctx.charge(Usage(0, 1))  # 预先把额度打满
    with pytest.raises(LimitExceeded) as exc:
        run_agent(
            system_prompt="s",
            user_prompt="u",
            tools=None,
            output_schema={"type": "object", "required": []},
            role=Role.PLANNER,
            ctx=ctx,
            client=llm,
            tool_impls={},
        )
    assert "推理预算" not in str(exc.value)
