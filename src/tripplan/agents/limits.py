"""单条候选线的资源额度。撞上就抛，由 run_slot 转成 EXHAUSTED 而不是卡住。"""

import time
from dataclasses import dataclass

from tripplan.llm.client import Usage


class LimitExceeded(Exception):
    pass


class Cancelled(Exception):
    """用户主动取消。

    **刻意不继承 LimitExceeded，也不继承任何被现有代码捕获的类型。**
    继承就等于重新掉进 slot.py:76 的 `except LimitExceeded` 和
    orchestrator.py:254 `_safe_slot` 的 `except Exception` 里：取消会被
    静默翻译成「候选生成失败」，`_run_to_pause` 若无其事地把 stage 推到
    AWAIT_CHOICE，advance 递增 revision，job 体照常 CAS 落盘——用户按了
    「取消」，系统给他写进盘里一份候选全失败的行程（spec §4.3）。
    """


def raise_if_cancelled(token) -> None:
    """token 是任何带 is_set() 的对象（threading.Event）；None = 没有取消通道。

    给「LLM turn 之间」以外的检查点用：候选与候选之间、CAS 之前。
    """
    if token is not None and token.is_set():
        raise Cancelled("已取消")


@dataclass(frozen=True)
class SlotLimits:
    max_rounds: int = 3
    max_tool_calls: int = 40  # 本 slot 累计
    #: 跨 provider 混用时这只是一道**粗粒度熔断，不是可比的计量**：
    #: OpenAI 推理模型的 completion_tokens 混着 reasoning tokens，同样"干一件
    #: 事"的计数可能是 Anthropic 的数倍，这个阈值不再对应稳定语义，候选线会
    #: 以看不出规律的方式提前 EXHAUSTED。
    #: 另外，对省略 usage 的网关（backends/openai.py 把它归一成 Usage(0,0)），
    #: 这道熔断根本不会触发，那条候选线只剩 deadline 兜底。
    max_output_tokens: int = 120_000  # 本 slot 累计
    max_schema_repairs: int = 2  # 每次 run_agent
    deadline_s: int = 600


def _noop(_event) -> None:
    pass


class SlotContext:
    """记账 + 取消标记。作用域是一条候选线，不是单次 run_agent ——
    generate + revise×N + critic 共享同一份额度。"""

    def __init__(
        self, limits: SlotLimits, clock=time.monotonic, emit=_noop, cancel=None
    ) -> None:
        self.limits = limits
        self.emit = emit
        self._clock = clock
        self._started = clock()
        self._usage = Usage(0, 0)
        self._tool_calls = 0
        self._cancelled = False
        #: 外部取消令牌。SlotContext 是在 orchestrator/slot 内部现场创建的，
        #: Web 层拿不到它的句柄，只能把一个 threading.Event 一路传进来。
        self._cancel = cancel

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
        if self._cancelled or (self._cancel is not None and self._cancel.is_set()):
            raise Cancelled("已取消")
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
