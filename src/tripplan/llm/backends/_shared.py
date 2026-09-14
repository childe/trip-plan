"""anthropic.py 与 openai.py 共用的错误消息拼装。

`var_hint` 之前在两个 backend 里逐字复制。它直接关系到安全：判断用户是否
写了字面量 key，写错了会把字面量密钥原样吐进错误消息。复制粘贴意味着任何
一侧的偏移都不会被另一侧的测试发现——`test_literal_key_is_never_echoed_in_message`
曾经只存在于 anthropic 侧的测试文件，openai 那份副本完全没有对应断言。
现在两个 backend 共用同一份实现，测试也压在同一份实现上，任何一侧走偏都
会被抓到。
"""

from tripplan.llm.config import ModelSpec, Role, is_var_reference


def where(role: Role | None, model_ref: str | None) -> str:
    if role is None or model_ref is None:
        return "某个 model"
    return f"角色 {role.value} 使用的 model「{model_ref}」"


def var_hint(spec: ModelSpec, env_hint: str) -> str:
    """只有 key_source 确实是个 ${...} 引用时才报它——用户写字面量 key 时
    key_source 就是明文密钥本身，原样吐出去就是泄漏。

    `env_hint` 是该 provider 的固定映射变量名（anthropic → ANTHROPIC_API_KEY，
    openai → OPENAI_API_KEY）：key_source 不是变量引用时回落到它。
    """
    if is_var_reference(spec.key_source):
        return spec.key_source.strip("${}").split(":-")[0]
    return env_hint
