"""内层 tool loop。LLM 想调几次工具就调几次 —— 在 ctx 的额度之内。"""

import json
import re

from tripplan.agents.limits import LimitExceeded, SlotContext
from tripplan.llm.client import LlmClient
from tripplan.llm.config import Role

_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.S)


class SchemaError(Exception):
    pass


def _parse_and_validate(text: str, schema: dict) -> dict:
    stripped = _FENCE.sub(r"\1", text).strip()
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as e:
        raise SchemaError(f"不是合法 JSON：{e}") from e
    if not isinstance(data, dict):
        raise SchemaError("顶层必须是对象")
    missing = [k for k in schema.get("required", []) if k not in data]
    if missing:
        raise SchemaError(f"缺少必填字段：{', '.join(missing)}")
    return data


def _repair_prompt(err: SchemaError, schema: dict) -> str:
    return "上一条回复没有通过校验：" + str(
        err
    ) + "\n" "请只输出符合下面 schema 的 JSON，不要任何解释文字：\n" + json.dumps(
        schema, ensure_ascii=False
    )


def run_agent(
    system_prompt: str,
    user_prompt: str,
    tools,
    output_schema: dict,
    role: Role,
    ctx: SlotContext,
    client: LlmClient,
    tool_impls: dict,
) -> dict:
    messages: list[dict] = [{"role": "user", "content": user_prompt}]
    repairs = 0

    while True:
        ctx.check()  # 超 deadline / token / 取消 → 抛
        resp = client.chat(role, system_prompt, messages, tools)
        ctx.charge(resp.usage)

        if resp.stop_reason == "tool_use":
            ctx.charge_tool_calls(len(resp.tool_calls))
            messages.append({"role": "assistant", "content": resp.text or "(tool_use)"})
            results = []
            for call in resp.tool_calls:
                impl = tool_impls.get(call.name)
                if impl is None:
                    results.append(f"[{call.name}] 错误：没有这个工具")
                    continue
                try:
                    results.append(
                        f"[{call.name}] "
                        + json.dumps(impl(**call.args), ensure_ascii=False)
                    )
                except Exception as e:  # 工具报错交回模型，不炸穿这条线
                    results.append(f"[{call.name}] 错误：{e}")
            messages.append({"role": "user", "content": "\n".join(results)})
            continue

        try:
            return _parse_and_validate(resp.text, output_schema)
        except SchemaError as e:
            repairs += 1
            if repairs > ctx.limits.max_schema_repairs:
                raise LimitExceeded(f"schema 修复 {repairs} 次仍失败：{e}") from e
            messages.append({"role": "assistant", "content": resp.text})
            messages.append(
                {"role": "user", "content": _repair_prompt(e, output_schema)}
            )
