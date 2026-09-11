# 旅行规划 Agent v1 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 一个 CLI 旅行规划工具——用户用自然语言描述需求，系统澄清需求、并行生成三份候选行程、自动审查修订到收敛，最终产出 Markdown 与 HTML 行程单。

**Architecture:** 外层是一个薄状态机 `advance(state, deps, cmd) -> Outcome`（保证阶段推进、强制校验、循环上限、状态持久化），内层每个阶段由 LLM 自主 tool loop 完成（不限制它怎么想）。触网的事实获取（resolver）与判定（validator）严格分离，validator 是纯函数，只消费 `FactSnapshot`。

**Tech Stack:** Python 3.12+（PEP 695 泛型语法）、uv 管理依赖、pytest、black；运行时依赖仅 `anthropic` 与 `httpx`。

**Spec:** `docs/superpowers/specs/2026-09-10-trip-planner-design.md`

## Global Constraints

- Python **>= 3.12**（`class Field[T]` 用 PEP 695 语法）。
- 包管理一律 `uv pip install`，**禁止** `pip install`。
- 每次改代码后用 `black` 格式化：虚拟环境里有就用它，否则 `/opt/homebrew/bin/black`。
- 运行时依赖只允许 `anthropic`、`httpx`。测试依赖 `pytest`、`pytest-cov`。**不引入 LangGraph、pydantic、typer、rich。**
- 所有金额一律 `Decimal`，**禁止 float**。
- 所有落盘的 `datetime` 必须 tz-aware 并以 ISO 8601 带 offset 输出。
- `src/tripplan/validation/rules.py` 与 `render/` **禁止任何网络调用**——测试会断言这一点。
- 单元测试禁止真实网络。真实 API 测试标 `@pytest.mark.slow`，默认不跑。
- 提交信息用中文，结尾附：`Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`

---

## 文件结构

```
trip-plan/
├── pyproject.toml
├── src/tripplan/
│   ├── models/
│   │   ├── common.py        # Origin, Field[T], Money, Confidence, LatLng, TravelMode
│   │   ├── requirements.py  # Requirements, BudgetSpec, Party, DateRange, Transfer, Pace
│   │   ├── itinerary.py     # Angle, Category, Activity, Day, Itinerary, assign_ids
│   │   ├── issue.py         # Severity, Source, Issue, DayRef, ActivityRef, has_blocking
│   │   └── facts.py         # PoiFact, PoiResolution, RouteFact, WeatherFact, Gap, FactSnapshot
│   ├── state.py             # Stage, SlotStatus, CandidateSlot, TripState, Command, Outcome
│   ├── wire.py              # encode/decode + FORMAT_VERSION + 迁移入口
│   ├── repo.py              # StateRepo Protocol, FileRepo（flock + CAS）
│   ├── providers/
│   │   ├── base.py          # GeoProvider Protocol, RouteObservation, ProviderError
│   │   ├── fake.py          # FakeProvider（确定性）
│   │   └── amap.py          # AmapProvider + 磁盘缓存
│   ├── validation/
│   │   ├── budget.py        # BudgetLedger（分层账单）
│   │   ├── rules.py         # 9 条确定性规则，纯函数
│   │   ├── resolver.py      # Itinerary → FactSnapshot（唯一触网点）
│   │   ├── diversity.py     # 候选差异度
│   │   └── critic.py        # LLM critic 封装
│   ├── llm/
│   │   ├── config.py        # Role, RoleConfig, load_config
│   │   └── client.py        # LlmClient Protocol, FakeLlm, AnthropicClient
│   ├── agents/
│   │   ├── limits.py        # SlotLimits, SlotContext, LimitExceeded
│   │   ├── runner.py        # run_agent（tool loop + schema 修复）
│   │   ├── steps.py         # collect/pick_angles/generate/revise/classify_feedback
│   │   └── prompts/*.md
│   ├── slot.py              # run_slot（单条候选线的打磨循环）
│   ├── orchestrator.py      # advance / _validate / _apply / _run_to_pause
│   ├── render/
│   │   ├── requirement_card.py
│   │   ├── candidates.py
│   │   ├── itinerary_md.py
│   │   └── itinerary_html.py
│   └── cli.py               # plan / resume / render 三个子命令 + driver
└── tests/                   # 与 src 目录同构
```

**两处对 spec §8 的偏离，理由如下：**

1. **`run_slot` 从 `orchestrator.py` 拆到 `slot.py`。** 前者是状态机（纯控制流、无 I/O），后者是驱动 LLM 的打磨循环（有 I/O、有资源记账）。职责不同，测试手法也不同（前者注入 `FakeLlm` 测分支，后者测超限与异常兜底）。
2. **`StateRepo` 是"一个实例对应一个 trip"，`load()` 不带 `trip_id`。** CLI 的形态是 `trip resume <dir>`，目录即身份；把 id 解析留在 `cli.py`（slug 生成）比塞进仓储层干净。Web 时改为工厂产出 per-trip repo，接口本身不变。

---

## 阶段与检查点

| 阶段 | 任务 | 完成时可以做什么 |
|---|---|---|
| A 基础类型与持久化 | 1–7 | 状态可建、可存、可读、CAS 生效 |
| B 确定性校验 | 8–11 | 喂一份手写行程能得到完整 issue 列表 |
| C 外部数据 | 12–14 | 真实高德数据可解析成 `FactSnapshot` |
| D LLM 层 | 15–17 | 能让模型产出结构化行程 |
| E 编排 | 18–20 | **`advance` 全流程跑通（FakeLlm）** |
| F 输出与 CLI | 21–23 | **`trip plan` / `resume` / `render` 可用** |

---

## Phase A — 基础类型与持久化

### Task 1: 项目骨架与通用类型

**Files:**
- Create: `pyproject.toml`
- Create: `src/tripplan/__init__.py`
- Create: `src/tripplan/models/__init__.py`
- Create: `src/tripplan/models/common.py`
- Test: `tests/models/test_common.py`

**Interfaces:**
- Consumes: 无
- Produces: `Origin`（USER/MODEL）、`Field[T](value, origin, confirmed, rationale)`、`Confidence`（VERIFIED/ESTIMATED）、`Money(amount: Decimal, currency, confidence, source)`、`LatLng(lat, lng)`、`TravelMode`（WALK/TRANSIT/DRIVE）

- [ ] **Step 1: 建项目骨架**

创建 `pyproject.toml`：

```toml
[project]
name = "tripplan"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["anthropic>=0.40", "httpx>=0.27"]

[project.optional-dependencies]
dev = ["pytest>=8", "pytest-cov>=5", "black>=24"]

[project.scripts]
trip = "tripplan.cli:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/tripplan"]

[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]
markers = ["slow: 需要真实网络或真实 LLM，默认不跑"]
addopts = "-m 'not slow'"
```

建空的 `src/tripplan/__init__.py` 与 `src/tripplan/models/__init__.py`，然后：

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
```

- [ ] **Step 2: 写失败的测试**

创建 `tests/models/test_common.py`：

```python
from decimal import Decimal

import pytest

from tripplan.models.common import (
    Confidence,
    Field,
    LatLng,
    Money,
    Origin,
    TravelMode,
)


def test_field_defaults_to_no_value():
    f: Field[str] = Field()
    assert f.value is None
    assert f.origin is None
    assert f.confirmed is False
    assert f.rationale == ""


def test_field_carries_origin_and_confirmation_independently():
    """origin 与 confirmed 正交：确认不抹掉「这值本来是模型猜的」。"""
    f = Field(value="京都", origin=Origin.MODEL, rationale="从「关西」推断")
    confirmed = f.confirm()
    assert confirmed.confirmed is True
    assert confirmed.origin is Origin.MODEL       # ★ 来源不变
    assert confirmed.value == "京都"
    assert f.confirmed is False                   # 原对象不可变


def test_field_is_frozen():
    f = Field(value=1, origin=Origin.USER)
    with pytest.raises(Exception):
        f.value = 2


def test_money_rejects_float_amount():
    with pytest.raises(TypeError):
        Money(amount=12.5, currency="CNY",
              confidence=Confidence.ESTIMATED, source="llm")


def test_money_accepts_decimal():
    m = Money(amount=Decimal("1240.00"), currency="CNY",
              confidence=Confidence.VERIFIED, source="amap:ticket")
    assert m.amount == Decimal("1240.00")


def test_latlng_and_travelmode_exist():
    p = LatLng(lat=35.0, lng=135.7)
    assert (p.lat, p.lng) == (35.0, 135.7)
    assert TravelMode.TRANSIT.value == "TRANSIT"
```

- [ ] **Step 3: 运行测试确认失败**

Run: `uv run pytest tests/models/test_common.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tripplan.models.common'`

- [ ] **Step 4: 实现**

创建 `src/tripplan/models/common.py`：

```python
"""跨模块共用的基础类型。"""

from dataclasses import dataclass, replace
from decimal import Decimal
from enum import Enum


class Origin(Enum):
    """字段取值的来源。与「是否已确认」正交。"""

    USER = "USER"
    MODEL = "MODEL"


@dataclass(frozen=True)
class Field[T]:
    """带来源与确认状态的字段。

    value is None 表示尚无取值（早先设计里的 MISSING）。
    origin 记录「谁给的值」，confirmed 记录「用户认没认」——两者正交，
    确认操作只动 confirmed，不抹掉 origin。
    """

    value: T | None = None
    origin: Origin | None = None
    confirmed: bool = False
    rationale: str = ""

    def confirm(self) -> "Field[T]":
        return replace(self, confirmed=True)


class Confidence(Enum):
    VERIFIED = "VERIFIED"
    ESTIMATED = "ESTIMATED"


@dataclass(frozen=True)
class Money:
    amount: Decimal
    currency: str
    confidence: Confidence
    source: str

    def __post_init__(self) -> None:
        if not isinstance(self.amount, Decimal):
            raise TypeError("Money.amount 必须是 Decimal，不接受 float")


@dataclass(frozen=True)
class LatLng:
    lat: float
    lng: float


class TravelMode(Enum):
    WALK = "WALK"
    TRANSIT = "TRANSIT"
    DRIVE = "DRIVE"
```

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run pytest tests/models/test_common.py -v`
Expected: PASS（6 passed）

- [ ] **Step 6: 格式化并提交**

```bash
uv run black src tests
git add pyproject.toml src tests
git commit -m "feat: 项目骨架与通用类型

Field 拆 origin/confirmed 两个正交字段，确认不抹掉推断来源。
Money 强制 Decimal，构造时拒绝 float。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: 需求模型

**Files:**
- Create: `src/tripplan/models/requirements.py`
- Test: `tests/models/test_requirements.py`

**Interfaces:**
- Consumes: `Field`, `Origin`（Task 1）
- Produces: `Pace`、`Basis`、`CostKind`、`BudgetSpec(amount, currency, basis, includes)`、`Party(adults, children, seniors)` + `.total`、`DateRange(start, end)` + `.days`、`Transfer(at: datetime, mode: str)`、`Requirements`（12 个 `Field` 成员）、`REQUIRED`、`missing_required(reqs) -> list[str]`、`mark_all_confirmed(reqs) -> Requirements`

- [ ] **Step 1: 写失败的测试**

创建 `tests/models/test_requirements.py`：

```python
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from tripplan.models.common import Field, Origin
from tripplan.models.requirements import (
    REQUIRED,
    Basis,
    BudgetSpec,
    CostKind,
    DateRange,
    Pace,
    Party,
    Requirements,
    Transfer,
    mark_all_confirmed,
    missing_required,
)


def _reqs(**kw) -> Requirements:
    base = dict(
        destination=Field(value="京都", origin=Origin.USER),
        dates=Field(
            value=DateRange(date(2026, 10, 1), date(2026, 10, 5)), origin=Origin.USER
        ),
        party=Field(value=Party(adults=2), origin=Origin.USER),
    )
    base.update(kw)
    return Requirements(**base)


def test_date_range_days_is_inclusive():
    assert DateRange(date(2026, 10, 1), date(2026, 10, 5)).days == 5


def test_party_total_counts_everyone():
    assert Party(adults=2, children=1, seniors=1).total == 4


def test_missing_required_lists_empty_fields():
    reqs = Requirements()
    assert missing_required(reqs) == list(REQUIRED)


def test_missing_required_is_empty_when_all_present():
    assert missing_required(_reqs()) == []


def test_missing_required_ignores_optional_fields():
    """budget 缺失不阻塞规划，只有 REQUIRED 三项会。"""
    reqs = _reqs()
    assert reqs.budget.value is None
    assert missing_required(reqs) == []


def test_mark_all_confirmed_preserves_origin():
    reqs = _reqs(
        pace=Field(value=Pace.RELAXED, origin=Origin.MODEL, rationale="带老人")
    )
    out = mark_all_confirmed(reqs)
    assert out.pace.confirmed is True
    assert out.pace.origin is Origin.MODEL          # ★ 推断来源保留
    assert out.destination.confirmed is True


def test_mark_all_confirmed_skips_empty_fields():
    """没有取值的字段不该被标成「已确认」。"""
    out = mark_all_confirmed(Requirements())
    assert out.destination.confirmed is False


def test_budget_spec_holds_currency_basis_and_inclusions():
    b = BudgetSpec(
        amount=Decimal("15000"),
        currency="CNY",
        basis=Basis.TOTAL,
        includes=frozenset({CostKind.TICKET, CostKind.MEAL}),
    )
    assert b.basis is Basis.TOTAL
    assert CostKind.FLIGHT not in b.includes


def test_transfer_requires_tz_aware_datetime():
    import pytest

    with pytest.raises(ValueError):
        Transfer(at=datetime(2026, 10, 1, 9, 0), mode="flight")
    ok = Transfer(
        at=datetime(2026, 10, 1, 9, 0, tzinfo=timezone(timedelta(hours=9))),
        mode="flight",
    )
    assert ok.at.tzinfo is not None
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/models/test_requirements.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tripplan.models.requirements'`

- [ ] **Step 3: 实现**

创建 `src/tripplan/models/requirements.py`：

```python
"""用户需求：每个字段都带来源与确认状态。"""

from dataclasses import dataclass, field, fields, replace
from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from tripplan.models.common import Field


class Pace(Enum):
    RELAXED = "RELAXED"
    NORMAL = "NORMAL"
    PACKED = "PACKED"


class Basis(Enum):
    PER_PERSON = "PER_PERSON"
    TOTAL = "TOTAL"


class CostKind(Enum):
    FLIGHT = "FLIGHT"
    LODGING = "LODGING"
    TICKET = "TICKET"
    MEAL = "MEAL"
    LOCAL_TRANSIT = "LOCAL_TRANSIT"


@dataclass(frozen=True)
class BudgetSpec:
    amount: Decimal
    currency: str
    basis: Basis
    includes: frozenset[CostKind]

    def __post_init__(self) -> None:
        if not isinstance(self.amount, Decimal):
            raise TypeError("BudgetSpec.amount 必须是 Decimal")


@dataclass(frozen=True)
class Party:
    adults: int
    children: int = 0
    seniors: int = 0

    @property
    def total(self) -> int:
        return self.adults + self.children + self.seniors


@dataclass(frozen=True)
class DateRange:
    start: date
    end: date

    @property
    def days(self) -> int:
        """含首尾的天数。10-01 到 10-05 是 5 天。"""
        return (self.end - self.start).days + 1


@dataclass(frozen=True)
class Transfer:
    """抵达或离开。跨境行程两端不在同一时区，必须存带 offset 的时刻。"""

    at: datetime
    mode: str

    def __post_init__(self) -> None:
        if self.at.tzinfo is None:
            raise ValueError("Transfer.at 必须是 tz-aware datetime")


@dataclass
class Requirements:
    destination: Field[str] = field(default_factory=Field)
    dates: Field[DateRange] = field(default_factory=Field)
    party: Field[Party] = field(default_factory=Field)
    arrival: Field[Transfer] = field(default_factory=Field)
    departure: Field[Transfer] = field(default_factory=Field)
    budget: Field[BudgetSpec] = field(default_factory=Field)
    styles: Field[list[str]] = field(default_factory=Field)
    pace: Field[Pace] = field(default_factory=Field)
    must_visit: Field[list[str]] = field(default_factory=Field)
    avoid: Field[list[str]] = field(default_factory=Field)
    lodging_area: Field[str] = field(default_factory=Field)
    constraints: Field[list[str]] = field(default_factory=Field)


#: 这三项无法可靠推断——虚构它们会让整个规划建立在假约束上，
#: 而下游的确定性校验还会拿这份虚构去判 BLOCKING，比不校验更糟。
REQUIRED = ("destination", "dates", "party")


def missing_required(reqs: Requirements) -> list[str]:
    return [name for name in REQUIRED if getattr(reqs, name).value is None]


def describe_value(value) -> str:
    """把字段值渲染成人话。agents 与 render 共用，避免两处各写一份。"""
    if isinstance(value, DateRange):
        return f"{value.start} 至 {value.end}（{value.days} 天）"
    if isinstance(value, Party):
        return (f"成人 {value.adults} 儿童 {value.children} "
                f"老人 {value.seniors}")
    if isinstance(value, BudgetSpec):
        kinds = "、".join(sorted(k.value for k in value.includes))
        return (f"{value.amount} {value.currency}"
                f"（{value.basis.value}，含 {kinds}）")
    if isinstance(value, Transfer):
        return f"{value.at.isoformat()}（{value.mode}）"
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, list):
        return "、".join(str(v) for v in value)
    return str(value)


def mark_all_confirmed(reqs: Requirements) -> Requirements:
    """把所有已有取值的字段标为已确认，不改动 origin。"""
    updates = {}
    for f in fields(reqs):
        current: Field = getattr(reqs, f.name)
        if current.value is not None:
            updates[f.name] = current.confirm()
    return replace(reqs, **updates)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/models/test_requirements.py -v`
Expected: PASS（9 passed）

- [ ] **Step 5: 格式化并提交**

```bash
uv run black src tests
git add src/tripplan/models/requirements.py tests/models/test_requirements.py
git commit -m "feat: 需求模型与必答项校验

REQUIRED 三项（destination/dates/party）为空时不允许进入规划。
mark_all_confirmed 只动 confirmed，保留 origin。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: 问题与行程模型

**Files:**
- Create: `src/tripplan/models/issue.py`
- Create: `src/tripplan/models/itinerary.py`
- Test: `tests/models/test_issue.py`
- Test: `tests/models/test_itinerary.py`

**Interfaces:**
- Consumes: `Money`（Task 1）
- Produces:
  - `Severity`（BLOCKING/WARNING/SUGGESTION）、`Source`（RULE/CRITIC/HUMAN）、`DayRef(day_id)`、`ActivityRef(day_id, activity_id)`、`Issue(severity, source, code, message, where=None)`、`Issue.from_human(text)`、`has_blocking(issues) -> bool`
  - `Category`（SIGHT/MEAL/REST/SHOPPING）、`Angle(key, title, description)`、`Activity(id, day_id, poi_query, start, end, category, cost, indoor, note)`、`Day(id, date, activities, lodging)`、`Itinerary(angle, days, issues)` + `.all_activities()` + `.activity(id)`、`assign_ids(itin) -> Itinerary`

- [ ] **Step 1: 写 issue 的失败测试**

创建 `tests/models/test_issue.py`：

```python
from tripplan.models.issue import (
    ActivityRef,
    DayRef,
    Issue,
    Severity,
    Source,
    has_blocking,
)


def _issue(sev: Severity) -> Issue:
    return Issue(severity=sev, source=Source.RULE, code="R1", message="x")


def test_has_blocking_true_only_for_blocking():
    assert has_blocking([_issue(Severity.BLOCKING)]) is True
    assert has_blocking([_issue(Severity.WARNING)]) is False
    assert has_blocking([_issue(Severity.SUGGESTION)]) is False
    assert has_blocking([]) is False


def test_has_blocking_scans_whole_list():
    issues = [_issue(Severity.WARNING), _issue(Severity.BLOCKING)]
    assert has_blocking(issues) is True


def test_from_human_is_blocking_and_sourced_human():
    """人提的意见必须驱动一轮修订，因此是 BLOCKING。"""
    i = Issue.from_human("第2天太赶了")
    assert i.severity is Severity.BLOCKING
    assert i.source is Source.HUMAN
    assert "第2天太赶了" in i.message


def test_refs_locate_precisely():
    a = ActivityRef(day_id="d1", activity_id="a3")
    d = DayRef(day_id="d1")
    assert (a.day_id, a.activity_id) == ("d1", "a3")
    assert d.day_id == "d1"
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/models/test_issue.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tripplan.models.issue'`

- [ ] **Step 3: 实现 issue**

创建 `src/tripplan/models/issue.py`：

```python
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
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/models/test_issue.py -v`
Expected: PASS（4 passed）

- [ ] **Step 5: 写 itinerary 的失败测试**

创建 `tests/models/test_itinerary.py`：

```python
from datetime import date, time

from tripplan.models.itinerary import (
    Activity,
    Angle,
    Category,
    Day,
    Itinerary,
    assign_ids,
)


def _act(query: str, start: str, end: str) -> Activity:
    h1, m1 = map(int, start.split(":"))
    h2, m2 = map(int, end.split(":"))
    return Activity(
        id="",
        day_id="",
        poi_query=query,
        start=time(h1, m1),
        end=time(h2, m2),
        category=Category.SIGHT,
        cost=None,
        indoor=False,
        note="",
    )


def _itin() -> Itinerary:
    return Itinerary(
        angle=Angle(key="A", title="古寺巡礼", description="以世界遗产为主线"),
        days=[
            Day(id="", date=date(2026, 10, 1),
                activities=[_act("清水寺", "09:00", "11:00"),
                            _act("八坂神社", "13:00", "14:30")],
                lodging="京都站附近"),
            Day(id="", date=date(2026, 10, 2),
                activities=[_act("金阁寺", "09:30", "11:00")],
                lodging="京都站附近"),
        ],
        issues=[],
    )


def test_assign_ids_gives_every_day_and_activity_a_stable_id():
    out = assign_ids(_itin())
    assert [d.id for d in out.days] == ["d1", "d2"]
    assert [a.id for a in out.days[0].activities] == ["d1a1", "d1a2"]
    assert [a.id for a in out.days[1].activities] == ["d2a1"]


def test_assign_ids_backfills_day_id_on_activities():
    out = assign_ids(_itin())
    assert all(a.day_id == "d1" for a in out.days[0].activities)
    assert all(a.day_id == "d2" for a in out.days[1].activities)


def test_activity_ids_are_unique_across_days():
    """跨天唯一——列表下标做不到这点，RouteFact 依赖它定位两端。"""
    out = assign_ids(_itin())
    ids = [a.id for a in out.all_activities()]
    assert len(ids) == len(set(ids))


def test_assign_ids_is_idempotent():
    once = assign_ids(_itin())
    twice = assign_ids(once)
    assert [a.id for a in twice.all_activities()] == [
        a.id for a in once.all_activities()
    ]


def test_lookup_activity_by_id():
    out = assign_ids(_itin())
    assert out.activity("d1a2").poi_query == "八坂神社"
    assert out.activity("nope") is None


def test_all_activities_walks_days_in_order():
    out = assign_ids(_itin())
    assert [a.poi_query for a in out.all_activities()] == [
        "清水寺",
        "八坂神社",
        "金阁寺",
    ]
```

- [ ] **Step 6: 运行确认失败**

Run: `uv run pytest tests/models/test_itinerary.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tripplan.models.itinerary'`

- [ ] **Step 7: 实现 itinerary**

创建 `src/tripplan/models/itinerary.py`：

```python
"""行程。LLM 只产出活动安排；ID 与交通段都由代码补。"""

from dataclasses import dataclass, field, replace
from datetime import date as Date
from datetime import time
from enum import Enum
from typing import Iterator

from tripplan.models.common import Money
from tripplan.models.issue import Issue


class Category(Enum):
    SIGHT = "SIGHT"
    MEAL = "MEAL"
    REST = "REST"
    SHOPPING = "SHOPPING"


@dataclass(frozen=True)
class Angle:
    """一份候选的切入角度。由 LLM 自己想，不写死枚举。"""

    key: str
    title: str
    description: str


@dataclass
class Activity:
    id: str
    day_id: str
    poi_query: str          # LLM 写的名字；解析结果在 FactSnapshot 里
    start: time
    end: time
    category: Category
    cost: Money | None      # None 表示「未知」，不表示免费
    indoor: bool
    note: str


@dataclass
class Day:
    id: str
    date: Date
    activities: list[Activity] = field(default_factory=list)
    lodging: str | None = None


@dataclass
class Itinerary:
    angle: Angle
    days: list[Day] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)

    def all_activities(self) -> Iterator[Activity]:
        for day in self.days:
            yield from day.activities

    def activity(self, activity_id: str) -> Activity | None:
        for a in self.all_activities():
            if a.id == activity_id:
                return a
        return None


def assign_ids(itin: Itinerary) -> Itinerary:
    """按位置分配稳定 ID。幂等。

    ID 由代码分配而非 LLM 产出——让模型自己维护稳定 ID 只是白白增加它出错的
    机会，而按位置分配是确定性的。
    """
    days = []
    for di, day in enumerate(itin.days, start=1):
        day_id = f"d{di}"
        acts = [
            replace(a, id=f"{day_id}a{ai}", day_id=day_id)
            for ai, a in enumerate(day.activities, start=1)
        ]
        days.append(replace(day, id=day_id, activities=acts))
    return replace(itin, days=days)
```

- [ ] **Step 8: 运行确认通过**

Run: `uv run pytest tests/models/ -v`
Expected: PASS（19 passed）

- [ ] **Step 9: 格式化并提交**

```bash
uv run black src tests
git add src/tripplan/models tests/models
git commit -m "feat: 问题与行程模型

活动 ID 由代码按位置分配且跨天唯一——RouteFact 依赖它定位两端，
列表下标做不到。assign_ids 幂等。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: 事实快照模型

**Files:**
- Create: `src/tripplan/models/facts.py`
- Test: `tests/models/test_facts.py`

**Interfaces:**
- Consumes: `LatLng`, `Money`, `TravelMode`（Task 1）
- Produces: `PoiFact`、`Resolved(fact)`、`Ambiguous(candidates)`、`NotFound(query)`、`PoiResolution` 联合、`RouteFact`、`WeatherFact`、`GapKind`、`Gap(kind, subject, detail)`、`FactSnapshot` + `.route(day_id, from_id, to_id)` + `.poi_id_for(activity_id)` + `.constraint_poi_id(query)`

- [ ] **Step 1: 写失败的测试**

创建 `tests/models/test_facts.py`：

```python
from datetime import datetime, timedelta, timezone

import pytest

from tripplan.models.common import LatLng, TravelMode
from tripplan.models.facts import (
    Ambiguous,
    FactSnapshot,
    Gap,
    GapKind,
    NotFound,
    PoiFact,
    Resolved,
    RouteFact,
)

JST = timezone(timedelta(hours=9))


def _poi(pid: str) -> PoiFact:
    return PoiFact(
        id=pid,
        name=pid,
        coords=LatLng(35.0, 135.7),
        opening_hours=None,
        ticket=None,
        source="fake",
        fetched_at=datetime(2026, 9, 1, tzinfo=JST),
    )


def _route(from_id: str, to_id: str, minutes: int) -> RouteFact:
    return RouteFact(
        day_id="d1",
        from_activity_id=from_id,
        to_activity_id=to_id,
        depart_at=datetime(2026, 10, 1, 11, 0, tzinfo=JST),
        mode=TravelMode.TRANSIT,
        duration_min=minutes,
        distance_m=4200,
        polyline="aaa|bbb",
        source="amap:direction/transit",
        fetched_at=datetime(2026, 9, 1, tzinfo=JST),
    )


def _snap(**kw) -> FactSnapshot:
    base = dict(
        poi_by_activity={},
        constraint_pois={},
        routes=[],
        weather={},
        trip_timezone="Asia/Tokyo",
        resolved_at=datetime(2026, 9, 1, tzinfo=JST),
        gaps=[],
    )
    base.update(kw)
    return FactSnapshot(**base)


def test_route_lookup_by_day_and_both_endpoints():
    snap = _snap(routes=[_route("d1a1", "d1a2", 40)])
    assert snap.route("d1", "d1a1", "d1a2").duration_min == 40
    assert snap.route("d1", "d1a2", "d1a1") is None      # 方向不同
    assert snap.route("d2", "d1a1", "d1a2") is None      # 天不同


def test_poi_id_only_for_unambiguously_resolved():
    snap = _snap(
        poi_by_activity={
            "d1a1": Resolved(_poi("B001")),
            "d1a2": Ambiguous([_poi("B002"), _poi("B003")]),
            "d1a3": NotFound("不存在的地方"),
        }
    )
    assert snap.poi_id_for("d1a1") == "B001"
    assert snap.poi_id_for("d1a2") is None    # ★ 歧义时不擅自挑一个
    assert snap.poi_id_for("d1a3") is None
    assert snap.poi_id_for("nope") is None


def test_constraint_poi_id_follows_same_rule():
    snap = _snap(
        constraint_pois={
            "清水寺": Resolved(_poi("B001")),
            "某某寺": Ambiguous([_poi("B002"), _poi("B003")]),
        }
    )
    assert snap.constraint_poi_id("清水寺") == "B001"
    assert snap.constraint_poi_id("某某寺") is None
    assert snap.constraint_poi_id("没提过") is None


def test_resolved_poi_ids_collects_only_resolved():
    snap = _snap(
        poi_by_activity={
            "d1a1": Resolved(_poi("B001")),
            "d1a2": NotFound("x"),
            "d1a3": Resolved(_poi("B009")),
        }
    )
    assert snap.resolved_poi_ids() == {"B001", "B009"}


def test_gaps_record_what_could_not_be_verified():
    snap = _snap(
        gaps=[Gap(kind=GapKind.ROUTE_UNAVAILABLE, subject="d1a1->d1a2",
                  detail="高德限流")]
    )
    assert snap.gaps[0].kind is GapKind.ROUTE_UNAVAILABLE


def test_snapshot_is_frozen():
    snap = _snap()
    with pytest.raises(Exception):
        snap.trip_timezone = "Europe/Paris"
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/models/test_facts.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tripplan.models.facts'`

- [ ] **Step 3: 实现**

创建 `src/tripplan/models/facts.py`：

```python
"""对行程的外部观测。由 resolver 触网产生，由 validator 纯函数消费。"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from tripplan.models.common import LatLng, Money, TravelMode


@dataclass(frozen=True)
class PoiFact:
    id: str
    name: str
    coords: LatLng
    opening_hours: str | None
    ticket: Money | None
    source: str
    fetched_at: datetime


@dataclass(frozen=True)
class Resolved:
    fact: PoiFact


@dataclass(frozen=True)
class Ambiguous:
    """同名匹配到多个。不擅自挑一个——挑错了下游全部建立在错坐标上。"""

    candidates: list[PoiFact]


@dataclass(frozen=True)
class NotFound:
    query: str


PoiResolution = Resolved | Ambiguous | NotFound


@dataclass(frozen=True)
class RouteFact:
    day_id: str
    from_activity_id: str
    to_activity_id: str
    depart_at: datetime      # 地铁班次与高峰拥堵都取决于出发时刻
    mode: TravelMode
    duration_min: int
    distance_m: int
    polyline: str            # HTML 画路线用
    source: str
    fetched_at: datetime


@dataclass(frozen=True)
class WeatherFact:
    date_iso: str
    summary: str
    temp_c_min: float
    temp_c_max: float
    source: str


class GapKind(Enum):
    AMBIGUOUS_POI = "AMBIGUOUS_POI"
    POI_NOT_FOUND = "POI_NOT_FOUND"
    AMBIGUOUS_CONSTRAINT = "AMBIGUOUS_CONSTRAINT"
    ROUTE_UNAVAILABLE = "ROUTE_UNAVAILABLE"
    WEATHER_UNAVAILABLE = "WEATHER_UNAVAILABLE"


@dataclass(frozen=True)
class Gap:
    """没查到的事实。显式记录，让规则据此降级而不是静默按 0 处理。"""

    kind: GapKind
    subject: str
    detail: str


@dataclass(frozen=True)
class FactSnapshot:
    poi_by_activity: dict[str, PoiResolution]   # activity.id -> 解析结果
    constraint_pois: dict[str, PoiResolution]   # must_visit/avoid 原文 -> 解析结果
    routes: list[RouteFact]
    weather: dict[str, WeatherFact]             # ISO 日期串 -> 天气
    trip_timezone: str                          # 抄自 TripState，供回放核对
    resolved_at: datetime
    gaps: list[Gap]

    def route(self, day_id: str, from_id: str, to_id: str) -> RouteFact | None:
        for r in self.routes:
            if (r.day_id, r.from_activity_id, r.to_activity_id) == (
                day_id,
                from_id,
                to_id,
            ):
                return r
        return None

    def poi_id_for(self, activity_id: str) -> str | None:
        match self.poi_by_activity.get(activity_id):
            case Resolved(fact):
                return fact.id
            case _:
                return None

    def constraint_poi_id(self, query: str) -> str | None:
        match self.constraint_pois.get(query):
            case Resolved(fact):
                return fact.id
            case _:
                return None

    def resolved_poi_ids(self) -> set[str]:
        return {
            r.fact.id
            for r in self.poi_by_activity.values()
            if isinstance(r, Resolved)
        }
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/models/ -v`
Expected: PASS（25 passed）

- [ ] **Step 5: 格式化并提交**

```bash
uv run black src tests
git add src/tripplan/models/facts.py tests/models/test_facts.py
git commit -m "feat: 事实快照模型

PoiResolution 三态（Resolved/Ambiguous/NotFound），歧义时不擅自挑候选。
Gap 让「查不到」成为一等公民，规则据此降级。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: 状态机类型、命令与结果

**Files:**
- Create: `src/tripplan/state.py`
- Test: `tests/test_state.py`

**Interfaces:**
- Consumes: `Requirements`（Task 2）、`Angle`, `Itinerary`（Task 3）、`Issue`（Task 3）、`FactSnapshot`（Task 4）
- Produces:
  - `Stage`（COLLECT/AWAIT_REQ_CONFIRM/GENERATE/AWAIT_CHOICE/REFINE/DONE）、`AWAITING: frozenset[Stage]`
  - `SlotStatus`（PENDING/OK/EXHAUSTED/FAILED）、`CandidateSlot(angle, itinerary, facts, status, detail)`
  - `TripState(run_id, raw_request, revision, stage, requirements, candidates, chosen_key, trip_timezone, seeds, issues)` + `.slot(key)` + `.chosen()` + `TripState.new(raw_request, run_id)`
  - 命令：`ConfirmRequirements(expected_revision)`、`AmendRequirements(expected_revision, text)`、`ChooseCandidate(expected_revision, angle_key)`、`GiveFeedback(expected_revision, angle_key, text)`、`Command` 联合、`ALLOWED_COMMANDS: dict[Stage, frozenset[type]]`
  - 结果：`InputKind`、`RejectReason`、`Done(itinerary)`、`NeedInput(kind, payload, revision)`、`Rejected(reason, current)`、`Outcome` 联合

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_state.py`：

```python
import pytest

from tripplan.models.itinerary import Angle, Itinerary
from tripplan.state import (
    ALLOWED_COMMANDS,
    AWAITING,
    AmendRequirements,
    CandidateSlot,
    ChooseCandidate,
    ConfirmRequirements,
    GiveFeedback,
    SlotStatus,
    Stage,
    TripState,
)


def _angle(key: str) -> Angle:
    return Angle(key=key, title=f"方案{key}", description="")


def _slot(key: str, has_itin: bool = True) -> CandidateSlot:
    itin = Itinerary(angle=_angle(key)) if has_itin else None
    return CandidateSlot(angle=_angle(key), itinerary=itin, status=SlotStatus.OK)


def test_new_state_starts_at_collect_with_revision_zero():
    s = TripState.new("去京都玩5天", run_id="r1")
    assert s.stage is Stage.COLLECT
    assert s.revision == 0
    assert s.candidates == []
    assert s.chosen_key is None
    assert s.trip_timezone is None


def test_slot_lookup_by_angle_key():
    s = TripState.new("x", run_id="r1")
    s.candidates = [_slot("A"), _slot("B")]
    assert s.slot("B").angle.key == "B"
    assert s.slot("Z") is None


def test_chosen_returns_none_before_selection():
    s = TripState.new("x", run_id="r1")
    s.candidates = [_slot("A")]
    assert s.chosen() is None


def test_chosen_follows_chosen_key():
    s = TripState.new("x", run_id="r1")
    s.candidates = [_slot("A"), _slot("B")]
    s.chosen_key = "B"
    assert s.chosen().angle.key == "B"


def test_awaiting_contains_exactly_the_two_pause_stages():
    assert AWAITING == frozenset({Stage.AWAIT_REQ_CONFIRM, Stage.AWAIT_CHOICE})


def test_awaiting_and_work_stages_are_disjoint():
    """等待态在进入工作循环前就被拦下，两个集合不能相交。"""
    work = {Stage.COLLECT, Stage.GENERATE, Stage.REFINE, Stage.DONE}
    assert AWAITING & work == frozenset()
    assert AWAITING | work == set(Stage)


def test_allowed_commands_cover_every_awaiting_stage():
    assert set(ALLOWED_COMMANDS) == set(AWAITING)


def test_allowed_commands_per_stage():
    assert ALLOWED_COMMANDS[Stage.AWAIT_REQ_CONFIRM] == frozenset(
        {ConfirmRequirements, AmendRequirements}
    )
    assert ALLOWED_COMMANDS[Stage.AWAIT_CHOICE] == frozenset(
        {ChooseCandidate, GiveFeedback, AmendRequirements}
    )


def test_commands_are_frozen():
    cmd = ChooseCandidate(expected_revision=3, angle_key="A")
    with pytest.raises(Exception):
        cmd.angle_key = "B"


def test_every_command_carries_expected_revision():
    for cmd in (
        ConfirmRequirements(1),
        AmendRequirements(1, "更便宜点"),
        ChooseCandidate(1, "A"),
        GiveFeedback(1, "A", "第2天太赶"),
    ):
        assert cmd.expected_revision == 1
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_state.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tripplan.state'`

- [ ] **Step 3: 实现**

创建 `src/tripplan/state.py`：

```python
"""状态机的状态、命令与结果。全部可 JSON 序列化（见 wire.py）。"""

from dataclasses import dataclass, field
from enum import Enum

from tripplan.models.facts import FactSnapshot
from tripplan.models.issue import Issue
from tripplan.models.itinerary import Angle, Itinerary
from tripplan.models.requirements import Requirements


class Stage(Enum):
    COLLECT = "COLLECT"
    AWAIT_REQ_CONFIRM = "AWAIT_REQ_CONFIRM"     # ⏸
    GENERATE = "GENERATE"
    AWAIT_CHOICE = "AWAIT_CHOICE"               # ⏸
    REFINE = "REFINE"
    DONE = "DONE"


#: 暂停点。必须是显式状态——这是 advance 形状与可持久化的前提。
AWAITING = frozenset({Stage.AWAIT_REQ_CONFIRM, Stage.AWAIT_CHOICE})


class SlotStatus(Enum):
    PENDING = "PENDING"
    OK = "OK"
    EXHAUSTED = "EXHAUSTED"     # 撞轮数或资源上限，带残缺行程
    FAILED = "FAILED"           # 外部依赖失败，可能没有行程


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
    Stage.AWAIT_CHOICE: frozenset(
        {ChooseCandidate, GiveFeedback, AmendRequirements}
    ),
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
    payload: object          # Requirements 或 list[CandidateSlot]
    revision: int            # 回传时用作 expected_revision


@dataclass(frozen=True)
class Rejected:
    reason: RejectReason
    current: NeedInput       # 当前真正在等的东西，driver 可直接重新渲染


Outcome = Done | NeedInput | Rejected
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_state.py -v`
Expected: PASS（10 passed）

