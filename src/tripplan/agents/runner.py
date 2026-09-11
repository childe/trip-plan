"""内层 tool loop。LLM 想调几次工具就调几次 —— 在 ctx 的额度之内。"""

import json
import re

from tripplan.agents.limits import LimitExceeded, SlotContext
from tripplan.llm.client import LlmClient
from tripplan.llm.config import Role

_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S)


class SchemaError(Exception):
    pass


def _load_json(text: str):
    """先按原文直接解析；只有在直接解析失败时，才去找第一个围栏代码块兜底。

    这样已经是合法 JSON 的正文——哪怕字符串值里恰好带字面三反引号——不会被
    围栏抽取误伤；围栏抽取只用来兜底"围栏前后带了几句闲聊文字，导致整段
    不是合法 JSON"的情况。只取第一个围栏块、不对全文做替换，避免被文本里
    散落的类围栏片段搅乱。"""
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError as e:
        match = _FENCE.search(text)
        if match is None:
            raise SchemaError(f"不是合法 JSON：{e}") from e
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError as e2:
            raise SchemaError(f"不是合法 JSON：{e2}") from e2


def _parse_and_validate(text: str, schema: dict) -> dict:
    data = _load_json(text)
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
            ctx.check()  # 本轮工具调用若把额度打穿，这一轮工具一个都不执行
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
                        + json.dumps(impl(**call.args), ensure_ascii=False, default=str)
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
