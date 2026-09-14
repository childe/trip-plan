"""Anthropic 适配器。全程不触网——构造不发请求，chat 用 patch 挡住。"""

import inspect
from unittest.mock import MagicMock, patch

import anthropic
import httpx
import pytest
from anthropic.resources.messages import Messages

from tripplan.llm.backends.anthropic import AnthropicBackend
from tripplan.llm.config import ModelSpec, Role
from tripplan.llm.errors import ConfigError, MissingCredential
from tripplan.providers.base import ProviderError


def _spec(**over):
    base = dict(
        provider="anthropic",
        name="claude-opus-5",
        base_url="",
        key="sk-test",
        name_source="claude-opus-5",
        key_source="${ANTHROPIC_API_KEY}",
    )
    base.update(over)
    return ModelSpec(**base)


@pytest.fixture(autouse=True)
def _sealed(monkeypatch, tmp_path):
    """凭据解析链必须被密封，否则测试结果随开发机而变。

    HOME 要指向空目录：SDK 的 _config_dir() 在 ANTHROPIC_CONFIG_DIR 未设时
    回落到 ~/.config/anthropic/。而 ANTHROPIC_CONFIG_DIR **不能** setenv 到
    空目录——那会把 profile 解析升级为「显式选择」，构造期直接抛
    CredentialsError（实测，_chain.py:119-129 的注释写明了这个语义）。
    """
    for var in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_PROFILE",
        "ANTHROPIC_CONFIG_DIR",
        "ANTHROPIC_IDENTITY_TOKEN",
        "ANTHROPIC_IDENTITY_TOKEN_FILE",
        "ANTHROPIC_FEDERATION_RULE_ID",
        "ANTHROPIC_ORGANIZATION_ID",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def _resp(*, text="{}", tool_uses=(), stop_reason="end_turn"):
    blocks = []
    if text:
        b = MagicMock()
        b.type = "text"
        b.text = text
        blocks.append(b)
    for tid, tname, tinput in tool_uses:
        b = MagicMock()
        b.type = "tool_use"
        b.id, b.name, b.input = tid, tname, tinput
        blocks.append(b)
    r = MagicMock()
    r.content = blocks
    r.stop_reason = stop_reason
    r.usage.input_tokens, r.usage.output_tokens = 10, 5
    return r


# ---------- 凭据（§6） ----------


def test_empty_key_is_handed_to_sdk_as_none():
    """规则一：空串必须归一成 None。anthropic SDK 用 `api_key is not None`
    判断"是否给了显式凭据"，`"" is not None` 为真，于是空串会被当成显式
    凭据、整条环境解析链（API_KEY → AUTH_TOKEN → profile → WIF）被跳过。"""
    with patch("anthropic.Anthropic") as MockAnthropic:
        MockAnthropic.return_value.api_key = "resolved-by-sdk"
        AnthropicBackend(_spec(key=""))
        assert MockAnthropic.call_args.kwargs["api_key"] is None


def test_empty_base_url_is_handed_to_sdk_as_none():
    with patch("anthropic.Anthropic") as MockAnthropic:
        MockAnthropic.return_value.api_key = "k"
        AnthropicBackend(_spec(base_url=""))
        assert MockAnthropic.call_args.kwargs["base_url"] is None


def test_non_empty_base_url_is_passed_through_unchanged():
    """base_url 是整个多 provider 特性存在的意义之一——指向内网网关。
    如果被静默丢弃（例如实现写成无条件 base_url=None），用户会在毫无
    提示的情况下打到公网端点，这是严重但无声的产品故障。"""
    with patch("anthropic.Anthropic") as MockAnthropic:
        MockAnthropic.return_value.api_key = "k"
        AnthropicBackend(_spec(base_url="https://gw.internal"))
        assert MockAnthropic.call_args.kwargs["base_url"] == "https://gw.internal"


def test_no_credential_anywhere_raises_missing_credential():
    """规则二：判据是 SDK 是否解析出了任何一种凭据，不是 auth_headers。
    这里打真实的 anthropic.Anthropic（构造不发请求），因为 MagicMock 的
    api_key 恒为 truthy，判据永远为假——那样断言的是 mock 不是 SDK。"""
    with pytest.raises(MissingCredential) as exc:
        AnthropicBackend(_spec(key=""), role=Role.PLANNER, model_ref="opus")
    message = str(exc.value)
    assert "planner" in message
    assert "opus" in message
    assert "ANTHROPIC_API_KEY" in message
    assert "--dry-run" in message


def test_auth_token_alone_is_accepted(monkeypatch):
    """ANTHROPIC_API_KEY 不是唯一凭据来源。只配 AUTH_TOKEN 的用户必须能跑。"""
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "bearer-abc")
    AnthropicBackend(_spec(key=""))  # 不抛


