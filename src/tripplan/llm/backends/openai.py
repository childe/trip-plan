"""OpenAI 适配器。

写法约束：内部用 `import openai` + `openai.OpenAI(...)`，**不要**
`from openai import OpenAI`——后者会让测试里的 patch("openai.OpenAI") 失效。
"""

import json
import logging

from tripplan.llm.client import LlmResponse, ToolCall, Usage
from tripplan.llm.config import ModelSpec, Role, is_var_reference
from tripplan.llm.errors import ConfigError, MissingCredential
from tripplan.providers.base import ProviderError

logger = logging.getLogger(__name__)

ENV_HINT = "OPENAI_API_KEY"
_MAX_DIAGNOSTIC = 200


def _where(role: Role | None, model_ref: str | None) -> str:
    if role is None or model_ref is None:
        return "某个 model"
    return f"角色 {role.value} 使用的 model「{model_ref}」"


def _var_hint(spec: ModelSpec) -> str:
    if is_var_reference(spec.key_source):
        return spec.key_source.strip("${}").split(":-")[0]
    return ENV_HINT


class OpenAIBackend:
    def __init__(
        self,
        spec: ModelSpec,
        role: Role | None = None,
        model_ref: str | None = None,
    ) -> None:
        try:
            import openai
        except ImportError as e:
            raise ConfigError(
                f"{_where(role, model_ref)} 的 provider 是 openai，"
                "但 openai 包没有安装。请执行 "
                "`uv pip install 'tripplan[openai]'`。"
            ) from e

        self.spec = spec

        try:
            # `or None` 是规则一。空串不会"静默带病上路"（实测：openai 3.13
            # 对 api_key="" 当场抛），但它会让 SDK **拒绝去读环境变量**——
            # 于是 ${OPENAI_API_KEY:-} 展开成空串时，配了该变量的用户反而起不来。
            self._client = openai.OpenAI(
                api_key=spec.key or None,
                base_url=spec.base_url or None,
            )
        except openai.OpenAIError as e:
            # 构造期 OpenAIError 就是 SDK 在说"我解析不出任何凭据"——实测确认
            # 这是它在构造期的唯一成因（坏 base_url / 空 base_url / 负 timeout
            # 全部构造成功，不抛）。
            #
            # 刻意**不做任何环境变量枚举**：既不在构造前守卫、也不在构造后查
            # client.api_key。两者都会误判——OPENAI_ADMIN_KEY 单独设置时构造
            # 成功但 client.api_key == ''，而 SDK 的凭据通道还有
            # workload_identity 等，枚举会随 SDK 新增通道持续失效。让 SDK 做
            # 权威，我们只读它的结论。
            raise MissingCredential(
                f"缺少凭据：{_where(role, model_ref)}（provider=openai）"
                f"没有可用的 API key。"
                f"请先执行 `export {_var_hint(spec)}=你的key` 再运行；"
                "如果只是想在没有凭据的情况下试跑工具，加 --dry-run。"
                f"（SDK 原文：{e}）"
            ) from e

    def chat(
        self,
        role: Role,
        model_ref: str,
        system: str,
        messages: list,
        tools: list | None,
        max_tokens: int,
    ) -> LlmResponse:
        import openai

        # 新建列表，不原地修改——run_agent 每轮复用同一个 messages list，
        # insert(0, ...) 会让 system 消息逐轮累积。
        payload = [{"role": "system", "content": system}, *messages]
        kwargs = dict(
            model=self.spec.name,
            messages=payload,
            max_completion_tokens=max_tokens,
        )
        if tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t["description"],
                        "parameters": t["input_schema"],
                    },
                }
                for t in tools
            ]
        try:
            resp = self._client.chat.completions.create(**kwargs)
        except openai.AuthenticationError as e:
            raise ProviderError(
                f"凭据被拒绝（401）：{_where(role, model_ref)}（provider=openai）。"
                f"请检查 `{_var_hint(self.spec)}` 是否正确或已过期。原始错误：{e}"
            ) from e
        except openai.OpenAIError as e:
            # 捕基类不捕 APIError——openai 里 OpenAIError 是基类、APIError 是
            # 其子类，凭据刷新一类的错误不会是 APIError。
            raise ProviderError(str(e)) from e

        if not resp.choices:
            raise ProviderError("上游返回了空的 choices")
        choice = resp.choices[0]
        message = choice.message

        if getattr(message, "refusal", None):
            raise ProviderError(f"模型拒绝了本次请求：{message.refusal}")
        raw_calls = list(message.tool_calls or [])
        if not raw_calls and choice.finish_reason == "content_filter":
            raise ProviderError("上游内容过滤拦截了本次生成")

        text = message.content or ""
        calls, notes = [], []
        for c in raw_calls:
            args, note = _parse_arguments(c.function.arguments)
            calls.append(ToolCall(c.id, c.function.name, args))
            if note:
                notes.append(note)
        if notes:
            # 拼接不覆盖：模型可能在发起工具调用的同时也吐了文本。
            # 前缀让这段适配器生成的文字在 assistant 历史里可辨认——
            # 它会经补偿一进入历史，模型否则会以为那是自己说的话。
            text = "\n".join(x for x in (text, *notes) if x)

        usage = resp.usage
        if usage is None:
            logger.debug("上游未返回 usage，计量按 0 记")
            counted = Usage(0, 0)
        else:
            counted = Usage(usage.prompt_tokens, usage.completion_tokens)
        logger.debug(
            "openai 响应 model=%s finish_reason=%s requested_max_completion_tokens=%d "
            "completion_tokens=%d",
            self.spec.name,
            choice.finish_reason,
            max_tokens,
            counted.output_tokens,
        )
        return LlmResponse(
            stop_reason=_stop_reason(choice.finish_reason, bool(calls)),
            text=text,
            tool_calls=calls,
            usage=counted,
        )


def _parse_arguments(raw: str) -> tuple[dict, str | None]:
    """arguments 是 JSON 字符串。空串（部分网关对无参调用的返回）按 {} 处理。

    解析失败不抛 ProviderError——那会杀死整条候选线。产出 args={} 让
    impl(**{}) 因缺必填参数抛 TypeError，走 runner.py:143-149 既有的
    「工具错误回喂给模型」通道；同时把原文注入 text，让模型知道是自己的
    JSON 坏了，而不是只看到"缺少必填参数"。
    """
    if not raw:
        return {}, None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        brief = raw[:_MAX_DIAGNOSTIC]
        logger.debug("工具参数不是合法 JSON：%s（%s）", brief, e)
        return {}, f"[适配器] 上一轮的工具参数不是合法 JSON：{brief}"
    if not isinstance(parsed, dict):
        return {}, f"[适配器] 工具参数必须是 JSON 对象，实际是 {type(parsed).__name__}"
    return parsed, None


def _stop_reason(finish_reason: str | None, has_tool_calls: bool) -> str:
    """判工具轮看 tool_calls 非空，不看 finish_reason。"""
    if has_tool_calls:
        return "tool_use"
    if finish_reason == "length":
        return "max_tokens"
    return "end_turn"