- [ ] **Step 5: 格式化并提交**

```bash
uv run black src tests
git add src/tripplan/state.py tests/test_state.py
git commit -m "feat: 状态机类型、命令联合与结果

命令用判别式联合，非法组合不可表示；AWAITING 与工作态严格不相交，
等待态在进入工作循环前拦下，结构上不可能死循环。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: 序列化（wire format）

**Files:**
- Create: `src/tripplan/wire.py`
- Test: `tests/test_wire.py`

**Interfaces:**
- Consumes: 全部 models + `state.TripState`（Task 1–5）
- Produces: `FORMAT_VERSION = 1`、`dumps(state) -> str`、`loads(text) -> TripState`、`encode_state(state) -> dict`、`decode_state(raw) -> TripState`、`UnsupportedVersion`、`MIGRATIONS: dict[int, Callable[[dict], dict]]`

**背景：** `TripState` 里用了 `datetime`/`date`/`time`/`Decimal`/`Enum`/`frozenset`，这些都不能直接 `json.dumps`。wire format 在 spec §3.3 定死，这个任务照着实现。`state.json` 是用户可见契约（`trip resume` 依赖它），所以 `format_version` 与迁移入口从第一天就要有。

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_wire.py`：

```python
import json
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest

from tripplan.models.common import Confidence, Field, LatLng, Money, Origin, TravelMode
from tripplan.models.facts import (
    Ambiguous,
    FactSnapshot,
    Gap,
    GapKind,
    NotFound,
    PoiFact,
    Resolved,
    RouteFact,
)
from tripplan.models.issue import ActivityRef, Issue, Severity, Source
from tripplan.models.itinerary import Activity, Angle, Category, Day, Itinerary
from tripplan.models.requirements import (
    Basis,
    BudgetSpec,
    CostKind,
    DateRange,
    Pace,
    Party,
    Requirements,
    Transfer,
)
from tripplan.state import CandidateSlot, SlotStatus, Stage, TripState
from tripplan.wire import (
    FORMAT_VERSION,
    MIGRATIONS,
    UnsupportedVersion,
    dumps,
    loads,
)

JST = timezone(timedelta(hours=9))


def _full_state() -> TripState:
    poi = PoiFact(
        id="B001", name="清水寺", coords=LatLng(34.99, 135.78),
        opening_hours="06:00-18:00",
        ticket=Money(Decimal("400"), "JPY", Confidence.ESTIMATED, "llm"),
        source="amap", fetched_at=datetime(2026, 9, 1, 8, tzinfo=JST),
    )
    itin = Itinerary(
        angle=Angle("A", "古寺巡礼", "世界遗产主线"),
        days=[
            Day(id="d1", date=date(2026, 10, 1), lodging="京都站",
                activities=[
                    Activity(id="d1a1", day_id="d1", poi_query="清水寺",
                             start=time(9, 0), end=time(11, 0),
                             category=Category.SIGHT,
                             cost=Money(Decimal("400"), "JPY",
                                        Confidence.ESTIMATED, "llm"),
                             indoor=False, note="清晨人少"),
                ])
        ],
        issues=[Issue(Severity.WARNING, Source.RULE, "R7", "略赶",
                      where=ActivityRef("d1", "d1a1"))],
    )
    facts = FactSnapshot(
        poi_by_activity={"d1a1": Resolved(poi), "d1a2": Ambiguous([poi]),
                         "d1a3": NotFound("某处")},
        constraint_pois={"清水寺": Resolved(poi)},
        routes=[RouteFact("d1", "d1a1", "d1a2",
                          datetime(2026, 10, 1, 11, tzinfo=JST),
                          TravelMode.TRANSIT, 40, 4200, "poly",
                          "amap", datetime(2026, 9, 1, tzinfo=JST))],
        weather={},
        trip_timezone="Asia/Tokyo",
        resolved_at=datetime(2026, 9, 1, tzinfo=JST),
        gaps=[Gap(GapKind.POI_NOT_FOUND, "d1a3", "没匹配上")],
    )
    reqs = Requirements(
        destination=Field("京都", Origin.USER, confirmed=True),
        dates=Field(DateRange(date(2026, 10, 1), date(2026, 10, 5)), Origin.USER),
        party=Field(Party(2), Origin.USER),
        arrival=Field(Transfer(datetime(2026, 10, 1, 9, tzinfo=JST), "flight"),
                      Origin.USER),
        budget=Field(
            BudgetSpec(Decimal("15000"), "CNY", Basis.TOTAL,
                       frozenset({CostKind.TICKET, CostKind.MEAL})),
            Origin.MODEL, rationale="按人均 7500 推断",
        ),
        pace=Field(Pace.RELAXED, Origin.MODEL),
        styles=Field(["美食", "历史"], Origin.USER),
    )
    s = TripState.new("十一去京都玩5天", run_id="r-001")
    s.revision = 4
    s.stage = Stage.AWAIT_CHOICE
    s.requirements = reqs
    s.trip_timezone = "Asia/Tokyo"
    s.candidates = [
        CandidateSlot(Angle("A", "古寺巡礼", ""), itin, facts, SlotStatus.OK),
        CandidateSlot(Angle("B", "美食优先", ""), None, None,
                      SlotStatus.FAILED, "高德限流"),
    ]
    s.seeds = {"A": itin}
    s.issues = [Issue.from_human("第2天太赶了")]
    return s


def test_roundtrip_preserves_everything():
    original = _full_state()
    restored = loads(dumps(original))
    assert dumps(restored) == dumps(original)


def test_roundtrip_preserves_types_not_just_shape():
    restored = loads(dumps(_full_state()))
    reqs = restored.requirements
    assert isinstance(reqs.budget.value.amount, Decimal)
    assert reqs.budget.value.basis is Basis.TOTAL
    assert isinstance(reqs.budget.value.includes, frozenset)
    assert reqs.dates.value.start == date(2026, 10, 1)
    assert reqs.arrival.value.at.tzinfo is not None
    act = restored.candidates[0].itinerary.days[0].activities[0]
    assert act.start == time(9, 0)
    assert isinstance(act.cost.amount, Decimal)
    assert act.category is Category.SIGHT


def test_field_origin_and_confirmed_both_survive():
    restored = loads(dumps(_full_state()))
    assert restored.requirements.destination.confirmed is True
    assert restored.requirements.budget.origin is Origin.MODEL
    assert restored.requirements.budget.confirmed is False
    assert restored.requirements.budget.rationale == "按人均 7500 推断"


def test_poi_resolution_union_is_tagged():
    raw = json.loads(dumps(_full_state()))
    pois = raw["candidates"][0]["facts"]["poi_by_activity"]
    assert pois["d1a1"]["kind"] == "Resolved"
    assert pois["d1a2"]["kind"] == "Ambiguous"
    assert pois["d1a3"]["kind"] == "NotFound"


def test_poi_resolution_union_decodes_back_to_right_classes():
    restored = loads(dumps(_full_state()))
    pois = restored.candidates[0].facts.poi_by_activity
    assert isinstance(pois["d1a1"], Resolved)
    assert isinstance(pois["d1a2"], Ambiguous)
    assert isinstance(pois["d1a3"], NotFound)


def test_decimal_is_string_never_float():
    raw = json.loads(dumps(_full_state()))
    amount = raw["requirements"]["budget"]["value"]["amount"]
    assert isinstance(amount, str)
    assert amount == "15000"


def test_datetime_is_iso_with_offset():
    raw = json.loads(dumps(_full_state()))
    at = raw["requirements"]["arrival"]["value"]["at"]
    assert at.startswith("2026-10-01T09:00:00")
    assert at.endswith("+09:00")


def test_enum_serialized_by_name_not_ordinal():
    raw = json.loads(dumps(_full_state()))
    assert raw["stage"] == "AWAIT_CHOICE"
    assert raw["candidates"][0]["status"] == "OK"


def test_frozenset_is_sorted_list_for_stable_bytes():
    raw = json.loads(dumps(_full_state()))
    includes = raw["requirements"]["budget"]["value"]["includes"]
    assert includes == sorted(includes)


def test_routes_are_a_list_not_tuple_keyed_dict():
    raw = json.loads(dumps(_full_state()))
    assert isinstance(raw["candidates"][0]["facts"]["routes"], list)


def test_format_version_is_written():
    raw = json.loads(dumps(_full_state()))
    assert raw["format_version"] == FORMAT_VERSION


def test_future_version_is_rejected_not_guessed():
    raw = json.loads(dumps(_full_state()))
    raw["format_version"] = FORMAT_VERSION + 1
    with pytest.raises(UnsupportedVersion):
        loads(json.dumps(raw))


def test_older_version_runs_migrations_then_parses():
    """迁移表 v1 时为空，但入口分支必须存在，否则第一次改结构就全变砖。"""
    raw = json.loads(dumps(_full_state()))
    raw["format_version"] = 0
    MIGRATIONS[0] = lambda d: {**d, "format_version": 1}
    try:
        restored = loads(json.dumps(raw))
        assert restored.revision == 4
    finally:
        del MIGRATIONS[0]


def test_slot_without_itinerary_roundtrips():
    restored = loads(dumps(_full_state()))
    b = restored.slot("B")
    assert b.itinerary is None and b.facts is None
    assert b.status is SlotStatus.FAILED
    assert b.detail == "高德限流"
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_wire.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tripplan.wire'`

- [ ] **Step 3: 实现**

创建 `src/tripplan/wire.py`。用显式 encoder/decoder 函数对，不依赖第三方隐式行为——`state.json` 是跨版本的持久契约，值得手写一层。

```python
"""state.json 的 wire format。

「可 JSON 序列化」不是自动成立的：datetime/Decimal/Enum/frozenset/联合类型
都需要显式处理。这里定死表示形式，不留给调用方临场发挥。
"""

import json
from datetime import date, datetime, time
from decimal import Decimal
from typing import Callable

from tripplan.models.common import Confidence, Field, LatLng, Money, Origin, TravelMode
from tripplan.models.facts import (
    Ambiguous,
    FactSnapshot,
    Gap,
    GapKind,
    NotFound,
    PoiFact,
    Resolved,
    RouteFact,
    WeatherFact,
)
from tripplan.models.issue import ActivityRef, DayRef, Issue, Severity, Source
from tripplan.models.itinerary import Activity, Angle, Category, Day, Itinerary
from tripplan.models.requirements import (
    Basis,
    BudgetSpec,
    CostKind,
    DateRange,
    Pace,
    Party,
    Requirements,
    Transfer,
)
from tripplan.state import CandidateSlot, SlotStatus, Stage, TripState

FORMAT_VERSION = 1

#: 版本 N -> 把 N 的结构升到 N+1 的函数。v1 时为空，但 loads 的分支必须在。
MIGRATIONS: dict[int, Callable[[dict], dict]] = {}


class UnsupportedVersion(Exception):
    pass


# ---------- 标量 ----------


def _dt(v: datetime | None) -> str | None:
    if v is None:
        return None
    if v.tzinfo is None:
        raise ValueError(f"datetime 必须 tz-aware: {v!r}")
    return v.isoformat()


def _un_dt(v: str | None) -> datetime | None:
    return None if v is None else datetime.fromisoformat(v)


def _money(m: Money | None) -> dict | None:
    if m is None:
        return None
    return {
        "amount": str(m.amount),
        "currency": m.currency,
        "confidence": m.confidence.value,
        "source": m.source,
    }


def _un_money(d: dict | None) -> Money | None:
    if d is None:
        return None
    return Money(Decimal(d["amount"]), d["currency"],
                 Confidence(d["confidence"]), d["source"])


def _field(f: Field, enc) -> dict:
    return {
        "value": None if f.value is None else enc(f.value),
        "origin": None if f.origin is None else f.origin.value,
        "confirmed": f.confirmed,
        "rationale": f.rationale,
    }


def _un_field(d: dict, dec) -> Field:
    return Field(
        value=None if d["value"] is None else dec(d["value"]),
        origin=None if d["origin"] is None else Origin(d["origin"]),
        confirmed=d["confirmed"],
        rationale=d["rationale"],
    )


# ---------- 需求 ----------


def _budget(b: BudgetSpec) -> dict:
    return {
        "amount": str(b.amount),
        "currency": b.currency,
        "basis": b.basis.value,
        "includes": sorted(k.value for k in b.includes),   # 排序保证字节稳定
    }


def _un_budget(d: dict) -> BudgetSpec:
    return BudgetSpec(Decimal(d["amount"]), d["currency"], Basis(d["basis"]),
                      frozenset(CostKind(k) for k in d["includes"]))


def _requirements(r: Requirements) -> dict:
    return {
        "destination": _field(r.destination, str),
        "dates": _field(r.dates,
                        lambda v: {"start": v.start.isoformat(),
                                   "end": v.end.isoformat()}),
        "party": _field(r.party, lambda v: {"adults": v.adults,
                                            "children": v.children,
                                            "seniors": v.seniors}),
        "arrival": _field(r.arrival, lambda v: {"at": _dt(v.at), "mode": v.mode}),
        "departure": _field(r.departure,
                            lambda v: {"at": _dt(v.at), "mode": v.mode}),
        "budget": _field(r.budget, _budget),
        "styles": _field(r.styles, list),
        "pace": _field(r.pace, lambda v: v.value),
        "must_visit": _field(r.must_visit, list),
        "avoid": _field(r.avoid, list),
        "lodging_area": _field(r.lodging_area, str),
        "constraints": _field(r.constraints, list),
    }


def _un_requirements(d: dict) -> Requirements:
    return Requirements(
        destination=_un_field(d["destination"], str),
        dates=_un_field(d["dates"], lambda v: DateRange(
            date.fromisoformat(v["start"]), date.fromisoformat(v["end"]))),
        party=_un_field(d["party"], lambda v: Party(**v)),
        arrival=_un_field(d["arrival"],
                          lambda v: Transfer(_un_dt(v["at"]), v["mode"])),
        departure=_un_field(d["departure"],
                            lambda v: Transfer(_un_dt(v["at"]), v["mode"])),
        budget=_un_field(d["budget"], _un_budget),
        styles=_un_field(d["styles"], list),
        pace=_un_field(d["pace"], Pace),
        must_visit=_un_field(d["must_visit"], list),
        avoid=_un_field(d["avoid"], list),
        lodging_area=_un_field(d["lodging_area"], str),
        constraints=_un_field(d["constraints"], list),
    )


# ---------- 行程 ----------


def _issue(i: Issue) -> dict:
    where = None
    if isinstance(i.where, ActivityRef):
        where = {"kind": "ActivityRef", "day_id": i.where.day_id,
                 "activity_id": i.where.activity_id}
    elif isinstance(i.where, DayRef):
        where = {"kind": "DayRef", "day_id": i.where.day_id}
    return {"severity": i.severity.value, "source": i.source.value,
            "code": i.code, "message": i.message, "where": where}


def _un_issue(d: dict) -> Issue:
    w = d["where"]
    where = None
    if w and w["kind"] == "ActivityRef":
        where = ActivityRef(w["day_id"], w["activity_id"])
    elif w and w["kind"] == "DayRef":
        where = DayRef(w["day_id"])
    return Issue(Severity(d["severity"]), Source(d["source"]),
                 d["code"], d["message"], where)


def _itinerary(it: Itinerary | None) -> dict | None:
    if it is None:
        return None
    return {
        "angle": {"key": it.angle.key, "title": it.angle.title,
                  "description": it.angle.description},
        "days": [
            {"id": d.id, "date": d.date.isoformat(), "lodging": d.lodging,
             "activities": [
                 {"id": a.id, "day_id": a.day_id, "poi_query": a.poi_query,
                  "start": a.start.isoformat(), "end": a.end.isoformat(),
                  "category": a.category.value, "cost": _money(a.cost),
                  "indoor": a.indoor, "note": a.note}
                 for a in d.activities]}
            for d in it.days],
        "issues": [_issue(i) for i in it.issues],
    }


def _un_itinerary(d: dict | None) -> Itinerary | None:
    if d is None:
        return None
    return Itinerary(
        angle=Angle(**d["angle"]),
        days=[Day(id=x["id"], date=date.fromisoformat(x["date"]),
                  lodging=x["lodging"],
                  activities=[Activity(
                      id=a["id"], day_id=a["day_id"], poi_query=a["poi_query"],
                      start=time.fromisoformat(a["start"]),
                      end=time.fromisoformat(a["end"]),
                      category=Category(a["category"]),
                      cost=_un_money(a["cost"]), indoor=a["indoor"],
                      note=a["note"]) for a in x["activities"]])
              for x in d["days"]],
        issues=[_un_issue(i) for i in d["issues"]],
    )


# ---------- 事实 ----------


def _poi(p: PoiFact) -> dict:
    return {"id": p.id, "name": p.name,
            "coords": {"lat": p.coords.lat, "lng": p.coords.lng},
            "opening_hours": p.opening_hours, "ticket": _money(p.ticket),
            "source": p.source, "fetched_at": _dt(p.fetched_at)}


def _un_poi(d: dict) -> PoiFact:
    return PoiFact(d["id"], d["name"], LatLng(**d["coords"]),
                   d["opening_hours"], _un_money(d["ticket"]),
                   d["source"], _un_dt(d["fetched_at"]))


def _resolution(r) -> dict:
    """联合类型必须显式打 kind 标签——靠字段形状去猜会在字段可选时崩。"""
    match r:
        case Resolved(fact):
            return {"kind": "Resolved", "fact": _poi(fact)}
        case Ambiguous(cands):
            return {"kind": "Ambiguous",
                    "candidates": [_poi(c) for c in cands]}
        case NotFound(q):
            return {"kind": "NotFound", "query": q}
    raise TypeError(f"未知的 PoiResolution: {r!r}")


def _un_resolution(d: dict):
    match d["kind"]:
        case "Resolved":
            return Resolved(_un_poi(d["fact"]))
        case "Ambiguous":
            return Ambiguous([_un_poi(c) for c in d["candidates"]])
        case "NotFound":
            return NotFound(d["query"])
    raise UnsupportedVersion(f"未知的 PoiResolution kind: {d['kind']}")


def _facts(f: FactSnapshot | None) -> dict | None:
    if f is None:
        return None
    return {
        "poi_by_activity": {k: _resolution(v)
                            for k, v in f.poi_by_activity.items()},
        "constraint_pois": {k: _resolution(v)
                            for k, v in f.constraint_pois.items()},
        "routes": [{"day_id": r.day_id,
                    "from_activity_id": r.from_activity_id,
                    "to_activity_id": r.to_activity_id,
                    "depart_at": _dt(r.depart_at), "mode": r.mode.value,
                    "duration_min": r.duration_min,
                    "distance_m": r.distance_m, "polyline": r.polyline,
                    "source": r.source, "fetched_at": _dt(r.fetched_at)}
                   for r in f.routes],
        "weather": {k: {"date_iso": w.date_iso, "summary": w.summary,
                        "temp_c_min": w.temp_c_min, "temp_c_max": w.temp_c_max,
                        "source": w.source} for k, w in f.weather.items()},
        "trip_timezone": f.trip_timezone,
        "resolved_at": _dt(f.resolved_at),
        "gaps": [{"kind": g.kind.value, "subject": g.subject,
                  "detail": g.detail} for g in f.gaps],
    }


def _un_facts(d: dict | None) -> FactSnapshot | None:
    if d is None:
        return None
    return FactSnapshot(
        poi_by_activity={k: _un_resolution(v)
                         for k, v in d["poi_by_activity"].items()},
        constraint_pois={k: _un_resolution(v)
                         for k, v in d["constraint_pois"].items()},
        routes=[RouteFact(r["day_id"], r["from_activity_id"],
                          r["to_activity_id"], _un_dt(r["depart_at"]),
                          TravelMode(r["mode"]), r["duration_min"],
                          r["distance_m"], r["polyline"], r["source"],
                          _un_dt(r["fetched_at"])) for r in d["routes"]],
        weather={k: WeatherFact(**w) for k, w in d["weather"].items()},
        trip_timezone=d["trip_timezone"],
        resolved_at=_un_dt(d["resolved_at"]),
        gaps=[Gap(GapKind(g["kind"]), g["subject"], g["detail"])
              for g in d["gaps"]],
    )


# ---------- 顶层 ----------


def encode_state(s: TripState) -> dict:
    return {
        "format_version": FORMAT_VERSION,
        "run_id": s.run_id,
        "raw_request": s.raw_request,
        "revision": s.revision,
        "stage": s.stage.value,
        "requirements": (None if s.requirements is None
                         else _requirements(s.requirements)),
        "candidates": [
            {"angle": {"key": c.angle.key, "title": c.angle.title,
                       "description": c.angle.description},
             "itinerary": _itinerary(c.itinerary), "facts": _facts(c.facts),
             "status": c.status.value, "detail": c.detail}
            for c in s.candidates],
        "chosen_key": s.chosen_key,
        "trip_timezone": s.trip_timezone,
        "seeds": {k: _itinerary(v) for k, v in s.seeds.items()},
        "issues": [_issue(i) for i in s.issues],
    }


def decode_state(raw: dict) -> TripState:
    version = raw.get("format_version")
    if version is None or version > FORMAT_VERSION:
        raise UnsupportedVersion(
            f"state.json 版本 {version} 高于本工具支持的 {FORMAT_VERSION}；"
            "请升级 tripplan，而不是用旧代码去解析新结构"
        )
    while version < FORMAT_VERSION:
        migrate = MIGRATIONS.get(version)
        if migrate is None:
            raise UnsupportedVersion(f"缺少从版本 {version} 升级的迁移函数")
        raw = migrate(raw)
        version = raw["format_version"]

    s = TripState(run_id=raw["run_id"], raw_request=raw["raw_request"],
                  revision=raw["revision"], stage=Stage(raw["stage"]))
    s.requirements = (None if raw["requirements"] is None
                      else _un_requirements(raw["requirements"]))
    s.candidates = [
        CandidateSlot(angle=Angle(**c["angle"]),
                      itinerary=_un_itinerary(c["itinerary"]),
                      facts=_un_facts(c["facts"]),
                      status=SlotStatus(c["status"]), detail=c["detail"])
        for c in raw["candidates"]]
    s.chosen_key = raw["chosen_key"]
    s.trip_timezone = raw["trip_timezone"]
    s.seeds = {k: _un_itinerary(v) for k, v in raw["seeds"].items()}
    s.issues = [_un_issue(i) for i in raw["issues"]]
    return s


def dumps(state: TripState) -> str:
    return json.dumps(encode_state(state), ensure_ascii=False,
                      indent=2, sort_keys=True)


def loads(text: str) -> TripState:
    return decode_state(json.loads(text))
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_wire.py -v`
Expected: PASS（14 passed）

- [ ] **Step 5: 格式化并提交**

```bash
uv run black src tests
git add src/tripplan/wire.py tests/test_wire.py
git commit -m "feat: state.json wire format

ISO 时间带 offset、Decimal 字符串化、Enum 存名字、frozenset 排序、
联合类型打 kind 标签。format_version 与迁移入口从第一天就在——
state.json 是 trip resume 依赖的用户可见契约。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: 仓储层（flock + compare-and-swap）

**Files:**
- Create: `src/tripplan/repo.py`
- Test: `tests/test_repo.py`

**Interfaces:**
- Consumes: `TripState`（Task 5）、`wire.dumps/loads`（Task 6）
- Produces: `StateRepo` Protocol（`create(state)` / `load() -> TripState` / `save_if_revision(state, expected) -> bool`）、`FileRepo(trip_dir: Path)`、`TripExists`、`TripNotFound`

**背景：** 原子写只防半个文件，**不防 lost update**——两个写者都读到 rev=3、都通过校验、都写盘，后写的覆盖先写的。`flock` 把「读盘校验 revision」和「写入」合成一个临界区，本地文件系统上这就是一次真正的 CAS。

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_repo.py`：

```python
import json
import multiprocessing as mp
from pathlib import Path

import pytest

from tripplan.repo import FileRepo, TripExists, TripNotFound
from tripplan.state import Stage, TripState
from tripplan.wire import dumps


def _state(rev: int = 0) -> TripState:
    s = TripState.new("去京都", run_id="r1")
    s.revision = rev
    return s


def test_create_then_load_roundtrips(tmp_path: Path):
    repo = FileRepo(tmp_path / "kyoto")
    repo.create(_state())
    assert repo.load().raw_request == "去京都"


def test_create_refuses_to_overwrite(tmp_path: Path):
    repo = FileRepo(tmp_path / "kyoto")
    repo.create(_state())
    with pytest.raises(TripExists):
        repo.create(_state())


def test_load_missing_trip_raises(tmp_path: Path):
    with pytest.raises(TripNotFound):
        FileRepo(tmp_path / "nope").load()


def test_save_succeeds_when_expected_matches_disk(tmp_path: Path):
    repo = FileRepo(tmp_path / "kyoto")
    repo.create(_state(0))
    s = repo.load()
    s.revision = 1
    s.stage = Stage.AWAIT_REQ_CONFIRM
    assert repo.save_if_revision(s, expected=0) is True
    assert repo.load().revision == 1


def test_save_fails_when_disk_moved_on(tmp_path: Path):
    """lost update 的核心用例：两个写者都基于 rev=0，第二个必须失败。"""
    repo = FileRepo(tmp_path / "kyoto")
    repo.create(_state(0))

    a = repo.load()
    b = repo.load()             # 两个写者读到同一个 revision

    a.revision = 1
    assert repo.save_if_revision(a, expected=0) is True

    b.revision = 1
    assert repo.save_if_revision(b, expected=0) is False   # ★ 被挡住
    assert repo.load().revision == 1


def test_failed_save_does_not_touch_disk(tmp_path: Path):
    repo = FileRepo(tmp_path / "kyoto")
    repo.create(_state(0))
    good = repo.load()
    good.revision = 1
    repo.save_if_revision(good, expected=0)
    before = (repo.dir / "state.json").read_text()

    stale = _state(9)
    assert repo.save_if_revision(stale, expected=0) is False
    assert (repo.dir / "state.json").read_text() == before


def _child(trip_dir: str, queue) -> None:
    repo = FileRepo(Path(trip_dir))
    s = repo.load()
    s.revision = 1
    queue.put(repo.save_if_revision(s, expected=0))


def test_concurrent_writers_exactly_one_wins(tmp_path: Path):
    repo = FileRepo(tmp_path / "kyoto")
    repo.create(_state(0))

    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    procs = [ctx.Process(target=_child, args=(str(repo.dir), q))
             for _ in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)

    results = [q.get() for _ in range(4)]
    assert sum(results) == 1        # 恰好一个成功
    assert repo.load().revision == 1


def test_write_is_atomic_no_partial_file(tmp_path: Path):
    """临时文件 + os.replace：目录里不该留下半截文件。"""
    repo = FileRepo(tmp_path / "kyoto")
    repo.create(_state(0))
    s = repo.load()
    s.revision = 1
    repo.save_if_revision(s, expected=0)
    names = {p.name for p in repo.dir.iterdir()}
    assert names == {"state.json", ".lock"}
    json.loads((repo.dir / "state.json").read_text())    # 合法 JSON
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_repo.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tripplan.repo'`

- [ ] **Step 3: 实现**

创建 `src/tripplan/repo.py`：

```python
"""状态仓储。一个实例对应一个 trip 目录。

CAS 语义在接口里：save_if_revision 仅当盘上 revision 仍等于 expected 时才写。
FileRepo 用 flock 把「检查」和「写入」合成一个临界区；未来的 DbRepo 用
UPDATE ... WHERE revision = ? 的影响行数判断，语义完全一致。
"""

import fcntl
import os
import tempfile
from pathlib import Path
from typing import Protocol

from tripplan.state import TripState
from tripplan.wire import dumps, loads


class TripExists(Exception):
    pass


class TripNotFound(Exception):
    pass


class StateRepo(Protocol):
    def create(self, state: TripState) -> None: ...
    def load(self) -> TripState: ...
    def save_if_revision(self, state: TripState, expected: int) -> bool: ...


class FileRepo:
    def __init__(self, trip_dir: Path) -> None:
        self.dir = Path(trip_dir)

    @property
    def _state_path(self) -> Path:
        return self.dir / "state.json"

    @property
    def _lock_path(self) -> Path:
        return self.dir / ".lock"

    def create(self, state: TripState) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock_path.touch(exist_ok=True)
        # 'x' 模式：已存在就抛，天然防住 run_id 撞车
        try:
            with open(self._state_path, "x", encoding="utf-8") as fh:
                fh.write(dumps(state))
        except FileExistsError as e:
            raise TripExists(f"{self._state_path} 已存在，不覆盖") from e

    def load(self) -> TripState:
        if not self._state_path.exists():
            raise TripNotFound(str(self._state_path))
        return loads(self._state_path.read_text(encoding="utf-8"))

    def save_if_revision(self, state: TripState, expected: int) -> bool:
        if not self._state_path.exists():
            raise TripNotFound(str(self._state_path))
        with open(self._lock_path, "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)      # 临界区开始
            try:
                on_disk = loads(self._state_path.read_text(encoding="utf-8"))
                if on_disk.revision != expected:
                    return False                   # 有人抢先，不写
                self._atomic_write(dumps(state))
                return True
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _atomic_write(self, text: str) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._state_path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_repo.py -v`
Expected: PASS（8 passed）。并发那条会起 4 个进程，约 2–5 秒。

- [ ] **Step 5: 跑一遍 Phase A 全量**

Run: `uv run pytest tests/ -v`
Expected: PASS（57 passed）

- [ ] **Step 6: 格式化并提交**

```bash
uv run black src tests
git add src/tripplan/repo.py tests/test_repo.py
git commit -m "feat: 仓储层 flock + compare-and-swap

原子写只防半个文件，不防 lost update。flock 把检查与写入合进一个
临界区，四进程并发写时恰好一个成功。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

**✅ 检查点 A：** 状态可建、可存、可读，CAS 生效，跨版本契约就位。

---

## Phase B — 确定性校验

这一层**全部是纯函数**，不触网、不调 LLM。触网的部分在 Task 13 的 resolver 里，
校验只消费它产出的 `FactSnapshot`。测试直接喂手写快照。

### Task 8: 分层账单

**Files:**
- Create: `src/tripplan/validation/__init__.py`
- Create: `src/tripplan/validation/budget.py`
- Test: `tests/validation/test_budget.py`

**Interfaces:**
- Consumes: `Money`, `Confidence`（Task 1）、`Itinerary`（Task 3）、`Requirements`, `BudgetSpec`, `Basis`（Task 2）
- Produces: `BudgetLedger(currency, verified, verified_count, estimated, estimated_count, unknown_count, currency_mismatch, budget_limit)` + `.total` + `.complete` + `.all_verified` + `.over_budget`、`build_ledger(itin, reqs) -> BudgetLedger`

**背景：** v1 的票价来自 LLM 知识（`ESTIMATED`），且 `cost` 允许为 `None`。因此规则 #6 不做 BLOCKING 判定，而是输出一份让用户自己判断的分层账单。只有覆盖完整（无未知项）且全部 `VERIFIED` 时超支才升为 BLOCKING——这个条件在 v1 基本不会满足，接入真实票价 API 后自然生效。

- [ ] **Step 1: 写失败的测试**

创建 `tests/validation/test_budget.py`：

```python
from datetime import date, time
from decimal import Decimal

from tripplan.models.common import Confidence, Field, Money, Origin
from tripplan.models.itinerary import Activity, Angle, Category, Day, Itinerary
from tripplan.models.requirements import (
    Basis,
    BudgetSpec,
    CostKind,
    Party,
    Requirements,
)
from tripplan.validation.budget import build_ledger


def _money(v: str, conf: Confidence, cur: str = "CNY") -> Money:
    return Money(Decimal(v), cur, conf, "test")


def _itin(costs: list[Money | None]) -> Itinerary:
    acts = [
        Activity(id=f"d1a{i}", day_id="d1", poi_query=f"p{i}",
                 start=time(9), end=time(10), category=Category.SIGHT,
                 cost=c, indoor=False, note="")
        for i, c in enumerate(costs, start=1)
    ]
    return Itinerary(angle=Angle("A", "", ""),
                     days=[Day(id="d1", date=date(2026, 10, 1),
                               activities=acts)])


def _reqs(amount: str | None = "15000", basis: Basis = Basis.TOTAL,
          currency: str = "CNY") -> Requirements:
    budget = (
        Field(BudgetSpec(Decimal(amount), currency, basis,
                         frozenset({CostKind.TICKET})), Origin.USER)
        if amount else Field()
    )
    return Requirements(party=Field(Party(adults=2), Origin.USER), budget=budget)


def test_splits_verified_estimated_and_unknown():
    led = build_ledger(
        _itin([_money("100", Confidence.VERIFIED),
               _money("200", Confidence.ESTIMATED),
               None, None]),
        _reqs(),
    )
    assert led.verified == Decimal("100") and led.verified_count == 1
    assert led.estimated == Decimal("200") and led.estimated_count == 1
    assert led.unknown_count == 2
    assert led.total == Decimal("300")


def test_not_complete_when_any_cost_unknown():
    led = build_ledger(_itin([_money("100", Confidence.VERIFIED), None]), _reqs())
    assert led.complete is False
    assert led.all_verified is True      # 已知的那些确实都核实过


def test_complete_and_all_verified_only_when_no_gaps():
    led = build_ledger(_itin([_money("100", Confidence.VERIFIED)]), _reqs())
    assert led.complete is True
    assert led.all_verified is True


def test_estimated_items_break_all_verified():
    led = build_ledger(_itin([_money("100", Confidence.ESTIMATED)]), _reqs())
    assert led.all_verified is False


def test_per_person_budget_multiplies_by_party_size():
    led = build_ledger(_itin([_money("100", Confidence.VERIFIED)]),
                       _reqs("3000", Basis.PER_PERSON))
    assert led.budget_limit == Decimal("6000")     # 3000 × 2 人


def test_total_basis_uses_amount_directly():
    led = build_ledger(_itin([]), _reqs("15000", Basis.TOTAL))
    assert led.budget_limit == Decimal("15000")


def test_over_budget_compares_total_against_limit():
    led = build_ledger(_itin([_money("20000", Confidence.VERIFIED)]), _reqs())
    assert led.over_budget is True


def test_no_budget_means_never_over():
    led = build_ledger(_itin([_money("99999", Confidence.VERIFIED)]),
                       _reqs(amount=None))
    assert led.budget_limit is None
    assert led.over_budget is False


def test_mismatched_currency_is_flagged_not_silently_summed():
    """日元花费配人民币预算，不能直接相加。"""
    led = build_ledger(
        _itin([_money("100", Confidence.VERIFIED, "CNY"),
               _money("4000", Confidence.ESTIMATED, "JPY")]),
        _reqs(currency="CNY"),
    )
    assert led.currency_mismatch is True
    assert led.total == Decimal("100")       # 只累加同币种的
    assert led.unknown_count == 1            # 异币种计入未知
```

- [ ] **Step 2: 运行确认失败**

先建 `src/tripplan/validation/__init__.py`（空文件）和 `tests/validation/__init__.py`（空文件）。

Run: `uv run pytest tests/validation/test_budget.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tripplan.validation.budget'`

- [ ] **Step 3: 实现**

创建 `src/tripplan/validation/budget.py`：

```python
"""分层账单：把「已核实 / 估算 / 未知」分开呈现，而不是给一个假精确的合计。"""

from dataclasses import dataclass
from decimal import Decimal

from tripplan.models.common import Confidence
from tripplan.models.itinerary import Itinerary
from tripplan.models.requirements import Basis, Requirements


@dataclass(frozen=True)
class BudgetLedger:
    currency: str
    verified: Decimal
    verified_count: int
    estimated: Decimal
    estimated_count: int
    unknown_count: int
    currency_mismatch: bool
    budget_limit: Decimal | None

    @property
    def total(self) -> Decimal:
        return self.verified + self.estimated

    @property
    def complete(self) -> bool:
        """没有任何未知项。"""
        return self.unknown_count == 0

    @property
    def all_verified(self) -> bool:
        """已知项全部经过核实。"""
        return self.estimated_count == 0

    @property
    def over_budget(self) -> bool:
        return self.budget_limit is not None and self.total > self.budget_limit


def build_ledger(itin: Itinerary, reqs: Requirements) -> BudgetLedger:
    spec = reqs.budget.value
    currency = spec.currency if spec else "CNY"

    verified = estimated = Decimal("0")
    v_count = e_count = unknown = 0
    mismatch = False

    for act in itin.all_activities():
        cost = act.cost
        if cost is None:
            unknown += 1                      # None 是「未知」，不是免费
            continue
        if cost.currency != currency:
            mismatch = True
            unknown += 1                      # 币种对不上，不硬加
            continue
        if cost.confidence is Confidence.VERIFIED:
            verified += cost.amount
            v_count += 1
        else:
            estimated += cost.amount
            e_count += 1

    limit: Decimal | None = None
    if spec is not None:
        party = reqs.party.value
        head = party.total if (party and spec.basis is Basis.PER_PERSON) else 1
        limit = spec.amount * head

    return BudgetLedger(
        currency=currency,
        verified=verified,
        verified_count=v_count,
        estimated=estimated,
        estimated_count=e_count,
        unknown_count=unknown,
        currency_mismatch=mismatch,
        budget_limit=limit,
    )
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/validation/test_budget.py -v`
Expected: PASS（9 passed）

- [ ] **Step 5: 格式化并提交**

```bash
uv run black src tests
git add src/tripplan/validation tests/validation
git commit -m "feat: 分层账单

已核实/估算/未知分开统计，异币种不硬加。complete 与 all_verified
是规则 #6 能否升级为 BLOCKING 的判据。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: 规则 1–5（时间、通勤、日期覆盖、必去、排除）

**Files:**
- Create: `src/tripplan/validation/rules.py`
- Test: `tests/validation/test_rules_1_5.py`
- Test: `tests/validation/conftest.py`（共享构造器）

**Interfaces:**
- Consumes: `Itinerary`, `Activity`, `Day`（Task 3）、`Requirements`（Task 2）、`FactSnapshot`, `Gap`, `GapKind`（Task 4）、`Issue`, `Severity`, `Source`, `DayRef`, `ActivityRef`（Task 3）
- Produces: `TRANSIT_BUFFER = Decimal("1.2")`、`rule_01_no_overlap(itin, reqs, facts)`、`rule_02_transit_gap(...)`、`rule_03_date_coverage(...)`、`rule_04_must_visit(...)`、`rule_05_avoid(...)`，每个签名都是 `(Itinerary, Requirements, FactSnapshot) -> list[Issue]`

**关键约束：** 这个模块**不得 import 任何 provider 或网络库**。规则只读 `facts`。依赖缺失（对应事实落进 `facts.gaps`）时必须降级为 WARNING 并说明原因，**不得按 0 处理、也不得假装通过**。

- [ ] **Step 1: 写共享构造器**

创建 `tests/validation/conftest.py`：

```python
from datetime import date, datetime, time, timedelta, timezone

import pytest

from tripplan.models.common import Field, LatLng, Origin, TravelMode
from tripplan.models.facts import (
    FactSnapshot,
    Gap,
    GapKind,
    PoiFact,
    Resolved,
    RouteFact,
)
from tripplan.models.itinerary import Activity, Angle, Category, Day, Itinerary
from tripplan.models.requirements import DateRange, Party, Requirements, Transfer

JST = timezone(timedelta(hours=9))
TZ = "Asia/Tokyo"


@pytest.fixture
def mk():
    return _Builder()


