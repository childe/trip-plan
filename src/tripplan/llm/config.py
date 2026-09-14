"""按角色配置模型，而不是全局一个。

配置分两层：先定义一组 model（各带 provider / name / base_url / key），
角色再引用 model 名。这样同一个 model 能被多个角色复用，而 max_tokens
按角色定——planner 要 16000、angle 只要 2000。
"""

import os
import re
import tomllib
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path

from tripplan.llm.errors import ConfigError


class Role(Enum):
    PLANNER = "planner"
    CRITIC = "critic"
    ANGLE = "angle"
    CLASSIFIER = "classifier"


PROVIDERS = ("anthropic", "openai")

#: 只认两个形状：${NAME} 与 ${NAME:-默认值}。默认值取到**第一个** } 为止，
#: 不支持嵌套、不提供转义——`${A:-${B}}` 会取到 `${B` 为止。这些写法在
#: key / url / 模型名里不存在，但规则必须写死，否则每个实现者会发明一套。
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand(value: str, where: str) -> str:
    """展开 ${VAR} 与 ${VAR:-default}。见 Task 2 的 docstring。"""

    def _sub(m: re.Match) -> str:
        name, default = m.group(1), m.group(2)
        env = os.environ.get(name)
        if env is not None:
            return env
        if default is not None:
            return default
        raise ConfigError(
            f"{where} 引用了未设置的环境变量 {name}。"
            f"请先 export 它，或在配置里写 ${{{name}:-默认值}} 给一个默认值。"
        )

    return _VAR.sub(_sub, value)


def is_var_reference(text: str) -> bool:
    """整段文本是否就是一个 ${...} 引用。

    只有为真时 `key_source` 才可以进日志或错误消息——用户写字面量 key 时
    `key_source` 就是明文密钥本身（见 ModelSpec.key_source 的注释）。
    """
    return _VAR.fullmatch(text) is not None


@dataclass(frozen=True)
class ModelSpec:
    provider: str  # "anthropic" | "openai"
    name: str  # 展开后的真实模型 ID
    base_url: str  # 展开后；"" = SDK 默认端点
    key: str  # 展开后；"" = 交给 SDK 自行解析凭据
    #: 用户在 TOML 里写的原文，仅用于错误消息。展开前后相同时与上面一致
    #: ——注意 key_source 因此可能就是**明文密钥本身**（用户写字面量时），
    #: 输出前必须先用 is_var_reference() 判形态，不可无条件取用。
    name_source: str
    key_source: str


@dataclass(frozen=True)
class RoleConfig:
    model: str  # ModelSpec 的键名
    max_tokens: int
    allow_same_model: bool = False  # 仅 critic 有意义


@dataclass(frozen=True)
class LlmConfig:
    models: dict[str, ModelSpec]
    roles: dict[Role, RoleConfig]


#: 内置默认。与现状行为一致——不给配置文件时不构成破坏性变更。
#: key 用 ${ANTHROPIC_API_KEY:-} 而不是 ${ANTHROPIC_API_KEY}：展开成空串后
#: backend 会把它归一成 None 交给 SDK，让 SDK 自己走完 API_KEY → AUTH_TOKEN
#: → profile → WIF 的解析链。写成无默认值的形式会把只有 AUTH_TOKEN 或
#: profile 的用户挡在门外。
_DEFAULT_MODELS: dict[str, dict] = {
    "opus": {
        "provider": "anthropic",
        "name": "claude-opus-5",
        "key": "${ANTHROPIC_API_KEY:-}",
    },
    "sonnet": {
        "provider": "anthropic",
        "name": "claude-sonnet-5",
        "key": "${ANTHROPIC_API_KEY:-}",
    },
    "haiku": {
        "provider": "anthropic",
        "name": "claude-haiku-4-5",
        "key": "${ANTHROPIC_API_KEY:-}",
    },
}

