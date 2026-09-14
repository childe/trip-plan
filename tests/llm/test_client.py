import pytest

from tripplan.llm.client import FakeLlm, LlmResponse, ToolCall, Usage
from tripplan.llm.config import Role


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


def test_fake_no_script_for_role_in_by_role_mode():
    """按角色模式中，某个角色完全没有脚本条目时，应该说"未脚本化"。"""
    llm = FakeLlm(
        by_role={Role.PLANNER: [LlmResponse("end_turn", "p", [], Usage(1, 1))]}
    )
    # CRITIC 不在 by_role 中
    with pytest.raises(AssertionError, match="该角色未脚本化"):
        llm.chat(Role.CRITIC, "sys", [], None)


def test_fake_script_exhausted_for_role_in_by_role_mode():
    """按角色模式中，某个角色的脚本用尽时，应该说"已用尽"。"""
    llm = FakeLlm(
        by_role={Role.PLANNER: [LlmResponse("end_turn", "p", [], Usage(1, 1))]}
    )
    llm.chat(Role.PLANNER, "sys", [], None)
    # PLANNER 在 by_role 中但已用尽
    with pytest.raises(AssertionError, match="脚本已用尽"):
        llm.chat(Role.PLANNER, "sys", [], None)


def test_fake_script_exhausted_in_global_mode():
    """全局脚本模式中，脚本用尽时，应该说"已用尽"。"""
    llm = FakeLlm([LlmResponse("end_turn", "x", [], Usage(1, 1))])
    llm.chat(Role.PLANNER, "sys", [], None)
    with pytest.raises(AssertionError, match="脚本已用尽"):
        llm.chat(Role.PLANNER, "sys", [], None)


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
