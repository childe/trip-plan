from unittest.mock import MagicMock, patch

import pytest

from tripplan.llm.client import AnthropicClient, FakeLlm, LlmResponse, ToolCall, Usage
from tripplan.llm.config import DEFAULT_ROLES, Role, RoleConfig


def test_fake_returns_scripted_responses_in_order():
    llm = FakeLlm(
        [
            LlmResponse("end_turn", '{"a": 1}', [], Usage(10, 5)),
            LlmResponse("end_turn", '{"a": 2}', [], Usage(10, 5)),
        ]
    )
    assert llm.chat(Role.PLANNER, "sys", [], None).text == '{"a": 1}'
    assert llm.chat(Role.PLANNER, "sys", [], None).text == '{"a": 2}'


def test_fake_raises_when_script_runs_out():
    llm = FakeLlm([LlmResponse("end_turn", "x", [], Usage(1, 1))])
    llm.chat(Role.PLANNER, "sys", [], None)
    with pytest.raises(AssertionError, match="脚本已用尽"):
        llm.chat(Role.PLANNER, "sys", [], None)


def test_fake_records_calls_for_assertions():
    llm = FakeLlm([LlmResponse("end_turn", "x", [], Usage(1, 1))])
    llm.chat(Role.CRITIC, "sys", [{"role": "user", "content": "hi"}], None)
    assert llm.calls[0].role is Role.CRITIC
    assert llm.calls[0].system == "sys"


def test_fake_can_be_scripted_per_role():
    llm = FakeLlm(
        by_role={
            Role.ANGLE: [LlmResponse("end_turn", "angles", [], Usage(1, 1))],
            Role.PLANNER: [LlmResponse("end_turn", "plan", [], Usage(1, 1))],
        }
    )
    assert llm.chat(Role.PLANNER, "s", [], None).text == "plan"
    assert llm.chat(Role.ANGLE, "s", [], None).text == "angles"


def test_fake_forbids_both_script_and_by_role():
    """同时指定 script 和 by_role 是错误的。"""
    with pytest.raises(ValueError, match="不支持同时指定"):
        FakeLlm(
            script=[LlmResponse("end_turn", "x", [], Usage(1, 1))],
            by_role={Role.PLANNER: [LlmResponse("end_turn", "y", [], Usage(1, 1))]},
        )


def test_fake_distinguishes_no_script_vs_exhausted():
    """角色未脚本化 vs 脚本已用尽的错误信息应该不同。"""
    # 按角色脚本但某个角色缺失：应该说"未脚本化"
    llm = FakeLlm(
        by_role={Role.PLANNER: [LlmResponse("end_turn", "p", [], Usage(1, 1))]}
    )
    llm.chat(Role.PLANNER, "sys", [], None)
    with pytest.raises(AssertionError, match="该角色未脚本化"):
        llm.chat(Role.CRITIC, "sys", [], None)

    # 全局脚本用尽：应该说"已用尽"
    llm2 = FakeLlm([LlmResponse("end_turn", "x", [], Usage(1, 1))])
    llm2.chat(Role.PLANNER, "sys", [], None)
    with pytest.raises(AssertionError, match="脚本已用尽"):
        llm2.chat(Role.PLANNER, "sys", [], None)


def test_tool_use_response_carries_calls():
    resp = LlmResponse(
        "tool_use",
        "",
        [ToolCall("t1", "search_poi", {"query": "清水寺"})],
        Usage(20, 3),
    )
    assert resp.tool_calls[0].name == "search_poi"
    assert resp.tool_calls[0].args["query"] == "清水寺"


def test_usage_addition_accumulates():
    assert Usage(1, 2) + Usage(10, 20) == Usage(11, 22)


def test_anthropic_client_with_no_text_blocks():
    """响应只有 tool_use 块（没有文本）时，text 为空字符串。"""
    configs = DEFAULT_ROLES

    # 创建 mock 客户端和响应
    mock_text_block = MagicMock()
    mock_text_block.type = "text"
    mock_text_block.text = ""

    mock_tool_block = MagicMock()
    mock_tool_block.type = "tool_use"
    mock_tool_block.id = "tool-1"
    mock_tool_block.name = "search"
    mock_tool_block.input = {"q": "京都"}

    mock_response = MagicMock()
    mock_response.content = [mock_tool_block]
    mock_response.stop_reason = "tool_use"
    mock_response.usage.input_tokens = 100
    mock_response.usage.output_tokens = 50

    with patch("anthropic.Anthropic") as MockAnthropic:
        mock_client_instance = MagicMock()
        MockAnthropic.return_value = mock_client_instance
        mock_client_instance.messages.create.return_value = mock_response

        client = AnthropicClient(configs)
        response = client.chat(Role.PLANNER, "system", [], None)

        assert response.text == ""
        assert len(response.tool_calls) == 1
        assert response.tool_calls[0].name == "search"


def test_anthropic_client_with_interleaved_text_and_tools():
    """响应有交错的文本和 tool_use 块时，文本连接，工具调用列出。"""
    configs = DEFAULT_ROLES

    # 模拟多个块
    mock_text_1 = MagicMock()
    mock_text_1.type = "text"
    mock_text_1.text = "Let me "

    mock_tool_1 = MagicMock()
    mock_tool_1.type = "tool_use"
    mock_tool_1.id = "t1"
    mock_tool_1.name = "search"
    mock_tool_1.input = {"q": "清水寺"}

    mock_text_2 = MagicMock()
    mock_text_2.type = "text"
    mock_text_2.text = "search that."

    mock_tool_2 = MagicMock()
    mock_tool_2.type = "tool_use"
    mock_tool_2.id = "t2"
    mock_tool_2.name = "analyze"
    mock_tool_2.input = {"data": "result"}

    mock_response = MagicMock()
    mock_response.content = [mock_text_1, mock_tool_1, mock_text_2, mock_tool_2]
    mock_response.stop_reason = "end_turn"
    mock_response.usage.input_tokens = 150
    mock_response.usage.output_tokens = 75

    with patch("anthropic.Anthropic") as MockAnthropic:
        mock_client_instance = MagicMock()
        MockAnthropic.return_value = mock_client_instance
        mock_client_instance.messages.create.return_value = mock_response

        client = AnthropicClient(configs)
        response = client.chat(Role.CRITIC, "system", [], None)

        assert response.text == "Let me search that."
        assert len(response.tool_calls) == 2
        assert response.tool_calls[0].name == "search"
        assert response.tool_calls[1].name == "analyze"
        assert response.stop_reason == "end_turn"


def test_anthropic_client_omits_tools_when_empty():
    """当 tools=None 时，不在 kwargs 中包含 tools。"""
    configs = DEFAULT_ROLES

    mock_text = MagicMock()
    mock_text.type = "text"
    mock_text.text = "Done"

    mock_response = MagicMock()
    mock_response.content = [mock_text]
    mock_response.stop_reason = "end_turn"
    mock_response.usage.input_tokens = 50
    mock_response.usage.output_tokens = 10

    with patch("anthropic.Anthropic") as MockAnthropic:
        mock_client_instance = MagicMock()
        MockAnthropic.return_value = mock_client_instance
        mock_client_instance.messages.create.return_value = mock_response

        client = AnthropicClient(configs)
        response = client.chat(Role.PLANNER, "sys", [], None)

        # 检查 create 被调用时 tools 没有被传入
        call_kwargs = mock_client_instance.messages.create.call_args[1]
        assert "tools" not in call_kwargs
        assert response.text == "Done"