def test_credentials_alone_is_accepted():
    """OAuth profile / WIF 场景：SDK 把凭据放进 credentials 属性，
    api_key 与 auth_token 都是 None。这条必须放行，不能被判成缺凭据——
    这正是规则二要防的假阳性方向（曾用 getattr(..., None) 兜底，一旦
    SDK 重命名该属性就会静默把这类用户判成缺凭据）。"""
    with patch("anthropic.Anthropic") as MockAnthropic:
        inst = MockAnthropic.return_value
        inst.api_key = None
        inst.auth_token = None
        inst.credentials = object()  # 任意非 None 的 provider 对象
        AnthropicBackend(_spec(key=""))  # 不抛


def test_credentials_none_alongside_empty_api_key_and_auth_token_raises():
    """三者都为空（或 None）才是真正的缺凭据。"""
    with patch("anthropic.Anthropic") as MockAnthropic:
        inst = MockAnthropic.return_value
        inst.api_key = None
        inst.auth_token = None
        inst.credentials = None
        with pytest.raises(MissingCredential):
            AnthropicBackend(_spec(key=""), role=Role.PLANNER, model_ref="opus")


def test_literal_key_is_never_echoed_in_message():
    """key_source 在用户写字面量时就是明文密钥本身。错误消息里只能出现
    provider 的固定映射变量名，不能把它原样吐出来。"""
    with pytest.raises(MissingCredential) as exc:
        AnthropicBackend(
            _spec(key="", key_source="sk-ant-SUPERSECRET"),
            role=Role.PLANNER,
            model_ref="opus",
        )
    assert "SUPERSECRET" not in str(exc.value)
    assert "ANTHROPIC_API_KEY" in str(exc.value)


def test_var_form_key_source_is_reported(monkeypatch):
    with pytest.raises(MissingCredential) as exc:
        AnthropicBackend(
            _spec(key="", key_source="${MY_OWN_VAR:-}"),
            role=Role.PLANNER,
            model_ref="opus",
        )
    assert "MY_OWN_VAR" in str(exc.value)


def test_construction_failure_becomes_config_error(monkeypatch, tmp_path):
    """坏 profile 会让构造期抛 CredentialsError（不是 APIError 子类）。
    它必须收成 ConfigError，并套用 §6.2 的消息契约——不能透传 SDK 英文原文。"""
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(tmp_path / "nope"))
    with pytest.raises(ConfigError) as exc:
        AnthropicBackend(_spec(key=""), role=Role.PLANNER, model_ref="opus")
    message = str(exc.value)
    assert "planner" in message
    assert "opus" in message
    assert "--dry-run" in message


def test_config_error_never_echoes_the_resolved_key():
    """安全契约：spec.key（解析后的明文密钥，不是 key_source）不能出现在
    ConfigError 里。既有的泄漏测试只覆盖了「key_source × MissingCredential」
    这一种组合；ConfigError 是构造期的另一条出路，同样要守住。"""
    with patch("anthropic.Anthropic") as MockAnthropic:
        MockAnthropic.side_effect = anthropic.AnthropicError("boom")
        with pytest.raises(ConfigError) as exc:
            AnthropicBackend(_spec(key="sk-ant-REALSECRET999"))
    assert "REALSECRET999" not in str(exc.value)


def test_provider_error_never_echoes_the_resolved_key():
    """同上，覆盖请求期的 ProviderError 这条出路。"""
    with patch("anthropic.Anthropic") as MockAnthropic:
        inst = MockAnthropic.return_value
        inst.api_key = "k"
        inst.messages.create.side_effect = anthropic.APIConnectionError(
            request=httpx.Request("POST", "https://api.anthropic.com")
        )
        with pytest.raises(ProviderError) as exc:
            AnthropicBackend(_spec(key="sk-ant-REALSECRET999")).chat(
                role=Role.PLANNER,
                model_ref="opus",
                system="s",
                messages=[],
                tools=None,
                max_tokens=100,
            )
    assert "REALSECRET999" not in str(exc.value)


# ---------- 请求形状 ----------


def test_tools_omitted_entirely_when_none():
    """四个角色里三个传 tools=None。不能发 "tools": null。"""
    with patch("anthropic.Anthropic") as MockAnthropic:
        inst = MockAnthropic.return_value
        inst.api_key = "k"
        inst.messages.create.return_value = _resp()
        AnthropicBackend(_spec()).chat(
            role=Role.CRITIC,
            model_ref="sonnet",
            system="sys",
            messages=[],
            tools=None,
            max_tokens=4000,
        )
        assert "tools" not in inst.messages.create.call_args.kwargs


