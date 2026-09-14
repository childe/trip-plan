from unittest.mock import patch

import pytest

from tripplan.llm.client import LlmResponse, Usage
from tripplan.llm.config import LlmConfig, ModelSpec, Role, RoleConfig
from tripplan.llm.router import RoutingClient


def _spec(provider="anthropic", name="claude-opus-5", base_url="", key="k"):
    return ModelSpec(provider, name, base_url, key, name, "${X}")


class _Recorder:
    """假 backend——记录构造与调用，不碰任何 SDK。"""

    made: list = []

    def __init__(self, spec, role=None, model_ref=None):
        self.spec, self.calls = spec, []
        _Recorder.made.append((spec.provider, spec.base_url, spec.key))

    def chat(self, role, model_ref, system, messages, tools, max_tokens):
        self.calls.append((role, model_ref, max_tokens))
        return LlmResponse("end_turn", "{}", [], Usage(1, 1))


@pytest.fixture(autouse=True)
def _reset():
    _Recorder.made = []


def _config(override=None):
    models = {
        "opus": _spec(),
        "sonnet": _spec(name="claude-sonnet-5"),
        "gpt5": _spec(provider="openai", name="gpt-5", base_url="https://gw/v1"),
    }
    roles = {
        Role.PLANNER: RoleConfig("opus", 16000),
        Role.CRITIC: RoleConfig("gpt5", 4000),
        Role.ANGLE: RoleConfig("sonnet", 2000),
        Role.CLASSIFIER: RoleConfig("sonnet", 1000),
    }
    if override:
        roles.update(override)
    return LlmConfig(models=models, roles=roles)


def _client(cfg):
    return RoutingClient(cfg, factories={"anthropic": _Recorder, "openai": _Recorder})


def test_chat_dispatches_to_the_roles_model():
    cfg = _config(override={Role.CRITIC: RoleConfig("gpt5", 4000)})
    client = _client(cfg)
    client.chat(Role.CRITIC, "sys", [], None)
    backend = client.backend_for(Role.CRITIC)
    assert backend.spec.provider == "openai"
    assert backend.calls[0][:2] == (Role.CRITIC, "gpt5")


def test_max_tokens_comes_from_the_role_not_the_model():
    """同一个 model 被 angle 与 classifier 复用，但预算不同。"""
    client = _client(_config())
    client.chat(Role.ANGLE, "sys", [], None)
    client.chat(Role.CLASSIFIER, "sys", [], None)
    backend = client.backend_for(Role.ANGLE)
    assert {c[2] for c in backend.calls} == {2000, 1000}


def test_backends_are_cached_by_provider_base_url_key():
    """§7 的默认配置里三个 model 同 provider、同端点、同 key——按 model 名
    缓存会开三个客户端、三份从不关闭的 httpx 连接池。name 不进缓存键：
    它是每次请求的参数，不是客户端的属性。"""
    _client(_config())
    assert len(_Recorder.made) == 2  # anthropic 那一份 + openai 那一份


def test_all_referenced_backends_are_built_at_load_time():
    """按需构造时 MissingCredential 会被 orchestrator.py:254 的
    except Exception 吞成「候选线出现未处理异常」，cli.py:380 永远等不到。"""
    boom = []

    class _Boom(_Recorder):
        def __init__(self, spec, role=None, model_ref=None):
            boom.append(role)
            raise RuntimeError("构造失败")

    with pytest.raises(RuntimeError):
        RoutingClient(_config(), factories={"anthropic": _Boom, "openai": _Boom})
    assert boom  # 构造发生在 __init__，不是第一次 chat


def test_chat_signature_is_positional_compatible_with_protocol():
    """runner.py:130 按位置调用 client.chat(role, system, messages, tools)。"""
    client = _client(_config())
    out = client.chat(Role.PLANNER, "sys", [], None)
    assert isinstance(out, LlmResponse)
