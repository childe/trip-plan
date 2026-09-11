"""单条候选线的资源额度。撞上就抛，由 run_slot 转成 EXHAUSTED 而不是卡住。"""

import time
from dataclasses import dataclass

from tripplan.llm.client import Usage


class LimitExceeded(Exception):
    pass


@dataclass(frozen=True)
class SlotLimits:
    max_rounds: int = 3
    max_tool_calls: int = 40  # 本 slot 累计
    max_output_tokens: int = 120_000  # 本 slot 累计
    max_schema_repairs: int = 2  # 每次 run_agent
    deadline_s: int = 600


def _noop(_event) -> None:
    pass


class SlotContext:
    """记账 + 取消标记。作用域是一条候选线，不是单次 run_agent ——
    generate + revise×N + critic 共享同一份额度。"""

    def __init__(self, limits: SlotLimits, clock=time.monotonic, emit=_noop) -> None:
        self.limits = limits
        self.emit = emit
        self._clock = clock
        self._started = clock()
        self._usage = Usage(0, 0)
        self._tool_calls = 0
        self._cancelled = False

    @property
    def spent(self) -> Usage:
        return self._usage

    @property
    def tool_calls(self) -> int:
        return self._tool_calls

    def charge(self, usage: Usage) -> None:
        self._usage = self._usage + usage

    def charge_tool_calls(self, n: int) -> None:
        self._tool_calls += n

    def cancel(self) -> None:
        self._cancelled = True

    def check(self) -> None:
        if self._cancelled:
            raise LimitExceeded("已取消")
        elapsed = self._clock() - self._started
        if elapsed > self.limits.deadline_s:
            raise LimitExceeded(f"超时（{elapsed:.0f}s > {self.limits.deadline_s}s）")
        if self._usage.output_tokens > self.limits.max_output_tokens:
            raise LimitExceeded(
                f"输出 token 超限（{self._usage.output_tokens} > "
                f"{self.limits.max_output_tokens}）"
            )
        if self._tool_calls > self.limits.max_tool_calls:
            raise LimitExceeded(
                f"工具调用超限（{self._tool_calls} > " f"{self.limits.max_tool_calls}）"
            )