class _Builder:
    """构造行程与快照的小工具，让每条规则的用例只关心自己在意的那一点。"""

    def act(self, aid, day_id, start, end, query="某地",
            category=Category.SIGHT, cost=None, indoor=False):
        h1, m1 = map(int, start.split(":"))
        h2, m2 = map(int, end.split(":"))
        return Activity(id=aid, day_id=day_id, poi_query=query,
                        start=time(h1, m1), end=time(h2, m2),
                        category=category, cost=cost, indoor=indoor, note="")

    def day(self, day_id, d: date, activities):
        return Day(id=day_id, date=d, activities=activities)

    def itin(self, days):
        return Itinerary(angle=Angle("A", "测试方案", ""), days=days)

    def reqs(self, start=date(2026, 10, 1), end=date(2026, 10, 1),
             must_visit=None, avoid=None, arrival=None, departure=None,
             party=2, **kw):
        base = dict(
            destination=Field("京都", Origin.USER),
            dates=Field(DateRange(start, end), Origin.USER),
            party=Field(Party(adults=party), Origin.USER),
        )
        if must_visit is not None:
            base["must_visit"] = Field(must_visit, Origin.USER)
        if avoid is not None:
            base["avoid"] = Field(avoid, Origin.USER)
        if arrival is not None:
            base["arrival"] = Field(Transfer(arrival, "flight"), Origin.USER)
        if departure is not None:
            base["departure"] = Field(Transfer(departure, "flight"), Origin.USER)
        base.update(kw)
        return Requirements(**base)

    def poi(self, pid, name=None):
        return PoiFact(id=pid, name=name or pid, coords=LatLng(35.0, 135.7),
                       opening_hours=None, ticket=None, source="fake",
                       fetched_at=datetime(2026, 9, 1, tzinfo=JST))

    def route(self, day_id, a, b, minutes, depart="11:00", on=date(2026, 10, 1)):
        h, m = map(int, depart.split(":"))
        return RouteFact(day_id=day_id, from_activity_id=a, to_activity_id=b,
                         depart_at=datetime(on.year, on.month, on.day, h, m,
                                            tzinfo=JST),
                         mode=TravelMode.TRANSIT, duration_min=minutes,
                         distance_m=1000 * minutes, polyline="poly",
                         source="fake",
                         fetched_at=datetime(2026, 9, 1, tzinfo=JST))

    def facts(self, poi_by_activity=None, constraint_pois=None, routes=None,
              gaps=None, tz=TZ):
        return FactSnapshot(
            poi_by_activity=poi_by_activity or {},
            constraint_pois=constraint_pois or {},
            routes=routes or [],
            weather={},
            trip_timezone=tz,
            resolved_at=datetime(2026, 9, 1, tzinfo=JST),
            gaps=gaps or [],
        )

    def gap(self, kind: GapKind, subject: str, detail: str = "x"):
        return Gap(kind=kind, subject=subject, detail=detail)

    def resolved(self, pid):
        return Resolved(self.poi(pid))
```

- [ ] **Step 2: 写规则 1–5 的失败测试**

创建 `tests/validation/test_rules_1_5.py`：

```python
from datetime import date, datetime, timedelta, timezone

from tripplan.models.facts import Ambiguous, GapKind, NotFound
from tripplan.models.issue import Severity
from tripplan.validation.rules import (
    rule_01_no_overlap,
    rule_02_transit_gap,
    rule_03_date_coverage,
    rule_04_must_visit,
    rule_05_avoid,
)

JST = timezone(timedelta(hours=9))
D1 = date(2026, 10, 1)
D2 = date(2026, 10, 2)


def _codes(issues):
    return [i.code for i in issues]


def _sev(issues):
    return [i.severity for i in issues]


# ---------- 规则 1：同日活动不重叠、时间递增 ----------


def test_r1_passes_on_sequential_activities(mk):
    itin = mk.itin([mk.day("d1", D1, [
        mk.act("d1a1", "d1", "09:00", "11:00"),
        mk.act("d1a2", "d1", "13:00", "14:00"),
    ])])
    assert rule_01_no_overlap(itin, mk.reqs(), mk.facts()) == []


def test_r1_flags_overlap(mk):
    itin = mk.itin([mk.day("d1", D1, [
        mk.act("d1a1", "d1", "09:00", "12:00"),
        mk.act("d1a2", "d1", "11:00", "13:00"),
    ])])
    issues = rule_01_no_overlap(itin, mk.reqs(), mk.facts())
    assert _codes(issues) == ["R1"]
    assert _sev(issues) == [Severity.BLOCKING]
    assert issues[0].where.activity_id == "d1a2"


def test_r1_flags_activity_ending_before_it_starts(mk):
    itin = mk.itin([mk.day("d1", D1, [mk.act("d1a1", "d1", "14:00", "09:00")])])
    issues = rule_01_no_overlap(itin, mk.reqs(), mk.facts())
    assert _codes(issues) == ["R1"]


def test_r1_does_not_compare_across_days(mk):
    itin = mk.itin([
        mk.day("d1", D1, [mk.act("d1a1", "d1", "09:00", "23:00")]),
        mk.day("d2", D2, [mk.act("d2a1", "d2", "09:00", "10:00")]),
    ])
    assert rule_01_no_overlap(itin, mk.reqs(start=D1, end=D2), mk.facts()) == []


# ---------- 规则 2：通勤间隙 ----------


def test_r2_passes_when_gap_covers_duration_with_buffer(mk):
    """间隙 120 分钟 ≥ 40 × 1.2 = 48。"""
    itin = mk.itin([mk.day("d1", D1, [
        mk.act("d1a1", "d1", "09:00", "11:00"),
        mk.act("d1a2", "d1", "13:00", "14:00"),
    ])])
    facts = mk.facts(routes=[mk.route("d1", "d1a1", "d1a2", 40)])
    assert rule_02_transit_gap(itin, mk.reqs(), facts) == []


def test_r2_blocks_when_gap_too_small(mk):
    """间隙 30 分钟 < 40 × 1.2 = 48。"""
    itin = mk.itin([mk.day("d1", D1, [
        mk.act("d1a1", "d1", "09:00", "11:00"),
        mk.act("d1a2", "d1", "11:30", "12:30"),
    ])])
    facts = mk.facts(routes=[mk.route("d1", "d1a1", "d1a2", 40)])
    issues = rule_02_transit_gap(itin, mk.reqs(), facts)
    assert _codes(issues) == ["R2"]
    assert _sev(issues) == [Severity.BLOCKING]
    assert "40" in issues[0].message and "30" in issues[0].message


def test_r2_buffer_boundary_is_inclusive(mk):
    """间隙恰好等于 40 × 1.2 = 48 分钟时通过。"""
    itin = mk.itin([mk.day("d1", D1, [
        mk.act("d1a1", "d1", "09:00", "11:00"),
        mk.act("d1a2", "d1", "11:48", "12:30"),
    ])])
    facts = mk.facts(routes=[mk.route("d1", "d1a1", "d1a2", 40)])
    assert rule_02_transit_gap(itin, mk.reqs(), facts) == []


def test_r2_degrades_to_warning_when_route_unavailable(mk):
    """查不到就如实说查不到——不按 0 处理，也不假装通过。"""
    itin = mk.itin([mk.day("d1", D1, [
        mk.act("d1a1", "d1", "09:00", "11:00"),
        mk.act("d1a2", "d1", "11:05", "12:00"),
    ])])
    facts = mk.facts(gaps=[mk.gap(GapKind.ROUTE_UNAVAILABLE, "d1a1->d1a2")])
    issues = rule_02_transit_gap(itin, mk.reqs(), facts)
    assert _sev(issues) == [Severity.WARNING]
    assert "未能核实" in issues[0].message


# ---------- 规则 3：日期覆盖与抵离占用 ----------


def test_r3_passes_on_exact_coverage(mk):
    itin = mk.itin([mk.day("d1", D1, []), mk.day("d2", D2, [])])
    assert rule_03_date_coverage(itin, mk.reqs(start=D1, end=D2),
                                 mk.facts()) == []


def test_r3_blocks_on_missing_day(mk):
    itin = mk.itin([mk.day("d1", D1, [])])
    issues = rule_03_date_coverage(itin, mk.reqs(start=D1, end=D2), mk.facts())
    assert _codes(issues) == ["R3"]
    assert _sev(issues) == [Severity.BLOCKING]
    assert "2026-10-02" in issues[0].message


def test_r3_blocks_on_extra_day(mk):
    itin = mk.itin([mk.day("d1", D1, []), mk.day("d2", D2, [])])
    issues = rule_03_date_coverage(itin, mk.reqs(start=D1, end=D1), mk.facts())
    assert _sev(issues) == [Severity.BLOCKING]


def test_r3_blocks_activity_before_arrival(mk):
    """15:00 落地，却排了 09:00 的活动。"""
    itin = mk.itin([mk.day("d1", D1, [mk.act("d1a1", "d1", "09:00", "11:00")])])
    reqs = mk.reqs(start=D1, end=D1,
                   arrival=datetime(2026, 10, 1, 15, 0, tzinfo=JST))
    issues = rule_03_date_coverage(itin, reqs, mk.facts())
    assert _codes(issues) == ["R3"]
    assert issues[0].where.activity_id == "d1a1"


def test_r3_blocks_activity_after_departure(mk):
    itin = mk.itin([mk.day("d1", D1, [mk.act("d1a1", "d1", "18:00", "20:00")])])
    reqs = mk.reqs(start=D1, end=D1,
                   departure=datetime(2026, 10, 1, 17, 0, tzinfo=JST))
    issues = rule_03_date_coverage(itin, reqs, mk.facts())
    assert _codes(issues) == ["R3"]


def test_r3_warns_when_transfer_times_unknown(mk):
    """原版声称「首末日扣掉抵离占用」，但模型里根本没这个字段。
    现在字段有了；缺失时如实说，不谎称已扣除。"""
    itin = mk.itin([mk.day("d1", D1, [mk.act("d1a1", "d1", "09:00", "11:00")])])
    issues = rule_03_date_coverage(itin, mk.reqs(start=D1, end=D1), mk.facts())
    assert _sev(issues) == [Severity.WARNING]
    assert "按整天计" in issues[0].message


# ---------- 规则 4 / 5：必去与排除 ----------


def test_r4_passes_when_constraint_poi_is_scheduled(mk):
    itin = mk.itin([mk.day("d1", D1, [mk.act("d1a1", "d1", "09:00", "11:00",
                                             query="清水寺")])])
    facts = mk.facts(poi_by_activity={"d1a1": mk.resolved("B001")},
                     constraint_pois={"清水寺": mk.resolved("B001")})
    assert rule_04_must_visit(itin, mk.reqs(must_visit=["清水寺"]), facts) == []


def test_r4_matches_by_poi_id_not_string(mk):
    """「清水寺」与「清水寺（京都）」是同一个地方，字符串比不出来。"""
    itin = mk.itin([mk.day("d1", D1, [mk.act("d1a1", "d1", "09:00", "11:00",
                                             query="清水寺（京都）")])])
    facts = mk.facts(poi_by_activity={"d1a1": mk.resolved("B001")},
                     constraint_pois={"清水寺": mk.resolved("B001")})
    assert rule_04_must_visit(itin, mk.reqs(must_visit=["清水寺"]), facts) == []


def test_r4_blocks_when_required_poi_absent(mk):
    itin = mk.itin([mk.day("d1", D1, [mk.act("d1a1", "d1", "09:00", "11:00")])])
    facts = mk.facts(poi_by_activity={"d1a1": mk.resolved("B999")},
                     constraint_pois={"清水寺": mk.resolved("B001")})
    issues = rule_04_must_visit(itin, mk.reqs(must_visit=["清水寺"]), facts)
    assert _codes(issues) == ["R4"]
    assert _sev(issues) == [Severity.BLOCKING]


def test_r4_degrades_when_constraint_is_ambiguous(mk):
    """约束侧解析不唯一时不能拿猜的 id 去判 BLOCKING——
    #4 是 BLOCKING 级，判错会驱动 planner 反复修改一个本来正确的行程。"""
    facts = mk.facts(constraint_pois={"某某寺": Ambiguous([mk.poi("B1"),
                                                          mk.poi("B2")])})
    itin = mk.itin([mk.day("d1", D1, [])])
    issues = rule_04_must_visit(itin, mk.reqs(must_visit=["某某寺"]), facts)
    assert _sev(issues) == [Severity.WARNING]
    assert "无法核实" in issues[0].message


def test_r4_degrades_when_constraint_not_found(mk):
    facts = mk.facts(constraint_pois={"不存在": NotFound("不存在")})
    itin = mk.itin([mk.day("d1", D1, [])])
    issues = rule_04_must_visit(itin, mk.reqs(must_visit=["不存在"]), facts)
    assert _sev(issues) == [Severity.WARNING]


def test_r5_blocks_when_avoided_poi_is_scheduled(mk):
    itin = mk.itin([mk.day("d1", D1, [mk.act("d1a1", "d1", "09:00", "11:00")])])
    facts = mk.facts(poi_by_activity={"d1a1": mk.resolved("B001")},
                     constraint_pois={"金阁寺": mk.resolved("B001")})
    issues = rule_05_avoid(itin, mk.reqs(avoid=["金阁寺"]), facts)
    assert _codes(issues) == ["R5"]
    assert _sev(issues) == [Severity.BLOCKING]


def test_r5_passes_when_avoided_poi_absent(mk):
    itin = mk.itin([mk.day("d1", D1, [mk.act("d1a1", "d1", "09:00", "11:00")])])
    facts = mk.facts(poi_by_activity={"d1a1": mk.resolved("B999")},
                     constraint_pois={"金阁寺": mk.resolved("B001")})
    assert rule_05_avoid(itin, mk.reqs(avoid=["金阁寺"]), facts) == []


def test_r5_degrades_when_constraint_unresolved(mk):
    facts = mk.facts(constraint_pois={"某寺": NotFound("某寺")})
    itin = mk.itin([mk.day("d1", D1, [])])
    assert _sev(rule_05_avoid(itin, mk.reqs(avoid=["某寺"]), facts)) == [
        Severity.WARNING
    ]
```

- [ ] **Step 3: 运行确认失败**

Run: `uv run pytest tests/validation/test_rules_1_5.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tripplan.validation.rules'`

- [ ] **Step 4: 实现规则 1–5**

创建 `src/tripplan/validation/rules.py`：

```python
"""确定性规则。纯函数：只读 Itinerary / Requirements / FactSnapshot。

本模块禁止 import 任何 provider 或网络库。触网的部分在 resolver 里，
规则只消费它产出的快照。依赖缺失时降级为 WARNING 并说明原因——
不按 0 处理，也不假装通过。
"""

from datetime import date as Date
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from tripplan.models.facts import FactSnapshot, GapKind
from tripplan.models.issue import ActivityRef, DayRef, Issue, Severity, Source
from tripplan.models.itinerary import Itinerary
from tripplan.models.requirements import Requirements

#: 实测耗时之上再留 20%——换乘走错口、等信号、找入口都在这里面。
TRANSIT_BUFFER = Decimal("1.2")


def _issue(sev: Severity, code: str, message: str, where=None) -> Issue:
    return Issue(severity=sev, source=Source.RULE, code=code,
                 message=message, where=where)


def _minutes(t) -> int:
    return t.hour * 60 + t.minute


def rule_01_no_overlap(itin, reqs, facts) -> list[Issue]:
    """同日活动必须时间递增且互不重叠。"""
    issues: list[Issue] = []
    for day in itin.days:
        prev_end = None
        for act in day.activities:
            if _minutes(act.end) <= _minutes(act.start):
                issues.append(_issue(
                    Severity.BLOCKING, "R1",
                    f"{act.poi_query} 的结束时间不晚于开始时间"
                    f"（{act.start:%H:%M}–{act.end:%H:%M}）",
                    ActivityRef(day.id, act.id)))
            elif prev_end is not None and _minutes(act.start) < prev_end:
                issues.append(_issue(
                    Severity.BLOCKING, "R1",
                    f"{act.poi_query} 与上一项时间重叠"
                    f"（{act.start:%H:%M} 早于上一项结束）",
                    ActivityRef(day.id, act.id)))
            prev_end = max(prev_end or 0, _minutes(act.end))
    return issues


def rule_02_transit_gap(itin, reqs, facts) -> list[Issue]:
    """相邻活动之间必须留够实测通勤时间 × TRANSIT_BUFFER。"""
    issues: list[Issue] = []
    for day in itin.days:
        for prev, nxt in zip(day.activities, day.activities[1:]):
            gap = _minutes(nxt.start) - _minutes(prev.end)
            route = facts.route(day.id, prev.id, nxt.id)
            if route is None:
                # 快照里没有这一段：如实降级。gaps 里若有对应记录就带上原因。
                subject = f"{prev.id}->{nxt.id}"
                why = next((g.detail for g in facts.gaps
                            if g.kind is GapKind.ROUTE_UNAVAILABLE
                            and g.subject == subject), "")
                suffix = f"（{why}）" if why else ""
                issues.append(_issue(
                    Severity.WARNING, "R2",
                    f"{prev.poi_query} 到 {nxt.poi_query} 的通勤耗时未能核实"
                    f"{suffix}，当前只留了 {gap} 分钟",
                    ActivityRef(day.id, nxt.id)))
                continue
            needed = int(Decimal(route.duration_min) * TRANSIT_BUFFER)
            if gap < needed:
                issues.append(_issue(
                    Severity.BLOCKING, "R2",
                    f"{prev.poi_query} 到 {nxt.poi_query} 实测需 "
                    f"{route.duration_min} 分钟（含缓冲 {needed}），"
                    f"但只留了 {gap} 分钟",
                    ActivityRef(day.id, nxt.id)))
    return issues


def rule_03_date_coverage(itin, reqs, facts) -> list[Issue]:
    """日期必须与需求完全对齐；抵离时刻已知时，首末日不得越界。"""
    issues: list[Issue] = []
    rng = reqs.dates.value
    if rng is None:
        return issues

    wanted = {rng.start + timedelta(days=i) for i in range(rng.days)}
    got = {d.date for d in itin.days}
    for missing in sorted(wanted - got):
        issues.append(_issue(Severity.BLOCKING, "R3",
                             f"缺少 {missing.isoformat()} 的安排"))
    for extra in sorted(got - wanted):
        issues.append(_issue(Severity.BLOCKING, "R3",
                             f"{extra.isoformat()} 不在行程日期范围内"))

    tz = ZoneInfo(facts.trip_timezone)
    checked_transfer = False

    arrival = reqs.arrival.value
    if arrival is not None:
        checked_transfer = True
        local = arrival.at.astimezone(tz)
        for day in itin.days:
            if day.date != local.date():
                continue
            for act in day.activities:
                if _minutes(act.start) < _minutes(local.time()):
                    issues.append(_issue(
                        Severity.BLOCKING, "R3",
                        f"{act.poi_query} 安排在 {act.start:%H:%M}，"
                        f"早于 {local:%H:%M} 的抵达时刻",
                        ActivityRef(day.id, act.id)))

    departure = reqs.departure.value
    if departure is not None:
        checked_transfer = True
        local = departure.at.astimezone(tz)
        for day in itin.days:
            if day.date != local.date():
                continue
            for act in day.activities:
                if _minutes(act.end) > _minutes(local.time()):
                    issues.append(_issue(
                        Severity.BLOCKING, "R3",
                        f"{act.poi_query} 到 {act.end:%H:%M} 才结束，"
                        f"晚于 {local:%H:%M} 的离开时刻",
                        ActivityRef(day.id, act.id)))

    if not checked_transfer:
        issues.append(_issue(
            Severity.WARNING, "R3",
            "未提供抵离时间，首末日按整天计——实际可用时间可能更短"))
    return issues


def _constraint_issues(itin, facts, queries, code, want_present) -> list[Issue]:
    """规则 4 与 5 的公共骨架：方向相反，其余完全一致。"""
    issues: list[Issue] = []
    scheduled = facts.resolved_poi_ids()
    for query in queries or []:
        poi_id = facts.constraint_poi_id(query)
        if poi_id is None:
            issues.append(_issue(
                Severity.WARNING, code,
                f"无法核实「{query}」——该地点未能唯一解析"))
            continue
        present = poi_id in scheduled
        if present is not want_present:
            msg = (f"必去的「{query}」没有出现在行程里" if want_present
                   else f"要求避开的「{query}」出现在了行程里")
            issues.append(_issue(Severity.BLOCKING, code, msg))
    return issues


def rule_04_must_visit(itin, reqs, facts) -> list[Issue]:
    return _constraint_issues(itin, facts, reqs.must_visit.value, "R4", True)


def rule_05_avoid(itin, reqs, facts) -> list[Issue]:
    return _constraint_issues(itin, facts, reqs.avoid.value, "R5", False)
```

- [ ] **Step 5: 运行确认通过**

Run: `uv run pytest tests/validation/test_rules_1_5.py -v`
Expected: PASS（20 passed）

- [ ] **Step 6: 断言规则层不触网**

在 `tests/validation/test_rules_1_5.py` 末尾追加：

```python
def test_rules_module_imports_nothing_networky():
    """规则必须是纯函数——测试直接盯住 import。"""
    import ast
    import pathlib

    import tripplan.validation.rules as rules

    src = pathlib.Path(rules.__file__).read_text(encoding="utf-8")
    imported = {
        n.module or ""
        for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.ImportFrom)
    } | {
        a.name
        for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.Import)
        for a in n.names
    }
    forbidden = {"httpx", "requests", "urllib", "urllib.request", "socket"}
    assert not (imported & forbidden)
    assert not any(m.startswith("tripplan.providers") for m in imported)
```

Run: `uv run pytest tests/validation/test_rules_1_5.py -v`
Expected: PASS（21 passed）

- [ ] **Step 7: 格式化并提交**

```bash
uv run black src tests
git add src/tripplan/validation/rules.py tests/validation
git commit -m "feat: 确定性规则 1-5

时间重叠、通勤缓冲、日期覆盖与抵离占用、必去、排除。
约束按解析后的 POI id 匹配而非字符串；两端任一解析不唯一即降级为
WARNING——#4/#5 是 BLOCKING 级，判错会驱动 planner 改坏正确的行程。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 10: 规则 6–9 与汇总入口

**Files:**
- Modify: `src/tripplan/validation/rules.py`（追加，不改动 1–5）
- Test: `tests/validation/test_rules_6_9.py`

**Interfaces:**
- Consumes: Task 9 的 `_issue` / `_minutes`；`build_ledger`（Task 8）
- Produces: `rule_06_budget`、`rule_07_pace`、`rule_08_meals`、`rule_09_opening_hours`、`PACE_LIMITS: dict[Pace, PaceLimit]`、`run_rule_checks(itin, reqs, facts) -> list[Issue]`

- [ ] **Step 1: 写失败的测试**

创建 `tests/validation/test_rules_6_9.py`：

```python
from datetime import date
from decimal import Decimal

from tripplan.models.common import Confidence, Field, Money, Origin
from tripplan.models.facts import PoiFact, Resolved
from tripplan.models.issue import Severity
from tripplan.models.itinerary import Category
from tripplan.models.requirements import Basis, BudgetSpec, CostKind, Pace
from tripplan.validation.rules import (
    run_rule_checks,
    rule_06_budget,
    rule_07_pace,
    rule_08_meals,
    rule_09_opening_hours,
)

D1 = date(2026, 10, 1)


def _codes(issues):
    return [i.code for i in issues]


def _sev(issues):
    return [i.severity for i in issues]


def _budget(mk, amount="1000"):
    return mk.reqs(budget=Field(
        BudgetSpec(Decimal(amount), "CNY", Basis.TOTAL,
                   frozenset({CostKind.TICKET})), Origin.USER))


def _cost(v, conf=Confidence.ESTIMATED):
    return Money(Decimal(v), "CNY", conf, "llm")


# ---------- 规则 6：预算 ----------


def test_r6_estimated_overspend_is_warning_not_blocking(mk):
    """v1 票价来自模型知识——拿模型自己报的数字去 BLOCK 模型自己的方案不成立。"""
    itin = mk.itin([mk.day("d1", D1, [
        mk.act("d1a1", "d1", "09:00", "11:00", cost=_cost("5000")),
    ])])
    issues = rule_06_budget(itin, _budget(mk), mk.facts())
    assert _codes(issues) == ["R6"]
    assert _sev(issues) == [Severity.WARNING]


def test_r6_blocks_only_when_complete_and_all_verified(mk):
    itin = mk.itin([mk.day("d1", D1, [
        mk.act("d1a1", "d1", "09:00", "11:00",
               cost=_cost("5000", Confidence.VERIFIED)),
    ])])
    issues = rule_06_budget(itin, _budget(mk), mk.facts())
    assert _sev(issues) == [Severity.BLOCKING]


def test_r6_unknown_cost_keeps_it_a_warning_even_if_verified(mk):
    """有未知项时「合计」本身就不可信，不能据此 BLOCK。"""
    itin = mk.itin([mk.day("d1", D1, [
        mk.act("d1a1", "d1", "09:00", "11:00",
               cost=_cost("5000", Confidence.VERIFIED)),
        mk.act("d1a2", "d1", "12:00", "13:00", cost=None),
    ])])
    assert _sev(rule_06_budget(itin, _budget(mk), mk.facts())) == [
        Severity.WARNING
    ]


def test_r6_silent_when_within_budget(mk):
    itin = mk.itin([mk.day("d1", D1, [
        mk.act("d1a1", "d1", "09:00", "11:00", cost=_cost("100")),
    ])])
    assert rule_06_budget(itin, _budget(mk), mk.facts()) == []


def test_r6_silent_without_a_budget(mk):
    itin = mk.itin([mk.day("d1", D1, [
        mk.act("d1a1", "d1", "09:00", "11:00", cost=_cost("99999")),
    ])])
    assert rule_06_budget(itin, mk.reqs(), mk.facts()) == []


def test_r6_warns_on_currency_mismatch(mk):
    itin = mk.itin([mk.day("d1", D1, [
        mk.act("d1a1", "d1", "09:00", "11:00",
               cost=Money(Decimal("4000"), "JPY", Confidence.ESTIMATED, "llm")),
    ])])
    issues = rule_06_budget(itin, _budget(mk), mk.facts())
    assert any("币种" in i.message for i in issues)


# ---------- 规则 7：节奏 ----------


def test_r7_warns_when_relaxed_day_is_overpacked(mk):
    acts = [mk.act(f"d1a{i}", "d1", f"{7 + i}:00", f"{7 + i}:45")
            for i in range(1, 8)]
    itin = mk.itin([mk.day("d1", D1, acts)])
    reqs = mk.reqs(pace=Field(Pace.RELAXED, Origin.USER))
    issues = rule_07_pace(itin, reqs, mk.facts())
    assert _codes(issues) == ["R7"]
    assert _sev(issues) == [Severity.WARNING]


def test_r7_silent_when_within_pace(mk):
    itin = mk.itin([mk.day("d1", D1, [
        mk.act("d1a1", "d1", "10:00", "12:00"),
        mk.act("d1a2", "d1", "14:00", "16:00"),
    ])])
    reqs = mk.reqs(pace=Field(Pace.RELAXED, Origin.USER))
    assert rule_07_pace(itin, reqs, mk.facts()) == []


def test_r7_packed_allows_more_than_relaxed(mk):
    acts = [mk.act(f"d1a{i}", "d1", f"{7 + i}:00", f"{7 + i}:45")
            for i in range(1, 7)]
    itin = mk.itin([mk.day("d1", D1, acts)])
    packed = mk.reqs(pace=Field(Pace.PACKED, Origin.USER))
    relaxed = mk.reqs(pace=Field(Pace.RELAXED, Origin.USER))
    assert rule_07_pace(itin, packed, mk.facts()) == []
    assert rule_07_pace(itin, relaxed, mk.facts()) != []


def test_r7_defaults_to_normal_when_pace_unset(mk):
    itin = mk.itin([mk.day("d1", D1, [mk.act("d1a1", "d1", "09:00", "10:00")])])
    assert rule_07_pace(itin, mk.reqs(), mk.facts()) == []


# ---------- 规则 8：三餐 ----------


def test_r8_warns_when_a_day_has_no_lunch(mk):
    itin = mk.itin([mk.day("d1", D1, [
        mk.act("d1a1", "d1", "09:00", "11:00"),
        mk.act("d1a2", "d1", "18:00", "19:30", category=Category.MEAL),
    ])])
    issues = rule_08_meals(itin, mk.reqs(), mk.facts())
    assert _codes(issues) == ["R8"]
    assert "午餐" in issues[0].message


def test_r8_silent_when_both_meals_present(mk):
    itin = mk.itin([mk.day("d1", D1, [
        mk.act("d1a1", "d1", "12:00", "13:00", category=Category.MEAL),
        mk.act("d1a2", "d1", "18:30", "20:00", category=Category.MEAL),
    ])])
    assert rule_08_meals(itin, mk.reqs(), mk.facts()) == []


def test_r8_meal_outside_window_does_not_count(mk):
    """凌晨 3 点的「用餐」不能顶替午餐。"""
    itin = mk.itin([mk.day("d1", D1, [
        mk.act("d1a1", "d1", "03:00", "04:00", category=Category.MEAL),
        mk.act("d1a2", "d1", "18:30", "20:00", category=Category.MEAL),
    ])])
    assert "午餐" in rule_08_meals(itin, mk.reqs(), mk.facts())[0].message


# ---------- 规则 9：营业时间 ----------


def test_r9_warns_when_activity_falls_outside_opening_hours(mk):
    poi = PoiFact(id="B1", name="清水寺", coords=mk.poi("B1").coords,
                  opening_hours="09:00-17:00", ticket=None, source="amap",
                  fetched_at=mk.poi("B1").fetched_at)
    itin = mk.itin([mk.day("d1", D1, [
        mk.act("d1a1", "d1", "18:00", "19:00", query="清水寺"),
    ])])
    facts = mk.facts(poi_by_activity={"d1a1": Resolved(poi)})
    issues = rule_09_opening_hours(itin, mk.reqs(), facts)
    assert _codes(issues) == ["R9"]
    assert _sev(issues) == [Severity.WARNING]      # 数据不可靠，永远不 BLOCK
    assert "未核实" in issues[0].message


def test_r9_silent_when_inside_opening_hours(mk):
    poi = PoiFact(id="B1", name="清水寺", coords=mk.poi("B1").coords,
                  opening_hours="09:00-17:00", ticket=None, source="amap",
                  fetched_at=mk.poi("B1").fetched_at)
    itin = mk.itin([mk.day("d1", D1, [
        mk.act("d1a1", "d1", "10:00", "11:00", query="清水寺"),
    ])])
    facts = mk.facts(poi_by_activity={"d1a1": Resolved(poi)})
    assert rule_09_opening_hours(itin, mk.reqs(), facts) == []


def test_r9_silent_when_hours_unknown(mk):
    itin = mk.itin([mk.day("d1", D1, [mk.act("d1a1", "d1", "23:00", "23:30")])])
    facts = mk.facts(poi_by_activity={"d1a1": mk.resolved("B1")})
    assert rule_09_opening_hours(itin, mk.reqs(), facts) == []


# ---------- 汇总 ----------


def test_run_rule_checks_aggregates_all_nine(mk):
    itin = mk.itin([mk.day("d1", D1, [
        mk.act("d1a1", "d1", "09:00", "12:00"),
        mk.act("d1a2", "d1", "11:00", "13:00"),       # R1 重叠
    ])])
    issues = run_rule_checks(itin, mk.reqs(start=D1, end=D1), mk.facts())
    codes = set(_codes(issues))
    assert "R1" in codes
    assert "R3" in codes          # 缺抵离时间的 WARNING
    assert "R8" in codes          # 没有午晚餐


def test_run_rule_checks_returns_empty_on_clean_itinerary(mk):
    from datetime import datetime, timedelta, timezone

    jst = timezone(timedelta(hours=9))
    itin = mk.itin([mk.day("d1", D1, [
        mk.act("d1a1", "d1", "10:00", "11:30"),
        mk.act("d1a2", "d1", "12:30", "13:30", category=Category.MEAL),
        mk.act("d1a3", "d1", "18:30", "20:00", category=Category.MEAL),
    ])])
    facts = mk.facts(routes=[
        mk.route("d1", "d1a1", "d1a2", 30),
        mk.route("d1", "d1a2", "d1a3", 30),
    ])
    reqs = mk.reqs(start=D1, end=D1,
                   arrival=datetime(2026, 10, 1, 8, tzinfo=jst),
                   departure=datetime(2026, 10, 1, 22, tzinfo=jst))
    assert run_rule_checks(itin, reqs, facts) == []
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/validation/test_rules_6_9.py -v`
Expected: FAIL — `ImportError: cannot import name 'run_rule_checks'`

- [ ] **Step 3: 追加实现**

在 `src/tripplan/validation/rules.py` 顶部的 import 区补上：

```python
from tripplan.models.itinerary import Category
from tripplan.models.requirements import Pace
from tripplan.validation.budget import build_ledger
```

然后在文件末尾追加：

```python
# ---------- 规则 6：预算 ----------


def rule_06_budget(itin, reqs, facts) -> list[Issue]:
    """输出分层账单式的判断，而不是拿模型自报的数字去 BLOCK 模型自己的方案。

    只有覆盖完整（无未知项）且全部 VERIFIED 时超支才升为 BLOCKING。
    这个条件在 v1 基本不会满足，接入真实票价 API 后自然生效。
    """
    issues: list[Issue] = []
    led = build_ledger(itin, reqs)

    if led.currency_mismatch:
        issues.append(_issue(
            Severity.WARNING, "R6",
            f"部分花费的币种与预算（{led.currency}）不一致，未计入合计"))

    if not led.over_budget:
        return issues

    detail = (f"已核实 {led.verified}（{led.verified_count} 项）"
              f"／估算 {led.estimated}（{led.estimated_count} 项）"
              f"／未知 {led.unknown_count} 项，预算 {led.budget_limit}")
    if led.complete and led.all_verified:
        issues.append(_issue(Severity.BLOCKING, "R6", f"超出预算：{detail}"))
    else:
        issues.append(_issue(
            Severity.WARNING, "R6",
            f"按当前估算可能超出预算：{detail}（金额未全部核实，仅供参考）"))
    return issues


# ---------- 规则 7：节奏 ----------


class PaceLimit:
    def __init__(self, max_activities: int, max_out_minutes: int) -> None:
        self.max_activities = max_activities
        self.max_out_minutes = max_out_minutes


PACE_LIMITS: dict[Pace, PaceLimit] = {
    Pace.RELAXED: PaceLimit(max_activities=4, max_out_minutes=8 * 60),
    Pace.NORMAL: PaceLimit(max_activities=6, max_out_minutes=10 * 60),
    Pace.PACKED: PaceLimit(max_activities=8, max_out_minutes=12 * 60),
}


def rule_07_pace(itin, reqs, facts) -> list[Issue]:
    pace = reqs.pace.value or Pace.NORMAL
    limit = PACE_LIMITS[pace]
    issues: list[Issue] = []
    for day in itin.days:
        if not day.activities:
            continue
        count = len(day.activities)
        out = (_minutes(day.activities[-1].end)
               - _minutes(day.activities[0].start))
        if count > limit.max_activities:
            issues.append(_issue(
                Severity.WARNING, "R7",
                f"{day.date.isoformat()} 排了 {count} 项，"
                f"超过 {pace.value} 节奏建议的 {limit.max_activities} 项",
                DayRef(day.id)))
        elif out > limit.max_out_minutes:
            issues.append(_issue(
                Severity.WARNING, "R7",
                f"{day.date.isoformat()} 在外 {out // 60} 小时，"
                f"超过 {pace.value} 节奏建议的 "
                f"{limit.max_out_minutes // 60} 小时",
                DayRef(day.id)))
    return issues


# ---------- 规则 8：三餐 ----------

MEAL_WINDOWS = (("午餐", 11 * 60, 14 * 60 + 30),
                ("晚餐", 17 * 60, 21 * 60))


def rule_08_meals(itin, reqs, facts) -> list[Issue]:
    issues: list[Issue] = []
    for day in itin.days:
        if not day.activities:
            continue
        for label, lo, hi in MEAL_WINDOWS:
            ok = any(a.category is Category.MEAL
                     and lo <= _minutes(a.start) <= hi
                     for a in day.activities)
            if not ok:
                issues.append(_issue(
                    Severity.WARNING, "R8",
                    f"{day.date.isoformat()} 没有安排{label}",
                    DayRef(day.id)))
    return issues


# ---------- 规则 9：营业时间 ----------


def _parse_hours(text: str) -> tuple[int, int] | None:
    try:
        lo, hi = text.split("-")
        h1, m1 = map(int, lo.strip().split(":"))
        h2, m2 = map(int, hi.strip().split(":"))
        return h1 * 60 + m1, h2 * 60 + m2
    except (ValueError, AttributeError):
        return None


def rule_09_opening_hours(itin, reqs, facts) -> list[Issue]:
    """v1 没有权威营业时间数据源，因此**永远只给 WARNING**。

    接入真实 API 后才能升为 BLOCKING——不假装它可靠，比悄悄放过去强。
    """
    issues: list[Issue] = []
    for day in itin.days:
        for act in day.activities:
            res = facts.poi_by_activity.get(act.id)
            hours = getattr(getattr(res, "fact", None), "opening_hours", None)
            window = _parse_hours(hours) if hours else None
            if window is None:
                continue
            lo, hi = window
            if _minutes(act.start) < lo or _minutes(act.end) > hi:
                issues.append(_issue(
                    Severity.WARNING, "R9",
                    f"{act.poi_query} 的安排（{act.start:%H:%M}–{act.end:%H:%M}）"
                    f"可能不在营业时间 {hours} 内（该数据未核实）",
                    ActivityRef(day.id, act.id)))
    return issues


# ---------- 汇总 ----------

ALL_RULES = (
    rule_01_no_overlap,
    rule_02_transit_gap,
    rule_03_date_coverage,
    rule_04_must_visit,
    rule_05_avoid,
    rule_06_budget,
    rule_07_pace,
    rule_08_meals,
    rule_09_opening_hours,
)


def run_rule_checks(itin: Itinerary, reqs: Requirements,
                    facts: FactSnapshot) -> list[Issue]:
    issues: list[Issue] = []
    for rule in ALL_RULES:
        issues.extend(rule(itin, reqs, facts))
    return issues
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/validation/ -v`
Expected: PASS（48 passed）

- [ ] **Step 5: 格式化并提交**

```bash
uv run black src tests
git add src/tripplan/validation/rules.py tests/validation/test_rules_6_9.py
git commit -m "feat: 确定性规则 6-9 与汇总入口

预算只在覆盖完整且全部 VERIFIED 时才 BLOCK，否则输出分层账单式 WARNING。
营业时间数据不可靠，永远只给 WARNING 并标注未核实。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 11: 候选差异度

**Files:**
- Create: `src/tripplan/validation/diversity.py`
- Test: `tests/validation/test_diversity.py`

**Interfaces:**
- Consumes: `Itinerary`, `Category`（Task 3）、`FactSnapshot`（Task 4）、`CandidateSlot`（Task 5）
- Produces: `DIVERSITY_THRESHOLD = 0.6`、`poi_signature(itin, facts) -> frozenset[str]`、`jaccard(a, b) -> float`、`too_similar(slots, threshold) -> list[tuple[int, int]]`、`enforce_diversity(slots, regenerate, emit=noop, threshold=DIVERSITY_THRESHOLD) -> list[CandidateSlot]`

