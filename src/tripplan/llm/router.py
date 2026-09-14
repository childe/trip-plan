"""按角色把请求分派到对应的 backend。

RoutingClient 实现现有的 LlmClient Protocol，所以 runner.py / steps.py /
deps.py 对 LLM 层的调用面完全不变。
"""

from tripplan.llm.client import LlmResponse
from tripplan.llm.config import LlmConfig, Role


def _default_factory(provider: str):
    """按 provider 惰性解析 backend 类——用到哪个才 import 哪个。

    不一次性 import 两个：openai 是可选依赖，只用 anthropic 的用户不该因为
    router 顺手 import 了 openai backend 模块而被牵连。
    """
    if provider == "anthropic":
        from tripplan.llm.backends.anthropic import AnthropicBackend

        return AnthropicBackend
    if provider == "openai":
        from tripplan.llm.backends.openai import OpenAIBackend

        return OpenAIBackend
    raise KeyError(provider)  # load_config 已经挡住了非法 provider


class RoutingClient:
    def __init__(self, config: LlmConfig, factories: dict | None = None) -> None:
        """加载期就把**每一个被角色引用到的** model 的 backend 构造出来。

        不按需构造：那样 critic 的 backend 要等第一次 critic 调用才构造，
        此时 planner 的 16000 token 已经花掉；更要命的是那时抛出的
        MissingCredential 会被 orchestrator.py 里 _safe_slot 的 except Exception
        吞成「候选线出现未处理异常」，cli.py 里 `except MissingCredential` 那一段
        永远等不到它。

        缓存键是 (provider, base_url, key)，不是 model 引用名——默认配置里
        三个 model 同端点同 key，按名缓存会开三份连接池。
        """
        self._config = config
        self._factories = factories or {}
        self._by_key: dict[tuple, object] = {}
        self._by_role: dict[Role, object] = {}
        # 逐 (role, model) 校验，而不是逐 backend：一个 backend 对应多个角色，
        # 按 backend 校验时凭据错误消息里的「角色名」只能任选一个。
        for role, rc in config.roles.items():
            spec = config.models[rc.model]
            cache_key = (spec.provider, spec.base_url, spec.key)
            backend = self._by_key.get(cache_key)
            if backend is None:
                factory = self._factories.get(spec.provider) or _default_factory(
                    spec.provider
                )
                backend = factory(spec, role=role, model_ref=rc.model)
                self._by_key[cache_key] = backend
            self._by_role[role] = backend

    def backend_for(self, role: Role):
        """测试与诊断用。"""
        return self._by_role[role]

    def chat(self, role: Role, system: str, messages: list, tools: list | None):
        """位置参数顺序必须与 LlmClient Protocol 一致——run_agent 里
        `client.chat(role, system_prompt, messages, tools)` 是按位置调用的。"""
        rc = self._config.roles[role]
        return self._by_role[role].chat(
            role=role,
            model_ref=rc.model,
            system=system,
            messages=messages,
            tools=tools,
            max_tokens=rc.max_tokens,
        )
