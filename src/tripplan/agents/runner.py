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


#: JSON Schema 的 type 关键字 → Python 侧的判定。bool 在 Python 里是 int 的
#: 子类，而 JSON 里 true 不是数字，所以 number/integer 必须显式排除 bool。
_TYPE_PREDICATES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def _json_type(value) -> str:
    for name, pred in _TYPE_PREDICATES.items():
        if name != "integer" and pred(value):
            return name
    return type(value).__name__


def _check_types(value, schema: dict, path: str) -> None:
    """按 schema 声明的 type 递归校验，命中不符就抛 SchemaError。

    只校验 type，不在嵌套层再查 required —— 这是刻意的分工：
    - 类型不对的标量（poi_query=null、note=3、lodging={...}、message=null）
      会被原封不动地塞进领域对象，一路干净地往返 state.json，最后在渲染
      HTML 时炸成一截 AttributeError，而且 `trip render` 会永远复现；
    - 嵌套层缺字段则另有一整套「跳过这一个活动/这一天/这一条点评，并留下
      一条 WARNING」的部分成功语义（steps.py 的三级兜底）。把嵌套 required
      也搬到这里，等于把「30 个活动里有 1 个缺 poi_query」升级成「整份行程
      作废重来」，那是另一个契约，不是本次要换的那个。

    校验放在 run_agent 这一层而不是消费侧，是因为这里还能让既有的修复轮
    把错误回喂给模型、要它自己改正；消费侧的任何 isinstance 守卫都只能
    把数据丢掉。
    """
    declared = schema.get("type")
    if declared is not None:
        allowed = [declared] if isinstance(declared, str) else list(declared)
        preds = [_TYPE_PREDICATES[t] for t in allowed if t in _TYPE_PREDICATES]
        if preds and not any(pred(value) for pred in preds):
            where = path or "顶层"
            raise SchemaError(
                f"{where} 需要 {'/'.join(allowed)}，实际是 {_json_type(value)}"
            )

    # 类型对不对是一回事，能不能往下走是另一回事：只要值确实是容器，就按
    # properties / items 继续下探，不管 type 里还列了别的什么（["object","null"]）。
    if isinstance(value, dict):
        for key, sub_schema in (schema.get("properties") or {}).items():
            if key in value:
                sub_path = f"{path}.{key}" if path else key
                _check_types(value[key], sub_schema, sub_path)
    elif isinstance(value, list):
        item_schema = schema.get("items")
        if item_schema:
            for index, element in enumerate(value):
                _check_types(element, item_schema, f"{path}[{index}]")


def _parse_and_validate(text: str, schema: dict) -> dict:
    data = _load_json(text)
    if not isinstance(data, dict):
        raise SchemaError("顶层必须是对象")
    missing = [k for k in schema.get("required", []) if k not in data]
    if missing:
        raise SchemaError(f"缺少必填字段：{', '.join(missing)}")
    _check_types(data, schema, "")
    return data


def _repair_prompt(err: SchemaError, schema: dict) -> str:
    return "上一条回复没有通过校验：" + str(
        err
    ) + "\n" "请只输出符合下面 schema 的 JSON，不要任何解释文字：\n" + json.dumps(
        schema, ensure_ascii=False
    )


_MAX_ARGS_IN_HISTORY = 200


def _brief_args(args: dict) -> str:
    s = json.dumps(args, ensure_ascii=False, default=str)
    return s if len(s) <= _MAX_ARGS_IN_HISTORY else s[:_MAX_ARGS_IN_HISTORY] + "…"


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
    # §13 的诊断需要知道：本次调用里有没有出现过「非工具轮」，以及它们是不是
    # 全都返回了空文本。作用域是**本次 run_agent 调用**，不是整条候选线——
    # 取 slot 作用域的话，generate 只要出过一次正常文本就永久置假，功能几乎
    # 永不触发。
    non_tool_rounds = 0
    blank_non_tool_rounds = 0

    try:
        while True:
            ctx.check()  # 超 deadline / token / 取消 → 抛
            resp = client.chat(role, system_prompt, messages, tools)
            ctx.charge(resp.usage)

            if resp.stop_reason == "tool_use":
                ctx.charge_tool_calls(len(resp.tool_calls))
                ctx.check()  # 本轮工具调用若把额度打穿，这一轮工具一个都不执行
                # 改动 1：把工具调用本身也拍平进 assistant 文本。不这么做的话，
                # OpenAI 的推理模型发起工具调用时 content 恒为 None，这一轮在
                # 历史里就只剩字面 "(tool_use)"，模型下一轮不知道自己查的是哪
                # 个词，会重复调用——而 max_tool_calls 是整条候选线的累计额度
                # （limits.py:16）。args 截断 200 字符：完整 JSON 会显著加长
                # planner 的历史，抬高撞上上下文窗口上限的概率。
                calls_text = "\n".join(
                    f"(调用工具) {c.name}({_brief_args(c.args)})"
                    for c in resp.tool_calls
                )
                content = (
                    "\n".join(x for x in (resp.text, calls_text) if x) or "(tool_use)"
                )
                messages.append({"role": "assistant", "content": content})
                results = []
                for call in resp.tool_calls:
                    impl = tool_impls.get(call.name)
                    if impl is None:
                        results.append(f"[{call.name}] 错误：没有这个工具")
                        continue
                    try:
                        results.append(
                            f"[{call.name}] "
                            + json.dumps(
                                impl(**call.args), ensure_ascii=False, default=str
                            )
                        )
                    except Exception as e:  # 工具报错交回模型，不炸穿这条线
                        results.append(f"[{call.name}] 错误：{e}")
                messages.append({"role": "user", "content": "\n".join(results)})
                continue

            non_tool_rounds += 1
            if not resp.text.strip():
                blank_non_tool_rounds += 1

            try:
                return _parse_and_validate(resp.text, output_schema)
            except SchemaError as e:
                repairs += 1
                if repairs > ctx.limits.max_schema_repairs:
                    raise LimitExceeded(f"schema 修复 {repairs} 次仍失败：{e}") from e
                # 改动 2：.strip() 不能省——Anthropic 对空 content 返回 400，
                # 对纯空白同样，而 "   " 是 truthy，裸 or 兜不住。
                messages.append(
                    {"role": "assistant", "content": resp.text.strip() or "(空回复)"}
                )
                messages.append(
                    {"role": "user", "content": _repair_prompt(e, output_schema)}
                )
    except LimitExceeded as e:
        # 三个条件全部满足才追加：本次调用、至少一个非工具轮、所有非工具轮都是
        # 空文本。少任何一条都会误报——工具空转烧穿额度时每轮 text 也都是空的
        # （OpenAI 推理模型工具轮 content 恒为 None），而零轮时「每轮都空」
        # 真空成立。
        #
        # 另有三类必须先被 backend 归一成 ProviderError、根本到不了这里：
        # refusal、content_filter、model_context_window_exceeded。三者都会产出
        # 「非工具轮 text 全空」从而满足谓词，但真因与 max_tokens 无关。
        #
        # 不带 max_tokens 的具体数值：run_agent 这一层拿不到 per-role 的
        # max_tokens（ctx.limits 是 SlotLimits，client 是只有 chat 的
        # Protocol），为一条诊断改 Protocol 代价不成比例。报角色名即可。
        if non_tool_rounds > 0 and blank_non_tool_rounds == non_tool_rounds:
            raise LimitExceeded(
                f"{e}（本次调用（角色 {role.value}）的每一轮非工具响应都是空文本，"
                "该角色的 max_tokens 可能被推理预算吃光）"
            ) from e
        raise