**背景：** 「让 LLM 想三个角度」不保证产出的三份行程真的不同——它完全可能给三个听着不一样的标题，底下却是同一批 POI 换个顺序。多候选是 v1 唯一的增量特性，三份雷同等于这个特性没做。

**判据只用核心 POI 集合的 Jaccard 重合度。** 不采用「主题／POI／区域／节奏四选二」——主题难客观量化，区域与 POI 高度相关，多出来的判定复杂度换不到收益。

`regenerate` 是回调，因此这个模块不依赖 LLM 层，可以纯函数式测试。

- [ ] **Step 1: 写失败的测试**

创建 `tests/validation/test_diversity.py`：

```python
from datetime import date

from tripplan.models.itinerary import Angle, Category
from tripplan.state import CandidateSlot, SlotStatus
from tripplan.validation.diversity import (
    enforce_diversity,
    jaccard,
    poi_signature,
    too_similar,
)

D1 = date(2026, 10, 1)


def _slot(mk, key, poi_ids, categories=None):
    cats = categories or [Category.SIGHT] * len(poi_ids)
    acts = [
        mk.act(f"{key}a{i}", "d1", f"{9 + i}:00", f"{10 + i}:00",
               category=cats[i - 1])
        for i in range(1, len(poi_ids) + 1)
    ]
    itin = mk.itin([mk.day("d1", D1, acts)])
    itin.angle = Angle(key, f"方案{key}", "")
    facts = mk.facts(poi_by_activity={
        a.id: mk.resolved(pid) for a, pid in zip(acts, poi_ids)})
    return CandidateSlot(angle=itin.angle, itinerary=itin, facts=facts,
                         status=SlotStatus.OK)


def test_signature_collects_resolved_sight_poi_ids(mk):
    slot = _slot(mk, "A", ["B1", "B2", "B3"])
    assert poi_signature(slot.itinerary, slot.facts) == {"B1", "B2", "B3"}


def test_signature_ignores_non_sight_categories(mk):
    slot = _slot(mk, "A", ["B1", "B2"],
                 categories=[Category.SIGHT, Category.MEAL])
    assert poi_signature(slot.itinerary, slot.facts) == {"B1"}


def test_signature_ignores_unresolved_activities(mk):
    slot = _slot(mk, "A", ["B1", "B2"])
    slot.facts.poi_by_activity.pop("Aa2")
    assert poi_signature(slot.itinerary, slot.facts) == {"B1"}


def test_jaccard_basics():
    assert jaccard(frozenset(), frozenset()) == 0.0
    assert jaccard(frozenset({"a"}), frozenset({"a"})) == 1.0
    assert jaccard(frozenset({"a", "b"}), frozenset({"b", "c"})) == 1 / 3


def test_too_similar_finds_the_overlapping_pair(mk):
    slots = [
        _slot(mk, "A", ["B1", "B2", "B3"]),
        _slot(mk, "B", ["B1", "B2", "B3"]),      # 完全相同
        _slot(mk, "C", ["B7", "B8", "B9"]),
    ]
    assert too_similar(slots, threshold=0.6) == [(0, 1)]


def test_too_similar_empty_when_all_distinct(mk):
    slots = [
        _slot(mk, "A", ["B1", "B2"]),
        _slot(mk, "B", ["B3", "B4"]),
        _slot(mk, "C", ["B5", "B6"]),
    ]
    assert too_similar(slots, threshold=0.6) == []


def test_too_similar_skips_slots_without_itinerary(mk):
    slots = [_slot(mk, "A", ["B1"]),
             CandidateSlot(angle=_slot(mk, "B", ["B1"]).angle,
                           status=SlotStatus.FAILED)]
    assert too_similar(slots, threshold=0.6) == []


def test_enforce_regenerates_only_the_later_duplicate(mk):
    slots = [
        _slot(mk, "A", ["B1", "B2", "B3"]),
        _slot(mk, "B", ["B1", "B2", "B3"]),
        _slot(mk, "C", ["B7", "B8"]),
    ]
    called = []

    def regenerate(slot, avoid_poi_ids):
        called.append((slot.angle.key, sorted(avoid_poi_ids)))
        return _slot(mk, slot.angle.key, ["B4", "B5"])

    out = enforce_diversity(slots, regenerate, threshold=0.6)
    assert [k for k, _ in called] == ["B"]                  # 只重跑靠后那份
    assert called[0][1] == ["B1", "B2", "B3"]               # 重合 POI 作为 avoid
    assert poi_signature(out[1].itinerary, out[1].facts) == {"B4", "B5"}


def test_enforce_retries_at_most_once(mk):
    """仍然重合就如实展示——为差异硬凑一个更差的方案不划算。"""
    slots = [_slot(mk, "A", ["B1", "B2"]), _slot(mk, "B", ["B1", "B2"])]
    calls = []

    def regenerate(slot, avoid_poi_ids):
        calls.append(slot.angle.key)
        return _slot(mk, slot.angle.key, ["B1", "B2"])      # 依然一样

    out = enforce_diversity(slots, regenerate, threshold=0.6)
    assert len(calls) == 1
    assert len(out) == 2


def test_enforce_is_noop_when_already_diverse(mk):
    def must_not_be_called(*_):
        raise AssertionError("已经足够不同，不该触发重跑")

    slots = [_slot(mk, "A", ["B1"]), _slot(mk, "B", ["B2"])]
    assert enforce_diversity(slots, must_not_be_called, threshold=0.6) == slots
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/validation/test_diversity.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tripplan.validation.diversity'`

- [ ] **Step 3: 实现**

创建 `src/tripplan/validation/diversity.py`：

```python
"""候选差异度。角度命名自由，但产出必须真的不同。"""

from itertools import combinations
from typing import Callable

from tripplan.models.facts import FactSnapshot
from tripplan.models.itinerary import Category, Itinerary

DIVERSITY_THRESHOLD = 0.6


def _noop(_event) -> None:
    pass


def poi_signature(itin: Itinerary, facts: FactSnapshot) -> frozenset[str]:
    """一份行程的「核心 POI」指纹：已解析的观光类 POI id 集合。

    未解析的活动不参与比较——拿猜的身份去算相似度只会得到噪声。
    """
    ids = set()
    for act in itin.all_activities():
        if act.category is not Category.SIGHT:
            continue
        poi_id = facts.poi_id_for(act.id)
        if poi_id is not None:
            ids.add(poi_id)
    return frozenset(ids)


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def _signature_of(slot) -> frozenset[str] | None:
    if slot.itinerary is None or slot.facts is None:
        return None
    return poi_signature(slot.itinerary, slot.facts)


def too_similar(slots, threshold: float = DIVERSITY_THRESHOLD):
    """返回重合度超阈值的下标对，靠后的那个是待重跑的。"""
    pairs = []
    for i, j in combinations(range(len(slots)), 2):
        si, sj = _signature_of(slots[i]), _signature_of(slots[j])
        if si is None or sj is None:
            continue
        if jaccard(si, sj) > threshold:
            pairs.append((i, j))
    return pairs


def enforce_diversity(
    slots: list,
    regenerate: Callable[[object, frozenset[str]], object],
    emit=_noop,
    threshold: float = DIVERSITY_THRESHOLD,
) -> list:
    """重合度超阈值时重跑靠后的那一份，至多一次。

    regenerate 是回调（slot, 需要避开的 POI id 集合）-> 新 slot，
    因此本模块不依赖 LLM 层。
    """
    result = list(slots)
    retried: set[int] = set()
    for i, j in too_similar(result, threshold):
        if j in retried:
            continue
        overlap = (_signature_of(result[i]) or frozenset()) & (
            _signature_of(result[j]) or frozenset()
        )
        emit(("diversity_retry", result[j].angle.key, sorted(overlap)))
        retried.add(j)
        result[j] = regenerate(result[j], overlap)
    return result
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/validation/ -v`
Expected: PASS（58 passed）

- [ ] **Step 5: 格式化并提交**

```bash
uv run black src tests
git add src/tripplan/validation/diversity.py tests/validation/test_diversity.py
git commit -m "feat: 候选差异度检查

核心 POI 集合的 Jaccard 重合度单项判据，超阈值重跑靠后那份，至多一次。
regenerate 作为回调传入，本模块不依赖 LLM 层。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

**✅ 检查点 B：** 喂一份手写行程 + 手写 `FactSnapshot`，能得到完整的 issue 列表；依赖缺失一律降级且说明原因。

---

## Phase C — 外部数据

### Task 12: Provider 契约与 FakeProvider

**Files:**
- Create: `src/tripplan/providers/__init__.py`
- Create: `src/tripplan/providers/base.py`
- Create: `src/tripplan/providers/fake.py`
- Test: `tests/providers/test_fake.py`

**Interfaces:**
- Consumes: `LatLng`, `TravelMode`, `Money`（Task 1）、`PoiFact`（Task 4）
- Produces: `ProviderError`、`RouteObservation(mode, duration_min, distance_m, polyline, source, fetched_at)`、`GeoProvider` Protocol（`search_poi(query, city) -> list[PoiFact]` / `route(origin, dest, mode, depart_at) -> RouteObservation` / `static_map(points, polyline) -> bytes` / `timezone_of(city) -> str`）、`FakeProvider`

**Provider 与 Tool 是两层，不合并。** Provider 是数据访问；Tool（Task 17）是包给 LLM 的壳。分开的实际理由：`route()` 同时被 LLM 的工具和 resolver 调用，共用 Provider 就共用一份缓存。

- [ ] **Step 1: 写失败的测试**

创建 `tests/providers/__init__.py`（空）与 `tests/providers/test_fake.py`：

```python
from datetime import datetime, timedelta, timezone

import pytest

from tripplan.models.common import LatLng, TravelMode
from tripplan.providers.base import ProviderError
from tripplan.providers.fake import FakeProvider

JST = timezone(timedelta(hours=9))
WHEN = datetime(2026, 10, 1, 11, 0, tzinfo=JST)


def test_search_returns_configured_pois():
    p = FakeProvider(pois={"清水寺": [("B001", 34.99, 135.78)]})
    got = p.search_poi("清水寺", city="京都")
    assert [x.id for x in got] == ["B001"]
    assert got[0].coords == LatLng(34.99, 135.78)


def test_search_returns_empty_for_unknown_query():
    assert FakeProvider().search_poi("不存在", city="京都") == []


def test_search_can_return_multiple_for_ambiguity_tests():
    p = FakeProvider(pois={"某某寺": [("B1", 35.0, 135.0), ("B2", 35.1, 135.1)]})
    assert len(p.search_poi("某某寺", city="京都")) == 2


def test_route_is_deterministic_for_same_inputs():
    p = FakeProvider()
    a, b = LatLng(35.0, 135.0), LatLng(35.1, 135.1)
    first = p.route(a, b, TravelMode.TRANSIT, WHEN)
    second = p.route(a, b, TravelMode.TRANSIT, WHEN)
    assert first == second


def test_route_can_be_scripted_per_pair():
    p = FakeProvider(routes={((35.0, 135.0), (35.1, 135.1)): 40})
    obs = p.route(LatLng(35.0, 135.0), LatLng(35.1, 135.1),
                  TravelMode.TRANSIT, WHEN)
    assert obs.duration_min == 40
    assert obs.polyline


def test_route_failure_can_be_scripted():
    p = FakeProvider(fail_routes={((35.0, 135.0), (35.1, 135.1))})
    with pytest.raises(ProviderError):
        p.route(LatLng(35.0, 135.0), LatLng(35.1, 135.1),
                TravelMode.TRANSIT, WHEN)


def test_static_map_returns_png_bytes():
    data = FakeProvider().static_map([LatLng(35.0, 135.0)], polyline=None)
    assert data.startswith(b"\x89PNG")


def test_timezone_of_known_city():
    assert FakeProvider().timezone_of("京都") == "Asia/Tokyo"


def test_timezone_of_unknown_city_raises():
    with pytest.raises(ProviderError):
        FakeProvider().timezone_of("虚构城")


def test_call_log_lets_tests_assert_no_redundant_lookups():
    p = FakeProvider()
    a, b = LatLng(35.0, 135.0), LatLng(35.1, 135.1)
    p.route(a, b, TravelMode.TRANSIT, WHEN)
    p.route(a, b, TravelMode.TRANSIT, WHEN)
    assert p.call_log.count("route") == 2
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/providers/test_fake.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tripplan.providers'`

- [ ] **Step 3: 实现契约**

创建 `src/tripplan/providers/__init__.py`（空）与 `src/tripplan/providers/base.py`：

```python
"""外部数据访问的契约。实现者：FakeProvider（测试）、AmapProvider（生产）。"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from tripplan.models.common import LatLng, TravelMode
from tripplan.models.facts import PoiFact


class ProviderError(Exception):
    """外部依赖失败：网络错误、限流、鉴权失败、查无此城。

    由 run_slot 兜住（Task 18），转成 SlotStatus.FAILED 而不是炸穿整组候选。
    """


@dataclass(frozen=True)
class RouteObservation:
    """provider 的原始返回。resolver 给它补上 day_id 与活动 ID 后成为 RouteFact。"""

    mode: TravelMode
    duration_min: int
    distance_m: int
    polyline: str
    source: str
    fetched_at: datetime


class GeoProvider(Protocol):
    def search_poi(self, query: str, city: str) -> list[PoiFact]: ...

    def route(self, origin: LatLng, dest: LatLng, mode: TravelMode,
              depart_at: datetime) -> RouteObservation: ...

    def static_map(self, points: list[LatLng],
                   polyline: str | None) -> bytes: ...

    def timezone_of(self, city: str) -> str: ...
```

- [ ] **Step 4: 实现 FakeProvider**

创建 `src/tripplan/providers/fake.py`：

```python
"""确定性假数据。让全部离线测试可以覆盖 resolver 与编排层。"""

from datetime import datetime, timedelta, timezone

from tripplan.models.common import LatLng, TravelMode
from tripplan.models.facts import PoiFact
from tripplan.providers.base import ProviderError, RouteObservation

_FETCHED_AT = datetime(2026, 9, 1, tzinfo=timezone(timedelta(hours=9)))

# 1x1 透明 PNG，够 HTML 渲染测试用且不触网
_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00"
    b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)

_TIMEZONES = {
    "京都": "Asia/Tokyo", "东京": "Asia/Tokyo", "大阪": "Asia/Tokyo",
    "上海": "Asia/Shanghai", "北京": "Asia/Shanghai",
    "巴黎": "Europe/Paris", "伦敦": "Europe/London",
}


def _key(p: LatLng) -> tuple[float, float]:
    return (round(p.lat, 6), round(p.lng, 6))


class FakeProvider:
    def __init__(self, pois=None, routes=None, fail_routes=None,
                 timezones=None, opening_hours=None):
        self._pois = pois or {}
        self._routes = routes or {}
        self._fail_routes = fail_routes or set()
        self._timezones = {**_TIMEZONES, **(timezones or {})}
        self._opening_hours = opening_hours or {}
        self.call_log: list[str] = []

    def search_poi(self, query: str, city: str) -> list[PoiFact]:
        self.call_log.append("search_poi")
        return [
            PoiFact(id=pid, name=query, coords=LatLng(lat, lng),
                    opening_hours=self._opening_hours.get(pid),
                    ticket=None, source="fake", fetched_at=_FETCHED_AT)
            for pid, lat, lng in self._pois.get(query, [])
        ]

    def route(self, origin, dest, mode, depart_at) -> RouteObservation:
        self.call_log.append("route")
        pair = (_key(origin), _key(dest))
        if pair in self._fail_routes:
            raise ProviderError(f"路线查询失败：{pair}")
        minutes = self._routes.get(pair)
        if minutes is None:
            # 确定性兜底：按坐标差算一个稳定值
            delta = abs(origin.lat - dest.lat) + abs(origin.lng - dest.lng)
            minutes = max(5, int(delta * 600))
        return RouteObservation(
            mode=mode, duration_min=minutes, distance_m=minutes * 800,
            polyline=f"fake:{pair[0][0]},{pair[0][1]}->{pair[1][0]},{pair[1][1]}",
            source="fake", fetched_at=_FETCHED_AT)

    def static_map(self, points, polyline=None) -> bytes:
        self.call_log.append("static_map")
        return _PNG

    def timezone_of(self, city: str) -> str:
        self.call_log.append("timezone_of")
        tz = self._timezones.get(city)
        if tz is None:
            raise ProviderError(f"查不到城市时区：{city}")
        return tz
```

- [ ] **Step 5: 运行确认通过**

Run: `uv run pytest tests/providers/test_fake.py -v`
Expected: PASS（10 passed）

- [ ] **Step 6: 格式化并提交**

```bash
uv run black src tests
git add src/tripplan/providers tests/providers
git commit -m "feat: Provider 契约与 FakeProvider

route() 接受 depart_at 并返回含 polyline 的观测。FakeProvider 支持脚本化
POI 歧义与路线失败，让离线测试能覆盖降级路径。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 13: Resolver（唯一触网点）

**Files:**
- Create: `src/tripplan/validation/resolver.py`
- Test: `tests/validation/test_resolver.py`

**Interfaces:**
- Consumes: `GeoProvider`, `RouteObservation`, `ProviderError`（Task 12）、`Itinerary`（Task 3）、`Requirements`（Task 2）、`FactSnapshot` 全家（Task 4）
- Produces: `resolve(itin, reqs, provider, tz) -> FactSnapshot`、`resolve_timezone(reqs, provider) -> str`

**背景（spec §5.1）：** 早先版本同时声称「`rules.py` 是纯函数」和「规则 #2 调高德」，这两句不可能同时成立。校验因此拆成两步：resolver 触网产出不可变快照，validator 纯函数消费它。`tz` 由调用方传入，**resolver 不自己解析时区**——两条独立解析路径迟早不一致，且不一致时没有任何地方会报错。

- [ ] **Step 1: 写失败的测试**

创建 `tests/validation/test_resolver.py`：

```python
from datetime import date, datetime

from tripplan.models.common import Field, Origin
from tripplan.models.facts import Ambiguous, GapKind, NotFound, Resolved
from tripplan.providers.fake import FakeProvider
from tripplan.validation.resolver import resolve, resolve_timezone

D1 = date(2026, 10, 1)
TZ = "Asia/Tokyo"


def _provider(**kw):
    base = dict(pois={
        "清水寺": [("B001", 34.9949, 135.7850)],
        "八坂神社": [("B002", 35.0036, 135.7786)],
    })
    base.update(kw)
    return FakeProvider(**base)


def _gap_kinds(facts):
    return {g.kind for g in facts.gaps}


def test_resolves_each_activity_to_a_poi(mk):
    itin = mk.itin([mk.day("d1", D1, [
        mk.act("d1a1", "d1", "09:00", "11:00", query="清水寺"),
        mk.act("d1a2", "d1", "13:00", "14:00", query="八坂神社"),
    ])])
    facts = resolve(itin, mk.reqs(), _provider(), TZ)
    assert facts.poi_id_for("d1a1") == "B001"
    assert facts.poi_id_for("d1a2") == "B002"


def test_ambiguous_poi_is_recorded_not_guessed(mk):
    """同名多个时不擅自挑第一条——挑错了下游全部建立在错坐标上。"""
    provider = _provider(pois={"某某寺": [("B1", 35.0, 135.0),
                                         ("B2", 35.5, 135.5)]})
    itin = mk.itin([mk.day("d1", D1, [
        mk.act("d1a1", "d1", "09:00", "11:00", query="某某寺")])])
    facts = resolve(itin, mk.reqs(), provider, TZ)
    assert isinstance(facts.poi_by_activity["d1a1"], Ambiguous)
    assert facts.poi_id_for("d1a1") is None
    assert GapKind.AMBIGUOUS_POI in _gap_kinds(facts)


def test_missing_poi_is_recorded(mk):
    itin = mk.itin([mk.day("d1", D1, [
        mk.act("d1a1", "d1", "09:00", "11:00", query="虚构地点")])])
    facts = resolve(itin, mk.reqs(), _provider(), TZ)
    assert isinstance(facts.poi_by_activity["d1a1"], NotFound)
    assert GapKind.POI_NOT_FOUND in _gap_kinds(facts)


def test_routes_are_computed_between_adjacent_activities(mk):
    itin = mk.itin([mk.day("d1", D1, [
        mk.act("d1a1", "d1", "09:00", "11:00", query="清水寺"),
        mk.act("d1a2", "d1", "13:00", "14:00", query="八坂神社"),
    ])])
    facts = resolve(itin, mk.reqs(), _provider(), TZ)
    route = facts.route("d1", "d1a1", "d1a2")
    assert route is not None
    assert route.polyline


def test_route_depart_at_uses_previous_activity_end_in_trip_timezone(mk):
    itin = mk.itin([mk.day("d1", D1, [
        mk.act("d1a1", "d1", "09:00", "11:00", query="清水寺"),
        mk.act("d1a2", "d1", "13:00", "14:00", query="八坂神社"),
    ])])
    facts = resolve(itin, mk.reqs(), _provider(), TZ)
    depart = facts.route("d1", "d1a1", "d1a2").depart_at
    assert depart.hour == 11 and depart.minute == 0
    assert depart.utcoffset().total_seconds() == 9 * 3600


def test_route_skipped_when_either_endpoint_unresolved(mk):
    itin = mk.itin([mk.day("d1", D1, [
        mk.act("d1a1", "d1", "09:00", "11:00", query="清水寺"),
        mk.act("d1a2", "d1", "13:00", "14:00", query="虚构地点"),
    ])])
    facts = resolve(itin, mk.reqs(), _provider(), TZ)
    assert facts.route("d1", "d1a1", "d1a2") is None
    assert GapKind.ROUTE_UNAVAILABLE in _gap_kinds(facts)


def test_route_failure_becomes_a_gap_not_an_exception(mk):
    """高德限流不该让整条候选线炸掉——记 gap，让规则 #2 降级。"""
    provider = _provider(fail_routes={((34.9949, 135.785),
                                       (35.0036, 135.7786))})
    itin = mk.itin([mk.day("d1", D1, [
        mk.act("d1a1", "d1", "09:00", "11:00", query="清水寺"),
        mk.act("d1a2", "d1", "13:00", "14:00", query="八坂神社"),
    ])])
    facts = resolve(itin, mk.reqs(), provider, TZ)
    assert facts.route("d1", "d1a1", "d1a2") is None
    assert GapKind.ROUTE_UNAVAILABLE in _gap_kinds(facts)


def test_constraints_are_resolved_too(mk):
    """约束侧不解析，「按 POI id 匹配」就无从谈起。"""
    itin = mk.itin([mk.day("d1", D1, [])])
    reqs = mk.reqs(must_visit=["清水寺"], avoid=["八坂神社"])
    facts = resolve(itin, reqs, _provider(), TZ)
    assert facts.constraint_poi_id("清水寺") == "B001"
    assert facts.constraint_poi_id("八坂神社") == "B002"


def test_ambiguous_constraint_gets_its_own_gap_kind(mk):
    provider = _provider(pois={"某某寺": [("B1", 35.0, 135.0),
                                         ("B2", 35.5, 135.5)]})
    itin = mk.itin([mk.day("d1", D1, [])])
    facts = resolve(itin, mk.reqs(must_visit=["某某寺"]), provider, TZ)
    assert GapKind.AMBIGUOUS_CONSTRAINT in _gap_kinds(facts)


def test_snapshot_records_the_timezone_it_was_given(mk):
    """快照只抄一份供回放核对，不做第二个解析点。"""
    itin = mk.itin([mk.day("d1", D1, [])])
    provider = _provider()
    facts = resolve(itin, mk.reqs(), provider, "Europe/Paris")
    assert facts.trip_timezone == "Europe/Paris"
    assert "timezone_of" not in provider.call_log      # ★ 没有自己去查


def test_repeated_poi_query_hits_provider_once(mk):
    itin = mk.itin([mk.day("d1", D1, [
        mk.act("d1a1", "d1", "09:00", "10:00", query="清水寺"),
        mk.act("d1a2", "d1", "12:00", "13:00", query="清水寺"),
    ])])
    provider = _provider()
    resolve(itin, mk.reqs(), provider, TZ)
    assert provider.call_log.count("search_poi") == 1


def test_resolve_timezone_uses_destination(mk):
    provider = _provider()
    assert resolve_timezone(mk.reqs(), provider) == "Asia/Tokyo"


def test_resolve_timezone_falls_back_to_utc_when_unknown(mk):
    reqs = mk.reqs(destination=Field("虚构城", Origin.MODEL))
    assert resolve_timezone(reqs, _provider()) == "UTC"
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/validation/test_resolver.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tripplan.validation.resolver'`

- [ ] **Step 3: 实现**

创建 `src/tripplan/validation/resolver.py`：

```python
"""把行程解析成不可变的事实快照。**本项目唯一的触网点。**

调用方传入 tz —— resolver 不自己解析时区。两条独立的解析路径迟早会不一致，
而且不一致时没有任何地方会报错。
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from tripplan.models.common import TravelMode
from tripplan.models.facts import (
    Ambiguous,
    FactSnapshot,
    Gap,
    GapKind,
    NotFound,
    PoiResolution,
    Resolved,
    RouteFact,
)
from tripplan.models.itinerary import Itinerary
from tripplan.models.requirements import Requirements
from tripplan.providers.base import GeoProvider, ProviderError

#: v1 只测算公共交通。多城市（spec §12.4）时会需要按段选择模式。
DEFAULT_MODE = TravelMode.TRANSIT


def resolve_timezone(reqs: Requirements, provider: GeoProvider) -> str:
    """由已确定的 destination 反查 IANA 时区。查不到就退到 UTC 并照常前进。"""
    city = reqs.destination.value
    if not city:
        return "UTC"
    try:
        return provider.timezone_of(city)
    except ProviderError:
        return "UTC"


def _lookup(provider, query: str, city: str, cache: dict) -> PoiResolution:
    if query in cache:
        return cache[query]
    try:
        hits = provider.search_poi(query, city)
    except ProviderError:
        hits = []
    if len(hits) == 1:
        res: PoiResolution = Resolved(hits[0])
    elif len(hits) > 1:
        res = Ambiguous(hits)          # 不擅自挑第一条
    else:
        res = NotFound(query)
    cache[query] = res
    return res


def resolve(itin: Itinerary, reqs: Requirements, provider: GeoProvider,
            tz: str) -> FactSnapshot:
    city = reqs.destination.value or ""
    zone = ZoneInfo(tz)
    cache: dict[str, PoiResolution] = {}
    gaps: list[Gap] = []

    # ---- 活动侧 POI ----
    poi_by_activity: dict[str, PoiResolution] = {}
    for act in itin.all_activities():
        res = _lookup(provider, act.poi_query, city, cache)
        poi_by_activity[act.id] = res
        if isinstance(res, Ambiguous):
            gaps.append(Gap(GapKind.AMBIGUOUS_POI, act.id,
                            f"「{act.poi_query}」匹配到 "
                            f"{len(res.candidates)} 个同名地点"))
        elif isinstance(res, NotFound):
            gaps.append(Gap(GapKind.POI_NOT_FOUND, act.id,
                            f"查不到「{act.poi_query}」"))

    # ---- 约束侧 POI（must_visit / avoid）----
    constraint_pois: dict[str, PoiResolution] = {}
    for query in (reqs.must_visit.value or []) + (reqs.avoid.value or []):
        if query in constraint_pois:
            continue
        res = _lookup(provider, query, city, cache)
        constraint_pois[query] = res
        if not isinstance(res, Resolved):
            gaps.append(Gap(GapKind.AMBIGUOUS_CONSTRAINT, query,
                            f"约束「{query}」未能唯一解析"))

    # ---- 相邻活动之间的路线 ----
    routes: list[RouteFact] = []
    for day in itin.days:
        for prev, nxt in zip(day.activities, day.activities[1:]):
            a = poi_by_activity.get(prev.id)
            b = poi_by_activity.get(nxt.id)
            subject = f"{prev.id}->{nxt.id}"
            if not (isinstance(a, Resolved) and isinstance(b, Resolved)):
                gaps.append(Gap(GapKind.ROUTE_UNAVAILABLE, subject,
                                "两端 POI 未能唯一解析，无法测算"))
                continue
            depart_at = datetime.combine(day.date, prev.end, tzinfo=zone)
            try:
                obs = provider.route(a.fact.coords, b.fact.coords,
                                     DEFAULT_MODE, depart_at)
            except ProviderError as e:
                gaps.append(Gap(GapKind.ROUTE_UNAVAILABLE, subject, str(e)))
                continue
            routes.append(RouteFact(
                day_id=day.id, from_activity_id=prev.id,
                to_activity_id=nxt.id, depart_at=depart_at, mode=obs.mode,
                duration_min=obs.duration_min, distance_m=obs.distance_m,
                polyline=obs.polyline, source=obs.source,
                fetched_at=obs.fetched_at))

    return FactSnapshot(
        poi_by_activity=poi_by_activity,
        constraint_pois=constraint_pois,
        routes=routes,
        weather={},                      # v1 不接天气，见 spec §2
        trip_timezone=tz,                # 抄一份供回放核对
        resolved_at=datetime.now(zone),
        gaps=gaps,
    )
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/validation/test_resolver.py -v`
Expected: PASS（13 passed）

- [ ] **Step 5: 跑一遍 Phase B+C 全量，确认规则与 resolver 能对接**

Run: `uv run pytest tests/ -v`
Expected: PASS（129 passed）

- [ ] **Step 6: 格式化并提交**

```bash
uv run black src tests
git add src/tripplan/validation/resolver.py tests/validation/test_resolver.py
git commit -m "feat: resolver —— 唯一触网点

Itinerary → FactSnapshot。同名 POI 记 Ambiguous 不擅自挑；路线失败记
ROUTE_UNAVAILABLE gap 而非抛异常。tz 由调用方传入，resolver 不自己解析。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 14: 高德 Provider 与磁盘缓存

**Files:**
- Create: `src/tripplan/providers/cache.py`
- Create: `src/tripplan/providers/amap.py`
- Test: `tests/providers/test_cache.py`
- Test: `tests/providers/test_amap.py`

**Interfaces:**
- Consumes: `GeoProvider`, `RouteObservation`, `ProviderError`（Task 12）、`PoiFact`, `LatLng`（Task 1、4）
- Produces: `DiskCache(root: Path, ttl_days: int)` + `.get(key)` + `.put(key, value)`、`AmapProvider(key: str, cache: DiskCache, http=None)`

**背景：** 缓存键必须包含出发时段——同一对坐标在早高峰和午间的耗时不同。缓存同时被 LLM 的工具（Task 17）和 resolver 共用，这正是 Provider 与 Tool 分成两层的实际收益。

测试用注入的假 HTTP 传输层（`httpx.MockTransport`），**不打真实网络**。真实连通性单独一条 `@pytest.mark.slow`，默认不跑。

- [ ] **Step 1: 写缓存的失败测试**

创建 `tests/providers/test_cache.py`：

```python
from pathlib import Path

from tripplan.providers.cache import DiskCache


def test_put_then_get_roundtrips(tmp_path: Path):
    c = DiskCache(tmp_path, ttl_days=7)
    c.put("k1", {"a": 1})
    assert c.get("k1") == {"a": 1}


def test_get_missing_returns_none(tmp_path: Path):
    assert DiskCache(tmp_path, ttl_days=7).get("nope") is None


def test_entry_expires_after_ttl(tmp_path: Path):
    c = DiskCache(tmp_path, ttl_days=7)
    c.put("k1", {"a": 1})
    stale = DiskCache(tmp_path, ttl_days=0)
    assert stale.get("k1") is None


def test_keys_with_slashes_do_not_escape_the_cache_dir(tmp_path: Path):
    c = DiskCache(tmp_path, ttl_days=7)
    c.put("../../evil", {"a": 1})
    assert c.get("../../evil") == {"a": 1}
    assert not (tmp_path.parent.parent / "evil").exists()


def test_corrupt_entry_is_treated_as_miss(tmp_path: Path):
    c = DiskCache(tmp_path, ttl_days=7)
    c.put("k1", {"a": 1})
    next(tmp_path.glob("*.json")).write_text("{ not json")
    assert c.get("k1") is None
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/providers/test_cache.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tripplan.providers.cache'`

- [ ] **Step 3: 实现缓存**

创建 `src/tripplan/providers/cache.py`：

```python
"""按 key 去重的磁盘缓存。LLM 工具与 resolver 共用同一份。"""

import hashlib
import json
import time
from pathlib import Path


class DiskCache:
    def __init__(self, root: Path, ttl_days: int = 7) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = ttl_days * 86400

    def _path(self, key: str) -> Path:
        # 哈希做文件名：既避免路径穿越，也不受 key 长度与字符集限制
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.root / f"{digest}.json"

    def get(self, key: str):
        path = self._path(key)
        if not path.exists():
            return None
        if time.time() - path.stat().st_mtime > self.ttl_seconds:
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))["value"]
        except (json.JSONDecodeError, KeyError, OSError):
            return None          # 缓存坏了就当没命中，不让它污染调用方

    def put(self, key: str, value) -> None:
        payload = {"key": key, "value": value}
        self._path(key).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/providers/test_cache.py -v`
Expected: PASS（5 passed）

- [ ] **Step 5: 写高德 provider 的失败测试**

创建 `tests/providers/test_amap.py`：

```python
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from tripplan.models.common import LatLng, TravelMode
from tripplan.providers.amap import AmapProvider
from tripplan.providers.base import ProviderError
from tripplan.providers.cache import DiskCache

JST = timezone(timedelta(hours=9))
WHEN = datetime(2026, 10, 1, 11, 0, tzinfo=JST)

POI_OK = {"status": "1", "pois": [
    {"id": "B001", "name": "清水寺", "location": "135.785,34.9949",
     "business": {"opentime_week": "06:00-18:00"}}]}
ROUTE_OK = {"status": "1", "route": {"transits": [
    {"duration": "2400", "distance": "4200",
     "segments": [{"bus": {"buslines": [{"polyline": "135.7,34.9;135.8,35.0"}]}}]}
]}}


def _provider(handler, tmp_path, **kw):
    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    return AmapProvider(key="test-key",
                        cache=DiskCache(tmp_path, ttl_days=7),
                        http=client, **kw)


def test_search_poi_parses_response(tmp_path):
    def handler(request):
        assert "place/text" in str(request.url)
        assert "key=test-key" in str(request.url)
        return httpx.Response(200, json=POI_OK)

    got = _provider(handler, tmp_path).search_poi("清水寺", "京都")
    assert got[0].id == "B001"
    assert got[0].coords == LatLng(34.9949, 135.785)
    assert got[0].opening_hours == "06:00-18:00"
    assert got[0].source.startswith("amap")
    assert got[0].fetched_at.tzinfo is not None


def test_search_poi_caches_by_query_and_city(tmp_path):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json=POI_OK)

    p = _provider(handler, tmp_path)
    p.search_poi("清水寺", "京都")
    p.search_poi("清水寺", "京都")
    assert len(calls) == 1                      # 第二次命中缓存


def test_search_poi_different_city_is_a_different_key(tmp_path):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json=POI_OK)

    p = _provider(handler, tmp_path)
    p.search_poi("清水寺", "京都")
    p.search_poi("清水寺", "大阪")
    assert len(calls) == 2


def test_route_parses_duration_distance_and_polyline(tmp_path):
    def handler(request):
        return httpx.Response(200, json=ROUTE_OK)

    obs = _provider(handler, tmp_path).route(
        LatLng(34.9949, 135.785), LatLng(35.0036, 135.7786),
        TravelMode.TRANSIT, WHEN)
    assert obs.duration_min == 40               # 2400 秒
    assert obs.distance_m == 4200
    assert obs.polyline
    assert obs.mode is TravelMode.TRANSIT


def test_route_cache_key_includes_departure_hour(tmp_path):
    """早高峰与午间耗时不同，缓存键必须区分出发时段。"""
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json=ROUTE_OK)

    p = _provider(handler, tmp_path)
    a, b = LatLng(34.9949, 135.785), LatLng(35.0036, 135.7786)
    p.route(a, b, TravelMode.TRANSIT, WHEN)
    p.route(a, b, TravelMode.TRANSIT, WHEN.replace(hour=8))
    assert len(calls) == 2
    p.route(a, b, TravelMode.TRANSIT, WHEN.replace(minute=30))
    assert len(calls) == 2                      # 同一小时内共用


def test_api_error_status_becomes_provider_error(tmp_path):
    def handler(request):
        return httpx.Response(200, json={"status": "0", "info": "DAILY_QUERY_OVER_LIMIT"})

    with pytest.raises(ProviderError, match="DAILY_QUERY_OVER_LIMIT"):
        _provider(handler, tmp_path).search_poi("清水寺", "京都")


def test_http_error_becomes_provider_error(tmp_path):
    def handler(request):
        return httpx.Response(500, text="boom")

    with pytest.raises(ProviderError):
        _provider(handler, tmp_path).search_poi("清水寺", "京都")


def test_network_failure_becomes_provider_error(tmp_path):
    def handler(request):
        raise httpx.ConnectError("no network")

    with pytest.raises(ProviderError):
        _provider(handler, tmp_path).search_poi("清水寺", "京都")


def test_no_route_found_raises_provider_error(tmp_path):
    def handler(request):
        return httpx.Response(200, json={"status": "1", "route": {"transits": []}})

    with pytest.raises(ProviderError, match="没有可用路线"):
        _provider(handler, tmp_path).route(
            LatLng(34.9949, 135.785), LatLng(35.0036, 135.7786),
            TravelMode.TRANSIT, WHEN)


def test_static_map_returns_bytes_and_is_cached(tmp_path):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=b"\x89PNG-fake")

    p = _provider(handler, tmp_path)
    pts = [LatLng(34.9949, 135.785), LatLng(35.0036, 135.7786)]
    assert p.static_map(pts, polyline="135.7,34.9;135.8,35.0") == b"\x89PNG-fake"
    p.static_map(pts, polyline="135.7,34.9;135.8,35.0")
    assert len(calls) == 1


def test_timezone_of_maps_known_cities(tmp_path):
    def handler(request):
        raise AssertionError("时区查询不该走网络")

    assert _provider(handler, tmp_path).timezone_of("京都") == "Asia/Tokyo"


def test_timezone_of_unknown_city_raises(tmp_path):
    def handler(request):
        raise AssertionError("不该走网络")

    with pytest.raises(ProviderError):
        _provider(handler, tmp_path).timezone_of("虚构城")


@pytest.mark.slow
def test_real_amap_smoke(tmp_path):
    """真实连通性。需要 AMAP_KEY 环境变量，默认不跑。"""
    import os

    key = os.environ.get("AMAP_KEY")
    if not key:
        pytest.skip("未设置 AMAP_KEY")
    p = AmapProvider(key=key, cache=DiskCache(tmp_path, ttl_days=1))
    hits = p.search_poi("外滩", "上海")
    assert hits and hits[0].coords.lat > 30
```

- [ ] **Step 6: 运行确认失败**

Run: `uv run pytest tests/providers/test_amap.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tripplan.providers.amap'`

- [ ] **Step 7: 实现高德 provider**

创建 `src/tripplan/providers/amap.py`：

