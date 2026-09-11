"""状态机的状态、命令与结果。全部可 JSON 序列化（见 wire.py）。"""

from dataclasses import dataclass, field
from enum import Enum

from tripplan.models.facts import FactSnapshot
from tripplan.models.issue import Issue
from tripplan.models.itinerary import Angle, Itinerary
from tripplan.models.requirements import Requirements


class Stage(Enum):
    COLLECT = "COLLECT"
    AWAIT_REQ_CONFIRM = "AWAIT_REQ_CONFIRM"  # ⏸
    GENERATE = "GENERATE"
    AWAIT_CHOICE = "AWAIT_CHOICE"  # ⏸
    REFINE = "REFINE"
    DONE = "DONE"


#: 暂停点。必须是显式状态——这是 advance 形状与可持久化的前提。
AWAITING = frozenset({Stage.AWAIT_REQ_CONFIRM, Stage.AWAIT_CHOICE})


class SlotStatus(Enum):
    PENDING = "PENDING"
    OK = "OK"
    EXHAUSTED = "EXHAUSTED"  # 撞轮数或资源上限，带残缺行程
    FAILED = "FAILED"  # 外部依赖失败，可能没有行程


@dataclass
class CandidateSlot:
    """包一层的理由：三条线并行，任何一条失败都不该让整组垮掉或悄悄变成两个。"""

    angle: Angle
    itinerary: Itinerary | None = None
    facts: FactSnapshot | None = None
    status: SlotStatus = SlotStatus.PENDING
    detail: str = ""


@dataclass
class TripState:
    run_id: str
    raw_request: str
    revision: int = 0
    stage: Stage = Stage.COLLECT
    requirements: Requirements | None = None
    candidates: list[CandidateSlot] = field(default_factory=list)
    chosen_key: str | None = None
    trip_timezone: str | None = None
    seeds: dict[str, Itinerary] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)

    @classmethod
    def new(cls, raw_request: str, run_id: str) -> "TripState":
        return cls(run_id=run_id, raw_request=raw_request)

    def slot(self, angle_key: str | None) -> CandidateSlot | None:
        if angle_key is None:
            return None
        for c in self.candidates:
            if c.angle.key == angle_key:
                return c
        return None

    def chosen(self) -> CandidateSlot | None:
        return self.slot(self.chosen_key)


# ---------- 命令：判别式联合，非法组合不可表示 ----------


@dataclass(frozen=True)
class ConfirmRequirements:
    expected_revision: int


@dataclass(frozen=True)
class AmendRequirements:
    expected_revision: int
    text: str


@dataclass(frozen=True)
class ChooseCandidate:
    expected_revision: int
    angle_key: str


@dataclass(frozen=True)
class GiveFeedback:
    expected_revision: int
    angle_key: str
    text: str


Command = ConfirmRequirements | AmendRequirements | ChooseCandidate | GiveFeedback

ALLOWED_COMMANDS: dict[Stage, frozenset[type]] = {
    Stage.AWAIT_REQ_CONFIRM: frozenset({ConfirmRequirements, AmendRequirements}),
    Stage.AWAIT_CHOICE: frozenset({ChooseCandidate, GiveFeedback, AmendRequirements}),
}


# ---------- 结果 ----------


class InputKind(Enum):
    CONFIRM_REQUIREMENTS = "CONFIRM_REQUIREMENTS"
    CHOOSE_OR_FEEDBACK = "CHOOSE_OR_FEEDBACK"


class RejectReason(Enum):
    STALE_REVISION = "STALE_REVISION"
    WRONG_COMMAND_FOR_STAGE = "WRONG_COMMAND_FOR_STAGE"
    MISSING_REQUIRED = "MISSING_REQUIRED"
    UNKNOWN_CANDIDATE = "UNKNOWN_CANDIDATE"
    UNSELECTABLE_CANDIDATE = "UNSELECTABLE_CANDIDATE"


@dataclass(frozen=True)
class Done:
    itinerary: Itinerary


@dataclass(frozen=True)
class NeedInput:
    kind: InputKind
    payload: object  # Requirements 或 list[CandidateSlot]
    revision: int  # 回传时用作 expected_revision


@dataclass(frozen=True)
class Rejected:
    reason: RejectReason
    current: NeedInput  # 当前真正在等的东西，driver 可直接重新渲染


Outcome = Done | NeedInput | Rejected
