"""按角色配置模型，而不是全局一个。"""

import os
import re
import tomllib
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path

from tripplan.llm.errors import ConfigError

#: 只认两个形状：${NAME} 与 ${NAME:-默认值}。默认值取到**第一个** } 为止，
#: 不支持嵌套、不提供转义——`${A:-${B}}` 会取到 `${B` 为止。这些写法在
#: key / url / 模型名里不存在，但规则必须写死，否则每个实现者会发明一套。
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand(value: str, where: str) -> str:
    """展开 ${VAR} 与 ${VAR:-default}。

    `where` 是出错时报给用户的位置（如 "models.gpt5.key"）。

    判「变量是否已定义」用 `os.environ.get(name) is None` 而不是真值判断：
    `export X=` 是用户显式表达"我知道它，但留空"，与"拼写错了"是两回事，
    前者应当放行成空串，后者应当当场报错。
    """

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


class Role(Enum):
    PLANNER = "planner"
    CRITIC = "critic"
    ANGLE = "angle"
    CLASSIFIER = "classifier"


@dataclass(frozen=True)
class RoleConfig:
    model: str
    max_tokens: int
    #: True 时只喂最终产物，不喂上游角色的推理过程。critic 需要它来保持独立视角。
    independent_context: bool = False


DEFAULT_ROLES: dict[Role, RoleConfig] = {
    Role.PLANNER: RoleConfig("claude-opus-5", max_tokens=16000),
    # 换模型是硬要求：planner 对自己的输出有系统性盲点。
    # 有其他厂商 key 时把这一项改成异厂商模型，效果更好。
    Role.CRITIC: RoleConfig(
        "claude-sonnet-5", max_tokens=4000, independent_context=True
    ),
    Role.ANGLE: RoleConfig("claude-sonnet-5", max_tokens=2000),
    Role.CLASSIFIER: RoleConfig("claude-haiku-4-5", max_tokens=1000),
}


def load_config(path: Path | None = None) -> dict[Role, RoleConfig]:
    cfg = dict(DEFAULT_ROLES)
    if path is None:
        return cfg

    raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    for name, overrides in (raw.get("roles") or {}).items():
        try:
            role = Role(name)
        except ValueError as e:
            raise ValueError(f"未知角色：{name}") from e
        try:
            cfg[role] = replace(cfg[role], **overrides)
        except TypeError as e:
            raise ValueError(f"角色 {name} 的配置字段无效：{e}") from e

    if cfg[Role.PLANNER].model == cfg[Role.CRITIC].model:
        raise ValueError(
            "critic 必须使用与 planner 不同的模型——同源自审会放过同一个盲点。"
            "若只有单一厂商，请选不同 tier。"
        )
    return cfg