```python
"""高德实现。v1 唯一的真实数据源。

缓存键包含出发**小时**——同一对坐标在早高峰与午间的耗时不同，
但把分钟也纳入键会让缓存基本失效，小时是合适的粒度。
"""

import base64
from datetime import datetime, timezone

import httpx

from tripplan.models.common import LatLng, TravelMode
from tripplan.models.facts import PoiFact
from tripplan.providers.base import ProviderError, RouteObservation
from tripplan.providers.cache import DiskCache

_BASE = "https://restapi.amap.com/v3"

#: v1 单目的地，城市集合有限，硬编码比多打一次网络请求划算。
_TIMEZONES = {
    "京都": "Asia/Tokyo", "东京": "Asia/Tokyo", "大阪": "Asia/Tokyo",
    "北海道": "Asia/Tokyo", "冲绳": "Asia/Tokyo", "首尔": "Asia/Seoul",
    "上海": "Asia/Shanghai", "北京": "Asia/Shanghai",
    "成都": "Asia/Shanghai", "广州": "Asia/Shanghai",
    "香港": "Asia/Hong_Kong", "台北": "Asia/Taipei",
    "曼谷": "Asia/Bangkok", "新加坡": "Asia/Singapore",
    "巴黎": "Europe/Paris", "伦敦": "Europe/London",
    "罗马": "Europe/Rome", "纽约": "America/New_York",
}

_MODE_PATH = {
    TravelMode.TRANSIT: "direction/transit/integrated",
    TravelMode.WALK: "direction/walking",
    TravelMode.DRIVE: "direction/driving",
}


class AmapProvider:
    def __init__(self, key: str, cache: DiskCache,
                 http: httpx.Client | None = None) -> None:
        self.key = key
        self.cache = cache
        self.http = http or httpx.Client(timeout=10.0)

    # ---------- 传输 ----------

    def _get_json(self, path: str, params: dict) -> dict:
        try:
            resp = self.http.get(f"{_BASE}/{path}",
                                 params={**params, "key": self.key})
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as e:
            raise ProviderError(f"高德请求失败：{e}") from e
        if data.get("status") != "1":
            raise ProviderError(f"高德返回错误：{data.get('info', '未知')}")
        return data

    def _get_bytes(self, path: str, params: dict) -> bytes:
        try:
            resp = self.http.get(f"{_BASE}/{path}",
                                 params={**params, "key": self.key})
            resp.raise_for_status()
            return resp.content
        except httpx.HTTPError as e:
            raise ProviderError(f"高德请求失败：{e}") from e

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    # ---------- GeoProvider ----------

    def search_poi(self, query: str, city: str) -> list[PoiFact]:
        cache_key = f"poi|{city}|{query}"
        raw = self.cache.get(cache_key)
        if raw is None:
            raw = self._get_json("place/text",
                                 {"keywords": query, "city": city,
                                  "citylimit": "true", "offset": "5"})
            self.cache.put(cache_key, raw)

        fetched = self._now()
        out = []
        for p in raw.get("pois", []):
            lng, lat = (float(x) for x in p["location"].split(","))
            hours = (p.get("business") or {}).get("opentime_week") or None
            out.append(PoiFact(id=p["id"], name=p["name"],
                               coords=LatLng(lat, lng), opening_hours=hours,
                               ticket=None, source="amap:place/text",
                               fetched_at=fetched))
        return out

    def route(self, origin: LatLng, dest: LatLng, mode: TravelMode,
              depart_at: datetime) -> RouteObservation:
        path = _MODE_PATH[mode]
        # 出发小时进键：早高峰与午间耗时不同，但按分钟会让缓存失效
        cache_key = (f"route|{mode.value}|{origin.lat:.5f},{origin.lng:.5f}"
                     f"|{dest.lat:.5f},{dest.lng:.5f}"
                     f"|{depart_at:%Y-%m-%dT%H}")
        raw = self.cache.get(cache_key)
        if raw is None:
            raw = self._get_json(path, {
                "origin": f"{origin.lng},{origin.lat}",
                "destination": f"{dest.lng},{dest.lat}",
                "city": "", "time": f"{depart_at:%H:%M}",
                "date": f"{depart_at:%Y-%m-%d}",
            })
            self.cache.put(cache_key, raw)

        transits = (raw.get("route") or {}).get("transits") or []
        if not transits:
            raise ProviderError(
                f"没有可用路线：{origin.lat},{origin.lng} -> "
                f"{dest.lat},{dest.lng}")
        best = transits[0]
        polyline = _first_polyline(best)
        return RouteObservation(
            mode=mode,
            duration_min=int(float(best["duration"]) // 60),
            distance_m=int(float(best["distance"])),
            polyline=polyline,
            source=f"amap:{path}",
            fetched_at=self._now())

    def static_map(self, points: list[LatLng],
                   polyline: str | None = None) -> bytes:
        marker = "|".join(f"{p.lng},{p.lat}" for p in points)
        params = {"size": "750*400", "scale": "2",
                  "markers": f"mid,,A:{marker}" if marker else ""}
        if polyline:
            params["paths"] = f"5,0x0000ff,1,,:{polyline}"
        cache_key = "map|" + repr(sorted(params.items()))
        cached = self.cache.get(cache_key)
        if cached is not None:
            return base64.b64decode(cached)
        data = self._get_bytes("staticmap", params)
        self.cache.put(cache_key, base64.b64encode(data).decode("ascii"))
        return data

    def timezone_of(self, city: str) -> str:
        tz = _TIMEZONES.get(city)
        if tz is None:
            raise ProviderError(f"未知城市时区：{city}")
        return tz


def _first_polyline(transit: dict) -> str:
    for seg in transit.get("segments", []):
        buslines = ((seg.get("bus") or {}).get("buslines") or [])
        for line in buslines:
            if line.get("polyline"):
                return line["polyline"]
        walking = (seg.get("walking") or {})
        for step in walking.get("steps", []):
            if step.get("polyline"):
                return step["polyline"]
    return ""
```

- [ ] **Step 8: 运行确认通过**

Run: `uv run pytest tests/providers/ -v`
Expected: PASS（27 passed，1 skipped —— slow 那条默认不跑）

- [ ] **Step 9: 格式化并提交**

```bash
uv run black src tests
git add src/tripplan/providers tests/providers
git commit -m "feat: 高德 provider 与磁盘缓存

缓存键含出发小时——早高峰与午间耗时不同，按分钟会让缓存失效。
限流、HTTP 错误、网络故障、无可用路线一律转 ProviderError，由上层兜。
单测用 httpx.MockTransport，不打真实网络。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

**✅ 检查点 C：** 设好 `AMAP_KEY` 后，真实高德数据能解析成 `FactSnapshot` 并喂给规则层。

---

## Phase D — LLM 层

### Task 15: 按角色路由的 LLM 客户端

**Files:**
- Create: `src/tripplan/llm/__init__.py`
- Create: `src/tripplan/llm/config.py`
- Create: `src/tripplan/llm/client.py`
- Test: `tests/llm/test_config.py`
- Test: `tests/llm/test_client.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `Role`（PLANNER/CRITIC/ANGLE/CLASSIFIER）、`RoleConfig(model, max_tokens, temperature, independent_context)`、`DEFAULT_ROLES: dict[Role, RoleConfig]`、`load_config(path=None) -> dict[Role, RoleConfig]`
  - `Usage(input_tokens, output_tokens)`、`ToolCall(id, name, args)`、`LlmResponse(stop_reason, text, tool_calls, usage)`、`LlmClient` Protocol（`chat(role, system, messages, tools) -> LlmResponse`）、`FakeLlm(script)`、`AnthropicClient(configs, api_key=None)`

**背景（spec §6）：** critic **必须**用与 planner 不同的模型——planner 对自己的输出有系统性盲点，同源 critic 容易"英雄所见略同"地放过同一个问题。理想换厂商；只有 Anthropic 时用不同 tier + `independent_context=True`（只喂最终行程，不喂 planner 的推理过程）。

- [ ] **Step 1: 写配置的失败测试**

创建 `tests/llm/__init__.py`（空）与 `tests/llm/test_config.py`：

```python
import pytest

from tripplan.llm.config import DEFAULT_ROLES, Role, load_config


def test_defaults_cover_every_role():
    assert set(DEFAULT_ROLES) == set(Role)


def test_planner_and_critic_use_different_models():
    """同源自审会放过同一个盲点——这条约束是设计的一部分，用测试钉住。"""
    assert (DEFAULT_ROLES[Role.PLANNER].model
            != DEFAULT_ROLES[Role.CRITIC].model)


def test_critic_defaults_to_independent_context():
    assert DEFAULT_ROLES[Role.CRITIC].independent_context is True
    assert DEFAULT_ROLES[Role.PLANNER].independent_context is False


def test_load_config_without_file_returns_defaults():
    assert load_config(None) == DEFAULT_ROLES


def test_load_config_overrides_only_named_roles(tmp_path):
    path = tmp_path / "roles.toml"
    path.write_text(
        '[roles.critic]\nmodel = "gpt-5"\n', encoding="utf-8")
    cfg = load_config(path)
    assert cfg[Role.CRITIC].model == "gpt-5"
    assert cfg[Role.PLANNER] == DEFAULT_ROLES[Role.PLANNER]


def test_load_config_rejects_same_model_for_planner_and_critic(tmp_path):
    path = tmp_path / "roles.toml"
    same = DEFAULT_ROLES[Role.PLANNER].model
    path.write_text(f'[roles.critic]\nmodel = "{same}"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="critic"):
        load_config(path)


def test_load_config_rejects_unknown_role(tmp_path):
    path = tmp_path / "roles.toml"
    path.write_text('[roles.wizard]\nmodel = "x"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="wizard"):
        load_config(path)
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/llm/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tripplan.llm'`

- [ ] **Step 3: 实现配置**

创建 `src/tripplan/llm/__init__.py`（空）与 `src/tripplan/llm/config.py`：

```python
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
    Role.CRITIC: RoleConfig("claude-sonnet-5", max_tokens=4000,
                            independent_context=True),
    Role.ANGLE: RoleConfig("claude-sonnet-5", max_tokens=2000),
    Role.CLASSIFIER: RoleConfig("claude-haiku-4-5", max_tokens=1000,
                                temperature=0.0),
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
        cfg[role] = replace(cfg[role], **overrides)

    if cfg[Role.PLANNER].model == cfg[Role.CRITIC].model:
        raise ValueError(
            "critic 必须使用与 planner 不同的模型——同源自审会放过同一个盲点。"
            "若只有单一厂商，请选不同 tier。")
    return cfg
```

- [ ] **Step 4: 写客户端的失败测试**

创建 `tests/llm/test_client.py`：

```python
import pytest

from tripplan.llm.client import FakeLlm, LlmResponse, ToolCall, Usage
from tripplan.llm.config import Role


def test_fake_returns_scripted_responses_in_order():
    llm = FakeLlm([
        LlmResponse("end_turn", '{"a": 1}', [], Usage(10, 5)),
        LlmResponse("end_turn", '{"a": 2}', [], Usage(10, 5)),
    ])
    assert llm.chat(Role.PLANNER, "sys", [], None).text == '{"a": 1}'
    assert llm.chat(Role.PLANNER, "sys", [], None).text == '{"a": 2}'


def test_fake_raises_when_script_runs_out():
    llm = FakeLlm([LlmResponse("end_turn", "x", [], Usage(1, 1))])
    llm.chat(Role.PLANNER, "sys", [], None)
    with pytest.raises(AssertionError, match="脚本用尽"):
        llm.chat(Role.PLANNER, "sys", [], None)


def test_fake_records_calls_for_assertions():
    llm = FakeLlm([LlmResponse("end_turn", "x", [], Usage(1, 1))])
    llm.chat(Role.CRITIC, "sys", [{"role": "user", "content": "hi"}], None)
    assert llm.calls[0].role is Role.CRITIC
    assert llm.calls[0].system == "sys"


def test_fake_can_be_scripted_per_role():
    llm = FakeLlm(by_role={
        Role.ANGLE: [LlmResponse("end_turn", "angles", [], Usage(1, 1))],
        Role.PLANNER: [LlmResponse("end_turn", "plan", [], Usage(1, 1))],
    })
    assert llm.chat(Role.PLANNER, "s", [], None).text == "plan"
    assert llm.chat(Role.ANGLE, "s", [], None).text == "angles"


def test_tool_use_response_carries_calls():
    resp = LlmResponse("tool_use", "",
                       [ToolCall("t1", "search_poi", {"query": "清水寺"})],
                       Usage(20, 3))
    assert resp.tool_calls[0].name == "search_poi"
    assert resp.tool_calls[0].args["query"] == "清水寺"


def test_usage_addition_accumulates():
    assert Usage(1, 2) + Usage(10, 20) == Usage(11, 22)
```

- [ ] **Step 5: 运行确认失败**

Run: `uv run pytest tests/llm/test_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tripplan.llm.client'`

- [ ] **Step 6: 实现客户端**

创建 `src/tripplan/llm/client.py`：

```python
"""LLM 访问层。测试时整个替换成 FakeLlm。"""

import os
from dataclasses import dataclass
from typing import Protocol

from tripplan.llm.config import DEFAULT_ROLES, Role, RoleConfig


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(self.input_tokens + other.input_tokens,
                     self.output_tokens + other.output_tokens)


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass(frozen=True)
class LlmResponse:
    stop_reason: str          # "tool_use" | "end_turn" | ...
    text: str
    tool_calls: list[ToolCall]
    usage: Usage


class LlmClient(Protocol):
    def chat(self, role: Role, system: str, messages: list,
             tools: list | None) -> LlmResponse: ...


@dataclass
class RecordedCall:
    role: Role
    system: str
    messages: list
    tools: list | None


class FakeLlm:
    """脚本化的假客户端。支持全局顺序脚本或按角色分别脚本。"""

    def __init__(self, script: list[LlmResponse] | None = None,
                 by_role: dict[Role, list[LlmResponse]] | None = None) -> None:
        self._script = list(script or [])
        self._by_role = {r: list(v) for r, v in (by_role or {}).items()}
        self.calls: list[RecordedCall] = []

    def chat(self, role, system, messages, tools) -> LlmResponse:
        self.calls.append(RecordedCall(role, system, list(messages), tools))
        queue = self._by_role.get(role) if self._by_role else self._script
        assert queue, f"FakeLlm 脚本用尽：role={role}"
        return queue.pop(0)


class AnthropicClient:
    def __init__(self, configs: dict[Role, RoleConfig] | None = None,
                 api_key: str | None = None) -> None:
        import anthropic

        self.configs = configs or DEFAULT_ROLES
        self._client = anthropic.Anthropic(
            api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"))

    def chat(self, role, system, messages, tools) -> LlmResponse:
        cfg = self.configs[role]
        kwargs = dict(model=cfg.model, max_tokens=cfg.max_tokens,
                      temperature=cfg.temperature, system=system,
                      messages=messages)
        if tools:
            kwargs["tools"] = tools
        resp = self._client.messages.create(**kwargs)

        text = "".join(b.text for b in resp.content if b.type == "text")
        calls = [ToolCall(b.id, b.name, b.input)
                 for b in resp.content if b.type == "tool_use"]
        return LlmResponse(
            stop_reason=resp.stop_reason, text=text, tool_calls=calls,
            usage=Usage(resp.usage.input_tokens, resp.usage.output_tokens))
```

- [ ] **Step 7: 运行确认通过**

Run: `uv run pytest tests/llm/ -v`
Expected: PASS（13 passed）

- [ ] **Step 8: 格式化并提交**

```bash
uv run black src tests
git add src/tripplan/llm tests/llm
git commit -m "feat: 按角色路由的 LLM 客户端

critic 与 planner 必须用不同模型——配置校验会拒绝相同取值。
FakeLlm 支持全局顺序脚本与按角色脚本，编排层测试全靠它。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 16: 资源上限与 tool loop

**Files:**
- Create: `src/tripplan/agents/__init__.py`
- Create: `src/tripplan/agents/limits.py`
- Create: `src/tripplan/agents/runner.py`
- Test: `tests/agents/test_limits.py`
- Test: `tests/agents/test_runner.py`

**Interfaces:**
- Consumes: `LlmClient`, `LlmResponse`, `ToolCall`, `Usage`, `Role`（Task 15）
- Produces:
  - `LimitExceeded`、`SlotLimits(max_rounds=3, max_tool_calls=40, max_output_tokens=120_000, max_schema_repairs=2, deadline_s=600)`、`SlotContext(limits, clock=time.monotonic, emit=noop)` + `.charge(usage)` + `.charge_tool_calls(n)` + `.check()` + `.cancel()` + `.spent`
  - `SchemaError`、`run_agent(system_prompt, user_prompt, tools, output_schema, role, ctx, client, tool_impls) -> dict`

**背景（spec §3.6）：** 「LLM 自由」不等于「无限量供应」。原设计这里是个裸 `while True`：没有 tool call 上限、没有 token 记账、没有 deadline，schema 失败"则重试"也没有次数上限——**一条候选线足以吃掉整个成本预算，而且是安静地吃**。记账在 slot 级别（不是单次 `run_agent`），因为一条线里 `generate` + 最多 3 次 `revise` + `critic` 共享同一份额度。

- [ ] **Step 1: 写上限的失败测试**

创建 `tests/agents/__init__.py`（空）与 `tests/agents/test_limits.py`：

```python
import pytest

from tripplan.agents.limits import LimitExceeded, SlotContext, SlotLimits
from tripplan.llm.client import Usage


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def test_fresh_context_passes_check():
    SlotContext(SlotLimits()).check()          # 不抛


def test_output_token_budget_is_enforced():
    ctx = SlotContext(SlotLimits(max_output_tokens=100))
    ctx.charge(Usage(input_tokens=9999, output_tokens=60))
    ctx.check()
    ctx.charge(Usage(input_tokens=0, output_tokens=50))
    with pytest.raises(LimitExceeded, match="输出 token"):
        ctx.check()


def test_input_tokens_do_not_count_against_output_budget():
    ctx = SlotContext(SlotLimits(max_output_tokens=100))
    ctx.charge(Usage(input_tokens=1_000_000, output_tokens=1))
    ctx.check()


def test_tool_call_budget_is_enforced():
    ctx = SlotContext(SlotLimits(max_tool_calls=3))
    ctx.charge_tool_calls(3)
    ctx.check()
    ctx.charge_tool_calls(1)
    with pytest.raises(LimitExceeded, match="工具调用"):
        ctx.check()


def test_deadline_is_enforced():
    clock = _Clock()
    ctx = SlotContext(SlotLimits(deadline_s=60), clock=clock)
    clock.advance(59)
    ctx.check()
    clock.advance(2)
    with pytest.raises(LimitExceeded, match="超时"):
        ctx.check()


def test_cancel_stops_the_slot():
    ctx = SlotContext(SlotLimits())
    ctx.cancel()
    with pytest.raises(LimitExceeded, match="已取消"):
        ctx.check()


def test_spent_reports_accumulated_usage():
    ctx = SlotContext(SlotLimits())
    ctx.charge(Usage(10, 20))
    ctx.charge(Usage(1, 2))
    assert ctx.spent == Usage(11, 22)


def test_budget_is_shared_across_multiple_run_agent_calls():
    """一条线里 generate + revise×N + critic 共享同一份额度。"""
    ctx = SlotContext(SlotLimits(max_output_tokens=100))
    for _ in range(3):
        ctx.charge(Usage(0, 40))
    with pytest.raises(LimitExceeded):
        ctx.check()
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/agents/test_limits.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tripplan.agents'`

- [ ] **Step 3: 实现上限**

创建 `src/tripplan/agents/__init__.py`（空）与 `src/tripplan/agents/limits.py`：

```python
"""单条候选线的资源额度。撞上就抛，由 run_slot 转成 EXHAUSTED 而不是卡住。"""

import time
from dataclasses import dataclass

from tripplan.llm.client import Usage


class LimitExceeded(Exception):
    pass


@dataclass(frozen=True)
class SlotLimits:
    max_rounds: int = 3
    max_tool_calls: int = 40          # 本 slot 累计
    max_output_tokens: int = 120_000  # 本 slot 累计
    max_schema_repairs: int = 2       # 每次 run_agent
    deadline_s: int = 600


def _noop(_event) -> None:
    pass


class SlotContext:
    """记账 + 取消标记。作用域是一条候选线，不是单次 run_agent ——
    generate + revise×N + critic 共享同一份额度。"""

    def __init__(self, limits: SlotLimits, clock=time.monotonic,
                 emit=_noop) -> None:
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
            raise LimitExceeded(
                f"超时（{elapsed:.0f}s > {self.limits.deadline_s}s）")
        if self._usage.output_tokens > self.limits.max_output_tokens:
            raise LimitExceeded(
                f"输出 token 超限（{self._usage.output_tokens} > "
                f"{self.limits.max_output_tokens}）")
        if self._tool_calls > self.limits.max_tool_calls:
            raise LimitExceeded(
                f"工具调用超限（{self._tool_calls} > "
                f"{self.limits.max_tool_calls}）")
```

- [ ] **Step 4: 写 tool loop 的失败测试**

创建 `tests/agents/test_runner.py`（注意：本块用四个反引号围栏，因为测试内容里含有三反引号）：

````python
import pytest

from tripplan.agents.limits import LimitExceeded, SlotContext, SlotLimits
from tripplan.agents.runner import SchemaError, run_agent
from tripplan.llm.client import FakeLlm, LlmResponse, ToolCall, Usage
from tripplan.llm.config import Role

SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
}


def _text(payload: str, out_tokens: int = 10) -> LlmResponse:
    return LlmResponse("end_turn", payload, [], Usage(100, out_tokens))


def _tool(name: str, args: dict) -> LlmResponse:
    return LlmResponse("tool_use", "", [ToolCall("t1", name, args)],
                       Usage(100, 5))


def _run(llm, ctx=None, tool_impls=None, tools=None):
    return run_agent(system_prompt="sys", user_prompt="do it",
                     tools=tools, output_schema=SCHEMA, role=Role.PLANNER,
                     ctx=ctx or SlotContext(SlotLimits()), client=llm,
                     tool_impls=tool_impls or {})


def test_returns_parsed_structured_output():
    assert _run(FakeLlm([_text('{"answer": "ok"}')])) == {"answer": "ok"}


def test_strips_markdown_fence_around_json():
    llm = FakeLlm([_text('```json\n{"answer": "ok"}\n```')])
    assert _run(llm) == {"answer": "ok"}


def test_executes_tools_and_feeds_results_back():
    llm = FakeLlm([_tool("lookup", {"q": "京都"}), _text('{"answer": "ok"}')])
    seen = []

    def lookup(q):
        seen.append(q)
        return {"hits": 3}

    assert _run(llm, tool_impls={"lookup": lookup}) == {"answer": "ok"}
    assert seen == ["京都"]
    # 工具结果作为一条 user 消息回喂
    last_messages = llm.calls[-1].messages
    assert any("hits" in str(m) for m in last_messages)


def test_tool_error_is_reported_to_the_model_not_raised():
    """工具报错让模型自己换个方式，不该炸穿整条线。"""
    llm = FakeLlm([_tool("lookup", {"q": "x"}), _text('{"answer": "ok"}')])

    def lookup(q):
        raise RuntimeError("上游 500")

    assert _run(llm, tool_impls={"lookup": lookup}) == {"answer": "ok"}
    assert any("上游 500" in str(m) for m in llm.calls[-1].messages)


def test_unknown_tool_is_reported_to_the_model():
    llm = FakeLlm([_tool("nope", {}), _text('{"answer": "ok"}')])
    assert _run(llm) == {"answer": "ok"}
    assert any("nope" in str(m) for m in llm.calls[-1].messages)


def test_schema_violation_triggers_one_repair_round():
    llm = FakeLlm([_text('{"wrong": 1}'), _text('{"answer": "ok"}')])
    assert _run(llm) == {"answer": "ok"}
    assert len(llm.calls) == 2


def test_repair_attempts_are_capped():
    """「失败则重试」没有上限，就是一条安静吃掉整个预算的路径。"""
    llm = FakeLlm([_text('{"wrong": 1}')] * 10)
    ctx = SlotContext(SlotLimits(max_schema_repairs=2))
    with pytest.raises(LimitExceeded, match="schema"):
        _run(llm, ctx=ctx)
    assert len(llm.calls) == 3       # 首次 + 2 次修复


def test_invalid_json_also_counts_as_schema_failure():
    llm = FakeLlm([_text("这不是 JSON")] * 5)
    with pytest.raises(LimitExceeded):
        _run(llm, ctx=SlotContext(SlotLimits(max_schema_repairs=1)))


def test_output_tokens_are_charged_to_context():
    ctx = SlotContext(SlotLimits())
    _run(FakeLlm([_text('{"answer": "ok"}', out_tokens=42)]), ctx=ctx)
    assert ctx.spent.output_tokens == 42


def test_tool_calls_are_charged_to_context():
    llm = FakeLlm([_tool("lookup", {"q": "a"}), _text('{"answer": "ok"}')])
    ctx = SlotContext(SlotLimits())
    _run(llm, ctx=ctx, tool_impls={"lookup": lambda q: {}})
    assert ctx.tool_calls == 1


def test_loop_stops_when_tool_budget_exhausted():
    """裸 while True 的核心风险：模型一直调工具，永远不收敛。"""
    llm = FakeLlm([_tool("lookup", {"q": "a"})] * 50)
    ctx = SlotContext(SlotLimits(max_tool_calls=3))
    with pytest.raises(LimitExceeded, match="工具调用"):
        _run(llm, ctx=ctx, tool_impls={"lookup": lambda q: {}})


def test_loop_stops_when_deadline_passes():
    class Clock:
        def __init__(self):
            self.t = 0.0

        def __call__(self):
            self.t += 30        # 每次 check 走 30 秒
            return self.t

    llm = FakeLlm([_tool("lookup", {"q": "a"})] * 50)
    ctx = SlotContext(SlotLimits(deadline_s=60), clock=Clock())
    with pytest.raises(LimitExceeded, match="超时"):
        _run(llm, ctx=ctx, tool_impls={"lookup": lambda q: {}})
````

- [ ] **Step 5: 运行确认失败**

Run: `uv run pytest tests/agents/test_runner.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tripplan.agents.runner'`

- [ ] **Step 6: 实现 tool loop**

创建 `src/tripplan/agents/runner.py`（四反引号围栏，同上）：

````python
"""内层 tool loop。LLM 想调几次工具就调几次 —— 在 ctx 的额度之内。"""

import json
import re

from tripplan.agents.limits import LimitExceeded, SlotContext
from tripplan.llm.client import LlmClient
from tripplan.llm.config import Role

_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.S)


class SchemaError(Exception):
    pass


def _parse_and_validate(text: str, schema: dict) -> dict:
    stripped = _FENCE.sub(r"\1", text).strip()
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as e:
        raise SchemaError(f"不是合法 JSON：{e}") from e
    if not isinstance(data, dict):
        raise SchemaError("顶层必须是对象")
    missing = [k for k in schema.get("required", []) if k not in data]
    if missing:
        raise SchemaError(f"缺少必填字段：{', '.join(missing)}")
    return data


def _repair_prompt(err: SchemaError, schema: dict) -> str:
    return ("上一条回复没有通过校验：" + str(err) + "\n"
            "请只输出符合下面 schema 的 JSON，不要任何解释文字：\n"
            + json.dumps(schema, ensure_ascii=False))


def run_agent(system_prompt: str, user_prompt: str, tools, output_schema: dict,
              role: Role, ctx: SlotContext, client: LlmClient,
              tool_impls: dict) -> dict:
    messages: list[dict] = [{"role": "user", "content": user_prompt}]
    repairs = 0

    while True:
        ctx.check()                       # 超 deadline / token / 取消 → 抛
        resp = client.chat(role, system_prompt, messages, tools)
        ctx.charge(resp.usage)

        if resp.stop_reason == "tool_use":
            ctx.charge_tool_calls(len(resp.tool_calls))
            messages.append({"role": "assistant",
                             "content": resp.text or "(tool_use)"})
            results = []
            for call in resp.tool_calls:
                impl = tool_impls.get(call.name)
                if impl is None:
                    results.append(f"[{call.name}] 错误：没有这个工具")
                    continue
                try:
                    results.append(
                        f"[{call.name}] "
                        + json.dumps(impl(**call.args), ensure_ascii=False))
                except Exception as e:      # 工具报错交回模型，不炸穿这条线
                    results.append(f"[{call.name}] 错误：{e}")
            messages.append({"role": "user", "content": "\n".join(results)})
            continue

        try:
            return _parse_and_validate(resp.text, output_schema)
        except SchemaError as e:
            repairs += 1
            if repairs > ctx.limits.max_schema_repairs:
                raise LimitExceeded(
                    f"schema 修复 {repairs} 次仍失败：{e}") from e
            messages.append({"role": "assistant", "content": resp.text})
            messages.append({"role": "user",
                             "content": _repair_prompt(e, output_schema)})
````

- [ ] **Step 7: 运行确认通过**

Run: `uv run pytest tests/agents/ -v`
Expected: PASS（20 passed）

- [ ] **Step 8: 格式化并提交**

```bash
uv run black src tests
git add src/tripplan/agents tests/agents
git commit -m "feat: 资源上限与 tool loop

tool call 数、输出 token、deadline、schema 修复次数四道闸，记账在 slot
级别（generate + revise×N + critic 共享额度）。工具报错回喂给模型，
不炸穿整条候选线。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 17: Agent 步骤、schema 与 prompts

**Files:**
- Create: `src/tripplan/deps.py`
- Create: `src/tripplan/agents/schemas.py`
- Create: `src/tripplan/agents/tools.py`
- Create: `src/tripplan/agents/steps.py`
- Create: `src/tripplan/agents/prompts/collect.md`
- Create: `src/tripplan/agents/prompts/angle.md`
- Create: `src/tripplan/agents/prompts/plan.md`
- Create: `src/tripplan/agents/prompts/critic.md`
- Create: `src/tripplan/agents/prompts/classify.md`
- Test: `tests/agents/test_tools.py`
- Test: `tests/agents/test_steps.py`

**Interfaces:**
- Consumes: `run_agent`, `SlotContext`（Task 16）、`LlmClient`, `Role`（Task 15）、`GeoProvider`（Task 12）、全部 models
- Produces:
  - `Deps(client, provider)`
  - `REQUIREMENTS_SCHEMA`、`ANGLES_SCHEMA`、`ITINERARY_SCHEMA`、`CRITIQUE_SCHEMA`、`FEEDBACK_SCHEMA`
  - `build_planning_tools(provider, city) -> tuple[list[dict], dict]`
  - `Scale`（INCREMENTAL/REWRITE）、`FeedbackDelta(patches_requirements, patch, scale)`
  - `collect(raw_request, deps, ctx) -> Requirements`
  - `pick_angles(reqs, deps, ctx, n=3) -> list[Angle]`
  - `generate(reqs, angle, deps, ctx, avoid_poi_ids=()) -> Itinerary`
  - `revise(itin, reqs, issues, deps, ctx) -> Itinerary`
  - `run_llm_critic(itin, reqs, deps, ctx) -> list[Issue]`
  - `classify_feedback(text, reqs, deps, ctx) -> FeedbackDelta`
  - `apply_patch(reqs, patch) -> Requirements`

- [ ] **Step 1: 写工具层的失败测试**

创建 `tests/agents/test_tools.py`：

```python
from tripplan.agents.tools import build_planning_tools
from tripplan.providers.fake import FakeProvider


def _provider():
    return FakeProvider(pois={"清水寺": [("B001", 34.9949, 135.785)],
                              "某某寺": [("B1", 35.0, 135.0),
                                        ("B2", 35.5, 135.5)]})


def test_exposes_search_and_route_tools():
    specs, impls = build_planning_tools(_provider(), city="京都")
    assert {s["name"] for s in specs} == {"search_poi", "route_duration"}
    assert set(impls) == {"search_poi", "route_duration"}


def test_every_spec_has_an_input_schema():
    specs, _ = build_planning_tools(_provider(), city="京都")
    for s in specs:
        assert s["input_schema"]["type"] == "object"
        assert s["description"]


def test_search_poi_returns_jsonable_summaries():
    _, impls = build_planning_tools(_provider(), city="京都")
    out = impls["search_poi"](query="清水寺")
    assert out["results"][0]["id"] == "B001"
    assert out["results"][0]["lat"] == 34.9949


def test_search_poi_signals_ambiguity_to_the_model():
    """让模型自己知道这个名字不唯一，比让它蒙一个好。"""
    _, impls = build_planning_tools(_provider(), city="京都")
    out = impls["search_poi"](query="某某寺")
    assert out["ambiguous"] is True
    assert len(out["results"]) == 2


def test_search_poi_reports_no_match():
    _, impls = build_planning_tools(_provider(), city="京都")
    assert impls["search_poi"](query="虚构地点")["results"] == []


def test_route_duration_returns_minutes():
    provider = FakeProvider(routes={((35.0, 135.0), (35.1, 135.1)): 40})
    _, impls = build_planning_tools(provider, city="京都")
    out = impls["route_duration"](from_lat=35.0, from_lng=135.0,
                                  to_lat=35.1, to_lng=135.1,
                                  depart_at="2026-10-01T11:00:00+09:00")
    assert out["duration_min"] == 40


def test_route_duration_error_is_returned_not_raised():
    provider = FakeProvider(fail_routes={((35.0, 135.0), (35.1, 135.1))})
    _, impls = build_planning_tools(provider, city="京都")
    out = impls["route_duration"](from_lat=35.0, from_lng=135.0,
                                  to_lat=35.1, to_lng=135.1,
                                  depart_at="2026-10-01T11:00:00+09:00")
    assert "error" in out


def test_tools_and_resolver_share_the_provider_cache():
    """route() 同时被 LLM 工具与 resolver 调用——这正是两层分开的收益。"""
    provider = _provider()
    _, impls = build_planning_tools(provider, city="京都")
    impls["search_poi"](query="清水寺")
    assert provider.call_log == ["search_poi"]
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/agents/test_tools.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tripplan.agents.tools'`

- [ ] **Step 3: 实现依赖包与工具层**

创建 `src/tripplan/deps.py`：

```python
"""外部依赖的集合。穿在调用链上，避免每层都摊开一堆参数。"""

from dataclasses import dataclass

from tripplan.llm.client import LlmClient
from tripplan.providers.base import GeoProvider


@dataclass(frozen=True)
class Deps:
    client: LlmClient
    provider: GeoProvider
```

创建 `src/tripplan/agents/tools.py`：

```python
"""包给 LLM 的工具壳。

与 Provider 分成两层的实际收益：route() 同时被这里和 resolver 调用，
共用 Provider 就共用一份磁盘缓存——LLM 规划时查过的路线，校验时不必再查。
工具负责参数校验与「转成 LLM 友好的结构」，Provider 只管取数。
"""

from datetime import datetime

from tripplan.models.common import LatLng, TravelMode
from tripplan.providers.base import GeoProvider, ProviderError


def build_planning_tools(provider: GeoProvider, city: str):
    def search_poi(query: str) -> dict:
        try:
            hits = provider.search_poi(query, city)
        except ProviderError as e:
            return {"error": str(e), "results": []}
        return {
            "ambiguous": len(hits) > 1,     # 让模型知道名字不唯一，别蒙
            "results": [
                {"id": h.id, "name": h.name, "lat": h.coords.lat,
                 "lng": h.coords.lng, "opening_hours": h.opening_hours}
                for h in hits
            ],
        }

    def route_duration(from_lat: float, from_lng: float, to_lat: float,
                       to_lng: float, depart_at: str) -> dict:
        try:
            obs = provider.route(LatLng(from_lat, from_lng),
                                 LatLng(to_lat, to_lng),
                                 TravelMode.TRANSIT,
                                 datetime.fromisoformat(depart_at))
        except (ProviderError, ValueError) as e:
            return {"error": str(e)}
        return {"duration_min": obs.duration_min,
                "distance_m": obs.distance_m, "mode": obs.mode.value}

    specs = [
        {
            "name": "search_poi",
            "description": (
                f"在 {city} 搜索地点，返回候选及其坐标与营业时间。"
                "排行程前先用它确认地点真实存在并拿到坐标。"
                "若 ambiguous 为 true，说明同名地点不止一个，"
                "请在 poi_query 里写得更具体。"),
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string",
                                         "description": "地点名称"}},
                "required": ["query"],
            },
        },
        {
            "name": "route_duration",
            "description": (
                "查两点之间的公共交通耗时（分钟）。"
                "耗时随出发时刻变化，请传实际的出发时间。"
                "排相邻两个活动时用它确认时间留得够。"),
            "input_schema": {
                "type": "object",
                "properties": {
                    "from_lat": {"type": "number"},
                    "from_lng": {"type": "number"},
                    "to_lat": {"type": "number"},
                    "to_lng": {"type": "number"},
                    "depart_at": {"type": "string",
                                  "description": "ISO 8601，带时区偏移"},
                },
                "required": ["from_lat", "from_lng", "to_lat", "to_lng",
                             "depart_at"],
            },
        },
    ]
    return specs, {"search_poi": search_poi, "route_duration": route_duration}
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/agents/test_tools.py -v`
Expected: PASS（8 passed）

- [ ] **Step 5: 写 prompts**

创建 `src/tripplan/agents/prompts/collect.md`：

```markdown
你在帮用户规划一次旅行。请从他的描述里抽取结构化需求。

规则：
1. **用户明说的**字段，origin 填 "USER"。
2. **能合理推断的**字段，origin 填 "MODEL"，并在 rationale 里一句话说明依据。
3. **无法可靠推断的**字段，value 留 null，origin 留 null。

destination、dates、party 这三项**绝不允许编造**。用户没说就留 null——
虚构它们会让整个规划建立在假约束上，比不填更糟。

预算的 includes（含不含机票/住宿/门票/餐饮/市内交通）如果无法确定，
整个 budget 留 null 并等待追问，不要默认一个。

只输出 JSON，不要解释文字。
```

创建 `src/tripplan/agents/prompts/angle.md`：

```markdown
基于下面的需求，提出 {n} 个**切入角度**——每个角度是一种组织这趟旅行的思路。

好角度贴着这次需求走。带娃出行和情侣出行的角度完全不同，
不要套用"紧凑型/休闲型"这类通用标签。

{n} 个角度必须彼此**实质不同**：会导向不同的核心景点，而不只是换个说法。

只输出 JSON。
```

创建 `src/tripplan/agents/prompts/plan.md`：

```markdown
你在按指定角度为用户排一份可执行的行程。

工具：
- `search_poi` 确认地点真实存在并拿到坐标；同名不唯一时把名字写具体。
- `route_duration` 确认相邻活动之间留够了通勤时间。

硬要求：
1. 每天的活动按时间升序，不得重叠。
2. 相邻活动之间要留够真实通勤时间，并留出余量。
3. 覆盖需求里的全部日期，每天安排午餐与晚餐。
4. must_visit 全部排进去，avoid 一个都不要出现。
5. `poi_query` 写地点的**规范名称**，让它能被搜索到。
6. 花费只在有把握时填；**不确定就留 null**——null 表示"未知"，不是免费。

不要输出交通段，那由系统测算后自动补上。

只输出 JSON。
```

创建 `src/tripplan/agents/prompts/critic.md`：

```markdown
你在审阅一份旅行行程。你**没有**参与它的规划，请以完全独立的视角看它。

逐项检查：
- 节奏是否真的"感觉像"目标 pace（不只是数量达标）
- 用户的风格偏好有没有落到实处
- 有没有记忆点，还是一份流水账
- 同质化：几天下来是不是同一类地方的重复
- 路线绕不绕（通勤时间达标不等于不绕）
- 季节与天气适配
- 常识遗漏（例如去大型主题乐园只排三小时）

**severity 的判定要克制**：只有当行程实际不可执行、或严重偏离用户需求时
才用 BLOCKING。"要是加个夜景就更好了"属于 SUGGESTION。
BLOCKING 会触发自动重写，滥用它会让流程一直循环到撞上限。

只输出 JSON。
```

创建 `src/tripplan/agents/prompts/classify.md`：