_DEFAULT_ROLES: dict[str, dict] = {
    "planner": {"model": "opus", "max_tokens": 16000},
    "critic": {"model": "sonnet", "max_tokens": 4000},
    "angle": {"model": "sonnet", "max_tokens": 2000},
    "classifier": {"model": "haiku", "max_tokens": 1000},
}

_MODEL_FIELDS = ("provider", "name", "base_url", "key")
_ROLE_FIELDS = ("model", "max_tokens", "allow_same_model")


def _read_toml(path: Path) -> dict:
    """step 1。捕两族异常：OSError（文件读不到）与 ValueError（内容解不开）。

    只点名 FileNotFoundError 与 TOMLDecodeError 会漏掉 IsADirectoryError /
    PermissionError / UnicodeDecodeError，它们会一路逃成裸 traceback。
    """
    try:
        return tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as e:
        raise ConfigError(f"读不到配置文件 {path}：{e}") from e
    except ValueError as e:  # TOMLDecodeError 与 UnicodeDecodeError 都是它的子类
        raise ConfigError(f"配置文件 {path} 解析失败：{e}") from e


def _merged_section(raw, key: str, defaults: dict, *, merge_fields: bool) -> dict:
    """step 2。顶层段必须是 table。

    `merge_fields` 区分两种合并粒度：

    - models（False）：用户同名条目**整条替换**内置条目，不做字段级合并——
      否则 provider="openai" 却静默继承 name="claude-opus-5" 只会制造困惑。
    - roles（True）：**字段级**合并，只覆盖给出的字段——与旧实现
      `dataclasses.replace(cfg[role], **overrides)` 的语义一致；把
      max_tokens 标成必填（只给了 model 就报错缺字段）是破坏性变更。
    """
    section = raw.get(key)
    if section is None:
        return {k: dict(v) for k, v in defaults.items()}
    if not isinstance(section, dict):
        raise ConfigError(
            f"配置里的 {key} 必须是一个表（[{key}.xxx]），实际是 {type(section).__name__}"
        )
    merged = {k: dict(v) for k, v in defaults.items()}
    for name, body in section.items():
        if not isinstance(body, dict):
            raise ConfigError(
                f"{key}.{name} 必须是一个表（[{key}.{name}]），实际是 {type(body).__name__}"
            )
        if merge_fields and name in merged:
            merged[name] = {**merged[name], **body}
        else:
            merged[name] = dict(body)
    return merged


def _check_models(models_raw: dict) -> None:
    """step 3 的 models 部分。显式白名单——不能用 **overrides 那套，
    ModelSpec 带 name_source / key_source 两个内部字段，用户不该写得进去。"""
    for name, body in models_raw.items():
        for field in body:
            if field not in _MODEL_FIELDS:
                raise ConfigError(
                    f"models.{name} 含未知字段：{field}"
                    f"（可用：{', '.join(_MODEL_FIELDS)}）"
                )
        for required in ("provider", "name"):
            if required not in body:
                raise ConfigError(f"models.{name} 缺少必填字段 {required}")
        for field, value in body.items():
            if not isinstance(value, str):
                raise ConfigError(
                    f"models.{name}.{field} 必须是字符串，实际是 {type(value).__name__}"
                )


def _build_roles(roles_raw: dict) -> dict[Role, RoleConfig]:
    """step 3 的 roles 部分 + 构造。"""
    out: dict[Role, RoleConfig] = {}
    for name, body in roles_raw.items():
        try:
            role = Role(name)
        except ValueError as e:
            raise ConfigError(f"未知角色：{name}") from e
        for field in body:
            if field not in _ROLE_FIELDS:
                raise ConfigError(f"roles.{name} 含未知字段：{field}")
        if "allow_same_model" in body and role is not Role.CRITIC:
            raise ConfigError(
                f"allow_same_model 只在 [roles.critic] 下有意义，"
                f"不能写在 roles.{name} 下"
            )
        if "model" in body and not isinstance(body["model"], str):
            raise ConfigError(
                f"roles.{name}.model 必须是字符串，实际是 {type(body['model']).__name__}"
            )
        # bool 是 int 的子类，必须显式排除——否则 max_tokens = true 会被当成 1
        if "max_tokens" in body and (
            isinstance(body["max_tokens"], bool)
            or not isinstance(body["max_tokens"], int)
        ):
            raise ConfigError(
                f"roles.{name}.max_tokens 必须是整数，"
                f"实际是 {type(body['max_tokens']).__name__}"
            )
        if "allow_same_model" in body and not isinstance(
            body["allow_same_model"], bool
        ):
            raise ConfigError(
                f"roles.{name}.allow_same_model 必须是布尔值（true / false，不加引号），"
                f"实际是 {type(body['allow_same_model']).__name__}"
            )
        out[role] = RoleConfig(**body)
    return out