def test_request_kwargs_match_the_real_sdk_signature():
    """防"关键字名拼错但 MagicMock 照样绿"——唯一的防线。"""
    with patch("anthropic.Anthropic") as MockAnthropic:
        inst = MockAnthropic.return_value
        inst.api_key = "k"
        inst.messages.create.return_value = _resp()
        AnthropicBackend(_spec()).chat(
            role=Role.PLANNER,
            model_ref="opus",
            system="sys",
            messages=[{"role": "user", "content": "hi"}],
            tools=[{"name": "t", "description": "d", "input_schema": {}}],
            max_tokens=16000,
        )
        kwargs = inst.messages.create.call_args.kwargs
        inspect.signature(Messages.create).bind(None, **kwargs)
        assert kwargs["max_tokens"] == 16000
        assert kwargs["model"] == "claude-opus-5"


# ---------- 响应归一化（§10.0） ----------


@pytest.mark.parametrize(
    "stop_reason, expected",
    [
        ("end_turn", "end_turn"),
        ("stop_sequence", "end_turn"),
        ("pause_turn", "end_turn"),  # 本项目不用服务端工具，此值不可达
        ("max_tokens", "max_tokens"),
        (None, "end_turn"),  # Message.stop_reason 的标注是 Optional
    ],
)
def test_stop_reason_normalisation(stop_reason, expected):
    with patch("anthropic.Anthropic") as MockAnthropic:
        inst = MockAnthropic.return_value
        inst.api_key = "k"
        inst.messages.create.return_value = _resp(stop_reason=stop_reason)
        out = AnthropicBackend(_spec()).chat(
            role=Role.PLANNER,
            model_ref="opus",
            system="s",
            messages=[],
            tools=None,
            max_tokens=100,
        )
        assert out.stop_reason == expected


def test_tool_use_blocks_make_it_a_tool_round_even_when_truncated():
    """有工具调用就是工具轮，stop_reason 只在没有工具调用时才决定分支。
    这是 OpenAI 那条"判工具轮看 tool_calls 非空"在 Anthropic 侧的对称情形。"""
    with patch("anthropic.Anthropic") as MockAnthropic:
        inst = MockAnthropic.return_value
        inst.api_key = "k"
        inst.messages.create.return_value = _resp(
            text="",
            tool_uses=[("id1", "search_poi", {"query": "芜湖"})],
            stop_reason="max_tokens",
        )
        out = AnthropicBackend(_spec()).chat(
            role=Role.PLANNER,
            model_ref="opus",
            system="s",
            messages=[],
            tools=None,
            max_tokens=100,
        )
        assert out.stop_reason == "tool_use"
        assert out.tool_calls[0].name == "search_poi"


@pytest.mark.parametrize(
    "stop_reason, needle",
    [("refusal", "拒绝"), ("model_context_window_exceeded", "上下文窗口")],
)
def test_hard_stop_reasons_become_provider_error(stop_reason, needle):
    """不归一的话会去解析空 text → SchemaError → 修复轮（而修复轮把消息
    再加长）→ LimitExceeded("schema 修复 2 次仍失败")，诊断与真因无关，
    且会假触发 §13 的推理预算提示。"""
    with patch("anthropic.Anthropic") as MockAnthropic:
        inst = MockAnthropic.return_value
        inst.api_key = "k"
        inst.messages.create.return_value = _resp(text="", stop_reason=stop_reason)
        with pytest.raises(ProviderError) as exc:
            AnthropicBackend(_spec()).chat(
                role=Role.PLANNER,
                model_ref="opus",
                system="s",
                messages=[],
                tools=None,
                max_tokens=100,
            )
        assert needle in str(exc.value)


# ---------- 响应形状归一化（Critical：网关可以省略字段） ----------

_FULL_MESSAGE_BODY = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": "claude-opus-5",
    "content": [{"type": "text", "text": "hi"}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 10, "output_tokens": 5},
}


def _client_over_mock_transport(body: dict):
    """真正走一次完整 HTTP 往返的 anthropic.Anthropic，响应体由我们控制。

    单纯 mock `messages.create` 的返回值测不到这条路径：MagicMock 的属性
    访问永远不是 None，测不出"SDK 用宽松解析（construct_type），网关省略
    字段时字段变成 None、而不是抛校验错误——没有 APIResponseValidationError
    可捕"这件事本身是真的。这里让真实 SDK 解析一份我们手工裁剪过的 JSON，
    证明 resp.usage / resp.content 确实会是 None，不是臆测。
    """
    import httpx2

    def handler(request):
        return httpx2.Response(200, json=body, request=request)

    transport = httpx2.MockTransport(handler)
    return anthropic.Anthropic(
        api_key="sk-test", http_client=httpx2.Client(transport=transport)
    )


