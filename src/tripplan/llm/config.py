"""按角色配置模型，而不是全局一个。"""

import tomllib
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path


class Role(Enum):
    PLANNER = "planner"
    CRITIC = "critic"
    ANGLE = "angle"
    CLASSIFIER = "classifier"


@dataclass(frozen=True)
class RoleConfig:
    model: str
    max_tokens: int
    temperature: float = 1.0
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
    Role.CLASSIFIER: RoleConfig("claude-haiku-4-5", max_tokens=1000, temperature=0.0),
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