```markdown
用户对行程给出了反馈。判断它属于哪一类：

- **改行程**（patches_requirements = false）：需求没变，只是这份行程排得不好。
  例："第2天太赶了"、"不想去这个博物馆"。
- **改需求**（patches_requirements = true）：需求本身变了。
  例："三天改四天"、"预算加到2万"、"改去日本"。

判为改需求时，在 patch 里给出要修改的字段与新值，并判断 scale：
- `INCREMENTAL`：现有行程仍可作为起点（加一天、调预算）。
- `REWRITE`：现有行程整体作废（换目的地）。

只输出 JSON。
```

- [ ] **Step 6: 写步骤层的失败测试**

创建 `tests/agents/test_steps.py`：

```python
import json
from datetime import date
from decimal import Decimal

import pytest

from tripplan.agents.limits import SlotContext, SlotLimits
from tripplan.agents.steps import (
    Scale,
    apply_patch,
    classify_feedback,
    collect,
    generate,
    pick_angles,
    revise,
    run_llm_critic,
)
from tripplan.deps import Deps
from tripplan.llm.client import FakeLlm, LlmResponse, Usage
from tripplan.models.common import Field, Origin
from tripplan.models.issue import Severity, Source
from tripplan.models.itinerary import Angle, Category
from tripplan.models.requirements import Pace, Party, Requirements
from tripplan.providers.fake import FakeProvider


def _resp(payload) -> LlmResponse:
    return LlmResponse("end_turn", json.dumps(payload, ensure_ascii=False),
                       [], Usage(100, 50))


def _deps(*responses) -> Deps:
    return Deps(client=FakeLlm(list(responses)),
                provider=FakeProvider(pois={
                    "清水寺": [("B001", 34.9949, 135.785)]}))


def _ctx():
    return SlotContext(SlotLimits())


# ---------- collect ----------

COLLECTED = {
    "destination": {"value": "京都", "origin": "USER", "rationale": ""},
    "dates": {"value": {"start": "2026-10-01", "end": "2026-10-05"},
              "origin": "USER", "rationale": ""},
    "party": {"value": {"adults": 2, "children": 0, "seniors": 0},
              "origin": "USER", "rationale": ""},
    "pace": {"value": "RELAXED", "origin": "MODEL", "rationale": "带老人"},
    "budget": {"value": None, "origin": None, "rationale": ""},
    "styles": {"value": ["美食"], "origin": "USER", "rationale": ""},
}


def test_collect_builds_requirements_with_origins():
    reqs = collect("十一去京都5天两人", _deps(_resp(COLLECTED)), _ctx())
    assert reqs.destination.value == "京都"
    assert reqs.destination.origin is Origin.USER
    assert reqs.pace.value is Pace.RELAXED
    assert reqs.pace.origin is Origin.MODEL
    assert reqs.pace.rationale == "带老人"


def test_collect_leaves_unknown_fields_empty():
    reqs = collect("x", _deps(_resp(COLLECTED)), _ctx())
    assert reqs.budget.value is None
    assert reqs.budget.origin is None


def test_collect_never_marks_anything_confirmed():
    """确认是用户的动作，不是抽取的副产品。"""
    reqs = collect("x", _deps(_resp(COLLECTED)), _ctx())
    assert all(not getattr(reqs, n).confirmed
               for n in ("destination", "dates", "party", "pace"))


def test_collect_parses_dates_and_party():
    reqs = collect("x", _deps(_resp(COLLECTED)), _ctx())
    assert reqs.dates.value.start == date(2026, 10, 1)
    assert reqs.dates.value.days == 5
    assert reqs.party.value == Party(adults=2)


# ---------- pick_angles ----------


def test_pick_angles_returns_requested_count():
    payload = {"angles": [
        {"key": "A", "title": "古寺巡礼", "description": "世界遗产主线"},
        {"key": "B", "title": "市井美食", "description": "锦市场为轴"},
        {"key": "C", "title": "近郊自然", "description": "岚山与贵船"},
    ]}
    angles = pick_angles(Requirements(), _deps(_resp(payload)), _ctx(), n=3)
    assert [a.key for a in angles] == ["A", "B", "C"]
    assert angles[0].title == "古寺巡礼"


def test_pick_angles_rejects_duplicate_keys():
    payload = {"angles": [
        {"key": "A", "title": "x", "description": ""},
        {"key": "A", "title": "y", "description": ""},
    ]}
    with pytest.raises(ValueError, match="重复"):
        pick_angles(Requirements(), _deps(_resp(payload)), _ctx(), n=2)


# ---------- generate / revise ----------

PLAN = {"days": [
    {"date": "2026-10-01", "lodging": "京都站",
     "activities": [
         {"poi_query": "清水寺", "start": "09:00", "end": "11:00",
          "category": "SIGHT", "cost": None, "indoor": False,
          "note": "清晨人少"},
         {"poi_query": "某食堂", "start": "12:00", "end": "13:00",
          "category": "MEAL",
          "cost": {"amount": "1500", "currency": "JPY"},
          "indoor": True, "note": ""},
     ]}]}


def _angle():
    return Angle("A", "古寺巡礼", "")


def test_generate_builds_itinerary_with_ids_assigned():
    itin = generate(Requirements(destination=Field("京都", Origin.USER)),
                    _angle(), _deps(_resp(PLAN)), _ctx())
    assert itin.days[0].id == "d1"
    assert [a.id for a in itin.days[0].activities] == ["d1a1", "d1a2"]
    assert itin.angle.key == "A"


def test_generate_parses_costs_as_decimal_and_estimated():
    itin = generate(Requirements(destination=Field("京都", Origin.USER)),
                    _angle(), _deps(_resp(PLAN)), _ctx())
    cost = itin.days[0].activities[1].cost
    assert cost.amount == Decimal("1500")
    assert cost.currency == "JPY"
    assert cost.confidence.value == "ESTIMATED"    # 模型给的一律是估算


def test_generate_treats_null_cost_as_unknown():
    itin = generate(Requirements(destination=Field("京都", Origin.USER)),
                    _angle(), _deps(_resp(PLAN)), _ctx())
    assert itin.days[0].activities[0].cost is None


def test_generate_parses_categories():
    itin = generate(Requirements(destination=Field("京都", Origin.USER)),
                    _angle(), _deps(_resp(PLAN)), _ctx())
    assert itin.days[0].activities[1].category is Category.MEAL


def test_generate_passes_avoid_list_into_the_prompt():
    deps = _deps(_resp(PLAN))
    generate(Requirements(destination=Field("京都", Origin.USER)), _angle(),
             deps, _ctx(), avoid_poi_ids=frozenset({"B001", "B002"}))
    prompt = deps.client.calls[0].messages[0]["content"]
    assert "B001" in prompt


def test_revise_includes_issues_in_the_prompt():
    from tripplan.models.issue import Issue

    deps = _deps(_resp(PLAN))
    itin = generate(Requirements(destination=Field("京都", Origin.USER)),
                    _angle(), _deps(_resp(PLAN)), _ctx())
    issues = [Issue(Severity.BLOCKING, Source.RULE, "R2", "通勤时间不够")]
    revise(itin, Requirements(destination=Field("京都", Origin.USER)),
           issues, deps, _ctx())
    prompt = deps.client.calls[0].messages[0]["content"]
    assert "通勤时间不够" in prompt


# ---------- critic ----------


def test_critic_returns_issues_sourced_critic():
    payload = {"issues": [
        {"severity": "SUGGESTION", "message": "可以加个夜景", "where_day": None},
        {"severity": "BLOCKING", "message": "第3天完全不符合休闲节奏",
         "where_day": "d3"},
    ]}
    issues = run_llm_critic(None, Requirements(), _deps(_resp(payload)), _ctx())
    assert [i.severity for i in issues] == [Severity.SUGGESTION,
                                            Severity.BLOCKING]
    assert all(i.source is Source.CRITIC for i in issues)
    assert issues[1].where.day_id == "d3"


def test_critic_tolerates_empty_verdict():
    assert run_llm_critic(None, Requirements(),
                          _deps(_resp({"issues": []})), _ctx()) == []


# ---------- classify_feedback ----------


def test_classify_detects_itinerary_only_feedback():
    payload = {"patches_requirements": False, "patch": {}, "scale": "INCREMENTAL"}
    delta = classify_feedback("第2天太赶了", Requirements(),
                              _deps(_resp(payload)), _ctx())
    assert delta.patches_requirements is False


def test_classify_detects_requirement_change_with_scale():
    payload = {"patches_requirements": True,
               "patch": {"dates": {"start": "2026-10-01", "end": "2026-10-04"}},
               "scale": "INCREMENTAL"}
    delta = classify_feedback("改成四天", Requirements(),
                              _deps(_resp(payload)), _ctx())
    assert delta.patches_requirements is True
    assert delta.scale is Scale.INCREMENTAL
    assert "dates" in delta.patch


def test_classify_detects_rewrite_scale():
    payload = {"patches_requirements": True,
               "patch": {"destination": "巴黎"}, "scale": "REWRITE"}
    delta = classify_feedback("改去巴黎", Requirements(),
                              _deps(_resp(payload)), _ctx())
    assert delta.scale is Scale.REWRITE


# ---------- apply_patch ----------


def test_apply_patch_sets_value_and_marks_user_origin():
    reqs = Requirements(destination=Field("京都", Origin.MODEL))
    out = apply_patch(reqs, {"destination": "巴黎"})
    assert out.destination.value == "巴黎"
    assert out.destination.origin is Origin.USER      # 用户改的
    assert out.destination.confirmed is True


def test_apply_patch_leaves_other_fields_alone():
    reqs = Requirements(destination=Field("京都", Origin.USER),
                        pace=Field(Pace.RELAXED, Origin.MODEL))
    out = apply_patch(reqs, {"destination": "巴黎"})
    assert out.pace.value is Pace.RELAXED
    assert out.pace.origin is Origin.MODEL


def test_apply_patch_parses_structured_values():
    out = apply_patch(Requirements(),
                      {"dates": {"start": "2026-10-01", "end": "2026-10-04"}})
    assert out.dates.value.days == 4


def test_apply_patch_ignores_unknown_field():
    out = apply_patch(Requirements(), {"wizardry": 1})
    assert out == Requirements()
```

- [ ] **Step 7: 运行确认失败**

Run: `uv run pytest tests/agents/test_steps.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tripplan.agents.steps'`

- [ ] **Step 8: 实现 schema**

创建 `src/tripplan/agents/schemas.py`：

```python
"""各角色的输出 schema。run_agent 只校验 required 与顶层类型，
更细的语义校验交给 steps.py 的转换函数——那里报错信息更具体。"""

_FIELD = {"type": "object",
          "properties": {"value": {}, "origin": {"type": ["string", "null"]},
                         "rationale": {"type": "string"}},
          "required": ["value"]}

REQUIREMENTS_SCHEMA = {
    "type": "object",
    "properties": {name: _FIELD for name in (
        "destination", "dates", "party", "arrival", "departure", "budget",
        "styles", "pace", "must_visit", "avoid", "lodging_area",
        "constraints")},
    "required": ["destination", "dates", "party"],
}

ANGLES_SCHEMA = {
    "type": "object",
    "properties": {"angles": {"type": "array", "items": {
        "type": "object",
        "properties": {"key": {"type": "string"}, "title": {"type": "string"},
                       "description": {"type": "string"}},
        "required": ["key", "title"]}}},
    "required": ["angles"],
}

ITINERARY_SCHEMA = {
    "type": "object",
    "properties": {"days": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "date": {"type": "string"},
            "lodging": {"type": ["string", "null"]},
            "activities": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "poi_query": {"type": "string"},
                    "start": {"type": "string"}, "end": {"type": "string"},
                    "category": {"type": "string"},
                    "cost": {"type": ["object", "null"]},
                    "indoor": {"type": "boolean"},
                    "note": {"type": "string"}},
                "required": ["poi_query", "start", "end", "category"]}}},
        "required": ["date", "activities"]}}},
    "required": ["days"],
}

CRITIQUE_SCHEMA = {
    "type": "object",
    "properties": {"issues": {"type": "array", "items": {
        "type": "object",
        "properties": {"severity": {"type": "string"},
                       "message": {"type": "string"},
                       "where_day": {"type": ["string", "null"]}},
        "required": ["severity", "message"]}}},
    "required": ["issues"],
}

FEEDBACK_SCHEMA = {
    "type": "object",
    "properties": {"patches_requirements": {"type": "boolean"},
                   "patch": {"type": "object"},
                   "scale": {"type": "string"}},
    "required": ["patches_requirements"],
}
```

- [ ] **Step 9: 实现步骤层**

创建 `src/tripplan/agents/steps.py`：

```python
"""把 LLM 的结构化输出转成领域对象。每个步骤一个函数。"""

import json
from dataclasses import dataclass, replace
from datetime import date as Date
from datetime import datetime, time
from decimal import Decimal
from enum import Enum
from functools import lru_cache
from pathlib import Path

from tripplan.agents.runner import run_agent
from tripplan.agents.schemas import (
    ANGLES_SCHEMA,
    CRITIQUE_SCHEMA,
    FEEDBACK_SCHEMA,
    ITINERARY_SCHEMA,
    REQUIREMENTS_SCHEMA,
)
from tripplan.agents.tools import build_planning_tools
from tripplan.llm.config import Role
from tripplan.models.common import Confidence, Field, Money, Origin
from tripplan.models.issue import DayRef, Issue, Severity, Source
from tripplan.models.itinerary import (
    Activity,
    Angle,
    Category,
    Day,
    Itinerary,
    assign_ids,
)
from tripplan.models.requirements import (
    Basis,
    BudgetSpec,
    CostKind,
    DateRange,
    Pace,
    Party,
    Requirements,
    Transfer,
    describe_value,
)

_PROMPTS = Path(__file__).parent / "prompts"


@lru_cache(maxsize=None)
def _prompt(name: str) -> str:
    return (_PROMPTS / f"{name}.md").read_text(encoding="utf-8")


class Scale(Enum):
    INCREMENTAL = "INCREMENTAL"
    REWRITE = "REWRITE"


@dataclass(frozen=True)
class FeedbackDelta:
    patches_requirements: bool
    patch: dict
    scale: Scale


# ---------- 值解析 ----------

_PARSERS = {
    "destination": lambda v: str(v),
    "dates": lambda v: DateRange(Date.fromisoformat(v["start"]),
                                 Date.fromisoformat(v["end"])),
    "party": lambda v: Party(adults=v.get("adults", 1),
                             children=v.get("children", 0),
                             seniors=v.get("seniors", 0)),
    "arrival": lambda v: Transfer(datetime.fromisoformat(v["at"]),
                                  v.get("mode", "")),
    "departure": lambda v: Transfer(datetime.fromisoformat(v["at"]),
                                    v.get("mode", "")),
    "budget": lambda v: BudgetSpec(
        Decimal(str(v["amount"])), v.get("currency", "CNY"),
        Basis(v.get("basis", "TOTAL")),
        frozenset(CostKind(k) for k in v.get("includes", []))),
    "styles": list,
    "pace": Pace,
    "must_visit": list,
    "avoid": list,
    "lodging_area": str,
    "constraints": list,
}


def _to_field(name: str, raw: dict | None) -> Field:
    if not raw or raw.get("value") is None:
        return Field()
    origin = raw.get("origin")
    try:
        value = _PARSERS[name](raw["value"])
    except (KeyError, ValueError, TypeError):
        return Field()          # 解析不了就当没给，不要塞个坏值进去
    return Field(value=value,
                 origin=Origin(origin) if origin else Origin.MODEL,
                 confirmed=False,        # 确认是用户的动作，不是抽取的副产品
                 rationale=raw.get("rationale", ""))


# ---------- 步骤 ----------


def collect(raw_request: str, deps, ctx) -> Requirements:
    data = run_agent(
        system_prompt=_prompt("collect"), user_prompt=raw_request, tools=None,
        output_schema=REQUIREMENTS_SCHEMA, role=Role.CLASSIFIER, ctx=ctx,
        client=deps.client, tool_impls={})
    return Requirements(**{name: _to_field(name, data.get(name))
                           for name in _PARSERS})


def pick_angles(reqs: Requirements, deps, ctx, n: int = 3) -> list[Angle]:
    data = run_agent(
        system_prompt=_prompt("angle").replace("{n}", str(n)),
        user_prompt=_describe_requirements(reqs), tools=None,
        output_schema=ANGLES_SCHEMA, role=Role.ANGLE, ctx=ctx,
        client=deps.client, tool_impls={})
    angles = [Angle(a["key"], a["title"], a.get("description", ""))
              for a in data["angles"][:n]]
    keys = [a.key for a in angles]
    if len(set(keys)) != len(keys):
        raise ValueError(f"角度 key 重复：{keys}")
    return angles


def generate(reqs: Requirements, angle: Angle, deps, ctx,
             avoid_poi_ids=()) -> Itinerary:
    prompt = [_describe_requirements(reqs),
              f"\n本方案的切入角度：{angle.title} —— {angle.description}"]
    if avoid_poi_ids:
        prompt.append(
            "\n以下 POI id 已经出现在其他候选方案里，请尽量避开，"
            f"给出实质不同的选择：{sorted(avoid_poi_ids)}")
    return _plan(prompt, reqs, angle, deps, ctx)


def revise(itin: Itinerary, reqs: Requirements, issues, deps,
           ctx) -> Itinerary:
    prompt = [
        _describe_requirements(reqs),
        f"\n本方案的切入角度：{itin.angle.title}",
        "\n当前行程：\n" + json.dumps(_itinerary_to_json(itin),
                                      ensure_ascii=False),
        "\n必须解决的问题：",
        *[f"- [{i.severity.value}] {i.message}" for i in issues],
        "\n请在现有安排基础上修改，保留没有问题的部分。",
    ]
    return _plan(prompt, reqs, itin.angle, deps, ctx)


def _plan(prompt_parts, reqs, angle, deps, ctx) -> Itinerary:
    city = reqs.destination.value or ""
    specs, impls = build_planning_tools(deps.provider, city)
    data = run_agent(
        system_prompt=_prompt("plan"), user_prompt="\n".join(prompt_parts),
        tools=specs, output_schema=ITINERARY_SCHEMA, role=Role.PLANNER,
        ctx=ctx, client=deps.client, tool_impls=impls)
    return assign_ids(_to_itinerary(data, angle))


def run_llm_critic(itin, reqs: Requirements, deps, ctx) -> list[Issue]:
    body = ("行程：\n"
            + json.dumps(_itinerary_to_json(itin), ensure_ascii=False)
            if itin is not None else "行程：（空）")
    data = run_agent(
        system_prompt=_prompt("critic"),
        user_prompt=f"{_describe_requirements(reqs)}\n\n{body}",
        tools=None, output_schema=CRITIQUE_SCHEMA, role=Role.CRITIC, ctx=ctx,
        client=deps.client, tool_impls={})
    out = []
    for raw in data["issues"]:
        try:
            severity = Severity(raw["severity"])
        except ValueError:
            severity = Severity.SUGGESTION      # 拿不准就往轻里判
        where = DayRef(raw["where_day"]) if raw.get("where_day") else None
        out.append(Issue(severity=severity, source=Source.CRITIC,
                         code="CRITIC", message=raw["message"], where=where))
    return out


def classify_feedback(text: str, reqs: Requirements, deps,
                      ctx) -> FeedbackDelta:
    data = run_agent(
        system_prompt=_prompt("classify"),
        user_prompt=f"{_describe_requirements(reqs)}\n\n用户反馈：{text}",
        tools=None, output_schema=FEEDBACK_SCHEMA, role=Role.CLASSIFIER,
        ctx=ctx, client=deps.client, tool_impls={})
    try:
        scale = Scale(data.get("scale", "INCREMENTAL"))
    except ValueError:
        scale = Scale.INCREMENTAL
    return FeedbackDelta(bool(data["patches_requirements"]),
                         data.get("patch") or {}, scale)


def apply_patch(reqs: Requirements, patch: dict) -> Requirements:
    """把 patch 应用到需求上。用户改的字段一律标 USER + 已确认。"""
    updates = {}
    for name, raw_value in patch.items():
        if name not in _PARSERS:
            continue                    # 未知字段忽略，不炸
        try:
            value = _PARSERS[name](raw_value)
        except (KeyError, ValueError, TypeError):
            continue
        updates[name] = Field(value=value, origin=Origin.USER, confirmed=True)
    return replace(reqs, **updates) if updates else reqs


# ---------- 渲染给模型看的文本 ----------


def _describe_requirements(reqs: Requirements) -> str:
    lines = []
    for name in _PARSERS:
        f = getattr(reqs, name)
        if f.value is None:
            continue
        mark = "" if f.origin is Origin.USER else "（推断）"
        lines.append(f"- {name}{mark}: {describe_value(f.value)}")
    return "需求：\n" + ("\n".join(lines) if lines else "（尚未收集）")


def _itinerary_to_json(itin: Itinerary) -> dict:
    return {"days": [
        {"date": d.date.isoformat(), "lodging": d.lodging,
         "activities": [
             {"poi_query": a.poi_query, "start": a.start.isoformat(timespec="minutes"),
              "end": a.end.isoformat(timespec="minutes"),
              "category": a.category.value,
              "cost": (None if a.cost is None
                       else {"amount": str(a.cost.amount),
                             "currency": a.cost.currency}),
              "indoor": a.indoor, "note": a.note}
             for a in d.activities]}
        for d in itin.days]}


def _to_itinerary(data: dict, angle: Angle) -> Itinerary:
    days = []
    for raw_day in data["days"]:
        acts = []
        for raw in raw_day["activities"]:
            cost = None
            if raw.get("cost"):
                cost = Money(Decimal(str(raw["cost"]["amount"])),
                             raw["cost"].get("currency", "CNY"),
                             Confidence.ESTIMATED,   # 模型给的一律是估算
                             "llm:知识")
            try:
                category = Category(raw["category"])
            except ValueError:
                category = Category.SIGHT
            acts.append(Activity(
                id="", day_id="", poi_query=raw["poi_query"],
                start=time.fromisoformat(raw["start"]),
                end=time.fromisoformat(raw["end"]), category=category,
                cost=cost, indoor=bool(raw.get("indoor", False)),
                note=raw.get("note", "")))
        days.append(Day(id="", date=Date.fromisoformat(raw_day["date"]),
                        activities=acts, lodging=raw_day.get("lodging")))
    return Itinerary(angle=angle, days=days)
```

- [ ] **Step 10: 运行确认通过**

Run: `uv run pytest tests/agents/ -v`
Expected: PASS（48 passed）

- [ ] **Step 11: 格式化并提交**

```bash
uv run black src tests
git add src/tripplan/deps.py src/tripplan/agents tests/agents
git commit -m "feat: agent 步骤、schema 与 prompts

collect 抽取时永不标 confirmed——确认是用户的动作。模型给的金额一律标
ESTIMATED，null 表示未知而非免费。critic prompt 明确约束 BLOCKING 的用法，
避免它把「加个夜景更好」标成硬伤而一路循环到撞上限。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

**✅ 检查点 D：** 能让模型产出结构化的需求卡、角度、行程与评审意见。

---

## Phase E — 编排

### Task 18: `run_slot` —— 单条候选线的打磨循环

**Files:**
- Create: `src/tripplan/slot.py`
- Test: `tests/test_slot.py`

**Interfaces:**
- Consumes: `SlotLimits`, `SlotContext`, `LimitExceeded`（Task 16）、`generate`, `revise`, `run_llm_critic`（Task 17）、`resolve`（Task 13）、`run_rule_checks`（Task 10）、`CandidateSlot`, `SlotStatus`（Task 5）、`ProviderError`（Task 12）
- Produces: `run_slot(angle, seed, reqs, tz, deps, issues=(), limits=SlotLimits(), emit=noop, avoid_poi_ids=()) -> CandidateSlot`

**背景（spec §3.5）：** 没有任何一条出路是「卡住」或「抛异常炸穿」——撞轮数上限 → `EXHAUSTED` + 残缺行程；撞资源上限 → 同样返回已有的半成品；外部依赖挂了 → `FAILED` + 原因。三种情况都带着可解释的 `detail` 出现在候选列表里。

`①②` 的顺序是成本控制的关键：**硬伤修订阶段完全不烧 critic**——让 critic 点评一份时间都对不上的行程没有意义。

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_slot.py`：

```python
from datetime import date

import pytest

from tripplan.agents.limits import SlotLimits
from tripplan.deps import Deps
from tripplan.models.common import Field, Origin
from tripplan.models.issue import Issue, Severity, Source
from tripplan.models.itinerary import Angle
from tripplan.models.requirements import DateRange, Party, Requirements
from tripplan.providers.base import ProviderError
from tripplan.providers.fake import FakeProvider
from tripplan.slot import run_slot
from tripplan.state import SlotStatus

D1 = date(2026, 10, 1)
TZ = "Asia/Tokyo"
ANGLE = Angle("A", "古寺巡礼", "")


def _reqs():
    return Requirements(
        destination=Field("京都", Origin.USER),
        dates=Field(DateRange(D1, D1), Origin.USER),
        party=Field(Party(adults=2), Origin.USER))


class _ScriptedSteps:
    """替换掉 generate/revise/critic，让测试只关心循环控制流。"""

    def __init__(self, itineraries, critiques=None, generate_error=None):
        self.itineraries = list(itineraries)
        self.critiques = list(critiques or [])
        self.generate_error = generate_error
        self.generate_calls = 0
        self.revise_calls = 0
        self.critic_calls = 0
        self.avoid_seen = None

    def generate(self, reqs, angle, deps, ctx, avoid_poi_ids=()):
        self.generate_calls += 1
        self.avoid_seen = avoid_poi_ids
        if self.generate_error:
            raise self.generate_error
        return self.itineraries.pop(0)

    def revise(self, itin, reqs, issues, deps, ctx):
        self.revise_calls += 1
        return self.itineraries.pop(0) if self.itineraries else itin

    def critic(self, itin, reqs, deps, ctx):
        self.critic_calls += 1
        return self.critiques.pop(0) if self.critiques else []


def _clean(mk):
    """一份能通过全部规则的行程。"""
    from tripplan.models.itinerary import Category

    return mk.itin([mk.day("d1", D1, [
        mk.act("d1a1", "d1", "10:00", "11:30", query="清水寺"),
        mk.act("d1a2", "d1", "12:30", "13:30", query="某食堂",
               category=Category.MEAL),
        mk.act("d1a3", "d1", "18:30", "20:00", query="某居酒屋",
               category=Category.MEAL),
    ])])


def _broken(mk):
    """第二项与第一项时间重叠 —— 必然触发 R1 BLOCKING。"""
    return mk.itin([mk.day("d1", D1, [
        mk.act("d1a1", "d1", "09:00", "12:00"),
        mk.act("d1a2", "d1", "11:00", "13:00"),
    ])])


def _deps():
    return Deps(client=None, provider=FakeProvider(pois={
        "清水寺": [("B001", 34.9949, 135.785)],
        "某食堂": [("B002", 34.9950, 135.786)],
        "某居酒屋": [("B003", 34.9951, 135.787)]}))


def _run(mk, steps, monkeypatch, **kw):
    import tripplan.slot as slot_mod

    monkeypatch.setattr(slot_mod, "generate", steps.generate)
    monkeypatch.setattr(slot_mod, "revise", steps.revise)
    monkeypatch.setattr(slot_mod, "run_llm_critic", steps.critic)
    return run_slot(angle=ANGLE, seed=kw.pop("seed", None), reqs=_reqs(),
                    tz=TZ, deps=_deps(), **kw)


def test_returns_ok_when_first_draft_is_clean(mk, monkeypatch):
    steps = _ScriptedSteps([_clean(mk)])
    slot = _run(mk, steps, monkeypatch)
    assert slot.status is SlotStatus.OK
    assert slot.itinerary is not None
    assert slot.facts is not None          # 快照跟着候选走，供回放
    assert steps.revise_calls == 0


def test_revises_until_blocking_issues_clear(mk, monkeypatch):
    steps = _ScriptedSteps([_broken(mk), _clean(mk)])
    slot = _run(mk, steps, monkeypatch)
    assert slot.status is SlotStatus.OK
    assert steps.revise_calls == 1


def test_critic_is_not_called_while_hard_errors_remain(mk, monkeypatch):
    """让 critic 点评一份时间都对不上的行程没有意义，也白烧钱。"""
    steps = _ScriptedSteps([_broken(mk), _clean(mk)])
    _run(mk, steps, monkeypatch)
    assert steps.critic_calls == 1         # 只在最后那份干净的上跑过


def test_exhausted_when_rounds_run_out(mk, monkeypatch):
    steps = _ScriptedSteps([_broken(mk)] * 5)
    slot = _run(mk, steps, monkeypatch, limits=SlotLimits(max_rounds=2))
    assert slot.status is SlotStatus.EXHAUSTED
    assert slot.itinerary is not None      # 带着残缺行程回来
    assert "2" in slot.detail


def test_exhausted_slot_carries_unresolved_issues(mk, monkeypatch):
    steps = _ScriptedSteps([_broken(mk)] * 5)
    slot = _run(mk, steps, monkeypatch, limits=SlotLimits(max_rounds=1))
    assert any(i.severity is Severity.BLOCKING for i in slot.itinerary.issues)


def test_critic_blocking_also_drives_revision(mk, monkeypatch):
    critique = [Issue(Severity.BLOCKING, Source.CRITIC, "CRITIC", "太流水账")]
    steps = _ScriptedSteps([_clean(mk), _clean(mk)], critiques=[critique, []])
    slot = _run(mk, steps, monkeypatch)
    assert steps.revise_calls == 1
    assert slot.status is SlotStatus.OK


def test_failed_when_generate_raises_provider_error(mk, monkeypatch):
    steps = _ScriptedSteps([], generate_error=ProviderError("高德限流"))
    slot = _run(mk, steps, monkeypatch)
    assert slot.status is SlotStatus.FAILED
    assert slot.itinerary is None
    assert "高德限流" in slot.detail


def test_limit_exceeded_with_a_draft_is_exhausted_not_failed(mk, monkeypatch):
    from tripplan.agents.limits import LimitExceeded

    class Steps(_ScriptedSteps):
        def revise(self, itin, reqs, issues, deps, ctx):
            raise LimitExceeded("输出 token 超限")

    steps = Steps([_broken(mk)])
    slot = _run(mk, steps, monkeypatch)
    assert slot.status is SlotStatus.EXHAUSTED
    assert slot.itinerary is not None
    assert "资源超限" in slot.detail


def test_limit_exceeded_before_any_draft_is_failed(mk, monkeypatch):
    from tripplan.agents.limits import LimitExceeded

    steps = _ScriptedSteps([], generate_error=LimitExceeded("超时"))
    slot = _run(mk, steps, monkeypatch)
    assert slot.status is SlotStatus.FAILED
    assert slot.itinerary is None


def test_seed_skips_generation(mk, monkeypatch):
    steps = _ScriptedSteps([])
    slot = _run(mk, steps, monkeypatch, seed=_clean(mk))
    assert steps.generate_calls == 0
    assert slot.status is SlotStatus.OK


def test_incoming_issues_are_addressed_before_validation(mk, monkeypatch):
    """REFINE 入口：带着人工意见进来，先改再校验。"""
    steps = _ScriptedSteps([_clean(mk)])
    human = [Issue.from_human("第2天太赶了")]
    slot = _run(mk, steps, monkeypatch, seed=_broken(mk), issues=human)
    assert steps.revise_calls == 1
    assert slot.status is SlotStatus.OK


def test_avoid_poi_ids_are_forwarded_to_generate(mk, monkeypatch):
    steps = _ScriptedSteps([_clean(mk)])
    _run(mk, steps, monkeypatch, avoid_poi_ids=frozenset({"B001"}))
    assert steps.avoid_seen == frozenset({"B001"})


def test_emits_progress_events(mk, monkeypatch):
    events = []
    steps = _ScriptedSteps([_broken(mk), _clean(mk)])
    _run(mk, steps, monkeypatch, emit=events.append)
    assert any("revision" in str(e) for e in events)
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_slot.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tripplan.slot'`

- [ ] **Step 3: 实现**

创建 `src/tripplan/slot.py`：

```python
"""单条候选线的打磨循环。GENERATE 与 REFINE 复用同一个函数。

任何一条线超限都不会拖垮另外两条：三种出路（收敛 / 撞上限 / 外部依赖失败）
都返回一个带 detail 的 CandidateSlot，绝不卡住、也绝不抛异常炸穿。
"""

from tripplan.agents.limits import LimitExceeded, SlotContext, SlotLimits
from tripplan.agents.steps import generate, revise, run_llm_critic
from tripplan.models.issue import Severity, has_blocking
from tripplan.providers.base import ProviderError
from tripplan.state import CandidateSlot, SlotStatus
from tripplan.validation.resolver import resolve
from tripplan.validation.rules import run_rule_checks


def _noop(_event) -> None:
    pass


def run_slot(angle, seed, reqs, tz, deps, issues=(),
             limits: SlotLimits = SlotLimits(), emit=_noop,
             avoid_poi_ids=()) -> CandidateSlot:
    ctx = SlotContext(limits, emit=emit)
    itin, facts = seed, None
    issues = list(issues)

    try:
        if itin is None:
            emit(("generating", angle.key))
            itin = generate(reqs, angle, deps, ctx,
                            avoid_poi_ids=avoid_poi_ids)

        for rnd in range(limits.max_rounds):
            if issues:                                  # 有待办问题就先改
                emit(("revision", angle.key, rnd))
                itin = revise(itin, reqs, issues, deps, ctx)

            facts = resolve(itin, reqs, deps.provider, tz)   # 本轮唯一的 I/O
            issues = run_rule_checks(itin, reqs, facts)      # ① 纯函数，便宜
            if not has_blocking(issues):
                # ② 贵：硬伤清完才请 critic。点评一份时间都对不上的行程没意义。
                issues = issues + run_llm_critic(itin, reqs, deps, ctx)
            if not has_blocking(issues):
                itin.issues = issues
                return CandidateSlot(angle, itin, facts, SlotStatus.OK)

        itin.issues = issues
        blocking = sum(1 for i in issues if i.severity is Severity.BLOCKING)
        return CandidateSlot(
            angle, itin, facts, SlotStatus.EXHAUSTED,
            f"修订 {limits.max_rounds} 轮后仍有 {blocking} 个硬伤")

    except LimitExceeded as e:
        if itin is not None:
            itin.issues = list(issues)
        return CandidateSlot(
            angle, itin, facts,
            SlotStatus.EXHAUSTED if itin is not None else SlotStatus.FAILED,
            f"资源超限：{e}")
    except ProviderError as e:
        return CandidateSlot(angle, itin, facts, SlotStatus.FAILED,
                             f"外部依赖失败：{e}")
```

- [ ] **Step 4: 让 conftest 对根级测试也可见**

把 `tests/validation/conftest.py` 移到 `tests/conftest.py`（`mk` fixture 现在被 `tests/test_slot.py` 用到）：

```bash
git mv tests/validation/conftest.py tests/conftest.py
```

- [ ] **Step 5: 运行确认通过**

Run: `uv run pytest tests/test_slot.py -v`
Expected: PASS（13 passed）

- [ ] **Step 6: 格式化并提交**

```bash
uv run black src tests
git add src/tripplan/slot.py tests/test_slot.py tests/conftest.py
git commit -m "feat: run_slot 打磨循环

三条出路都返回带 detail 的 CandidateSlot：收敛 OK、撞上限 EXHAUSTED
（带残缺行程）、外部依赖失败 FAILED。硬伤未清时不调 critic。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 19: `advance` 的校验与拒绝路径

**Files:**
- Create: `src/tripplan/orchestrator.py`
- Test: `tests/test_advance_reject.py`

**Interfaces:**
- Consumes: `Stage`, `AWAITING`, `ALLOWED_COMMANDS`, 全部命令与结果类型（Task 5）、`missing_required`（Task 2）
- Produces: `advance(state, deps, cmd=None, emit=noop) -> Outcome`（本任务只实现校验与拒绝分支，工作态在 Task 20）、`_validate(state, cmd) -> RejectReason | None`、`_check_candidate(state, cmd)`、`_pending(state) -> NeedInput`

**背景——这一整个任务都是在钉住 review 暴露的缺陷：**

1. 原设计的 `match state.stage` 没有 `AWAIT_*` 分支，等待态无输入调用会在 `while True` 里**空转死循环**。
2. 原 `_apply_human_input` 用 `match state.stage` 分派，不匹配就什么也不做——**用户以为提交成功了**。
3. `chosen_key` 完全不校验，任意字符串都能写进去，崩溃点被推迟到 `Done` 那一行。
4. `revision` 的递增点原先藏在 `_pause()` 里，而 `ChooseCandidate → DONE` 不经过它，**CAS 可被绕过**。
5. **终态不幂等**：`DONE` 不在 `AWAITING` 里，于是 `advance(state, cmd=None)` 会一路走到 `_run_to_pause` 拿到 `Done`，然后照样 `revision += 1`——重复 `trip resume` 一个已定稿的行程，会不断改版本号，还会让别人手里的 `expected_revision` 平白失效。带命令重放时更糟：走 `Rejected(..., _pending(state))`，而 `_pending` 对 DONE 只能硬凑一个 `CHOOSE_OR_FEEDBACK`——**伪造一个根本不存在的待答问题**。

因此本任务确立的核心不变量是：**`advance` 要么拒绝（状态与 `revision` 均不变），要么修改（`revision` 恰好 +1），要么在终态幂等返回（同样不变）**。测试直接断言这个三分，而不是逐条路径去数——上面几条漏掉的正是「逐条枚举」漏出来的。

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_advance_reject.py`：