def test_missing_usage_becomes_zero():
    """网关省略 usage：SDK 把 resp.usage 解成 None（实测）。裸读
    resp.usage.output_tokens 是 AttributeError——不是 ProviderError，会绕
    过 run_slot 直接落到 orchestrator._safe_slot，把已生成的行程硬编码
    丢弃。base_url 指向自建网关是本项目的一等功能，网关的响应形状不再受
    Anthropic 官方契约约束，这条路径是真实可达的。"""
    body = dict(_FULL_MESSAGE_BODY)
    del body["usage"]
    backend = AnthropicBackend(_spec())
    backend._client = _client_over_mock_transport(body)
    out = backend.chat(
        role=Role.PLANNER,
        model_ref="opus",
        system="s",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        max_tokens=100,
    )
    assert (out.usage.input_tokens, out.usage.output_tokens) == (0, 0)


def test_missing_content_becomes_empty():
    """网关省略 content：SDK 把 resp.content 解成 None（实测）。裸迭代
    None 是 TypeError——同样绕过 ProviderError，同样的后果。"""
    body = dict(_FULL_MESSAGE_BODY)
    del body["content"]
    backend = AnthropicBackend(_spec())
    backend._client = _client_over_mock_transport(body)
    out = backend.chat(
        role=Role.PLANNER,
        model_ref="opus",
        system="s",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        max_tokens=100,
    )
    assert out.tool_calls == []
    assert out.text == ""


# ---------- 请求期错误映射（铁律） ----------


def test_api_error_becomes_provider_error():
    """`str(anthropic.APIConnectionError(...))` 恰好总是 'Connection error.'——
    这个分支不改消息就丢光全部上下文，网关连不上时用户只会看到
    "外部依赖失败：Connection error."，猜不出是哪个角色、哪个 model、
    哪个 provider。断言角色名与 model 名都必须出现在最终消息里。"""
    request = httpx.Request("POST", "https://api.anthropic.com")
    with patch("anthropic.Anthropic") as MockAnthropic:
        inst = MockAnthropic.return_value
        inst.api_key = "k"
        inst.messages.create.side_effect = anthropic.APIConnectionError(request=request)
        with pytest.raises(ProviderError) as exc:
            AnthropicBackend(_spec()).chat(
                role=Role.PLANNER,
                model_ref="opus",
                system="s",
                messages=[],
                tools=None,
                max_tokens=100,
            )
        message = str(exc.value)
        assert "planner" in message
        assert "opus" in message
        assert "anthropic" in message
        assert "Connection error" in message  # SDK 原文仍要保留


def test_non_api_error_vendor_exception_also_becomes_provider_error():
    """铁律"漏捕"一侧的唯一防线。

    CredentialsError / RetryableError / IdentityTokenFileError 都不是
    APIError 子类，而它们会在**请求期**刷新令牌时抛出（AccessTokenAuth
    的 auth_flow 调 TokenCache.get_token）；_base_client.py:1296-1302 明确
    把 AnthropicError 原样穿出、不包装成 APIConnectionError。

    写成 `except APIError` 的实现会让这个异常绕过 ProviderError，落到
    orchestrator._safe_slot，而它把 itinerary 硬编码成 None——长跑 planner
    中途令牌过期，已经生成好的行程当场丢失。
    """
    with patch("anthropic.Anthropic") as MockAnthropic:
        inst = MockAnthropic.return_value
        inst.api_key = "k"
        inst.messages.create.side_effect = anthropic.CredentialsError("token 过期")
        with pytest.raises(ProviderError):
            AnthropicBackend(_spec()).chat(
                role=Role.PLANNER,
                model_ref="opus",
                system="s",
                messages=[],
                tools=None,
                max_tokens=100,
            )


def test_authentication_error_keeps_provider_error_type_but_gains_message():
    """401 改消息**不改类型**。转成 MissingCredential 会让它绕过 run_slot
    的 except（slot.py:76/86 只捕 LimitExceeded 与 ProviderError），落到
    _safe_slot 把已生成的行程丢掉，然后照旧打印"请先修改需求后重试"。"""
    request = httpx.Request("POST", "https://api.anthropic.com")
    response = httpx.Response(401, request=request)
    with patch("anthropic.Anthropic") as MockAnthropic:
        inst = MockAnthropic.return_value
        inst.api_key = "k"
        inst.messages.create.side_effect = anthropic.AuthenticationError(
            "unauthorized", response=response, body=None
        )
        with pytest.raises(ProviderError) as exc:
            AnthropicBackend(_spec()).chat(
                role=Role.CRITIC,
                model_ref="sonnet",
                system="s",
                messages=[],
                tools=None,
                max_tokens=100,
            )
        message = str(exc.value)
        assert "401" in message
        assert "critic" in message
        assert "sonnet" in message
        assert "ANTHROPIC_API_KEY" in message
