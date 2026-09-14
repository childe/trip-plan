"""OpenAI 适配器。patch("openai.OpenAI")——backend 必须写成
`import openai` + `openai.OpenAI(...)`，用 `from openai import OpenAI`
会让这个 patch 失效。"""

import inspect
from unittest.mock import MagicMock, patch

import httpx
import openai
import pytest

from tripplan.llm.backends.openai import OpenAIBackend
from tripplan.llm.config import ModelSpec, Role
from tripplan.llm.errors import MissingCredential
from tripplan.providers.base import ProviderError


def _spec(**over):
    base = dict(
        provider="openai",
        name="gpt-5",
        base_url="https://gw.example.com/v1",
        key="sk-test",
        name_source="gpt-5",
        key_source="${OPENAI_API_KEY}",
    )
    base.update(over)
    return ModelSpec(**base)


def _resp(
    *,
    content="{}",
    tool_calls=None,
    finish_reason="stop",
    refusal=None,
    usage=(10, 5),
    choices=1,
):
    r = MagicMock()
    if choices == 0:
        r.choices = []
        return r
    msg = MagicMock()
    msg.content = content
    msg.refusal = refusal
    msg.tool_calls = tool_calls or []
    choice = MagicMock()
    choice.message, choice.finish_reason = msg, finish_reason
    r.choices = [choice]
    if usage is None:
        r.usage = None
    else:
        r.usage.prompt_tokens, r.usage.completion_tokens = usage
    return r


def _call(name, arguments, cid="c1"):
    c = MagicMock()
    c.id = cid
    c.type = "function"
    c.function.name, c.function.arguments = name, arguments
    return c


def _chat(backend, **over):
    kw = dict(
        role=Role.PLANNER,
        model_ref="gpt5",
        system="sys",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        max_tokens=4000,
    )
    kw.update(over)
    return backend.chat(**kw)


@pytest.fixture
def backend():
    """产出 (backend实例, 底层 mock 客户端) 二元组，而不是把 mock 挂到
    backend 实例上——生产的 OpenAIBackend 对象不该带着测试专用的 `_mock`
    属性,那会让被测对象的形状偏离真实运行时。"""
    with patch("openai.OpenAI") as MockOpenAI:
        MockOpenAI.return_value.api_key = "sk-test"
        b = OpenAIBackend(_spec())
        yield b, MockOpenAI.return_value


# ---------- 凭据 ----------