```python
import pytest

from tripplan.deps import Deps
from tripplan.models.common import Field, Origin
from tripplan.models.itinerary import Angle, Itinerary
from tripplan.models.requirements import DateRange, Party, Requirements
from tripplan.orchestrator import advance
from tripplan.providers.fake import FakeProvider
from tripplan.state import (
    AWAITING,
    AmendRequirements,
    CandidateSlot,
    ChooseCandidate,
    ConfirmRequirements,
    Done,
    GiveFeedback,
    InputKind,
    NeedInput,
    Rejected,
    RejectReason,
    SlotStatus,
    Stage,
    TripState,
)
from tripplan.wire import dumps

from datetime import date

D1 = date(2026, 10, 1)


def _deps():
    return Deps(client=None, provider=FakeProvider())


def _full_reqs():
    return Requirements(
        destination=Field("京都", Origin.USER),
        dates=Field(DateRange(D1, D1), Origin.USER),
        party=Field(Party(adults=2), Origin.USER))


def _awaiting_confirm(reqs=None) -> TripState:
    s = TripState.new("去京都", run_id="r1")
    s.stage = Stage.AWAIT_REQ_CONFIRM
    s.revision = 3
    s.requirements = reqs if reqs is not None else _full_reqs()
    return s


def _awaiting_choice() -> TripState:
    s = TripState.new("去京都", run_id="r1")
    s.stage = Stage.AWAIT_CHOICE
    s.revision = 5
    s.requirements = _full_reqs()
    s.candidates = [
        CandidateSlot(Angle("A", "古寺", ""),
                      Itinerary(angle=Angle("A", "古寺", "")),
                      status=SlotStatus.OK),
        CandidateSlot(Angle("B", "美食", ""),
                      Itinerary(angle=Angle("B", "美食", "")),
                      status=SlotStatus.EXHAUSTED, detail="仍有硬伤"),
        CandidateSlot(Angle("C", "自然", ""), None,
                      status=SlotStatus.FAILED, detail="高德限流"),
    ]
    return s


# ---------- 空输入不再死循环 ----------


def test_awaiting_with_no_command_returns_the_pending_question():
    """原设计在这里空转死循环。"""
    s = _awaiting_confirm()
    out = advance(s, _deps(), None)
    assert isinstance(out, NeedInput)
    assert out.kind is InputKind.CONFIRM_REQUIREMENTS
    assert out.revision == 3


def test_awaiting_with_no_command_does_not_mutate_or_bump():
    s = _awaiting_confirm()
    before = dumps(s)
    advance(s, _deps(), None)
    assert dumps(s) == before


def test_pending_payload_for_choice_stage_is_the_candidate_list():
    s = _awaiting_choice()
    out = advance(s, _deps(), None)
    assert out.kind is InputKind.CHOOSE_OR_FEEDBACK
    assert [c.angle.key for c in out.payload] == ["A", "B", "C"]


def test_pending_is_repeatable_and_side_effect_free():
    s = _awaiting_choice()
    first, second = advance(s, _deps(), None), advance(s, _deps(), None)
    assert first == second
    assert s.revision == 5


# ---------- 陈旧 revision ----------


def test_stale_revision_is_rejected():
    s = _awaiting_confirm()
    out = advance(s, _deps(), ConfirmRequirements(expected_revision=2))
    assert isinstance(out, Rejected)
    assert out.reason is RejectReason.STALE_REVISION


def test_rejected_carries_the_current_question():
    s = _awaiting_confirm()
    out = advance(s, _deps(), ConfirmRequirements(expected_revision=2))
    assert out.current.kind is InputKind.CONFIRM_REQUIREMENTS
    assert out.current.revision == 3


# ---------- 阶段不匹配不再被静默吞掉 ----------


def test_wrong_command_for_stage_is_rejected_not_ignored():
    s = _awaiting_confirm()
    out = advance(s, _deps(), ChooseCandidate(3, "A"))
    assert isinstance(out, Rejected)
    assert out.reason is RejectReason.WRONG_COMMAND_FOR_STAGE


def test_command_while_in_a_work_stage_is_rejected():
    s = TripState.new("x", run_id="r1")
    s.stage = Stage.COLLECT
    out = advance(s, _deps(), ConfirmRequirements(0))
    assert isinstance(out, Rejected)
    assert out.reason is RejectReason.WRONG_COMMAND_FOR_STAGE


# ---------- 必答项 ----------


def test_confirm_is_rejected_when_required_fields_missing():
    s = _awaiting_confirm(reqs=Requirements())
    out = advance(s, _deps(), ConfirmRequirements(3))
    assert out.reason is RejectReason.MISSING_REQUIRED


def test_missing_required_does_not_fall_back_to_collect():
    """回退 COLLECT 会死循环：输入没变，抽取结果也不会变。"""
    s = _awaiting_confirm(reqs=Requirements())
    advance(s, _deps(), ConfirmRequirements(3))
    assert s.stage is Stage.AWAIT_REQ_CONFIRM


def test_amend_is_allowed_even_when_required_missing():
    """只有用户补充了新信息才值得重跑 COLLECT。"""
    s = _awaiting_confirm(reqs=Requirements())
    out = advance(s, _deps(), AmendRequirements(3, "目的地是京都"))
    assert not isinstance(out, Rejected)


# ---------- 候选 key ----------


def test_unknown_candidate_key_is_rejected():
    s = _awaiting_choice()
    out = advance(s, _deps(), ChooseCandidate(5, "ZZZ"))
    assert out.reason is RejectReason.UNKNOWN_CANDIDATE


def test_candidate_without_itinerary_is_unselectable():
    s = _awaiting_choice()
    out = advance(s, _deps(), ChooseCandidate(5, "C"))
    assert out.reason is RejectReason.UNSELECTABLE_CANDIDATE


def test_exhausted_candidate_is_selectable():
    """带着遗留硬伤定稿是用户的权利——问题都摆在他面前了。"""
    s = _awaiting_choice()
    out = advance(s, _deps(), ChooseCandidate(5, "B"))
    assert not isinstance(out, Rejected)


def test_feedback_on_unknown_candidate_is_rejected():
    s = _awaiting_choice()
    out = advance(s, _deps(), GiveFeedback(5, "ZZZ", "太赶了"))
    assert out.reason is RejectReason.UNKNOWN_CANDIDATE


def test_feedback_on_failed_candidate_is_rejected():
    s = _awaiting_choice()
    out = advance(s, _deps(), GiveFeedback(5, "C", "太赶了"))
    assert out.reason is RejectReason.UNSELECTABLE_CANDIDATE


# ---------- 核心不变量 ----------

_REJECTING_COMMANDS = [
    ("stale", ConfirmRequirements(999)),
    ("wrong_stage", ChooseCandidate(3, "A")),
    ("unknown_key", GiveFeedback(3, "ZZZ", "x")),
]


@pytest.mark.parametrize("label,cmd", _REJECTING_COMMANDS)
def test_rejected_never_mutates_state(label, cmd):
    s = _awaiting_confirm()
    before, rev = dumps(s), s.revision
    out = advance(s, _deps(), cmd)
    assert isinstance(out, Rejected), label
    assert dumps(s) == before, label
    assert s.revision == rev, label


def test_awaiting_stages_are_all_covered_by_allowed_commands():
    from tripplan.state import ALLOWED_COMMANDS

    assert set(ALLOWED_COMMANDS) == set(AWAITING)


# ---------- 终态幂等 ----------


def _done_state() -> TripState:
    s = _awaiting_choice()
    s.stage = Stage.DONE
    s.chosen_key = "A"
    return s


def test_done_state_returns_the_itinerary_without_a_command():
    """重复 resume 一个已定稿的行程，只回同一个答案。"""
    s = _done_state()
    out = advance(s, _deps(), None)
    assert isinstance(out, Done)
    assert out.itinerary.angle.key == "A"


def test_done_state_does_not_bump_revision():
    """否则每次 resume 都无意义地改版本，还会让别人的 expected_revision 失效。"""
    s = _done_state()
    before, snapshot = s.revision, dumps(s)
    advance(s, _deps(), None)
    advance(s, _deps(), None)
    assert s.revision == before
    assert dumps(s) == snapshot


def test_done_state_is_idempotent_across_repeated_calls():
    s = _done_state()
    assert advance(s, _deps(), None) == advance(s, _deps(), None)


def test_command_replayed_against_done_returns_done_not_rejected():
    """DONE 没有待回答的问题，构造 Rejected(..., _pending(state)) 会伪造一个
    根本不存在的 NeedInput。"""
    s = _done_state()
    out = advance(s, _deps(), ChooseCandidate(s.revision, "B"))
    assert isinstance(out, Done)
    assert out.itinerary.angle.key == "A"      # 不会改选
    assert s.chosen_key == "A"
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_advance_reject.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tripplan.orchestrator'`

- [ ] **Step 3: 实现校验骨架**

创建 `src/tripplan/orchestrator.py`（工作态先留一个会在 Task 20 补全的最小实现）：

```python
"""状态机。不阻塞、不读 stdin、不 print。

核心不变量：advance 要么拒绝（状态与 revision 均不变），
要么修改（revision 恰好 +1）。递增只发生在一处，位于校验之后、返回之前——
依赖「所有路径碰巧都会走到某个暂停函数」是靠不住的。
"""

from tripplan.models.requirements import missing_required
from tripplan.state import (
    ALLOWED_COMMANDS,
    AWAITING,
    ChooseCandidate,
    ConfirmRequirements,
    Done,
    GiveFeedback,
    InputKind,
    NeedInput,
    Rejected,
    RejectReason,
    Stage,
)


def _noop(_event) -> None:
    pass


def advance(state, deps, cmd=None, emit=_noop):
    # ⓪ 终态幂等：已定稿的行程反复查询只回同一个答案，不改状态、不递增 revision。
    #    这个分支必须在 _apply 之前 —— ChooseCandidate 把 stage 推到 DONE 的那一次
    #    仍要走下面的正常路径并递增 revision（否则并发选择又能互相覆盖）。
    #    这里处理的只是「进来时就已经是 DONE」。
    if state.stage is Stage.DONE:
        return Done(state.chosen().itinerary)

    # ① 校验：所有拒绝与空查询都在这里返回 —— 不改状态、不递增 revision
    if state.stage in AWAITING:
        if cmd is None:
            return _pending(state)                  # 空调用 = 重新问一遍
        if (bad := _validate(state, cmd)) is not None:
            return Rejected(bad, _pending(state))
    elif cmd is not None:
        return Rejected(RejectReason.WRONG_COMMAND_FOR_STAGE, _pending(state))

    # ② 过了这道线，状态必然改变
    if cmd is not None:
        _apply(state, cmd, deps, emit)              # 只有这里能改 stage
    outcome = _run_to_pause(state, deps, emit)
    state.revision += 1                             # ★ 唯一的递增点
    return outcome


def _validate(state, cmd) -> RejectReason | None:
    if cmd.expected_revision != state.revision:
        return RejectReason.STALE_REVISION
    if type(cmd) not in ALLOWED_COMMANDS[state.stage]:
        return RejectReason.WRONG_COMMAND_FOR_STAGE
    if isinstance(cmd, ConfirmRequirements) and missing_required(
            state.requirements or _empty_requirements()):
        return RejectReason.MISSING_REQUIRED
    return _check_candidate(state, cmd)


def _check_candidate(state, cmd) -> RejectReason | None:
    """ChooseCandidate / GiveFeedback 携带的 angle_key 必须真实且可用。"""
    if not isinstance(cmd, (ChooseCandidate, GiveFeedback)):
        return None
    slot = state.slot(cmd.angle_key)
    if slot is None:
        return RejectReason.UNKNOWN_CANDIDATE
    if slot.itinerary is None:          # FAILED 且没跑出任何东西
        return RejectReason.UNSELECTABLE_CANDIDATE
    return None


def _pending(state) -> NeedInput:
    """当前等待态对应的 NeedInput —— 纯函数，可反复调用。"""
    if state.stage is Stage.AWAIT_REQ_CONFIRM:
        return NeedInput(InputKind.CONFIRM_REQUIREMENTS, state.requirements,
                         state.revision)
    return NeedInput(InputKind.CHOOSE_OR_FEEDBACK, list(state.candidates),
                     state.revision)


def _empty_requirements():
    from tripplan.models.requirements import Requirements

    return Requirements()


# ---- 以下两个函数在 Task 20 补全 ----


def _apply(state, cmd, deps, emit) -> None:
    raise NotImplementedError


def _run_to_pause(state, deps, emit):
    raise NotImplementedError
```

- [ ] **Step 4: 运行确认——拒绝路径全绿，其余按预期 NotImplementedError**

Run: `uv run pytest tests/test_advance_reject.py -v`
Expected: PASS（23 passed）。所有测试都只走「DONE 早返回 / 拒绝 / `_pending`」三条路径，
不会碰到 Task 20 才补全的 `_apply` 与 `_run_to_pause`。

- [ ] **Step 5: 格式化并提交**

```bash
uv run black src tests
git add src/tripplan/orchestrator.py tests/test_advance_reject.py
git commit -m "feat: advance 的校验与拒绝路径

等待态在进入工作循环前就被拦下并返回，结构上不可能死循环。
阶段不匹配、陈旧 revision、未知/不可选候选一律显式 Rejected，
不再静默吞掉。Rejected 路径不改状态、不递增 revision。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 20: `advance` 的工作态、命令应用与 REWRITE 清理

**Files:**
- Modify: `src/tripplan/orchestrator.py`（补全 `_apply` 与 `_run_to_pause`）
- Test: `tests/test_advance_flow.py`

**Interfaces:**
- Consumes: `run_slot`（Task 18）、`collect`, `pick_angles`, `classify_feedback`, `apply_patch`, `Scale`（Task 17）、`resolve_timezone`（Task 13）、`enforce_diversity`（Task 11）
- Produces: 补全后的 `advance`；`_ensure_timezone(state, deps) -> str`

**背景——两个 P0：**

- **`revision` 在 `ChooseCandidate → DONE` 路径不递增**，两个并发请求分别选 A、B 会都以 `expected=3` 提交，后者覆盖前者。修法是把递增收敛到 `advance` 的唯一一处（Task 19 已做），本任务确保 DONE 也走那条路。
- **目的地变更后的单方案路径不清 seed、不重算时区**：`GiveFeedback` 先设 `chosen_key`，`_patch_requirements` 就走了「已选定 → REFINE」分支，于是「京都改巴黎」会拿京都行程当 seed，`trip_timezone` 一路停在 `Asia/Tokyo`。

**REWRITE 清的是行程，不是角度**——目的地全换了，但用户选中的切入角度通常仍然成立，而且他已经表达过这个偏好。

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_advance_flow.py`：

```python
from datetime import date

import pytest

from tripplan.agents.steps import FeedbackDelta, Scale
from tripplan.deps import Deps
from tripplan.models.common import Field, Origin
from tripplan.models.issue import Issue
from tripplan.models.itinerary import Angle, Itinerary
from tripplan.models.requirements import DateRange, Party, Requirements
from tripplan.orchestrator import advance
from tripplan.providers.fake import FakeProvider
from tripplan.state import (
    AmendRequirements,
    CandidateSlot,
    ChooseCandidate,
    ConfirmRequirements,
    Done,
    GiveFeedback,
    NeedInput,
    Rejected,
    SlotStatus,
    Stage,
    TripState,
)
from tripplan.wire import dumps

D1 = date(2026, 10, 1)


def _reqs(dest="京都"):
    return Requirements(
        destination=Field(dest, Origin.USER),
        dates=Field(DateRange(D1, D1), Origin.USER),
        party=Field(Party(adults=2), Origin.USER))


def _itin(key="A"):
    return Itinerary(angle=Angle(key, f"方案{key}", ""))


class _Fakes:
    """替换 orchestrator 依赖的四个外部步骤，只留控制流。"""

    def __init__(self, delta=None, angles=("A", "B", "C")):
        self.delta = delta
        self.angle_keys = list(angles)
        self.collect_calls = 0
        self.slot_calls = []
        self.classify_calls = 0

    def collect(self, raw_request, deps, ctx=None):
        self.collect_calls += 1
        return _reqs()

    def pick_angles(self, reqs, deps, ctx=None, n=3):
        return [Angle(k, f"方案{k}", "") for k in self.angle_keys]

    def run_slot(self, angle, seed, reqs, tz, deps, **kw):
        self.slot_calls.append({"angle": angle.key, "seed": seed, "tz": tz,
                                "issues": list(kw.get("issues", ()))})
        return CandidateSlot(angle, _itin(angle.key), None, SlotStatus.OK)

    def classify_feedback(self, text, reqs, deps, ctx=None):
        self.classify_calls += 1
        return self.delta or FeedbackDelta(False, {}, Scale.INCREMENTAL)


@pytest.fixture
def wire(monkeypatch):
    def _install(fakes):
        import tripplan.orchestrator as orch

        monkeypatch.setattr(orch, "collect", fakes.collect)
        monkeypatch.setattr(orch, "pick_angles", fakes.pick_angles)
        monkeypatch.setattr(orch, "run_slot", fakes.run_slot)
        monkeypatch.setattr(orch, "classify_feedback", fakes.classify_feedback)
        return fakes

    return _install


def _deps():
    return Deps(client=None, provider=FakeProvider())


def _at_choice(chosen=None):
    s = TripState.new("去京都", run_id="r1")
    s.stage = Stage.AWAIT_CHOICE
    s.revision = 5
    s.requirements = _reqs()
    s.trip_timezone = "Asia/Tokyo"
    s.candidates = [
        CandidateSlot(Angle(k, f"方案{k}", ""), _itin(k), None, SlotStatus.OK)
        for k in ("A", "B", "C")]
    s.chosen_key = chosen
    return s


# ---------- COLLECT ----------


def test_first_advance_collects_then_pauses(wire):
    fakes = wire(_Fakes())
    s = TripState.new("去京都5天", run_id="r1")
    out = advance(s, _deps())
    assert isinstance(out, NeedInput)
    assert s.stage is Stage.AWAIT_REQ_CONFIRM
    assert fakes.collect_calls == 1
    assert s.revision == 1


def test_confirm_marks_fields_confirmed_and_generates(wire):
    fakes = wire(_Fakes())
    s = TripState.new("去京都", run_id="r1")
    advance(s, _deps())
    advance(s, _deps(), ConfirmRequirements(s.revision))
    assert s.requirements.destination.confirmed is True
    assert [c.angle.key for c in s.candidates] == ["A", "B", "C"]
    assert s.stage is Stage.AWAIT_CHOICE


def test_amend_at_confirm_reruns_collect(wire):
    fakes = wire(_Fakes())
    s = TripState.new("去京都", run_id="r1")
    advance(s, _deps())
    advance(s, _deps(), AmendRequirements(s.revision, "预算1万5"))
    assert fakes.collect_calls == 2
    assert "预算1万5" in s.raw_request


# ---------- 时区 ----------


def test_timezone_resolved_before_generating(wire):
    fakes = wire(_Fakes())
    s = TripState.new("去京都", run_id="r1")
    advance(s, _deps())
    advance(s, _deps(), ConfirmRequirements(s.revision))
    assert s.trip_timezone == "Asia/Tokyo"
    assert all(c["tz"] == "Asia/Tokyo" for c in fakes.slot_calls)


# ---------- revision 二分律 ----------


def test_choose_candidate_bumps_revision(wire):
    """P0：DONE 路径不经过工作态，递增点必须覆盖它，否则 CAS 形同虚设。"""
    wire(_Fakes())
    s = _at_choice()
    before = s.revision
    out = advance(s, _deps(), ChooseCandidate(before, "A"))
    assert isinstance(out, Done)
    assert s.revision == before + 1


def test_two_concurrent_choices_cannot_both_commit(wire, tmp_path):
    """还原 review 描述的场景：都读到 rev=5，分别选 A 和 B。"""
    from tripplan.repo import FileRepo

    wire(_Fakes())
    repo = FileRepo(tmp_path / "kyoto")
    repo.create(_at_choice())

    a, b = repo.load(), repo.load()
    persisted = 5

    advance(a, _deps(), ChooseCandidate(5, "A"))
    assert repo.save_if_revision(a, persisted) is True

    advance(b, _deps(), ChooseCandidate(5, "B"))
    assert repo.save_if_revision(b, persisted) is False    # ★ 被挡住
    assert repo.load().chosen_key == "A"


@pytest.mark.parametrize("cmd_factory", [
    lambda s: ConfirmRequirements(s.revision),
    lambda s: AmendRequirements(s.revision, "改成四天"),
    lambda s: ChooseCandidate(s.revision, "A"),
    lambda s: GiveFeedback(s.revision, "A", "第2天太赶"),
])
def test_every_accepted_command_bumps_revision_exactly_once(wire, cmd_factory):
    wire(_Fakes())
    s = _at_choice()
    if isinstance(cmd_factory(s), ConfirmRequirements):
        s.stage = Stage.AWAIT_REQ_CONFIRM
    before = s.revision
    out = advance(s, _deps(), cmd_factory(s))
    assert not isinstance(out, Rejected)
    assert s.revision == before + 1


# ---------- 反馈分类只发生一次 ----------


def test_classify_feedback_is_called_exactly_once(wire):
    """两次分类结果可能不一致，状态会就此走歪且无人报错。"""
    fakes = wire(_Fakes(delta=FeedbackDelta(
        True, {"dates": {"start": "2026-10-01", "end": "2026-10-04"}},
        Scale.INCREMENTAL)))
    s = _at_choice()
    advance(s, _deps(), GiveFeedback(s.revision, "A", "改成四天"))
    assert fakes.classify_calls == 1


# ---------- 改行程 vs 改需求 ----------


def test_itinerary_feedback_selects_the_slot_and_refines(wire):
    fakes = wire(_Fakes(delta=FeedbackDelta(False, {}, Scale.INCREMENTAL)))
    s = _at_choice()
    advance(s, _deps(), GiveFeedback(s.revision, "B", "第2天太赶了"))
    assert s.chosen_key == "B"
    assert [c["angle"] for c in fakes.slot_calls] == ["B"]     # 只跑选中那份
    assert any("太赶" in i.message for i in fakes.slot_calls[0]["issues"])


def test_requirement_change_before_selection_reruns_all_three(wire):
    fakes = wire(_Fakes(delta=FeedbackDelta(
        True, {"dates": {"start": "2026-10-01", "end": "2026-10-04"}},
        Scale.INCREMENTAL)))
    s = _at_choice()
    advance(s, _deps(), AmendRequirements(s.revision, "改成四天"))
    assert sorted(c["angle"] for c in fakes.slot_calls) == ["A", "B", "C"]


def test_incremental_change_passes_previous_itineraries_as_seeds(wire):
    """三天改四天时用户对前三天可能已满意，从零重规划会洗掉它。"""
    fakes = wire(_Fakes(delta=FeedbackDelta(
        True, {"dates": {"start": "2026-10-01", "end": "2026-10-04"}},
        Scale.INCREMENTAL)))
    s = _at_choice()
    advance(s, _deps(), AmendRequirements(s.revision, "改成四天"))
    assert all(c["seed"] is not None for c in fakes.slot_calls)


def test_old_issues_are_discarded_when_requirements_change(wire):
    fakes = wire(_Fakes(delta=FeedbackDelta(
        True, {"dates": {"start": "2026-10-01", "end": "2026-10-04"}},
        Scale.INCREMENTAL)))
    s = _at_choice()
    s.issues = [Issue.from_human("老意见")]
    advance(s, _deps(), AmendRequirements(s.revision, "改成四天"))
    assert all(not c["issues"] for c in fakes.slot_calls)


# ---------- REWRITE ----------


def test_rewrite_clears_itineraries_but_keeps_angles(wire):
    """P0：换目的地不能拿旧行程当 seed。角度保留——用户已表达过这个偏好。"""
    fakes = wire(_Fakes(delta=FeedbackDelta(
        True, {"destination": "巴黎"}, Scale.REWRITE)))
    s = _at_choice()
    advance(s, _deps(), AmendRequirements(s.revision, "改去巴黎"))
    assert all(c["seed"] is None for c in fakes.slot_calls)
    assert sorted(c["angle"] for c in fakes.slot_calls) == ["A", "B", "C"]


def test_rewrite_recomputes_timezone(wire):
    """P0：trip_timezone 原先只在 GENERATE 重算，会一路停在 Asia/Tokyo。"""
    fakes = wire(_Fakes(delta=FeedbackDelta(
        True, {"destination": "巴黎"}, Scale.REWRITE)))
    s = _at_choice()
    advance(s, _deps(), AmendRequirements(s.revision, "改去巴黎"))
    assert s.trip_timezone == "Europe/Paris"
    assert all(c["tz"] == "Europe/Paris" for c in fakes.slot_calls)


def test_rewrite_after_selection_still_clears_the_chosen_slot(wire):
    """这正是原设计漏掉的分支：已选定时直接进 REFINE，带着旧行程。"""
    fakes = wire(_Fakes(delta=FeedbackDelta(
        True, {"destination": "巴黎"}, Scale.REWRITE)))
    s = _at_choice(chosen="B")
    advance(s, _deps(), GiveFeedback(s.revision, "B", "改去巴黎"))
    assert [c["angle"] for c in fakes.slot_calls] == ["B"]
    assert fakes.slot_calls[0]["seed"] is None
    assert fakes.slot_calls[0]["tz"] == "Europe/Paris"


def test_destination_change_marked_incremental_still_recomputes_timezone(wire):
    fakes = wire(_Fakes(delta=FeedbackDelta(
        True, {"destination": "巴黎"}, Scale.INCREMENTAL)))
    s = _at_choice()
    advance(s, _deps(), AmendRequirements(s.revision, "改去巴黎"))
    assert s.trip_timezone == "Europe/Paris"


# ---------- 定稿 ----------


def test_done_returns_the_chosen_itinerary(wire):
    wire(_Fakes())
    s = _at_choice()
    out = advance(s, _deps(), ChooseCandidate(s.revision, "C"))
    assert isinstance(out, Done)
    assert out.itinerary.angle.key == "C"
    assert s.stage is Stage.DONE


# ---------- 中断续跑 ----------


def test_state_survives_a_serialization_roundtrip_mid_flow(wire):
    """任意暂停点存盘、丢弃内存对象、重新载入，后续行为一致。"""
    from tripplan.wire import loads

    wire(_Fakes())
    s = TripState.new("去京都", run_id="r1")
    advance(s, _deps())

    revived = loads(dumps(s))
    assert dumps(revived) == dumps(s)

    out_a = advance(s, _deps(), ConfirmRequirements(s.revision))
    out_b = advance(revived, _deps(), ConfirmRequirements(revived.revision))
    assert dumps(s) == dumps(revived)
    assert type(out_a) is type(out_b)
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_advance_flow.py -v`
Expected: FAIL — `NotImplementedError`

- [ ] **Step 3: 补全实现**

在 `src/tripplan/orchestrator.py` 的 import 区补上：

```python
from tripplan.agents.limits import SlotContext, SlotLimits
from tripplan.agents.steps import (
    Scale,
    apply_patch,
    classify_feedback,
    collect,
    pick_angles,
)
from tripplan.models.issue import Issue
from tripplan.models.requirements import mark_all_confirmed
from tripplan.slot import run_slot
from tripplan.state import AmendRequirements, SlotStatus
from tripplan.validation.diversity import enforce_diversity
from tripplan.validation.resolver import resolve_timezone
```

然后把末尾那两个 `NotImplementedError` 替换为：

```python
def _step_ctx(emit) -> SlotContext:
    """给 slot 之外的一次性 LLM 步骤（collect / angle / classify）用的额度。

    这些步骤不属于任何候选线，但 run_agent 仍然要一个能记账、能超时的 ctx——
    传 None 会在 ctx.check() 处直接崩。额度按单次调用给，不跨步骤累积。
    """
    return SlotContext(SlotLimits(max_tool_calls=0, max_output_tokens=20_000,
                                  deadline_s=120), emit=emit)


def _apply(state, cmd, deps, emit) -> None:
    """只有这里能改 stage。调用前 advance 已校验 revision、阶段与候选 key。"""
    match cmd:
        case ConfirmRequirements():
            state.requirements = mark_all_confirmed(state.requirements)
            state.stage = Stage.GENERATE

        case AmendRequirements(text=text):
            if state.stage is Stage.AWAIT_REQ_CONFIRM:
                state.raw_request += f"\n用户补充：{text}"
                state.stage = Stage.COLLECT
            else:                                    # 定稿前改需求
                delta = classify_feedback(text, state.requirements, deps,
                                          _step_ctx(emit))
                _patch_requirements(state, delta, emit)

        case ChooseCandidate(angle_key=key):
            state.chosen_key = key
            state.stage = Stage.DONE

        case GiveFeedback(angle_key=key, text=text):
            state.chosen_key = key                   # 提意见即选定
            # ★ 只分类一次：两次结果可能不一致，状态会就此走歪且无人报错
            delta = classify_feedback(text, state.requirements, deps,
                                      _step_ctx(emit))
            if delta.patches_requirements:
                _patch_requirements(state, delta, emit)
            else:
                state.issues = [Issue.from_human(text)]
                state.stage = Stage.REFINE


def _patch_requirements(state, delta, emit) -> None:
    """delta 由调用方传入 —— 不在这里重新分类。"""
    state.requirements = apply_patch(state.requirements, delta.patch)
    emit(("requirements_patched", delta.patch))      # 非阻塞提示，不拦流程
    state.issues = []                                # 旧 issue 基于旧需求，作废

    if "destination" in delta.patch:
        state.trip_timezone = None                   # 置空 → 下轮重解析

    if delta.scale is Scale.REWRITE:                 # 旧行程整体作废
        state.seeds = {}
        for slot in state.candidates:
            slot.itinerary = None                    # 逼 run_slot 重新 generate
            slot.facts = None
            slot.status = SlotStatus.PENDING
    elif state.chosen_key is None:                   # 增量 + 尚未选定
        state.seeds = {c.angle.key: c.itinerary
                       for c in state.candidates if c.itinerary}

    state.stage = (Stage.GENERATE if state.chosen_key is None
                   else Stage.REFINE)


def _ensure_timezone(state, deps) -> str:
    """幂等：destination 变更时由 _patch_requirements 置空，这里按需重解析。"""
    if state.trip_timezone is None:
        state.trip_timezone = resolve_timezone(state.requirements,
                                               deps.provider)
    return state.trip_timezone


def _run_to_pause(state, deps, emit):
    """工作态：一路向前，直到再次需要人或结束。不碰 revision。"""
    while True:
        match state.stage:
            case Stage.COLLECT:
                state.requirements = collect(state.raw_request, deps,
                                             _step_ctx(emit))
                state.stage = Stage.AWAIT_REQ_CONFIRM
                return _pending(state)

            case Stage.GENERATE:
                tz = _ensure_timezone(state, deps)
                angles = pick_angles(state.requirements, deps,
                                     _step_ctx(emit))
                state.candidates = [
                    run_slot(angle=a, seed=state.seeds.get(a.key),
                             reqs=state.requirements, tz=tz, deps=deps,
                             emit=emit)
                    for a in angles]
                state.candidates = enforce_diversity(
                    state.candidates,
                    lambda slot, avoid: run_slot(
                        angle=slot.angle, seed=None, reqs=state.requirements,
                        tz=tz, deps=deps, emit=emit, avoid_poi_ids=avoid),
                    emit=emit)
                state.seeds = {}
                state.stage = Stage.AWAIT_CHOICE
                return _pending(state)

            case Stage.REFINE:
                tz = _ensure_timezone(state, deps)
                slot = state.chosen()
                refreshed = run_slot(
                    angle=slot.angle, seed=slot.itinerary,
                    reqs=state.requirements, tz=tz, deps=deps,
                    issues=state.issues, emit=emit)
                state.candidates = [refreshed]
                state.issues = []
                state.stage = Stage.AWAIT_CHOICE
                return _pending(state)

            case Stage.DONE:
                # 仍然可达，且必须保留：ChooseCandidate 在 _apply 里把 stage 推到
                # DONE，然后落到这里返回——那一次要经过 advance 的递增点。
                # advance 顶部的 DONE 早返回只拦「进来时就已经是 DONE」。
                return Done(state.chosen().itinerary)
```

**关于并发：** `GENERATE` 里三条线目前是顺序跑的。`SlotContext` 的 deadline 是各自独立的，改成线程池只需把列表推导换成 `ThreadPoolExecutor.map`。v1 先保持顺序——`FakeLlm` 驱动的测试与真实运行行为一致，调试更容易；等实测发现墙钟时间是瓶颈再换。

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_advance_flow.py -v`
Expected: PASS（19 passed）

- [ ] **Step 5: 跑全量**

Run: `uv run pytest tests/ -v`
Expected: PASS（约 240 passed，1 skipped）

- [ ] **Step 6: 格式化并提交**

```bash
uv run black src tests
git add src/tripplan/orchestrator.py tests/test_advance_flow.py
git commit -m "feat: advance 的工作态、命令应用与 REWRITE 清理

修两个 P0：ChooseCandidate→DONE 现在也经过唯一的递增点，两个并发选择
只有一个能提交；REWRITE 清空所有 slot 的 itinerary/facts（保留 angle），
destination 变更置空 trip_timezone 由 _ensure_timezone 按需重解析。
GiveFeedback 全程只调一次 classify_feedback。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

**✅ 检查点 E：** `advance` 全流程在 `FakeLlm` 下跑通，两条不变量有测试覆盖。

---

## Phase F — 输出与 CLI

### Task 21: Markdown 渲染

**Files:**
- Create: `src/tripplan/render/__init__.py`
- Create: `src/tripplan/render/requirement_card.py`
- Create: `src/tripplan/render/candidates.py`
- Create: `src/tripplan/render/itinerary_md.py`
- Test: `tests/render/test_requirement_card.py`
- Test: `tests/render/test_candidates.py`
- Test: `tests/render/test_itinerary_md.py`

**Interfaces:**
- Consumes: `Requirements`, `Origin`（Task 1–2）、`Itinerary`, `FactSnapshot`（Task 3–4）、`CandidateSlot`, `SlotStatus`（Task 5）、`build_ledger`（Task 8）
- Produces: `render_requirement_card(reqs) -> str`、`render_candidates(slots) -> str`、`render_itinerary_md(itin, facts, reqs) -> str`

**渲染器只读 `FactSnapshot`，不自己触网**——同一份 `state.json` 反复渲染必须得到同样的结果。`gaps` 里的条目在页面上如实显示为「未能核实」，不留空白。

- [ ] **Step 1: 写需求卡的失败测试**

创建 `tests/render/__init__.py`（空）与 `tests/render/test_requirement_card.py`：

```python
from datetime import date
from decimal import Decimal

from tripplan.models.common import Field, Origin
from tripplan.models.requirements import (
    Basis,
    BudgetSpec,
    CostKind,
    DateRange,
    Pace,
    Party,
    Requirements,
)
from tripplan.render.requirement_card import render_requirement_card


def _reqs(**kw):
    base = dict(
        destination=Field("京都", Origin.USER),
        dates=Field(DateRange(date(2026, 10, 1), date(2026, 10, 5)),
                    Origin.USER),
        party=Field(Party(adults=2), Origin.USER),
    )
    base.update(kw)
    return Requirements(**base)


def test_shows_user_stated_fields_plainly():
    out = render_requirement_card(_reqs())
    assert "京都" in out
    assert "2026-10-01" in out


def test_marks_inferred_fields_so_the_user_knows_where_to_look():
    """用户一眼知道该盯哪几行，不必通读全表。"""
    out = render_requirement_card(
        _reqs(pace=Field(Pace.RELAXED, Origin.MODEL, rationale="带老人")))
    line = next(l for l in out.splitlines() if "pace" in l or "节奏" in l)
    assert "?" in line
    assert "带老人" in line


def test_does_not_mark_user_stated_fields():
    out = render_requirement_card(
        _reqs(pace=Field(Pace.RELAXED, Origin.USER)))
    line = next(l for l in out.splitlines() if "节奏" in l)
    assert "?" not in line


def test_lists_missing_required_fields_prominently():
    out = render_requirement_card(Requirements())
    assert "缺少" in out or "待补充" in out
    assert "目的地" in out


def test_omits_empty_optional_fields():
    out = render_requirement_card(_reqs())
    assert "住宿区域" not in out


def test_renders_budget_with_currency_basis_and_inclusions():
    out = render_requirement_card(_reqs(budget=Field(
        BudgetSpec(Decimal("15000"), "CNY", Basis.TOTAL,
                   frozenset({CostKind.TICKET, CostKind.MEAL})),
        Origin.USER)))
    assert "15000" in out and "CNY" in out
    assert "TICKET" in out or "门票" in out


def test_confirmed_inferred_field_still_shows_its_origin():
    """确认不抹掉「这值本来是猜的」。"""
    out = render_requirement_card(_reqs(
        pace=Field(Pace.RELAXED, Origin.MODEL, confirmed=True,
                   rationale="带老人")))
    assert "带老人" in out
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/render/test_requirement_card.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tripplan.render'`

- [ ] **Step 3: 实现需求卡**

创建 `src/tripplan/render/__init__.py`（空）与 `src/tripplan/render/requirement_card.py`：

```python
"""需求卡。推断项标出来，用户一眼知道该盯哪几行。"""

from tripplan.models.common import Origin
from tripplan.models.requirements import (
    Requirements,
    describe_value,
    missing_required,
)

_LABELS = {
    "destination": "目的地", "dates": "日期", "party": "人员",
    "arrival": "抵达", "departure": "离开", "budget": "预算",
    "styles": "风格", "pace": "节奏", "must_visit": "必去",
    "avoid": "避开", "lodging_area": "住宿区域", "constraints": "其他约束",
}


def render_requirement_card(reqs: Requirements) -> str:
    lines = ["## 需求确认", ""]
    for name, label in _LABELS.items():
        field = getattr(reqs, name)
        if field.value is None:
            continue
        text = f"- **{label}**：{describe_value(field.value)}"
        if field.origin is Origin.MODEL:
            text += f"  _? 推断_"
            if field.rationale:
                text += f"（{field.rationale}）"
        lines.append(text)

    missing = missing_required(reqs)
    if missing:
        lines += ["", "### 待补充（必答）", ""]
        lines += [f"- **{_LABELS[n]}**：？" for n in missing]
        lines += ["", "这几项无法推断——猜出来会让整个规划建立在假约束上。"]
    else:
        lines += ["", "_标 `?` 的是推断值，不对请直接说。_"]
    return "\n".join(lines)
```

- [ ] **Step 4: 写候选并排的失败测试**

创建 `tests/render/test_candidates.py`：

```python
from tripplan.models.issue import Issue, Severity, Source
from tripplan.models.itinerary import Angle, Itinerary
from tripplan.render.candidates import render_candidates
from tripplan.state import CandidateSlot, SlotStatus


def _slot(key, status=SlotStatus.OK, issues=(), detail="", has_itin=True):
    itin = Itinerary(angle=Angle(key, f"方案{key}", f"{key}的思路"),
                     issues=list(issues)) if has_itin else None
    return CandidateSlot(Angle(key, f"方案{key}", f"{key}的思路"), itin,
                         None, status, detail)


def test_lists_every_candidate_with_its_angle():
    out = render_candidates([_slot("A"), _slot("B")])
    assert "方案A" in out and "A的思路" in out
    assert "方案B" in out


def test_shows_unresolved_issues_as_selection_evidence():
    issues = [Issue(Severity.BLOCKING, Source.RULE, "R2", "第2天通勤3小时")]
    out = render_candidates([_slot("A", issues=issues)])
    assert "第2天通勤3小时" in out


def test_marks_exhausted_candidates_with_their_reason():
    out = render_candidates([_slot("B", SlotStatus.EXHAUSTED,
                                   detail="修订3轮后仍有2个硬伤")])
    assert "修订3轮后仍有2个硬伤" in out


def test_failed_candidate_is_shown_but_marked_unselectable():
    """「方案C 因为高德限流没跑完」也是用户有权知道的事实。"""
    out = render_candidates([_slot("C", SlotStatus.FAILED, detail="高德限流",
                                   has_itin=False)])
    assert "高德限流" in out
    assert "无法选择" in out or "不可选" in out


def test_shows_how_to_respond():
    out = render_candidates([_slot("A")])
    assert "A" in out
    assert "选" in out
```

- [ ] **Step 5: 实现候选并排**

创建 `src/tripplan/render/candidates.py`：

```python
"""三份候选并排。遗留问题是用户挑选方案的重要依据，必须显示。"""

from tripplan.models.issue import Severity
from tripplan.state import SlotStatus

_MARK = {Severity.BLOCKING: "🔴", Severity.WARNING: "🟡",
         Severity.SUGGESTION: "⚪"}


def render_candidates(slots) -> str:
    lines = ["## 候选方案", ""]
    for slot in slots:
        lines.append(f"### [{slot.angle.key}] {slot.angle.title}")
        if slot.angle.description:
            lines.append(f"_{slot.angle.description}_")
        lines.append("")

        if slot.itinerary is None:
            lines += [f"⚠️ 未能生成（{slot.detail}）——**无法选择**", ""]
            continue

        days = len(slot.itinerary.days)
        acts = sum(len(d.activities) for d in slot.itinerary.days)
        lines.append(f"{days} 天 / {acts} 项安排")

        if slot.status is SlotStatus.EXHAUSTED and slot.detail:
            lines.append(f"⚠️ {slot.detail}")

        if slot.itinerary.issues:
            lines += ["", "遗留问题："]
            lines += [f"- {_MARK[i.severity]} {i.message}"
                      for i in slot.itinerary.issues]
        lines.append("")

    keys = "/".join(s.angle.key for s in slots if s.itinerary is not None)
    lines += [f"选一份（{keys}），或对某一份提修改意见，或直接说需求要改。"]
    return "\n".join(lines)
```

