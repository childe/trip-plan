"""Anthropic 适配器。

凭据处理见设计文档 §6：原方案查 client.auth_headers 两个方向都错——空串
key 会让它返回 {'X-Api-Key': ''}（非空，假阴性放行，然后请求期抛裸
TypeError），而 OAuth profile / WIF 的 auth_headers 恒为空（假阳性，把能
正常工作的用户判成缺凭据）。正确判据是「SDK 是否解析出了任何一种凭据」。
"""

import logging

from tripplan.llm.backends._shared import var_hint, where as _where
from tripplan.llm.client import LlmResponse, ToolCall, Usage
from tripplan.llm.config import ModelSpec, Role
from tripplan.llm.errors import ConfigError, MissingCredential
from tripplan.providers.base import ProviderError

logger = logging.getLogger(__name__)

#: 没有可用凭据时，提示用户去 export 哪个变量。
ENV_HINT = "ANTHROPIC_API_KEY"


def _var_hint(spec: ModelSpec) -> str:
    return var_hint(spec, ENV_HINT)


class AnthropicBackend:
    def __init__(
        self,
        spec: ModelSpec,
        role: Role | None = None,
        model_ref: str | None = None,
    ) -> None:
        import anthropic

        self.spec = spec
        try:
            # `or None` 是规则一：空串会被 SDK 当成"显式给了凭据"，
            # 从而跳过整条 API_KEY → AUTH_TOKEN → profile → WIF 的解析链。
            self._client = anthropic.Anthropic(
                api_key=spec.key or None,
                base_url=spec.base_url or None,
            )
        except anthropic.AnthropicError as e:
            # 捕厂商基类而不是 APIError：CredentialsError 等构造期异常都不是
            # APIError 子类。消息套用 §6.2 的契约，不透传 SDK 英文原文。
            raise ConfigError(
                f"{_where(role, model_ref)} 的 Anthropic 客户端构造失败。"
                f"请检查凭据配置（通常是 `export {_var_hint(spec)}=你的key`）；"
                f"只想试跑工具就加 --dry-run。原始错误：{e}"
            ) from e

        if not (
            self._client.api_key or self._client.auth_token or self._client.credentials
        ):
            raise MissingCredential(
                f"缺少凭据：{_where(role, model_ref)}（provider=anthropic）"
                f"没有可用的 API key。"
                f"请先执行 `export {_var_hint(spec)}=你的key` 再运行；"
                "如果只是想在没有凭据的情况下试跑工具，加 --dry-run。"
            )

        if spec.base_url.rstrip("/").endswith("/v1"):
            # 两家 base_url 的后缀语义不同：anthropic SDK 在其后追加
            # /v1/messages，openai 追加 /chat/completions（所以 openai 的
            # base_url 要自带 /v1）。把同一个网关地址原样复制过来会 404，
            # 而请求期 404 的诊断离真因太远。
            logger.debug(
                "base_url 以 /v1 结尾，anthropic 会在其后再追加 /v1/messages，"
                "这多半是从 openai 的配置复制过来的：%s",
                spec.base_url,
            )

    def chat(
        self,
        role: Role,
        model_ref: str,
        system: str,
        messages: list,
        tools: list | None,
        max_tokens: int,
    ) -> LlmResponse:
        import anthropic

        # 不传 temperature/top_p/top_k：当代模型收到采样参数会返回 400。
        kwargs = dict(
            model=self.spec.name,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
        )
        if tools:
            kwargs["tools"] = tools
        try:
            resp = self._client.messages.create(**kwargs)
        except anthropic.AuthenticationError as e:
            # 401 改消息不改类型——见本文件顶部与设计文档 §6.3。
            raise ProviderError(
                f"凭据被拒绝（401）：{_where(role, model_ref)}（provider=anthropic）。"
                f"请检查 `{_var_hint(self.spec)}` 是否正确或已过期。原始错误：{e}"
            ) from e
        except anthropic.AnthropicError as e:
            # 捕基类不捕 APIError：请求期刷新令牌失败会抛 CredentialsError 等，
            # 它们不是 APIError 子类，SDK 也明确不把它们包装成 APIConnectionError。
            #
            # 消息必须带上下文，不能是裸 str(e)：str(anthropic.APIConnectionError(...))
            # 恰好总是 'Connection error.'——网关连不上是双 provider 时代最常见的
            # 故障，比 401 常见得多，裸消息会让用户连是哪个角色、哪个 model、
            # 哪个 provider 都猜不出来。类型不变，仍是 ProviderError。
            raise ProviderError(
                f"{_where(role, model_ref)}（provider=anthropic）请求失败：{e}"
            ) from e

        # `resp.content` 与 `resp.usage` 都可能是 None：SDK 用宽松解析
        # （construct_type）——网关省略字段时得到的是 None，不是校验错误，
        # 没有 APIResponseValidationError 可捕。裸迭代 None / 裸读
        # None.output_tokens 都不是 ProviderError，会绕过 run_slot 直接
        # 落到 orchestrator._safe_slot 把已生成的行程丢弃（见本文件顶部
        # 与设计文档 §10 的铁律）。base_url 指向自建网关是本项目的一等
        # 功能，网关的响应形状不再受 Anthropic 契约约束，这两个空洞是
        # 真实可达的（已用 httpx2.MockTransport 实测复现）。
        blocks = resp.content or []
        calls = [
            ToolCall(b.id, b.name, b.input) for b in blocks if b.type == "tool_use"
        ]
        if not calls:
            if resp.stop_reason == "refusal":
                raise ProviderError("模型拒绝了本次请求（stop_reason=refusal）")
            if resp.stop_reason == "model_context_window_exceeded":
                raise ProviderError("上下文窗口已超出：对话历史太长")

        text = "".join(b.text for b in blocks if b.type == "text")

        usage = resp.usage
        if usage is None:
            logger.debug("上游未返回 usage，计量按 0 记")
            counted = Usage(0, 0)
        else:
            # 字段级归一，不能只判 usage 本身是不是 None：网关返回空对象
            # {} 或半残 usage（只给 input_tokens 不给 output_tokens）时，
            # SDK 同样用宽松解析把缺的字段填成 None，不抛校验错误。
            # Usage(None, ...) 在这里不会炸，但会在下一帧 ctx.charge()
            # （Usage.__add__ 里的 int + None）抛 TypeError——不是
            # ProviderError，绕过 run_slot 直接落到 orchestrator._safe_slot，
            # 已生成的行程丢失。实测确认可达（见设计文档 §10.5）。
            counted = Usage(usage.input_tokens or 0, usage.output_tokens or 0)
        logger.debug(
            "anthropic 响应 model=%s stop_reason=%s requested_max_tokens=%d "
            "output_tokens=%d",
            self.spec.name,
            resp.stop_reason,
            max_tokens,
            counted.output_tokens,
        )
        return LlmResponse(
            stop_reason=_stop_reason(resp.stop_reason, bool(calls)),
            text=text,
            tool_calls=calls,
            usage=counted,
        )


def _stop_reason(raw: str | None, has_tool_calls: bool) -> str:
    """有工具调用就是工具轮——stop_reason 只在没有工具调用时才决定分支。

    完整枚举有七个值（实测）：end_turn / max_tokens / stop_sequence /
    tool_use / pause_turn / refusal / model_context_window_exceeded，且标注
    是 Optional 所以可能是 None。refusal 与 model_context_window_exceeded
    已在上面转成 ProviderError；pause_turn 是服务端工具的续跑信号，本项目
    工具全在本地实现，此值不可达。
    """
    if has_tool_calls:
        return "tool_use"
    if raw == "max_tokens":
        return "max_tokens"
    return "end_turn"
