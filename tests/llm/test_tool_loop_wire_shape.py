"""覆盖 §3 那个地基级取舍：工具结果被拍平成纯文本，messages 恒为
{"role": str, "content": str}，两家 API 都能直接接受。

这是整个设计能成立的前提，必须有端到端测试钉住——否则后来人会把它
"修好"成原生 tool 协议，provider 中立性当场毁掉。
"""

import json

import pytest

from tripplan.agents.limits import SlotContext, SlotLimits
from tripplan.agents.runner import run_agent
from tripplan.llm.client import FakeLlm, LlmResponse, ToolCall, Usage
from tripplan.llm.config import Role

_SCHEMA = {
    "type": "object",
    "required": ["ok"],
    "properties": {"ok": {"type": "boolean"}},
}


def _ctx():
    return SlotContext(SlotLimits(), emit=lambda *a, **k: None)


def test_tool_round_wire_shape_stays_provider_neutral():
    llm = FakeLlm(
        [
            LlmResponse(
                "tool_use", "", [ToolCall("c1", "probe", {"q": "芜湖"})], Usage(1, 1)
            ),
            LlmResponse("end_turn", '{"ok": true}', [], Usage(1, 1)),
        ]
    )
    run_agent(
        system_prompt="sys",
        user_prompt="hi",
        tools=[{"name": "probe", "description": "d", "input_schema": {}}],
        output_schema=_SCHEMA,
        role=Role.PLANNER,
        ctx=_ctx(),
        client=llm,
        tool_impls={"probe": lambda q: {"hit": q}},
    )
    sent = llm.calls[-1].messages
    assert all(set(m) == {"role", "content"} for m in sent)
    assert all(isinstance(m["content"], str) and m["content"] for m in sent)
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]
    assert not any("tool_calls" in m for m in sent)
    assert not any(m["role"] == "tool" for m in sent)


def test_assistant_turn_shows_what_the_model_actually_called():
    """补偿一。OpenAI 推理模型发起工具调用时 content 恒为 None，历史里那一轮
    只剩字面 "(tool_use)"，模型不知道自己查的是哪个词 → 重复调用 →
    烧穿 max_tool_calls=40（整条候选线的累计额度）。"""
    llm = FakeLlm(
        [
            LlmResponse(
                "tool_use", "", [ToolCall("c1", "probe", {"q": "芜湖"})], Usage(1, 1)
            ),
            LlmResponse("end_turn", '{"ok": true}', [], Usage(1, 1)),
        ]
    )
    run_agent(
        system_prompt="sys",
        user_prompt="hi",
        tools=None,
        output_schema=_SCHEMA,
        role=Role.PLANNER,
        ctx=_ctx(),
        client=llm,
        tool_impls={"probe": lambda q: {"hit": q}},
    )
    assistant = llm.calls[-1].messages[1]["content"]
    assert "probe" in assistant
    assert "芜湖" in assistant


def test_tool_args_are_truncated_in_history():
    """args 完整抄进历史会显著加长 planner 的对话，抬高撞上
    model_context_window_exceeded 的概率。用 §10.1 同一把尺子：200 字符。"""
    long_arg = "x" * 500
    llm = FakeLlm(
        [
            LlmResponse(
                "tool_use", "", [ToolCall("c1", "probe", {"q": long_arg})], Usage(1, 1)
            ),
            LlmResponse("end_turn", '{"ok": true}', [], Usage(1, 1)),
        ]
    )
    run_agent(
        system_prompt="sys",
        user_prompt="hi",
        tools=None,
        output_schema=_SCHEMA,
        role=Role.PLANNER,
        ctx=_ctx(),
        client=llm,
        tool_impls={"probe": lambda q: {"hit": "ok"}},
    )
    assistant = llm.calls[-1].messages[1]["content"]
    assert len(assistant) < 400


@pytest.mark.parametrize("blank", ["", "   ", "\n\t"])
def test_repair_round_never_sends_blank_assistant_content(blank):
    """补偿二。Anthropic 对空 content 返回 400，对纯空白同样。而 `"   "`
    是 truthy，裸 `or` 兜不住——必须 .strip()。

    只用 blank="" 测的话，`resp.text or ...` 与 `resp.text.strip() or ...`
    两种实现都会变绿，而前者已被证伪。参数化是区分它们的唯一手段。
    """
    llm = FakeLlm(
        [
            LlmResponse("end_turn", blank, [], Usage(1, 1)),  # 触发 SchemaError
            LlmResponse("end_turn", '{"ok": true}', [], Usage(1, 1)),
        ]
    )
    run_agent(
        system_prompt="sys",
        user_prompt="hi",
        tools=None,
        output_schema=_SCHEMA,
        role=Role.PLANNER,
        ctx=_ctx(),
        client=llm,
        tool_impls={},
    )
    sent = llm.calls[-1].messages
    assistant = [m for m in sent if m["role"] == "assistant"]
    assert assistant, "修复轮必须往 messages 里写过一条 assistant"
    assert all(m["content"].strip() for m in assistant)
    assert assistant[0]["content"] == "(空回复)"