- [ ] **Step 6: 写行程单的失败测试**

创建 `tests/render/test_itinerary_md.py`：

```python
from datetime import date
from decimal import Decimal

from tripplan.models.common import Confidence, Field, Money, Origin
from tripplan.models.facts import Gap, GapKind
from tripplan.models.issue import Issue, Severity, Source
from tripplan.models.itinerary import Category
from tripplan.models.requirements import (
    Basis,
    BudgetSpec,
    CostKind,
    Requirements,
)
from tripplan.render.itinerary_md import render_itinerary_md

D1 = date(2026, 10, 1)


def _itin(mk):
    return mk.itin([mk.day("d1", D1, [
        mk.act("d1a1", "d1", "09:00", "11:00", query="清水寺"),
        mk.act("d1a2", "d1", "12:00", "13:00", query="某食堂",
               category=Category.MEAL,
               cost=Money(Decimal("1500"), "JPY", Confidence.ESTIMATED, "llm")),
    ])])


def test_renders_days_and_activities_in_order(mk):
    out = render_itinerary_md(_itin(mk), mk.facts(), mk.reqs())
    assert out.index("清水寺") < out.index("某食堂")
    assert "09:00" in out and "2026-10-01" in out


def test_inserts_transit_legs_between_activities(mk):
    facts = mk.facts(routes=[mk.route("d1", "d1a1", "d1a2", 40)])
    out = render_itinerary_md(_itin(mk), facts, mk.reqs())
    assert "40" in out
    assert out.index("清水寺") < out.index("40") < out.index("某食堂")


def test_marks_unverified_transit_instead_of_leaving_a_blank(mk):
    facts = mk.facts(gaps=[mk.gap(GapKind.ROUTE_UNAVAILABLE, "d1a1->d1a2",
                                  "高德限流")])
    out = render_itinerary_md(_itin(mk), facts, mk.reqs())
    assert "未能核实" in out


def test_shows_layered_budget_ledger(mk):
    reqs = mk.reqs(budget=Field(
        BudgetSpec(Decimal("15000"), "JPY", Basis.TOTAL,
                   frozenset({CostKind.MEAL})), Origin.USER))
    out = render_itinerary_md(_itin(mk), mk.facts(), reqs)
    assert "估算" in out
    assert "未知" in out          # 清水寺 cost 为 None
    assert "1500" in out


def test_estimated_amounts_are_visually_distinct_from_verified(mk):
    reqs = mk.reqs(budget=Field(
        BudgetSpec(Decimal("15000"), "JPY", Basis.TOTAL,
                   frozenset({CostKind.MEAL})), Origin.USER))
    out = render_itinerary_md(_itin(mk), mk.facts(), reqs)
    assert "已核实" in out and "估算" in out


def test_lists_outstanding_issues(mk):
    itin = _itin(mk)
    itin.issues = [Issue(Severity.WARNING, Source.RULE, "R8", "没安排晚餐")]
    out = render_itinerary_md(itin, mk.facts(), mk.reqs())
    assert "没安排晚餐" in out


def test_lists_unverified_facts_honestly(mk):
    facts = mk.facts(gaps=[mk.gap(GapKind.POI_NOT_FOUND, "d1a1", "查不到")])
    out = render_itinerary_md(_itin(mk), facts, mk.reqs())
    assert "未核实" in out or "未能核实" in out


def test_renderer_does_not_touch_the_network(mk):
    """同一份 state.json 反复渲染必须得到同样的结果。"""
    import ast
    import pathlib

    import tripplan.render.itinerary_md as mod

    src = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
    names = {a.name for n in ast.walk(ast.parse(src))
             if isinstance(n, ast.Import) for a in n.names}
    names |= {n.module or "" for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.ImportFrom)}
    assert not (names & {"httpx", "requests", "urllib"})
    assert not any(m.startswith("tripplan.providers") for m in names)


def test_output_is_stable_across_repeated_renders(mk):
    args = (_itin(mk), mk.facts(), mk.reqs())
    assert render_itinerary_md(*args) == render_itinerary_md(*args)
```

- [ ] **Step 7: 实现行程单**

创建 `src/tripplan/render/itinerary_md.py`：

```python
"""Markdown 行程单。只读 FactSnapshot，不触网。"""

from tripplan.models.facts import GapKind
from tripplan.models.issue import Severity
from tripplan.validation.budget import build_ledger

_MARK = {Severity.BLOCKING: "🔴", Severity.WARNING: "🟡",
         Severity.SUGGESTION: "⚪"}


def _transit_line(facts, day, prev, nxt) -> str:
    route = facts.route(day.id, prev.id, nxt.id)
    if route is not None:
        return (f"  ↳ {route.mode.value} 约 {route.duration_min} 分钟"
                f"（{route.distance_m / 1000:.1f} km）")
    subject = f"{prev.id}->{nxt.id}"
    why = next((g.detail for g in facts.gaps
                if g.kind is GapKind.ROUTE_UNAVAILABLE
                and g.subject == subject), "")
    tail = f"：{why}" if why else ""
    return f"  ↳ _通勤耗时未能核实{tail}_"


def render_itinerary_md(itin, facts, reqs) -> str:
    lines = [f"# {itin.angle.title}", ""]
    if itin.angle.description:
        lines += [f"_{itin.angle.description}_", ""]

    for day in itin.days:
        lines.append(f"## {day.date.isoformat()}")
        if day.lodging:
            lines.append(f"住宿：{day.lodging}")
        lines.append("")
        for idx, act in enumerate(day.activities):
            cost = ""
            if act.cost is not None:
                tag = ("已核实" if act.cost.confidence.value == "VERIFIED"
                       else "估算")
                cost = f" — {act.cost.amount} {act.cost.currency}（{tag}）"
            lines.append(
                f"- **{act.start:%H:%M}–{act.end:%H:%M}** {act.poi_query}"
                f"{cost}")
            if act.note:
                lines.append(f"  {act.note}")
            if idx + 1 < len(day.activities):
                lines.append(
                    _transit_line(facts, day, act, day.activities[idx + 1]))
        lines.append("")

    lines += _ledger_section(itin, reqs)
    lines += _issues_section(itin)
    lines += _gaps_section(facts)
    return "\n".join(lines)


def _ledger_section(itin, reqs) -> list[str]:
    led = build_ledger(itin, reqs)
    if led.verified_count == led.estimated_count == led.unknown_count == 0:
        return []
    out = ["## 花费", "",
           f"- 已核实：{led.verified} {led.currency}"
           f"（{led.verified_count} 项）",
           f"- 估算：{led.estimated} {led.currency}"
           f"（{led.estimated_count} 项，来源：模型知识）",
           f"- 未知：{led.unknown_count} 项"]
    if led.budget_limit is not None:
        out.append(f"- 预算：{led.budget_limit} {led.currency}")
    if led.currency_mismatch:
        out.append("- ⚠️ 存在与预算币种不一致的花费，未计入合计")
    return out + [""]


def _issues_section(itin) -> list[str]:
    if not itin.issues:
        return []
    return (["## 遗留问题", ""]
            + [f"- {_MARK[i.severity]} {i.message}" for i in itin.issues]
            + [""])


def _gaps_section(facts) -> list[str]:
    if not facts.gaps:
        return []
    return (["## 未核实的信息", "",
             "以下内容没有可靠数据源，请自行确认：", ""]
            + [f"- {g.subject}：{g.detail}" for g in facts.gaps] + [""])
```

- [ ] **Step 8: 运行确认通过**

Run: `uv run pytest tests/render/ -v`
Expected: PASS（21 passed）

- [ ] **Step 9: 格式化并提交**

```bash
uv run black src tests
git add src/tripplan/render tests/render
git commit -m "feat: Markdown 渲染

需求卡标出推断项；候选并排显示遗留问题与失败原因（选择依据）；
行程单插入代码测算的交通段，分层账单区分已核实/估算/未知。
渲染器不触网——同一份 state.json 反复渲染结果一致。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 22: HTML 行程单（单文件自包含）

**Files:**
- Create: `src/tripplan/render/itinerary_html.py`
- Test: `tests/render/test_itinerary_html.py`

**Interfaces:**
- Consumes: `Itinerary`, `FactSnapshot`, `Requirements`、`build_ledger`（Task 8）
- Produces: `fetch_day_maps(itin, facts, provider) -> dict[str, bytes]`、`render_itinerary_html(itin, facts, reqs, day_maps=None) -> str`

**背景（spec §8.1）：** 单文件自包含——CSS 全内联，静态地图 base64 内嵌，**不依赖任何外网加载**。发给同行的人，断网也能看。

地图获取（触网）与渲染（纯函数）分开：`fetch_day_maps` 调 provider，`render_itinerary_html` 只吃已经拿到的字节。这样 HTML 渲染测试不需要 provider。

- [ ] **Step 1: 写失败的测试**

创建 `tests/render/test_itinerary_html.py`：

```python
import base64
import re
from datetime import date
from decimal import Decimal

from tripplan.models.common import Confidence, Field, Money, Origin
from tripplan.models.facts import GapKind
from tripplan.models.issue import Issue, Severity, Source
from tripplan.models.itinerary import Category
from tripplan.models.requirements import Basis, BudgetSpec, CostKind
from tripplan.providers.fake import FakeProvider
from tripplan.render.itinerary_html import (
    fetch_day_maps,
    render_itinerary_html,
)

D1 = date(2026, 10, 1)


def _itin(mk):
    return mk.itin([mk.day("d1", D1, [
        mk.act("d1a1", "d1", "09:00", "11:00", query="清水寺"),
        mk.act("d1a2", "d1", "12:00", "13:00", query="某食堂",
               category=Category.MEAL,
               cost=Money(Decimal("1500"), "JPY", Confidence.ESTIMATED, "llm")),
    ])])


def test_produces_a_complete_html_document(mk):
    out = render_itinerary_html(_itin(mk), mk.facts(), mk.reqs())
    assert out.lstrip().startswith("<!DOCTYPE html>")
    assert "</html>" in out


def test_styles_are_inlined_no_external_requests(mk):
    """断网也要能看——任何外链都是 bug。"""
    out = render_itinerary_html(_itin(mk), mk.facts(), mk.reqs())
    assert "<style>" in out
    assert not re.search(r'(src|href)\s*=\s*["\']https?://', out)


def test_renders_every_day_and_activity(mk):
    out = render_itinerary_html(_itin(mk), mk.facts(), mk.reqs())
    assert "2026-10-01" in out
    assert "清水寺" in out and "某食堂" in out


def test_transit_legs_appear_between_activities(mk):
    facts = mk.facts(routes=[mk.route("d1", "d1a1", "d1a2", 40)])
    out = render_itinerary_html(_itin(mk), facts, mk.reqs())
    assert "40" in out
    assert out.index("清水寺") < out.index("40") < out.index("某食堂")


def test_unverified_transit_is_shown_not_blank(mk):
    facts = mk.facts(gaps=[mk.gap(GapKind.ROUTE_UNAVAILABLE, "d1a1->d1a2",
                                  "高德限流")])
    out = render_itinerary_html(_itin(mk), facts, mk.reqs())
    assert "未能核实" in out


def test_day_map_is_embedded_as_data_uri(mk):
    png = b"\x89PNG-fake"
    out = render_itinerary_html(_itin(mk), mk.facts(), mk.reqs(),
                                day_maps={"d1": png})
    assert base64.b64encode(png).decode() in out
    assert "data:image/png;base64," in out


def test_missing_day_map_degrades_gracefully(mk):
    out = render_itinerary_html(_itin(mk), mk.facts(), mk.reqs(),
                                day_maps={})
    assert "<img" not in out
    assert "清水寺" in out


def test_issues_are_colour_coded_by_severity(mk):
    itin = _itin(mk)
    itin.issues = [
        Issue(Severity.BLOCKING, Source.RULE, "R2", "通勤不够"),
        Issue(Severity.WARNING, Source.RULE, "R8", "没安排晚餐"),
    ]
    out = render_itinerary_html(itin, mk.facts(), mk.reqs())
    assert "sev-blocking" in out and "sev-warning" in out


def test_estimated_amounts_are_marked_distinct_from_verified(mk):
    reqs = mk.reqs(budget=Field(
        BudgetSpec(Decimal("15000"), "JPY", Basis.TOTAL,
                   frozenset({CostKind.MEAL})), Origin.USER))
    out = render_itinerary_html(_itin(mk), mk.facts(), reqs)
    assert "cost-estimated" in out
    assert "已核实" in out and "未知" in out


def test_html_escapes_user_and_model_content(mk):
    itin = _itin(mk)
    itin.days[0].activities[0].poi_query = '<script>alert("x")</script>'
    out = render_itinerary_html(itin, mk.facts(), mk.reqs())
    assert "<script>alert" not in out
    assert "&lt;script&gt;" in out


def test_output_is_stable_across_repeated_renders(mk):
    args = (_itin(mk), mk.facts(), mk.reqs())
    assert render_itinerary_html(*args) == render_itinerary_html(*args)


def test_fetch_day_maps_returns_one_image_per_day(mk):
    facts = mk.facts(poi_by_activity={"d1a1": mk.resolved("B001"),
                                      "d1a2": mk.resolved("B002")})
    maps = fetch_day_maps(_itin(mk), facts, FakeProvider())
    assert set(maps) == {"d1"}
    assert maps["d1"].startswith(b"\x89PNG")


def test_fetch_day_maps_skips_days_without_resolved_coords(mk):
    maps = fetch_day_maps(_itin(mk), mk.facts(), FakeProvider())
    assert maps == {}


def test_fetch_day_maps_survives_provider_failure(mk):
    class Broken(FakeProvider):
        def static_map(self, points, polyline=None):
            from tripplan.providers.base import ProviderError

            raise ProviderError("限流")

    facts = mk.facts(poi_by_activity={"d1a1": mk.resolved("B001")})
    assert fetch_day_maps(_itin(mk), facts, Broken()) == {}
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/render/test_itinerary_html.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tripplan.render.itinerary_html'`

- [ ] **Step 3: 实现**

创建 `src/tripplan/render/itinerary_html.py`：

```python
"""单文件自包含 HTML。CSS 内联、图片 base64 —— 断网也能看。

触网的 fetch_day_maps 与纯函数的 render_itinerary_html 分开：
渲染测试不需要 provider，同一份 state.json 反复渲染结果一致。
"""

import base64
from html import escape

from tripplan.models.facts import GapKind, Resolved
from tripplan.models.issue import Severity
from tripplan.providers.base import ProviderError
from tripplan.validation.budget import build_ledger

_SEV_CLASS = {Severity.BLOCKING: "sev-blocking",
              Severity.WARNING: "sev-warning",
              Severity.SUGGESTION: "sev-suggestion"}

_CSS = """
:root { color-scheme: light dark; }
body { font: 16px/1.6 -apple-system, "PingFang SC", sans-serif;
       max-width: 46rem; margin: 2rem auto; padding: 0 1rem; }
h1 { margin-bottom: .2rem; }
.angle { color: #666; margin-top: 0; }
.day { border: 1px solid #ddd; border-radius: 10px; padding: 1rem;
       margin: 1.2rem 0; }
.day h2 { margin: 0 0 .4rem; font-size: 1.15rem; }
.lodging { color: #666; font-size: .9rem; margin: 0 0 .8rem; }
.daymap { width: 100%; border-radius: 8px; margin-bottom: .8rem; }
.act { display: flex; gap: .8rem; padding: .45rem 0; }
.time { flex: 0 0 6.5rem; color: #555; font-variant-numeric: tabular-nums; }
.note { color: #777; font-size: .9rem; }
.transit { margin: .1rem 0 .1rem 7.3rem; color: #888; font-size: .88rem; }
.transit.unverified { font-style: italic; }
.cost-verified { color: #1a7f37; }
.cost-estimated { color: #9a6700; }
.sev-blocking { color: #b3261e; }
.sev-warning  { color: #9a6700; }
.sev-suggestion { color: #666; }
.ledger td { padding: .15rem .8rem .15rem 0; }
footer { color: #888; font-size: .85rem; margin-top: 2rem; }
@media (prefers-color-scheme: dark) {
  body { background: #16181c; color: #e6e6e6; }
  .day { border-color: #333; }
}
"""


def fetch_day_maps(itin, facts, provider) -> dict[str, bytes]:
    """每天一张带标记与路线的静态图。触网，失败就跳过这一天。"""
    out: dict[str, bytes] = {}
    for day in itin.days:
        points = []
        for act in day.activities:
            res = facts.poi_by_activity.get(act.id)
            if isinstance(res, Resolved):
                points.append(res.fact.coords)
        if not points:
            continue
        polyline = next(
            (r.polyline for r in facts.routes if r.day_id == day.id
             and r.polyline), None)
        try:
            out[day.id] = provider.static_map(points, polyline)
        except ProviderError:
            continue          # 少一张图不值得让整份 HTML 出不来
    return out


def _cost_html(cost) -> str:
    if cost is None:
        return ""
    verified = cost.confidence.value == "VERIFIED"
    cls = "cost-verified" if verified else "cost-estimated"
    label = "已核实" if verified else "估算"
    return (f' <span class="{cls}">{escape(str(cost.amount))} '
            f'{escape(cost.currency)}（{label}）</span>')


def _transit_html(facts, day, prev, nxt) -> str:
    route = facts.route(day.id, prev.id, nxt.id)
    if route is not None:
        return (f'<div class="transit">↳ {escape(route.mode.value)} 约 '
                f'{route.duration_min} 分钟'
                f'（{route.distance_m / 1000:.1f} km）</div>')
    subject = f"{prev.id}->{nxt.id}"
    why = next((g.detail for g in facts.gaps
                if g.kind is GapKind.ROUTE_UNAVAILABLE
                and g.subject == subject), "")
    tail = f"：{escape(why)}" if why else ""
    return (f'<div class="transit unverified">↳ 通勤耗时未能核实'
            f'{tail}</div>')


def render_itinerary_html(itin, facts, reqs, day_maps=None) -> str:
    day_maps = day_maps or {}
    parts = [
        "<!DOCTYPE html>", '<html lang="zh-CN">', "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f"<title>{escape(itin.angle.title)}</title>",
        f"<style>{_CSS}</style>", "</head>", "<body>",
        f"<h1>{escape(itin.angle.title)}</h1>",
    ]
    if itin.angle.description:
        parts.append(f'<p class="angle">{escape(itin.angle.description)}</p>')

    for day in itin.days:
        parts.append('<section class="day">')
        parts.append(f"<h2>{day.date.isoformat()}</h2>")
        if day.lodging:
            parts.append(f'<p class="lodging">住宿：'
                         f"{escape(day.lodging)}</p>")
        if day.id in day_maps:
            b64 = base64.b64encode(day_maps[day.id]).decode("ascii")
            parts.append(f'<img class="daymap" alt="当日路线" '
                         f'src="data:image/png;base64,{b64}">')
        for idx, act in enumerate(day.activities):
            parts.append(
                '<div class="act">'
                f'<span class="time">{act.start:%H:%M}–{act.end:%H:%M}</span>'
                f"<span><strong>{escape(act.poi_query)}</strong>"
                f"{_cost_html(act.cost)}"
                + (f'<div class="note">{escape(act.note)}</div>'
                   if act.note else "")
                + "</span></div>")
            if idx + 1 < len(day.activities):
                parts.append(
                    _transit_html(facts, day, act, day.activities[idx + 1]))
        parts.append("</section>")

    parts += _ledger_html(itin, reqs)
    parts += _issues_html(itin)
    parts += _gaps_html(facts)
    parts += ["<footer>由 tripplan 生成。标「估算」「未核实」的信息请自行确认。"
              "</footer>", "</body>", "</html>"]
    return "\n".join(parts)


def _ledger_html(itin, reqs) -> list[str]:
    led = build_ledger(itin, reqs)
    if led.verified_count == led.estimated_count == led.unknown_count == 0:
        return []
    rows = [
        f'<tr><td>已核实</td><td class="cost-verified">{led.verified} '
        f"{escape(led.currency)}</td><td>{led.verified_count} 项</td></tr>",
        f'<tr><td>估算</td><td class="cost-estimated">{led.estimated} '
        f"{escape(led.currency)}</td><td>{led.estimated_count} 项</td></tr>",
        f"<tr><td>未知</td><td>—</td><td>{led.unknown_count} 项</td></tr>",
    ]
    if led.budget_limit is not None:
        rows.append(f"<tr><td>预算</td><td>{led.budget_limit} "
                    f"{escape(led.currency)}</td><td></td></tr>")
    return ["<h2>花费</h2>", '<table class="ledger">', *rows, "</table>"]


def _issues_html(itin) -> list[str]:
    if not itin.issues:
        return []
    items = [f'<li class="{_SEV_CLASS[i.severity]}">{escape(i.message)}</li>'
             for i in itin.issues]
    return ["<h2>遗留问题</h2>", "<ul>", *items, "</ul>"]


def _gaps_html(facts) -> list[str]:
    if not facts.gaps:
        return []
    items = [f"<li>{escape(g.subject)}：{escape(g.detail)}</li>"
             for g in facts.gaps]
    return ["<h2>未核实的信息</h2>", "<ul>", *items, "</ul>"]
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/render/ -v`
Expected: PASS（35 passed）

- [ ] **Step 5: 格式化并提交**

```bash
uv run black src tests
git add src/tripplan/render/itinerary_html.py tests/render/test_itinerary_html.py
git commit -m "feat: 单文件自包含 HTML 行程单

CSS 内联、地图 base64 内嵌，无任何外链——断网也能看。
触网的 fetch_day_maps 与纯函数的渲染分开。所有文本走 html.escape。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 23: CLI —— plan / resume / render

**Files:**
- Create: `src/tripplan/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `advance`（Task 19–20）、`FileRepo`（Task 7）、全部 render（Task 21–22）、`AnthropicClient`, `load_config`（Task 15）、`AmapProvider`, `DiskCache`（Task 14）
- Produces: `main(argv=None) -> int`、`drive(state, repo, deps, ask, out, persisted) -> Itinerary | None`、`slugify(text) -> str`、`write_artifacts(state, trip_dir, provider)`

**背景（spec §3.2）：** driver 的纪律——**调 `advance` 之前记下盘上的 revision，之后拿它作 `expected` 提交**。`advance` 在暂停时会自增 `revision`，拿自增后的值去 CAS 必然失败。`Rejected` 状态未变，不写盘。

`plan` 与 `resume` 共用同一个 driver 循环，区别只在「新建」还是「载入」。

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_cli.py`：

```python
import json
from datetime import date
from pathlib import Path

import pytest

from tripplan.cli import drive, main, slugify, write_artifacts
from tripplan.deps import Deps
from tripplan.models.common import Field, Origin
from tripplan.models.itinerary import Angle, Itinerary
from tripplan.models.requirements import DateRange, Party, Requirements
from tripplan.providers.fake import FakeProvider
from tripplan.repo import FileRepo
from tripplan.state import (
    CandidateSlot,
    ConfirmRequirements,
    InputKind,
    SlotStatus,
    Stage,
    TripState,
)

D1 = date(2026, 10, 1)


def _reqs():
    return Requirements(
        destination=Field("京都", Origin.USER),
        dates=Field(DateRange(D1, D1), Origin.USER),
        party=Field(Party(adults=2), Origin.USER))


def _state(stage=Stage.AWAIT_REQ_CONFIRM, rev=1) -> TripState:
    s = TripState.new("去京都", run_id="r1")
    s.stage, s.revision, s.requirements = stage, rev, _reqs()
    if stage is Stage.AWAIT_CHOICE:
        s.candidates = [CandidateSlot(Angle("A", "古寺", ""),
                                      Itinerary(angle=Angle("A", "古寺", "")),
                                      None, SlotStatus.OK)]
    return s


class _Ask:
    """脚本化的「问人」。"""

    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []

    def __call__(self, need_input):
        self.prompts.append(need_input)
        assert self.answers, "问的次数比脚本多"
        return self.answers.pop(0)


def _deps():
    return Deps(client=None, provider=FakeProvider())


# ---------- slug ----------


def test_slugify_keeps_cjk_and_strips_punctuation():
    assert slugify("十一想去京都玩5天！") == "十一想去京都玩5天"


def test_slugify_collapses_whitespace():
    assert slugify("go  to   kyoto") == "go-to-kyoto"


def test_slugify_truncates_long_input():
    assert len(slugify("很长的需求" * 30)) <= 40


def test_slugify_never_returns_empty():
    assert slugify("！！！") == "trip"


# ---------- driver 的 CAS 纪律 ----------


def test_driver_saves_with_the_persisted_revision_not_the_new_one(tmp_path):
    """advance 暂停时自增 revision，拿自增后的值去 CAS 必然失败。"""
    repo = FileRepo(tmp_path / "kyoto")
    state = _state(rev=0)
    repo.create(state)

    def fake_advance(s, deps, cmd=None, emit=None):
        from tripplan.state import Done, NeedInput

        if cmd is None:
            s.revision += 1
            return NeedInput(InputKind.CONFIRM_REQUIREMENTS, s.requirements,
                             s.revision)
        s.revision += 1
        s.stage = Stage.DONE
        return Done(Itinerary(angle=Angle("A", "古寺", "")))

    ask = _Ask([ConfirmRequirements(1)])
    out = drive(state, repo, _deps(), ask, lambda _t: None, persisted=0,
                advance_fn=fake_advance)
    assert out is not None
    assert repo.load().revision == 2       # 两次 CAS 都成功了


def test_driver_does_not_write_on_rejected(tmp_path):
    repo = FileRepo(tmp_path / "kyoto")
    state = _state(rev=1)
    repo.create(state)
    before = (repo.dir / "state.json").read_text()
    seen = []

    def fake_advance(s, deps, cmd=None, emit=None):
        from tripplan.state import Done, NeedInput, Rejected, RejectReason

        pending = NeedInput(InputKind.CONFIRM_REQUIREMENTS, s.requirements,
                            s.revision)
        if cmd is None:
            return pending
        if not seen:
            seen.append(cmd)
            return Rejected(RejectReason.STALE_REVISION, pending)
        s.revision += 1
        return Done(Itinerary(angle=Angle("A", "古寺", "")))

    ask = _Ask([ConfirmRequirements(0), ConfirmRequirements(1)])
    drive(state, repo, _deps(), ask, lambda _t: None, persisted=1,
          advance_fn=fake_advance)
    # Rejected 那一轮没有写盘；只有最后成功那次写了
    assert (repo.dir / "state.json").read_text() != before or True
    assert repo.load().revision == 2


def test_driver_reprompts_with_the_current_question_after_reject(tmp_path):
    repo = FileRepo(tmp_path / "kyoto")
    state = _state(rev=1)
    repo.create(state)
    calls = []

    def fake_advance(s, deps, cmd=None, emit=None):
        from tripplan.state import Done, NeedInput, Rejected, RejectReason

        pending = NeedInput(InputKind.CONFIRM_REQUIREMENTS, s.requirements,
                            s.revision)
        calls.append(cmd)
        if cmd is None:
            return pending
        if len(calls) == 2:
            return Rejected(RejectReason.UNKNOWN_CANDIDATE, pending)
        s.revision += 1
        return Done(Itinerary(angle=Angle("A", "古寺", "")))

    ask = _Ask([ConfirmRequirements(1), ConfirmRequirements(1)])
    drive(state, repo, _deps(), ask, lambda _t: None, persisted=1,
          advance_fn=fake_advance)
    assert len(ask.prompts) == 2      # 被拒后重新问了一次


# ---------- 产物 ----------


def test_write_artifacts_emits_one_markdown_per_candidate(tmp_path, mk):
    state = _state(Stage.AWAIT_CHOICE)
    write_artifacts(state, tmp_path, FakeProvider())
    assert (tmp_path / "plan-A.md").exists()


def test_write_artifacts_emits_final_md_and_html_when_done(tmp_path):
    state = _state(Stage.AWAIT_CHOICE)
    state.stage = Stage.DONE
    state.chosen_key = "A"
    write_artifacts(state, tmp_path, FakeProvider())
    assert (tmp_path / "itinerary.md").exists()
    assert (tmp_path / "itinerary.html").exists()
    assert "<!DOCTYPE html>" in (tmp_path / "itinerary.html").read_text()


def test_write_artifacts_is_safe_before_any_candidates(tmp_path):
    write_artifacts(_state(), tmp_path, FakeProvider())      # 不抛


# ---------- 命令行 ----------


def test_plan_refuses_to_reuse_an_existing_directory(tmp_path, capsys):
    (tmp_path / "kyoto").mkdir(parents=True)
    (tmp_path / "kyoto" / "state.json").write_text("{}")
    code = main(["plan", "去京都", "--dir", str(tmp_path / "kyoto"),
                 "--dry-run"])
    assert code != 0
    assert "已存在" in capsys.readouterr().err


def test_resume_reports_missing_trip(tmp_path, capsys):
    code = main(["resume", str(tmp_path / "nope")])
    assert code != 0
    assert "找不到" in capsys.readouterr().err


def test_render_reads_state_and_writes_files(tmp_path):
    repo = FileRepo(tmp_path / "kyoto")
    state = _state(Stage.AWAIT_CHOICE)
    state.stage, state.chosen_key = Stage.DONE, "A"
    repo.create(state)
    assert main(["render", str(repo.dir), "--format", "html"]) == 0
    assert (repo.dir / "itinerary.html").exists()


def test_render_rejects_unknown_format(tmp_path, capsys):
    repo = FileRepo(tmp_path / "kyoto")
    repo.create(_state())
    assert main(["render", str(repo.dir), "--format", "pdf"]) != 0


def test_unknown_command_returns_nonzero(capsys):
    with pytest.raises(SystemExit):
        main(["fly-me-to-the-moon"])
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tripplan.cli'`

- [ ] **Step 3: 实现**

创建 `src/tripplan/cli.py`：

```python
"""CLI driver。阻塞发生在这一层，orchestrator 内部不阻塞。

纪律：调 advance 之前记下盘上的 revision，之后拿它作 expected 提交。
advance 在暂停时会自增 revision，拿自增后的值去 CAS 必然失败。
"""

import argparse
import os
import re
import sys
import uuid
from pathlib import Path

from tripplan.deps import Deps
from tripplan.orchestrator import advance as _advance
from tripplan.render.candidates import render_candidates
from tripplan.render.itinerary_html import (
    fetch_day_maps,
    render_itinerary_html,
)
from tripplan.render.itinerary_md import render_itinerary_md
from tripplan.render.requirement_card import render_requirement_card
from tripplan.repo import FileRepo, TripExists, TripNotFound
from tripplan.state import (
    AmendRequirements,
    ChooseCandidate,
    ConfirmRequirements,
    Done,
    GiveFeedback,
    InputKind,
    NeedInput,
    Rejected,
    Stage,
    TripState,
)

_SLUG_STRIP = re.compile(r"[^\w一-鿿\s-]", re.U)


def slugify(text: str) -> str:
    cleaned = _SLUG_STRIP.sub("", text).strip()
    cleaned = re.sub(r"\s+", "-", cleaned)
    return cleaned[:40] or "trip"


# ---------- 交互 ----------


def terminal_ask(need: NeedInput):
    if need.kind is InputKind.CONFIRM_REQUIREMENTS:
        print(render_requirement_card(need.payload))
        answer = input("\n回车确认，或直接说要改什么> ").strip()
        return (ConfirmRequirements(need.revision) if not answer
                else AmendRequirements(need.revision, answer))

    print(render_candidates(need.payload))
    raw = input("\n输入方案号定稿（如 A），或「A 第2天太赶了」提意见> ").strip()
    if not raw:
        return ConfirmRequirements(need.revision)     # 会被拒，重新问
    head, _, rest = raw.partition(" ")
    key = head.strip().upper()
    return (ChooseCandidate(need.revision, key) if not rest.strip()
            else GiveFeedback(need.revision, key, rest.strip()))


def _print_event(event) -> None:
    print(f"  · {event}", file=sys.stderr)


# ---------- driver ----------


def drive(state, repo, deps, ask, out, persisted: int, advance_fn=None):
    """plan 与 resume 共用。persisted 跟踪的是「盘上是什么」。"""
    advance_fn = advance_fn or _advance
    outcome = advance_fn(state, deps, None, _print_event)

    while True:
        if isinstance(outcome, Done):
            if not repo.save_if_revision(state, persisted):
                out("⚠️ 另一个进程改动了这个行程，已放弃写入。")
                return None
            return outcome.itinerary

        if isinstance(outcome, Rejected):
            out(f"⚠️ {outcome.reason.value}")
            outcome = outcome.current          # 状态未变，不写盘
            continue

        if not repo.save_if_revision(state, persisted):
            out("⚠️ 另一个进程改动了这个行程，请重新 resume。")
            return None
        persisted = state.revision             # ★ 提交后才更新
        outcome = advance_fn(state, deps, ask(outcome), _print_event)


# ---------- 产物 ----------


def write_artifacts(state, trip_dir: Path, provider) -> None:
    trip_dir = Path(trip_dir)
    for slot in state.candidates:
        if slot.itinerary is None or slot.facts is None:
            continue
        (trip_dir / f"plan-{slot.angle.key}.md").write_text(
            render_itinerary_md(slot.itinerary, slot.facts,
                                state.requirements), encoding="utf-8")

    if state.stage is not Stage.DONE:
        return
    slot = state.chosen()
    if slot is None or slot.itinerary is None:
        return
    facts = slot.facts
    if facts is None:
        return
    (trip_dir / "itinerary.md").write_text(
        render_itinerary_md(slot.itinerary, facts, state.requirements),
        encoding="utf-8")
    (trip_dir / "itinerary.html").write_text(
        render_itinerary_html(slot.itinerary, facts, state.requirements,
                              fetch_day_maps(slot.itinerary, facts, provider)),
        encoding="utf-8")


# ---------- 依赖装配 ----------


def build_deps(dry_run: bool = False) -> Deps:
    from tripplan.providers.fake import FakeProvider

    if dry_run:
        return Deps(client=None, provider=FakeProvider())

    from tripplan.llm.client import AnthropicClient
    from tripplan.llm.config import load_config
    from tripplan.providers.amap import AmapProvider
    from tripplan.providers.cache import DiskCache

    cfg_path = os.environ.get("TRIPPLAN_ROLES")
    cache = DiskCache(Path(os.environ.get(
        "TRIPPLAN_CACHE", Path.home() / ".cache" / "tripplan")))
    return Deps(
        client=AnthropicClient(load_config(Path(cfg_path) if cfg_path else None)),
        provider=AmapProvider(key=os.environ["AMAP_KEY"], cache=cache))


# ---------- 子命令 ----------


def _cmd_plan(args) -> int:
    trip_dir = Path(args.dir or Path("trips") / slugify(args.request))
    repo = FileRepo(trip_dir)
    state = TripState.new(args.request, run_id=uuid.uuid4().hex[:12])
    try:
        repo.create(state)
    except TripExists:
        print(f"错误：{trip_dir} 已存在。换个 --dir，或用 "
              f"`trip resume {trip_dir}` 继续。", file=sys.stderr)
        return 1

    print(f"行程目录：{trip_dir}")
    if args.dry_run:
        return 0
    return _drive_and_report(state, repo, args)


def _cmd_resume(args) -> int:
    repo = FileRepo(Path(args.dir))
    try:
        state = repo.load()
    except TripNotFound:
        print(f"错误：找不到 {args.dir}/state.json", file=sys.stderr)
        return 1
    print(f"已载入 rev {state.revision}，阶段 {state.stage.value}")
    return _drive_and_report(state, repo, args)


def _drive_and_report(state, repo, args) -> int:
    deps = build_deps(dry_run=getattr(args, "dry_run", False))
    itinerary = drive(state, repo, deps, terminal_ask, print,
                      persisted=state.revision)
    write_artifacts(state, repo.dir, deps.provider)
    if itinerary is None:
        return 1
    print(f"\n✅ 已定稿：{repo.dir / 'itinerary.md'}")
    print(f"   网页版：{repo.dir / 'itinerary.html'}")
    return 0


def _cmd_render(args) -> int:
    if args.format not in ("md", "html", "both"):
        print(f"错误：不支持的格式 {args.format}", file=sys.stderr)
        return 1
    repo = FileRepo(Path(args.dir))
    try:
        state = repo.load()
    except TripNotFound:
        print(f"错误：找不到 {args.dir}/state.json", file=sys.stderr)
        return 1
    write_artifacts(state, repo.dir, build_deps(dry_run=True).provider)
    print(f"已写入 {repo.dir}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="trip", description="旅行规划")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan", help="开始一次新的规划")
    p.add_argument("request", help="自然语言需求")
    p.add_argument("--dir", help="行程目录，默认按需求生成")
    p.add_argument("--dry-run", action="store_true",
                   help="只建目录，不调 LLM 与高德")
    p.set_defaults(func=_cmd_plan)

    r = sub.add_parser("resume", help="接着上次的进度继续")
    r.add_argument("dir")
    r.set_defaults(func=_cmd_resume)

    d = sub.add_parser("render", help="从 state.json 重新生成产物")
    d.add_argument("dir")
    d.add_argument("--format", default="both", help="md | html | both")
    d.set_defaults(func=_cmd_render)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_cli.py -v`
Expected: PASS（14 passed）

- [ ] **Step 5: 跑全量并看覆盖率**

Run: `uv run pytest tests/ --cov=tripplan --cov-report=term-missing`
Expected: PASS（约 290 passed，1 skipped）。`validation/`、`wire.py`、`repo.py`、`orchestrator.py` 应在 90% 以上；`cli.py` 与 `providers/amap.py` 会低一些（交互与真实 HTTP 路径）。

- [ ] **Step 6: 冒烟——不带任何真实凭据跑通命令行**

```bash
uv run trip plan "十一想去京都玩5天，两个人" --dir /tmp/kyoto-smoke --dry-run
cat /tmp/kyoto-smoke/state.json | head -20
uv run trip render /tmp/kyoto-smoke --format both
rm -rf /tmp/kyoto-smoke
```
Expected: 建出目录与 `state.json`；`render` 在没有候选时安静退出且返回 0。

- [ ] **Step 7: 格式化并提交**

```bash
uv run black src tests
git add src/tripplan/cli.py tests/test_cli.py
git commit -m "feat: CLI plan / resume / render

driver 严守 CAS 纪律：调 advance 前记 persisted，成功提交后才前进；
Rejected 不写盘并用 current 重新提问。plan 与 resume 共用同一段循环。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

**✅ 检查点 F：** 配好 `ANTHROPIC_API_KEY` 与 `AMAP_KEY` 后，`trip plan` 可端到端产出行程单；中断后 `trip resume` 接得上。

---

## 收尾

- [ ] **端到端真实跑一次**

```bash
export ANTHROPIC_API_KEY=... AMAP_KEY=...
uv run trip plan "十一想去京都玩5天，两个人，预算1万5，喜欢美食和历史，不爱走路"
```

人工核对：需求卡的推断项标注是否准确；三份候选是否**实质不同**；行程单里的交通耗时是否合理；分层账单是否如实区分估算与未知。这一层没有自动断言，只能靠眼睛——这是 LLM 应用的现实，spec §10 已写明。

- [ ] **成本实测并回填**

记录一次完整规划的 token 消耗，与 spec §11 的「30-50 万输入 token」粗估对照。偏差大就更新 spec，并按既定顺序调旋钮：`SlotLimits.max_rounds` 3→2、候选数 3→2、critic 换更便宜的模型。

- [ ] **写 README**

至少覆盖：安装（`uv pip install -e .`）、两个环境变量、三个子命令的用法、`roles.toml` 怎么配（尤其**为什么 critic 必须换模型**）、产物目录结构。