def _build_spec(ref: str, body: dict) -> ModelSpec:
    """step 6。provider 原样比较（不展开），三个字符串字段展开。"""
    provider = body["provider"]
    if provider not in PROVIDERS:
        raise ConfigError(
            f"models.{ref}.provider 不支持：{provider}"
            f"（只能是 {' 或 '.join(PROVIDERS)}）"
        )
    name_source = body["name"]
    key_source = body.get("key", "")
    return ModelSpec(
        provider=provider,
        name=expand(name_source, f"models.{ref}.name"),
        base_url=expand(body.get("base_url", ""), f"models.{ref}.base_url"),
        key=expand(key_source, f"models.{ref}.key"),
        name_source=name_source,
        key_source=key_source,
    )


def _check_critic(roles: dict[Role, RoleConfig], models: dict[str, ModelSpec]) -> None:
    """step 7。判据是展开后的 (provider, name)，base_url 不参与——同一个
    模型放在不同网关后面，盲点不变。

    只比 model 引用名是不够的：用户复制一个 [models.*] 块通常是为了换
    base_url / 换 key（多网关、灰度、区域），不会意识到自己顺手关掉了这道
    安全阀。能被复制粘贴无声关闭的安全阀只提供虚假的保障感。
    """
    critic, planner = roles[Role.CRITIC], roles[Role.PLANNER]
    if critic.allow_same_model:
        return
    c, p = models[critic.model], models[planner.model]
    if (c.provider, c.name) != (p.provider, p.name):
        return
    raise ConfigError(
        f"critic 与 planner 指向同一个模型：{c.provider}/{c.name}"
        f"（critic 的 name 原文为 {c.name_source!r}，planner 的为 {p.name_source!r}）。"
        "同源自审会放过同一个盲点。若确实要这样，在 [roles.critic] 下写 "
        "allow_same_model = true。"
    )


def load_config(path: Path | None = None) -> LlmConfig:
    """按 §5 的七步加载。顺序不能变——引用解析必须先于展开，这样"未知的
    model 引用"报错里出现的永远是用户写的原文。"""
    raw = {} if path is None else _read_toml(path)  # step 1
    models_raw = _merged_section(
        raw, "models", _DEFAULT_MODELS, merge_fields=False
    )  # step 2
    roles_raw = _merged_section(raw, "roles", _DEFAULT_ROLES, merge_fields=True)
    _check_models(models_raw)  # step 3
    roles = _build_roles(roles_raw)

    for role, rc in roles.items():  # step 4：先于展开
        if rc.model not in models_raw:
            raise ConfigError(
                f"未知的 model 引用：{rc.model}（被角色 {role.value} 使用）。"
                f"请先用 [models.{rc.model}] 定义它。"
                "注意 roles.*.model 现在填的是 model 的引用名，不是真实模型 ID。"
            )

    referenced = {rc.model for rc in roles.values()}  # step 5
    # 只对被引用到的 model 做 provider 校验与展开：配置里囤着的备用 model
    # 若引用了未设置的变量或写了非法 provider，不应阻塞启动。
    models = {ref: _build_spec(ref, models_raw[ref]) for ref in referenced}  # step 6
    _check_critic(roles, models)  # step 7
    return LlmConfig(models=models, roles=roles)
