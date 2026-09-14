"""anthropic.py 与 openai.py 共用的 `llm/backends/_shared.py`。

`_var_hint`（现 `_shared.var_hint`）之前在两个 backend 里逐字复制，直接
关系到安全：判断用户是否写了字面量 key，写错了会把明文密钥原样吐进错误
消息。`test_literal_key_is_never_echoed_in_message` 曾经只存在于
tests/llm/test_anthropic_backend.py——openai 那份逐字复制的实现完全没有
对应的测试覆盖，任何一侧的偏移都不会被发现。

现在两个 backend 共用同一份 `_shared` 实现，这里用同一条参数化测试同时
压两侧：任何一侧走偏（甚至将来有人把 anthropic.py / openai.py 改回各自
维护一份）都会被这一个测试文件抓到。
"""

import pytest

from tripplan.llm.backends.anthropic import AnthropicBackend
from tripplan.llm.backends.openai import OpenAIBackend
from tripplan.llm.config import ModelSpec, Role
from tripplan.llm.errors import MissingCredential


def _anthropic_spec(key_source: str) -> ModelSpec:
    return ModelSpec(
        provider="anthropic",
        name="claude-opus-5",
        base_url="",
        key="",
        name_source="claude-opus-5",
        key_source=key_source,
    )


def _openai_spec(key_source: str) -> ModelSpec:
    return ModelSpec(
        provider="openai",
        name="gpt-5",
        base_url="https://gw.example.com/v1",
        key="",
        name_source="gpt-5",
        key_source=key_source,
    )


@pytest.fixture(autouse=True)
def _sealed(monkeypatch, tmp_path):
    """两个 backend 都要在没有真实凭据的环境里构造，否则结果随开发机而变。

    HOME 指向空目录是 anthropic 侧的要求（见 test_anthropic_backend.py 里
    同名 fixture 的注释）；openai 侧不读 profile 文件，只需要 delenv。
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
        "OPENAI_API_KEY",
        "OPENAI_ADMIN_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


@pytest.mark.parametrize(
    "backend_cls, make_spec, env_hint",
    [
        (AnthropicBackend, _anthropic_spec, "ANTHROPIC_API_KEY"),
        (OpenAIBackend, _openai_spec, "OPENAI_API_KEY"),
    ],
)
def test_literal_key_is_never_echoed_in_message(backend_cls, make_spec, env_hint):
    """key_source 在用户写字面量时就是明文密钥本身。缺凭据消息里只能出现
    该 provider 固定映射的变量名，不能把字面量原样吐出来。"""
    with pytest.raises(MissingCredential) as exc:
        backend_cls(
            make_spec(key_source="sk-literal-SUPERSECRET"),
            role=Role.PLANNER,
            model_ref="m",
        )
    message = str(exc.value)
    assert "SUPERSECRET" not in message
    assert env_hint in message


@pytest.mark.parametrize(
    "backend_cls, make_spec, env_hint",
    [
        (AnthropicBackend, _anthropic_spec, "ANTHROPIC_API_KEY"),
        (OpenAIBackend, _openai_spec, "OPENAI_API_KEY"),
    ],
)
def test_var_form_key_source_is_reported(backend_cls, make_spec, env_hint):
    """整段文本恰好是一个 ${VAR} 引用时，报的是用户自己写的变量名，
    不是该 provider 的固定映射——这是与上一条测试对称的另一半：
    var_hint 的分支判断必须两边都对，只测一边测不出另一边的回归。

    断言 `export {env_hint}` 不在消息里，而不是断言 `env_hint` 整体不在：
    openai 侧 MissingCredential 消息末尾附了 SDK 原文，而 SDK 原文本身就
    含 "OPENAI_API_KEY" 字样（与 provider 固定映射恰好同名），裸判
    `env_hint not in message` 在 openai 侧会假红。"""
    with pytest.raises(MissingCredential) as exc:
        backend_cls(
            make_spec(key_source="${MY_OWN_VAR:-}"),
            role=Role.PLANNER,
            model_ref="m",
        )
    message = str(exc.value)
    assert "MY_OWN_VAR" in message
    assert f"export {env_hint}" not in message