@pytest.mark.parametrize(
    "key_source, expected_var",
    [
        ("${OPENAI_API_KEY}", "OPENAI_API_KEY"),
        ("${COMPANY_GW_TOKEN}", "COMPANY_GW_TOKEN"),
    ],
)
def test_construction_failure_becomes_missing_credential(
    monkeypatch, key_source, expected_var
):
    """判据就是"构造成不成功"——不做任何环境变量枚举。

    打真实的 openai.OpenAI（构造不发请求）：环境里没有任何凭据时它会抛
    OpenAIError，我们把它转成带 §6.2 消息契约的 MissingCredential。

    断言 f"export {expected_var}" 而不是光秃秃的变量名：SDK 的英文原文本身
    就含 "OPENAI_API_KEY"（"...set the `OPENAI_API_KEY` or `OPENAI_ADMIN_KEY`
    environment variable"），单独断言变量名字符串在场，在 key_source 指向
    别的变量名时也会被 SDK 原文"顺便"满足，测不出 _var_hint 有没有真的被用
    进消息。第二组参数（COMPANY_GW_TOKEN，SDK 原文里根本不会出现的名字）
    钉住这一点。
    """
    for var in ("OPENAI_API_KEY", "OPENAI_ADMIN_KEY"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(MissingCredential) as exc:
        OpenAIBackend(
            _spec(key="", key_source=key_source), role=Role.CRITIC, model_ref="gpt5"
        )
    message = str(exc.value)
    assert "critic" in message
    assert "gpt5" in message
    assert f"export {expected_var}" in message
    assert "--dry-run" in message


def test_admin_key_alone_is_accepted(monkeypatch):
    """不许枚举环境变量：OPENAI_ADMIN_KEY 单独设置时 SDK 构造得起来
    （但 client.api_key == ''），凭据完全正常的用户不能被判成缺凭据。

    这是规则二在 openai 侧的形态——枚举 OPENAI_API_KEY 的实现会在这里红。
    """
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_ADMIN_KEY", "sk-admin-env")
    OpenAIBackend(_spec(key=""))  # 不抛


def test_empty_key_is_handed_to_sdk_as_none():
    """规则一。空串不会静默带病上路（openai 3.13 对 api_key="" 当场抛），
    但它会让 SDK **拒绝去读环境变量**——于是 ${OPENAI_API_KEY:-} 展开成
    空串时，配了该变量的用户反而起不来。

    这里 patch 了 openai.OpenAI，SDK 根本不会真的去读环境变量，所以不设
    OPENAI_API_KEY——断言的是我们传给 SDK 的 api_key 参数本身，与 env 无关。
    """
    with patch("openai.OpenAI") as MockOpenAI:
        MockOpenAI.return_value.api_key = "sk-env"
        OpenAIBackend(_spec(key=""))
        assert MockOpenAI.call_args.kwargs["api_key"] is None


def test_non_empty_base_url_is_passed_through():
    """整个多 provider 特性的存在意义就是能指向内网网关；base_url 被静默
    丢弃是无声的产品故障。

    同上：patch 了 openai.OpenAI，不需要真实环境变量。
    """
    with patch("openai.OpenAI") as MockOpenAI:
        MockOpenAI.return_value.api_key = "sk-env"
        OpenAIBackend(_spec(base_url="https://gw.internal/v1"))
        assert MockOpenAI.call_args.kwargs["base_url"] == "https://gw.internal/v1"


# ---------- 请求形状 ----------


def test_tools_field_omitted_when_none(backend):
    b, mock = backend
    mock.chat.completions.create.return_value = _resp()
    _chat(b, tools=None)
    kwargs = mock.chat.completions.create.call_args.kwargs
    assert "tools" not in kwargs  # 不是 "tools": None


def test_tools_are_translated_to_function_shape(backend):
    b, mock = backend
    mock.chat.completions.create.return_value = _resp()
    _chat(
        b,
        tools=[{"name": "search_poi", "description": "d", "input_schema": {"a": 1}}],
    )
    tools = mock.chat.completions.create.call_args.kwargs["tools"]
    assert tools == [
        {
            "type": "function",
            "function": {
                "name": "search_poi",
                "description": "d",
                "parameters": {"a": 1},
            },
        }
    ]


def test_system_is_prepended_without_mutating_caller_list(backend):
    """run_agent 每轮复用同一个 messages list。原地 insert(0, ...) 会让
    system 消息逐轮累积。"""
    b, mock = backend
    mock.chat.completions.create.return_value = _resp()
    caller_messages = [{"role": "user", "content": "hi"}]
    _chat(b, messages=caller_messages)
    _chat(b, messages=caller_messages)
    assert caller_messages == [{"role": "user", "content": "hi"}]
    sent = mock.chat.completions.create.call_args.kwargs["messages"]
    assert sent[0] == {"role": "system", "content": "sys"}


def test_uses_max_completion_tokens(backend):
    b, mock = backend
    mock.chat.completions.create.return_value = _resp()
    _chat(b, max_tokens=4000)
    kwargs = mock.chat.completions.create.call_args.kwargs
    assert kwargs["max_completion_tokens"] == 4000
    assert "max_tokens" not in kwargs
    inspect.signature(openai.resources.chat.completions.Completions.create).bind(
        None, **kwargs
    )


# ---------- 响应归一化 ----------


def test_tool_calls_decide_the_round_not_finish_reason(backend):
    """多家网关会在带 tool_calls 的响应上给出 finish_reason="stop"/"length"。
    按 finish_reason 判会让这类响应被当成普通文本轮，tool_calls 非空却无人
    执行，模型空转到 max_tool_calls 或 deadline。"""
    b, mock = backend
    mock.chat.completions.create.return_value = _resp(
        content=None,
        tool_calls=[_call("search_poi", '{"query": "芜湖"}')],
        finish_reason="stop",
    )
    out = _chat(b)
    assert out.stop_reason == "tool_use"
    assert out.tool_calls[0].args == {"query": "芜湖"}


def test_none_content_becomes_empty_string(backend):
    """LlmResponse.text 的类型是 str。不归一则 run_agent 里修复轮那句
    `resp.text.strip() or "(空回复)"` 会把 None 塞进 messages，随后
    `_parse_and_validate(resp.text, ...)` → `_load_json` → `text.strip()`
    抛 AttributeError——既不是 ProviderError 也不是 LimitExceeded，只会被
    orchestrator 吞成「候选线出现未处理异常」。"""
    b, mock = backend
    mock.chat.completions.create.return_value = _resp(content=None)
    assert _chat(b).text == ""


def test_length_becomes_max_tokens_and_does_not_raise(backend):
    """归一成 max_tokens 让 runner 的修复轮照常工作。抛 ProviderError 会让
    一次普通的输出截断在 OpenAI 侧杀死整条候选线，而 Anthropic 侧只是进
    修复轮——那正是本设计承诺要消灭的 provider 分歧。"""
    b, mock = backend
    mock.chat.completions.create.return_value = _resp(
        content="", finish_reason="length"
    )
    assert _chat(b).stop_reason == "max_tokens"


@pytest.mark.parametrize(
    "kwargs, needle",
    [
        ({"choices": 0}, "空的 choices"),
        ({"finish_reason": "content_filter"}, "内容过滤"),
        ({"refusal": "我不能帮你做这个"}, "拒绝"),
    ],
)
def test_response_holes_become_provider_error(backend, kwargs, needle):
    b, mock = backend
    mock.chat.completions.create.return_value = _resp(**kwargs)
    with pytest.raises(ProviderError) as exc:
        _chat(b)
    assert needle in str(exc.value)


def test_missing_usage_becomes_zero(backend):
    """计量失真好过整条候选线以无用诊断挂掉。"""
    b, mock = backend
    mock.chat.completions.create.return_value = _resp(usage=None)
    out = _chat(b)
    assert (out.usage.input_tokens, out.usage.output_tokens) == (0, 0)


def test_custom_tool_call_becomes_provider_error(backend):
    """`ChatCompletionMessageCustomToolCall`（type="custom"）没有 `.function`
    字段。本项目从未注册过 custom tool，但网关/SDK 版本升级仍可能把它塞进
    响应；裸读 `c.function` 会抛 AttributeError，不是 ProviderError，会绕过
    run_slot 直接落到 orchestrator._safe_slot 把已生成的行程丢弃。"""
    b, mock = backend
    custom_call = MagicMock(spec=["id", "type", "custom"])
    custom_call.id = "c1"
    custom_call.type = "custom"
    custom_call.custom.name, custom_call.custom.input = "some_tool", "{}"
    mock.chat.completions.create.return_value = _resp(
        content=None, tool_calls=[custom_call]
    )
    with pytest.raises(ProviderError) as exc:
        _chat(b)
    assert "custom" in str(exc.value)


def test_empty_arguments_string_becomes_empty_dict(backend):
    """部分网关对无参调用返回 "" 而非 "{}"。"""
    b, mock = backend
    mock.chat.completions.create.return_value = _resp(
        content=None, tool_calls=[_call("t", "")]
    )
    assert _chat(b).tool_calls[0].args == {}


def test_broken_arguments_go_through_the_tool_error_channel(backend):
    """模型写坏 JSON 是可自愈的模型失误，不是外部依赖挂了。

    用 ProviderError 的真实代价：slot.py:87-89 把 itin/facts 原样交给一个
    FAILED slot，而 candidates.py:15 只在 itinerary is None 时报警、:23 只在
    EXHAUSTED 时显示 detail——产出的是一个看起来完整、可被用户选中、失败
    原因被静默隐藏的候选。比"清零"更糟。

    改走 args={}：impl(**{}) 会因缺必填参数抛 TypeError，被 run_agent 里
    工具调用那段 except Exception 捕获并回喂给模型自己改正。
    """
    b, mock = backend
    mock.chat.completions.create.return_value = _resp(
        content=None, tool_calls=[_call("search_poi", '{"query": "京')]
    )
    out = _chat(b)
    assert out.stop_reason == "tool_use"
    assert out.tool_calls[0].args == {}
    assert "[适配器]" in out.text


def test_adapter_diagnostic_is_appended_not_replacing_model_text(backend):
    b, mock = backend
    mock.chat.completions.create.return_value = _resp(
        content="我先查一下", tool_calls=[_call("search_poi", "{bad")]
    )
    out = _chat(b)
    assert out.text.startswith("我先查一下")
    assert "[适配器]" in out.text


# ---------- 请求期错误映射（铁律） ----------


def test_api_error_becomes_provider_error(backend):
    """`str(openai.APIConnectionError(...))` 恰好总是 'Connection error.'——
    不改消息就丢光全部上下文，网关连不上时用户看不出是哪个角色、哪个
    model、哪个 provider。断言角色名与 model 名都必须出现在最终消息里。"""
    b, mock = backend
    mock.chat.completions.create.side_effect = openai.APIConnectionError(
        request=httpx.Request("POST", "https://gw.example.com")
    )
    with pytest.raises(ProviderError) as exc:
        _chat(b)
    message = str(exc.value)
    assert "planner" in message
    assert "gpt5" in message
    assert "openai" in message
    assert "Connection error" in message  # SDK 原文仍要保留


def test_non_api_error_vendor_exception_also_becomes_provider_error(backend):
    """铁律"漏捕"一侧。写成 `except APIError` 的实现会让这个异常绕过
    ProviderError，落到 _safe_slot 把已生成的行程丢掉。"""
    b, mock = backend
    mock.chat.completions.create.side_effect = openai.OpenAIError("凭据刷新失败")
    with pytest.raises(ProviderError):
        _chat(b)


def test_authentication_error_keeps_provider_error_type(backend):
    b, mock = backend
    request = httpx.Request("POST", "https://gw.example.com")
    mock.chat.completions.create.side_effect = openai.AuthenticationError(
        "unauthorized", response=httpx.Response(401, request=request), body=None
    )
    with pytest.raises(ProviderError) as exc:
        _chat(b, role=Role.CRITIC, model_ref="gpt5")
    message = str(exc.value)
    assert "401" in message
    assert "critic" in message
    assert "OPENAI_API_KEY" in message
