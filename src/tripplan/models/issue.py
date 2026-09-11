"""校验产出的问题。severity 是防死循环的机制：只有 BLOCKING 触发自动修订。"""

from dataclasses import dataclass
from enum import Enum


class Severity(Enum):
    BLOCKING = "BLOCKING"
    WARNING = "WARNING"
    SUGGESTION = "SUGGESTION"


class Source(Enum):
    RULE = "RULE"
    CRITIC = "CRITIC"
    HUMAN = "HUMAN"


@dataclass(frozen=True)
class DayRef:
    day_id: str


@dataclass(frozen=True)
class ActivityRef:
    day_id: str
    activity_id: str


@dataclass(frozen=True)
class Issue:
    severity: Severity
    source: Source
    code: str
    message: str
    where: DayRef | ActivityRef | None = None

    @classmethod
    def from_human(cls, text: str) -> "Issue":
        return cls(
            severity=Severity.BLOCKING,
            source=Source.HUMAN,
            code="HUMAN",
            message=f"用户要求：{text}",
        )


def has_blocking(issues) -> bool:
    return any(i.severity is Severity.BLOCKING for i in issues)
