"""LLM 访问层。测试时整个替换成 FakeLlm。"""

from dataclasses import dataclass
from typing import Protocol

from tripplan.llm.config import Role


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
        )


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass(frozen=True)
class LlmResponse:
    stop_reason: str  # "tool_use" | "end_turn" | ...
    text: str
    tool_calls: list[ToolCall]
    usage: Usage


class LlmClient(Protocol):
    def chat(
        self, role: Role, system: str, messages: list, tools: list | None
    ) -> LlmResponse: ...


@dataclass
class RecordedCall:
    role: Role
    system: str
    messages: list
    tools: list | None


class FakeLlm:
    """脚本化的假客户端。支持全局顺序脚本或按角色分别脚本。"""

    def __init__(
        self,
        script: list[LlmResponse] | None = None,
        by_role: dict[Role, list[LlmResponse]] | None = None,
    ) -> None:
        if script is not None and by_role is not None:
            raise ValueError("FakeLlm 不支持同时指定 script 和 by_role——请选择其中一种")
        self._script = list(script or [])
        self._by_role = {r: list(v) for r, v in (by_role or {}).items()}
        self.calls: list[RecordedCall] = []

    def chat(self, role, system, messages, tools) -> LlmResponse:
        self.calls.append(
            RecordedCall(role, system, list(messages), list(tools) if tools else None)
        )
        if self._by_role:
            queue = self._by_role.get(role)
            if queue is None:
                raise AssertionError(f"该角色未脚本化：role={role}")
            if not queue:
                raise AssertionError(f"脚本已用尽：role={role}")
        else:
            queue = self._script
            if not queue:
                raise AssertionError(f"脚本已用尽：role={role}")
        return queue.pop(0)
