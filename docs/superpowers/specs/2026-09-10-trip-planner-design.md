# 旅行规划 Agent — 设计文档

日期：2026-09-10
状态：已确认，待转实现计划

## 1. 目标

一个 CLI 旅行规划工具。用户用自然语言描述需求，系统澄清需求、生成多份候选行程、
自动审查并修订，最终产出可执行的行程单。

核心流程：**需求卡 → 三份候选并行打磨 → 人工挑选/反馈 → 定稿**。

设计上的一条主线贯穿全文：**流程骨架由代码保证，规划智能由 LLM 发挥**。
凡是"必须发生"的事（校验、循环上限、状态持久化）交给代码；凡是"需要判断力"的事
（去哪、怎么排、好不好）交给 LLM，且不限制它怎么想。

## 2. 范围

### v1 包含

- 需求抽取 + 推断 + 一次性确认
- 单目的地行程规划
- 三份差异化候选方案，各自打磨到收敛后一并展示
- 三层 review：确定性规则 + LLM critic（换模型）+ 人工
- 需求变更处理（"三天改四天"）
- 高德地图数据接入（POI、点到点耗时、静态地图）
- Markdown 行程单输出
- HTML 行程单输出（单文件自包含，含每日路线图）

### v1 不包含

| 不做 | 原因 |
|---|---|
| 多城市行程 | 引入城际交通、住宿切换、行李寄存等一整套约束，数据模型要加 city 维度 |
| `trip resume` 命令 | 见下方"关于持久化" |
| ics 导出 | 纯渲染层，`Itinerary` 已结构化，随时可加 |
| Skill 形态 | 见 §12.2，架构已为此留路，且比预想的便宜 |
| Web 界面 | 见 §12.1，架构已为此留路 |
| 机票 / 酒店真实预订 | 需要携程内部 API，见 §12.3 |

### 已知局限（诚实记录，不假装）

**只有真实测得的事实才配做 BLOCKING**，这条原则贯穿 §5.1，v1 有三处受它约束：

- **营业时间**没有权威数据源，靠 LLM 填充 + 高德 POI 兜底 → 规则 #9 只给 WARNING，
  行程单上标"未核实"。
- **票价**同理，`Money.confidence` 绝大多数是 `ESTIMATED` → 规则 #6 输出分层账单
  而非 BLOCKING 判定（详见 §5.1）。
- **交通耗时**是 v1 唯一真实测得的数据（高德），所以规则 #2 是 BLOCKING——
  但查询失败落进 `FactSnapshot.gaps` 时同样降级。

接入真实 API 后，#9 与 #6 自然升级，无需改动规则代码。

### 关于持久化与 `resume`

"每步存盘却不提供 resume 命令"看着像自相矛盾，但存盘在 v1 有两个独立的消费者：
崩溃取证（一次规划烧 30-50 万 token，死在第三轮时你需要现场）和 §12.2 的 skill
形态（它本质上就是外部进程反复调 `advance`）。所以存盘不是为 resume 而存在的。

不过话说回来：给定 `advance()` 的形状，`trip resume <dir>` 就是 `load` + 进入
driver 循环，十行左右。**以一次运行的成本计，崩溃后能接着跑的价值远高于这十行**。
建议纳入 v1；此处按既定范围记为不做，等确认后一并调整。

## 3. 整体架构

### 3.1 核心理念

```
┌─────────────────────────────────────────────┐
│  外层骨架（确定性代码，约 200 行）              │
│  · 阶段推进   · 强制校验                       │
│  · 循环上限   · 状态持久化                     │
│  ┌───────────────────────────────────────┐  │
│  │  内层：LLM 自主 tool loop（不受限）      │  │
│  │  调什么工具、调几次、什么顺序、          │  │
│  │  行程怎么排 —— 全由 LLM 决定             │  │
│  └───────────────────────────────────────┘  │
└─────────────────────────────────────────────┘
```

关键约束：**校验由外层强制执行，不是 LLM 可选调用的工具**。
LLM 交出行程 → 外层自动跑 validator → 有硬伤就把问题清单塞回去让它改 → 再校验。
LLM 无法"觉得没问题"而跳过。

被否决的替代方案：

- **全自主 agent tool loop** — 最灵活，但关键步骤会被静默跳过（LLM 写完行程觉得
  合理就不调 validate，或调了看到冲突觉得问题不大直接输出）。三层 review 里最值钱
  的"确定性规则一定会跑"无法保证。且同输入两次结果差异大，调试困难。
- **LangGraph 等图编排框架** — 五个节点的图不值得引入框架依赖和概念负担。

### 3.2 `advance` 接口

orchestrator **不阻塞、不读 stdin、不 print**。它暴露一个"推进一步"的函数：

```python
def advance(state: TripState,
            cmd: Command | None = None,
            emit: Callable[[Event], None] = noop) -> Outcome:
    """推进到下一个需要人参与的点，或终点。
    调用方负责：持久化 state、获取人类输入、再次调用。
    等待态传 cmd=None 是合法的 —— 原样返回当前在等的问题。"""

Outcome = Done | NeedInput | Rejected

@dataclass
class Done:
    itinerary: Itinerary

@dataclass
class NeedInput:
    kind: InputKind        # CONFIRM_REQUIREMENTS | CHOOSE_OR_FEEDBACK
    payload: object        # 渲染所需数据（需求卡 / 候选列表）
    revision: int          # 回传时用作 expected_revision

@dataclass
class Rejected:
    reason: RejectReason   # STALE_REVISION | WRONG_COMMAND_FOR_STAGE | MISSING_REQUIRED
    current: NeedInput     # 当前真正在等的东西，driver 可直接重新渲染
```

`Rejected` 让"命令与当前阶段不匹配"成为**显式返回值而不是静默忽略**。
driver 拿到它就重新渲染 `current`，用户不会对着一个已经不存在的问题作答。

对应的输入侧是**判别式命令联合**，不是一袋可混用的可选字段：

```python
@dataclass(frozen=True)
class ConfirmRequirements:                 # 需求卡原样通过
    expected_revision: int

@dataclass(frozen=True)
class AmendRequirements:                   # 补充/修改需求
    expected_revision: int
    text: str

@dataclass(frozen=True)
class ChooseCandidate:                     # 接受某份，定稿
    expected_revision: int
    angle_key: str

@dataclass(frozen=True)
class GiveFeedback:                        # 对某份提意见
    expected_revision: int
    angle_key: str
    text: str

Command = ConfirmRequirements | AmendRequirements | ChooseCandidate | GiveFeedback
```

三个设计点：

- **判别式而非布尔袋。** 早先的 `HumanInput(confirmed, accepted, chosen, text)` 允许
  表达 `confirmed=True, accepted=True` 这种无意义组合，处理端必须靠约定去猜。
  联合类型让非法状态不可表示，`match` 也能穷尽检查。
- **`angle_key` 而非列表下标。** 需求变更后三份候选会各自重跑，顺序可能变，
  下标会指错方案。
- **`expected_revision` 是乐观并发令牌。** v1 CLI 单进程用不上它，但它同时挡住
  重复提交和"用户拿着旧 payload 回答新问题"。一个 int 字段加三行检查，
  现在放进去比日后给 web driver 补要便宜得多（见 §12.1）。

CLI 和未来的 Web 各写一个 driver，共用同一个 `advance`：

```python
# CLI driver —— 阻塞发生在这一层
outcome = advance(state, emit=print_progress)
while True:
    match outcome:
        case Done(itinerary):
            return itinerary
        case Rejected(reason, current):
            warn(reason)                      # 极少见：CLI 单进程基本不会撞上
            outcome = NeedInput(**current)
        case NeedInput():
            save_atomic(state)                # 先落盘再问人
            outcome = advance(state, terminal_ask(outcome), emit=print_progress)

# Web driver —— 同一个 advance，不同 driver
@app.post("/trips/{id}/input")
def submit(id, payload):
    state = load(id)
    outcome = advance(state, parse_command(payload), emit=sse_push)
    save_atomic(state)
    return outcome                            # Rejected 时前端据 current 重渲染
```

这个形状同时换来：可测试性（不用 mock stdin）、日志与 UI 解耦、断点续跑几乎白送。

### 3.3 状态机

```python
class Stage(Enum):
    COLLECT            = auto()   # 抽取需求
    AWAIT_REQ_CONFIRM  = auto()   # ⏸ 等用户确认需求卡
    GENERATE           = auto()   # 三条线并行打磨
    AWAIT_CHOICE       = auto()   # ⏸ 等用户挑选 / 反馈
    REFINE             = auto()   # 单份打磨
    DONE               = auto()
```

暂停点必须是显式状态——这是 `advance` 形状的要求，也是可持久化的前提。

```python
@dataclass
class CandidateSlot:
    angle: Angle
    itinerary: Itinerary | None = None
    facts: FactSnapshot | None = None         # 本候选的判定依据，见 §5.1
    status: SlotStatus = SlotStatus.PENDING   # PENDING | OK | EXHAUSTED | FAILED
    detail: str = ""                          # 残缺/失败原因，直接展示给用户

@dataclass
class TripState:
    run_id: str                               # 每次 start 生成，日志与产物归档用
    raw_request: str
    revision: int = 0                         # 每次 advance 成功推进后 +1
    stage: Stage = Stage.COLLECT
    requirements: Requirements | None = None
    candidates: list[CandidateSlot] = field(default_factory=list)
    chosen_key: str | None = None             # 已选定的 angle.key
    seeds: dict[str, Itinerary] = field(default_factory=dict)   # angle.key -> 上一版
    issues: list[Issue] = field(default_factory=list)
```

`TripState` 必须完全可 JSON 序列化——没有栈上的隐藏状态。

**`CandidateSlot` 而不是裸 `Itinerary` 列表**：三条线并行，任何一条都可能撞上预算
或 deadline（§3.6）。用 slot 包一层，失败的那条能带着原因出现在结果里，而不是让
整个三方案流程一起垮掉，也不是悄悄变成两个方案。

**`chosen_key` 而不是 `itinerary` 字段**：定稿只是"某个 slot 被选中"，多存一份
行程副本必然带来两处不同步的风险。取用一律走 `state.chosen()`。

**写盘必须原子**：写临时文件再 `os.replace()`。单次规划要烧 30-50 万 token，
写到一半崩溃却把 `state.json` 也毁掉，代价太高。

### 3.4 主循环

```python
def advance(state, cmd: Command | None = None, emit=noop) -> Outcome:
    # ① 等待态：命令合法性先判掉，非法一律显式拒绝，绝不静默忽略
    if state.stage in AWAITING:
        if cmd is None:
            return _pending(state)                          # 空调用 = 重新问一遍
        if cmd.expected_revision != state.revision:
            return Rejected(STALE_REVISION, _pending(state))
        if type(cmd) not in ALLOWED_COMMANDS[state.stage]:
            return Rejected(WRONG_COMMAND_FOR_STAGE, _pending(state))
        if isinstance(cmd, ConfirmRequirements) and missing_required(state.requirements):
            return Rejected(MISSING_REQUIRED, _pending(state))   # 必答项没答，不放行
        _apply(state, cmd, emit)                            # 只有这里能改 stage
    elif cmd is not None:
        return Rejected(WRONG_COMMAND_FOR_STAGE, _pending(state))

    # ② 工作态：一路向前，直到再次需要人或结束
    while True:
        match state.stage:
            case Stage.COLLECT:
                state.requirements = collect(state.raw_request, emit)
                return _pause(state, Stage.AWAIT_REQ_CONFIRM)

            case Stage.GENERATE:
                angles = pick_angles(state.requirements, emit)
                state.candidates = parallel(
                    run_slot(a, seed=state.seeds.get(a.key),
                             reqs=state.requirements, emit=emit)
                    for a in angles)
                enforce_diversity(state, emit)                 # §6.1
                return _pause(state, Stage.AWAIT_CHOICE)

            case Stage.REFINE:
                slot = state.chosen()
                state.candidates = [run_slot(slot.angle, seed=slot.itinerary,
                                             reqs=state.requirements,
                                             issues=state.issues, emit=emit)]
                return _pause(state, Stage.AWAIT_CHOICE)

            case Stage.DONE:
                return Done(state.chosen().itinerary)


AWAITING = {Stage.AWAIT_REQ_CONFIRM, Stage.AWAIT_CHOICE}

ALLOWED_COMMANDS = {
    Stage.AWAIT_REQ_CONFIRM: {ConfirmRequirements, AmendRequirements},
    Stage.AWAIT_CHOICE:      {ChooseCandidate, GiveFeedback, AmendRequirements},
}

def _pause(state, stage) -> NeedInput:
    state.stage = stage
    state.revision += 1
    return _pending(state)

def _pending(state) -> NeedInput:
    """当前等待态对应的 NeedInput —— 纯函数，可反复调用。"""
```

修掉的两个缺陷：

1. **`while True` 在等待态无出口。** 原版 `match` 没有 `AWAIT_*` 分支，无输入调用
   时三个 case 全不命中，直接死循环。现在等待态在进入循环**之前**就被拦下并返回，
   循环体只处理工作态——`AWAITING` 与 `match` 的分支集合互不相交，结构上不可能
   再漏。
2. **错误阶段的输入被静默吞掉。** 原版 `_apply_human_input` 用 `match state.stage`
   分派，不匹配就什么也不做，用户以为提交成功了。现在返回 `Rejected` 并附上当前
   真正在等的问题。

`_pending()` 是纯函数：等待态可以被反复查询而不产生副作用，这也让 driver 的
"重新渲染一次当前问题"变成零成本操作。

### 3.5 打磨循环 `run_slot`

GENERATE 与 REFINE 复用同一个函数。每个 slot 有独立的资源上限，**任何一条线超限
都不会拖垮另外两条**：

```python
@dataclass(frozen=True)
class SlotLimits:
    max_rounds:         int = 3
    max_tool_calls:     int = 40        # 本 slot 累计
    max_output_tokens:  int = 120_000   # 本 slot 累计
    max_schema_repairs: int = 2         # 每次 run_agent
    deadline_s:         int = 600


def run_slot(angle, seed, reqs, issues=(),
             limits=SlotLimits(), emit=noop) -> CandidateSlot:
    ctx = SlotContext(limits, emit)      # 记账 + 取消标记，超限抛 LimitExceeded
    itin, facts = seed, None
    try:
        if itin is None:
            itin = generate(reqs, angle, ctx)

        for rnd in range(limits.max_rounds):
            if issues:                                     # 有待办问题就先改
                emit(RevisionStarted(angle, rnd))
                itin = revise(itin, reqs, issues, ctx)

            facts  = resolve(itin, reqs, provider)         # 触网，本轮唯一的 I/O
            issues = run_rule_checks(itin, reqs, facts)    # ① 纯函数，便宜，先跑
            if not has_blocking(issues):
                issues += run_llm_critic(itin, reqs, ctx)  # ② 贵，硬伤清完才请它
            if not has_blocking(issues):
                itin.issues = issues
                return CandidateSlot(angle, itin, facts, SlotStatus.OK)

        itin.issues = issues
        return CandidateSlot(angle, itin, facts, SlotStatus.EXHAUSTED,
                             f"修订 {limits.max_rounds} 轮后仍有硬伤")

    except LimitExceeded as e:           # 预算 / deadline / 修复次数
        return CandidateSlot(angle, itin, facts,
                             SlotStatus.EXHAUSTED if itin else SlotStatus.FAILED,
                             f"资源超限：{e}")
    except ProviderError as e:           # 高德挂了、LLM 持续 5xx
        return CandidateSlot(angle, itin, facts,
                             SlotStatus.FAILED, f"外部依赖失败：{e}")
```

入参 `issues` 统一了两种入口：GENERATE 首轮传空（先生成再校验），REFINE 带着人工
意见或上一轮遗留问题进来（先按意见改再校验）。两条路径共用同一段代码。

`①②` 的顺序是成本控制的关键：让 critic 点评一份时间都对不上的行程没有意义。
硬伤修订阶段完全不烧 critic。

**没有任何一条出路是"卡住"或"抛异常炸穿"**：撞轮数上限 → `EXHAUSTED` + 残缺行程；
撞资源上限 → 同样返回已有的半成品；外部依赖挂了 → `FAILED` + 原因。三种情况都
带着可解释的 `detail` 出现在候选列表里。遗留问题本身就是用户挑选方案的依据，
"方案 C 因为高德限流没跑完"也是用户有权知道的事实。

`parallel()` 必须**捕获而不是传播**单个 slot 的异常，否则一条线炸掉会连坐另外两条。

### 3.6 内层 tool loop

```python
def run_agent(system_prompt, user_prompt, tools, output_schema, role, ctx) -> dict:
    """LLM 想调几次工具就调几次，想什么顺序就什么顺序 —— 在 ctx 的额度之内。
    唯一要求：最后交出符合 output_schema 的结构化结果。"""
    messages = [user(user_prompt)]
    repairs = 0
    while True:
        ctx.check()                                   # 超 deadline / token / 取消 → 抛
        resp = llm.chat(role, system_prompt, messages, tools=tools)
        ctx.charge(resp.usage)

        if resp.stop_reason == "tool_use":
            ctx.charge_tool_calls(len(resp.tool_calls))
            for call in resp.tool_calls:
                messages.append(tool_result(call, TOOLS[call.name](**call.args)))
            continue

        try:
            return parse_and_validate(resp.content, output_schema)
        except SchemaError as e:
            repairs += 1
            if repairs > ctx.limits.max_schema_repairs:
                raise LimitExceeded(f"schema 修复 {repairs} 次仍失败") from e
            messages += [assistant(resp.content), user(repair_prompt(e))]
```

**"LLM 自由"从来不等于"无限量供应"。** 原版这里是个裸 `while True`：没有 tool call
上限、没有 token 记账、没有 deadline、schema 失败"则重试"也没有次数上限——一条
候选线足以吃掉整个 §11 的成本预算，而且是安静地吃。

`ctx` 在 slot 级别记账（不是单次 `run_agent` 级别），因为一条线里 `generate` +
最多 3 次 `revise` + `critic` 是共享同一份额度的。`ctx.check()` 同时检查取消标记，
未来 web driver 要支持"用户中途取消"时不必再动这里。

### 3.7 束缚边界一览

| | LLM 自由 | 骨架强制 |
|---|:---:|:---:|
| 调哪些工具、调几次、什么顺序 | ✅ | |
| 行程怎么排、去哪、住哪、节奏 | ✅ | |
| 需求卡推断什么默认值 | ✅ | |
| 三个候选的切入角度 | ✅ | |
| 校验跑不跑 | | ✅ |
| 修订几轮后停 | | ✅ |
| 什么时候问人 | | ✅ |
| 状态存不存 | | ✅ |

## 4. 数据模型

### 4.1 需求

每个字段都带**来源状态**，而不是用一个 `inferred: set[str]` 旁挂：

```python
class FieldStatus(Enum):
    MISSING   = auto()   # 用户没说，且不敢推断 —— 必须追问
    INFERRED  = auto()   # 模型推断，待用户确认
    CONFIRMED = auto()   # 用户明说，或已确认过

@dataclass(frozen=True)
class Field[T]:
    value: T | None
    status: FieldStatus
    rationale: str = ""              # INFERRED 时说明推断依据，直接显示在需求卡上

@dataclass
class Requirements:
    destination: Field[str]          # ★ 必答
    dates:       Field[DateRange]    # ★ 必答（起止日期，或起始日 + 天数）
    party:       Field[Party]        # ★ 必答（人数 / 成人·儿童·老人构成）
    arrival:     Field[Transfer]     # 抵达日期与时刻、交通方式
    departure:   Field[Transfer]
    budget:      Field[BudgetSpec]
    styles:      Field[list[str]]    # 自由标签："美食"、"历史"、"不爱走路"
    pace:        Field[Pace]         # RELAXED / NORMAL / PACKED
    must_visit:  Field[list[str]]
    avoid:       Field[list[str]]
    lodging_area: Field[str]
    constraints: Field[list[str]]    # 自由文本兜底："第2天下午有个电话会"

REQUIRED = ("destination", "dates", "party")
```

**`REQUIRED` 三项为 MISSING 时不允许进入 GENERATE。** 拦截点在 `advance()` 的
命令校验里：`ConfirmRequirements` 遇到缺失必答项直接 `Rejected(MISSING_REQUIRED)`，
需求卡会把缺的那几项高亮出来继续等。**不能**用"回退到 COLLECT 重新抽取"来处理——
输入没变，抽取结果也不会变，那是个死循环。只有用户通过 `AmendRequirements` 补充了
新信息，才值得重跑 COLLECT。
这三项无中生有地推断出来，后果不是"猜得不准"而是"整个规划建立在虚构约束上"，
而且下游的确定性校验会拿这份虚构去判定 BLOCKING——比不校验更糟。其余字段允许
INFERRED，但必须在需求卡上标出并给 `rationale`，用户一眼知道该盯哪几行。

这正是"抽取 + 推断，一次性确认"能成立的前提：**推断的边界必须是显式的**。
早先版本把所有字段设成必填、只用 `inferred` 旁注，等于要求模型无论如何都填个值出来。

```python
@dataclass(frozen=True)
class BudgetSpec:
    amount:   Decimal
    currency: str                    # CNY / JPY …
    basis:    Basis                  # PER_PERSON | TOTAL
    includes: frozenset[CostKind]    # FLIGHT/LODGING/TICKET/MEAL/LOCAL_TRANSIT
```

`includes` 不可省。"预算一万五"在包不包机票两种解读下是完全不同的行程，
这一项缺失时按 MISSING 处理并追问，而不是默认一个。

### 4.2 行程

```python
@dataclass
class Activity:
    id:        str                  # 稳定 ID，由代码分配（LLM 不产出 ID）
    day_id:    str
    poi_query: str                  # LLM 写的名字，如"清水寺"
    poi_key:   str | None           # resolver 解析后指向 FactSnapshot.pois
    start:     time
    end:       time
    category:  Category             # SIGHT / MEAL / REST / SHOPPING
    cost:      Money | None         # None ≠ 免费，而是"未知"，见 §5.1
    indoor:    bool
    note:      str                  # LLM 写给用户的一句话理由

@dataclass
class Day:
    id:         str
    date:       date
    activities: list[Activity]
    lodging:    str | None

@dataclass
class Itinerary:
    angle:  Angle                   # 切入角度，展示时当方案标题
    days:   list[Day]
    issues: list[Issue]
```

**ID 由代码在解析 LLM 输出后分配，不进 output schema。** 让模型自己维护稳定
ID 是白白增加它出错的机会，而代码分配是确定性的。

**`Leg` 从 `Itinerary` 里移除了。** 交通不是行程的一部分，而是**对行程的观测**，
它属于 `FactSnapshot`（§5.1）：

```python
@dataclass(frozen=True)
class RouteFact:
    day_id:           str
    from_activity_id: str            # 稳定 ID，跨天唯一
    to_activity_id:   str
    depart_at:        datetime       # ★ 出发时刻
    mode:             TravelMode
    duration_min:     int
    distance_m:       int
    polyline:         str            # HTML 画路线用
    source:           str            # "amap:direction/transit"
    fetched_at:       datetime
```

三处是被 review 揪出来的实质缺陷，都必须修：

- **原来的 `from_activity: int` 是列表下标，跨天不唯一。** `Itinerary.legs` 是一个
  扁平列表，而活动分散在各个 `Day.activities` 里，下标根本无法定位到具体是哪天的
  第几个活动。改成 `day_id` + 稳定活动 ID。
- **原来没有 `depart_at`。** 而规则 #2 要算"间隙 ≥ 实测耗时 × 1.2"——地铁班次、
  早晚高峰拥堵都取决于出发时刻，不带时间查出来的耗时是个泛化值，拿它做 BLOCKING
  判定站不住。
- **原来没有 `polyline`，§8.1 却承诺 HTML 里"连出路线"。** 只有距离和时长画不出线。

`fetched_at` 用于缓存有效期与可复现性：事后复盘一份行程时，能知道当初是基于
什么时候的数据做的判断。

```python
@dataclass(frozen=True)
class Money:
    amount:     Decimal
    currency:   str
    confidence: Confidence           # VERIFIED | ESTIMATED
    source:     str                  # "amap:ticket" / "llm:知识"
```

`Money` 带 `confidence` 与 `source`：v1 的票价绝大多数是 `ESTIMATED / llm:知识`，
这一点必须能在数据层面看出来，否则下游没法区分"确认过的 300 元"和"模型猜的 300 元"
（见 §5.1 规则 #6）。`cost=None` 表示**未知**，不表示免费。

**交通由代码测算，不由 LLM 输出。** LLM 只排活动，resolver 用高德算相邻活动的真实
耗时，渲染时代码把"→ 地铁 40 分钟"插进去。理由：交通耗时是纯客观数据，
没有创造性可言，交给 LLM 只会引入编造。

```python
@dataclass
class Issue:
    severity: Severity              # BLOCKING | WARNING | SUGGESTION
    source:   Source                # RULE | CRITIC | HUMAN
    where:    DayRef | ActivityRef | None    # 精确定位，LLM 才知道改哪
    message:  str
```

**`severity` 是防死循环的真正机制**：只有 `BLOCKING` 触发自动修订，
`WARNING` / `SUGGESTION` 原样带给用户。critic 的 prompt 必须明确约束：
只有"行程实际不可执行或严重偏离需求"才给 BLOCKING，否则它会把
"要是加个夜景就更好了"标成 BLOCKING，然后一路循环到撞上限。

## 5. 三层校验

### 5.1 resolver + validator（`validation/`）

早先版本同时声称"`rules.py` 是纯函数、完全可单测"和"规则 #2 调高德算实测耗时"。
这两句不可能同时成立。**校验拆成两步**：

```
Itinerary ──resolver──▶ FactSnapshot ──validator──▶ list[Issue]
           (触网、有缓存)   (不可变事实)    (纯函数、可回放)
```

```python
@dataclass(frozen=True)
class FactSnapshot:
    pois:     dict[str, PoiFact]        # activity.poi_key -> 解析结果
    routes:   dict[RouteKey, RouteFact] # (day_id, from_id, to_id) -> 实测
    weather:  dict[date, WeatherFact]
    resolved_at: datetime
    gaps:     list[Gap]                 # ★ 没查到的事实，显式记录

def resolve(itin: Itinerary, reqs: Requirements, provider) -> FactSnapshot: ...
def run_rule_checks(itin: Itinerary, reqs: Requirements,
                    facts: FactSnapshot) -> list[Issue]: ...        # 纯函数
```

三个收益：

- **validator 真的是纯函数**，测试喂一个手写的 `FactSnapshot` 即可，不触网、
  不 mock HTTP、不依赖高德账号。§10 的"完全确定性"这才名副其实。
- **可回放。** 快照跟着候选走（`CandidateSlot.facts`），事后能精确复现"当时是基于
  这些事实判定 BLOCKING 的"。
- **`gaps` 让"查不到"成为一等公民。** POI 没匹配上、路线查询失败时，不是静默按
  0 处理，而是记一条 gap，由规则降级为 WARNING 并说明原因。

每个 slot 持有自己的 `FactSnapshot`（`CandidateSlot.facts`），保证候选之间的判定
互不串味；共享的是 provider 那层磁盘缓存，属于实现细节，不进状态。

| # | 规则 | 级别 | 依赖 |
|---|---|---|---|
| 1 | 同日活动不重叠、时间递增 | BLOCKING | — |
| 2 | 相邻活动间隙 ≥ 实测耗时 × 1.2 | BLOCKING / WARNING* | `routes` |
| 3 | 日期覆盖完整，首末日扣掉抵离占用 | BLOCKING / WARNING* | `arrival`/`departure` |
| 4 | `must_visit` 全部出现 | BLOCKING | `pois` |
| 5 | `avoid` 未出现 | BLOCKING | `pois` |
| 6 | 花费合计 ≤ budget | **WARNING**（见下） | `budget` |
| 7 | 日均强度符合 pace（活动数 / 步行距离 / 在外时长） | WARNING | `routes` |
| 8 | 每天有午餐、晚餐时段 | WARNING | — |
| 9 | 营业时间冲突 | WARNING | `pois` |

\* **依赖缺失即降级，且必须说明。** 规则 #2 在对应 route 落进 `gaps` 时给 WARNING
（"这一段耗时未能核实"），不按 0 处理也不假装通过。规则 #3 在 `arrival` /
`departure` 为 MISSING 时同样降级——原版声称"首末日扣掉抵离占用"，但模型里
压根没有抵离时间这个字段，**它校验的是不存在的数据**。现在字段有了（§4.1），
缺失时如实说"未提供抵离时间，首末日按整天计"，不谎称已扣除。

#### 规则 #6 为什么从 BLOCKING 降为 WARNING

v1 的票价来自 LLM 知识（`confidence=ESTIMATED`），而且 `cost` 允许为 `None`。
把它设为 BLOCKING 会同时犯两个错：拿模型自己报的数字判定模型自己的方案；
以及在大量 `cost=None` 时"合计"很小、轻松通过，制造虚假的安全感。

改为输出一份**分层账单**，让用户自己判断：

```
已核实   ¥ 1,240   (3 项)
估算     ¥ 6,800   (11 项，来源：模型知识)
未知     — (5 项：晚餐、纪念品、市内交通…)
─────────────────
预算     ¥15,000 / 两人总额，含门票餐饮，不含机票
```

只有当**覆盖完整**（无 `cost=None` 项）**且全部 `VERIFIED`** 时，超支才升为
BLOCKING。这个条件在 v1 基本不会满足，接入真实票价 API 后自然生效——
和规则 #9 是同一个道理，早先版本对 #9 做了诚实降级却漏了 #6，是内部不一致。

### 5.2 LLM critic（`validation/critic.py`）

**必须使用与 planner 不同的模型。** planner 对自己的输出有系统性盲点，同源 critic
容易"英雄所见略同"地放过同一个问题。理想情况换厂商；若只有单一厂商，退而使用不同
tier + 完全独立的 prompt（**不给它看 planner 的推理过程**，只给最终行程），
效果打折但仍优于同源自审。

checklist：

- 节奏是否真的"感觉像"目标 pace（不只是数量达标）
- `styles` 有没有落到实处
- 有没有记忆点，还是流水账
- 同质化：三天是不是三个差不多的寺庙
- 路线绕不绕（通勤达标 ≠ 不绕）
- 季节与天气适配
- 常识遗漏（去迪士尼只排 3 小时）

### 5.3 人工 review 与反馈分类

用户反馈有两种性质，**必须先分类再处理**：

| 类型 | 例子 | 处理 |
|---|---|---|
| A. 行程有问题 | "第2天太赶了"、"不想去这个博物馆" | 需求不变，记为 `Issue(source=HUMAN)` |
| B. 需求变了 | "三天改四天"、"预算加到2万"、"改去日本" | **先 patch 需求卡**，再重新规划 |

不分类会导致一个静默 bug：`run_rule_checks(new_itinerary, old_requirements)`
拿旧需求校验新行程——4 天行程按 3 天预算算会误报超支，日均强度、返程日期全部错判。
确定性校验会从"最可靠的一层"退化成噪声源，而且不报错，只是给出错误的 issue，
LLM 拿着错误的 issue 越改越歪。

命令联合让分派变成穷尽匹配，不再有"落到 else 里被吞掉"的路径：

```python
def _apply(state, cmd: Command, emit) -> None:
    """只有这里能改 stage。调用前 advance() 已校验 revision 与阶段合法性。"""
    match cmd:
        case ConfirmRequirements():
            state.stage = Stage.GENERATE

        case AmendRequirements(text=text):
            if state.stage is Stage.AWAIT_REQ_CONFIRM:
                state.raw_request += f"\n用户补充：{text}"
                state.stage = Stage.COLLECT
            else:                                        # 定稿前改需求
                _patch_requirements(state, text, emit)

        case ChooseCandidate(angle_key=key):
            state.chosen_key = key
            state.stage = Stage.DONE

        case GiveFeedback(angle_key=key, text=text):
            delta = classify_feedback(text, state.requirements)
            if delta.patches_requirements:
                state.chosen_key = key                   # 提意见即选定
                _patch_requirements(state, text, emit)
            else:
                state.chosen_key = key
                state.issues = [Issue.from_human(text)]
                state.stage = Stage.REFINE


def _patch_requirements(state, text, emit) -> None:
    delta = classify_feedback(text, state.requirements)
    state.requirements = apply_patch(state.requirements, delta.patch)
    emit(RequirementsPatched(delta.patch))               # 非阻塞提示，不拦流程
    state.issues = []                                    # 旧 issue 基于旧需求，作废
    if state.chosen_key is None:                         # 尚未选定 → 三份各自重跑
        state.seeds = ({c.angle.key: c.itinerary for c in state.candidates
                        if c.itinerary} if delta.scale is INCREMENTAL else {})
        state.stage = Stage.GENERATE
    else:
        state.stage = Stage.REFINE
```

`state.chosen()` 按 `chosen_key` 从 `candidates` 里取 slot；`chosen_key is None`
就是"尚未选定"，不需要另一个布尔字段来表示同一件事。

设计要点：

- **需求变更不做阻塞式 diff 确认。** 只 `emit` 一行提示（`✓ 需求已更新：天数 3 → 4`），
  理解错了用户下一轮自然会说。少一次回车。
- **`Intent.START_OVER` 不存在。** "改去日本"只是 `destination` 字段的一次 patch，
  差别仅在 `delta.scale` 判为 REWRITE、seed 置空。统一机制，少一条代码路径。
- **`seed` 让修订成为增量。** 三天改四天时用户对前三天可能已满意，从零重规划会洗掉
  他喜欢的安排且白烧 token。seed 传给 LLM 时 prompt 说明"用户认可现有安排，请在此
  基础上扩展"，改动范围由 LLM 判断，但起点不是空白。
- **选定之后不回到多候选。** 用户已表达方向偏好，再甩三个新方案是倒退。

## 6. Agent 与 LLM 配置

按**角色**配置模型，不是全局一个：

| 角色 | 职责 | 默认选型 |
|---|---|---|
| `planner` | 排行程、调工具 | `claude-opus-5` — 最强模型，长输出 + 工具调用 |
| `critic` | 挑主观问题 | **换厂商**；仅有 Anthropic 时用 `claude-sonnet-5` + 独立 prompt |
| `angle` | 想 3 个切入角度 | `claude-sonnet-5` — 短输出 |
| `classifier` | 反馈分类、抽 patch | `claude-haiku-4-5` — 近乎纯抽取 |

```toml
[roles.planner]
model = "claude-opus-5"
max_tokens = 16000

[roles.critic]
model = "claude-sonnet-5"   # 默认 fallback；有其他厂商 key 时改成异厂商模型
independent_context = true  # 只喂最终行程，不喂 planner 的推理过程

[roles.angle]
model = "claude-sonnet-5"

[roles.classifier]
model = "claude-haiku-4-5"
```

`LlmClient.chat(role, ...)` 按角色路由。测试时整个 client 替换为 `FakeLlm`。

**切入角度由 LLM 自己想，不写死"紧凑型/休闲型"枚举。** 好角度是贴着需求走的——
带娃出行的三个角度（低强度亲子 / 博物馆主题 / 近郊自然）和情侣出行完全不同，
写死枚举反而框住它。

### 6.1 候选差异度检查

但"让 LLM 想三个角度"**不保证产出的三份行程真的不同**。它完全可能给出三个听着
不一样的标题，底下却是同一批 POI 换个顺序。多候选是 v1 唯一的增量特性，
三份雷同等于这个特性没做。

角度命名仍然自由，只在生成后加一道确定性检查：

```python
def enforce_diversity(state, emit, threshold=0.6) -> None:
    """任意两份候选的核心 POI 重合度超过阈值时，重跑靠后的那一份（至多一次）。"""
```

判据只用一项：**核心 POI 集合的 Jaccard 重合度**（`category == SIGHT` 的
`poi_key` 集合）。不采用 review 建议的"主题 / 核心 POI / 每日区域 / 节奏
四选二"——后三项要么难以客观量化（主题），要么与第一项高度相关（区域），
引入的判定复杂度换不来相应的收益。POI 重合度单项就能抓住"换汤不换药"这个
真正要防的失败模式。

重跑时把重合的 POI 作为 `avoid` 提示传给 planner。**至多重跑一次**，仍然重合就
如实展示——把"这两份比较像"讲清楚，好过为了差异而硬凑一个更差的方案。

## 7. 数据 provider 层

```python
class GeoProvider(Protocol):
    def search_poi(self, query: str, city: str) -> list[PoiFact]: ...
    def route(self, origin: LatLng, dest: LatLng,
              mode: TravelMode, depart_at: datetime) -> RouteObservation: ...
    def static_map(self, points: list[LatLng], polyline: str | None) -> bytes: ...
```

`route()` 必须接受 `depart_at` 并返回含 `polyline` 的观测——理由见 §4.2。
`RouteObservation` 是 provider 的原始返回，resolver 给它补上 `day_id` 与
活动 ID 后成为 `RouteFact`。

- `AmapProvider` — 高德实现，磁盘缓存（key = 坐标对 + mode + 出发时段），
  失败与限流按 `ProviderError` 抛出，由 `run_slot` 兜住（§3.5）
- `FakeProvider` — 测试用，确定性假数据；`static_map` 返回占位 PNG

**Provider 和 Tool 是两层，不合并。** Provider 是数据访问；Tool 是包给 LLM 的壳
（参数校验 + 结果转 LLM 友好文本）。分开的实际理由：`route()` 同时被 LLM 的工具
和 resolver 调用，共用 Provider 就共用一份缓存——LLM 规划时查过的路线，
resolver 不必再查。合成一层做不到这点。

v1 只接高德。选它的理由：个人开发者 key 几分钟就能申请，免费额度充裕，而确定性
校验里最值钱的两条（时间冲突、通勤超限）都依赖真实的点到点耗时——没有它，
这两条规则形同虚设。其余数据（票价、营业时间、季节信息）v1 靠 LLM 知识填充。

未来接入携程内部 API 时，是新增 `CtripFlightProvider` / `CtripHotelProvider` 实现，
`orchestrator` 和 `rules` 一行不改。

## 8. 模块划分

```
trip-plan/
├── pyproject.toml                 # uv 管理
├── src/tripplan/
│   ├── cli.py                     # CLI driver：交互循环 + 渲染
│   ├── orchestrator.py            # ★ advance() / run_slot() / 阶段推进
│   ├── state.py                   # TripState、Stage、save/load
│   ├── models/
│   │   ├── requirements.py
│   │   ├── itinerary.py
│   │   └── issue.py
│   ├── llm/
│   │   ├── client.py              # 按角色分发的多模型 client
│   │   └── config.py
│   ├── agents/
│   │   ├── runner.py              # run_agent：tool loop + schema 校验重试
│   │   └── prompts/               # collect / angle / plan / critic / classify.md
│   ├── tools/                     # 给 LLM 调的工具 + 注册表
│   ├── providers/
│   │   ├── base.py                # Protocol
│   │   ├── amap.py
│   │   └── fake.py
│   ├── validation/
│   │   ├── resolver.py            # Itinerary → FactSnapshot（唯一触网点）
│   │   ├── rules.py               # 9 条规则，纯函数，只消费 FactSnapshot
│   │   ├── diversity.py           # 候选差异度检查，§6.1
│   │   └── critic.py
│   └── render/
│       ├── requirement_card.py
│       ├── candidates.py
│       ├── itinerary_md.py
│       └── itinerary_html.py      # 单文件自包含 HTML
└── tests/
```

### 8.1 HTML 输出

`trip render <dir> --format html` 产出单文件自包含 HTML：CSS 与 JS 全部内联，
静态地图 base64 内嵌，**不依赖任何外网加载**——发给同行的人，断网也能看。

版式：每天一张卡片，卡片内是活动时间轴，交通段（`FactSnapshot.routes` 里的
`RouteFact`）插在相邻活动之间，遗留 `Issue` 按 severity 标色
（BLOCKING 红 / WARNING 黄 / SUGGESTION 灰），花费按 §5.1 的分层账单呈现，
`ESTIMATED` 与"未知"必须与已核实金额视觉可区分。

每天顶部一张高德静态地图，标出当天所有 POI 并用 `RouteFact.polyline` 连出路线。
地图通过 `GeoProvider.static_map()` 获取，与 POI、路线共用同一层缓存。
`FakeProvider` 返回一张占位 PNG，使 HTML 渲染测试不触网。

渲染器**只读 `FactSnapshot`，不自己触网**——同一份 `state.json` 反复渲染必须
得到同样的结果。`gaps` 里的条目在页面上如实显示为"未能核实"，不留空白。

## 9. CLI 与产物

```
$ trip plan "十一想去京都玩5天，两个人，预算1万5"

  需求卡（推断项标灰加 ?）→ 回车确认或直接改
  ⠋ 三条线并行打磨中…  A✓  B(修订 2/3)  C✓
  三份成品并排 + 各自遗留问题
  选一份 / 对某份提意见 / 改需求
```

产物落在 `./trips/<slug>/`：

| 文件 | 内容 | 谁写 |
|---|---|---|
| `state.json` | **唯一真相来源**：需求、三个 `CandidateSlot`（含各自 `FactSnapshot`）、`revision` | 每个暂停点原子写入 |
| `plan-{A,B,C}.md` | 三份候选的渲染视图 | renderer |
| `itinerary.md` / `.html` | 定稿的渲染视图 | renderer |

结构化数据只存在于 `state.json`；`plan-*.md` 与 HTML 都是它的**投影**，可随时
从状态重新生成，不作为读取来源。（早先版本 §12.2 写"agent 读 `plan-*.json`"，
与产物清单里的 `plan-*.md` 对不上，且会引入第二份结构化真相——已改为一律读
`state.json`。）

## 10. 测试策略

| 层 | 方法 | 确定性 |
|---|---|---|
| `rules.py` | 喂手写 `FactSnapshot`，每条规则一正一负，外加"依赖缺失 → 降级"用例 | 完全 |
| `render/` | 快照测试；HTML 用 `FakeProvider` 的占位图，不触网 | 完全 |
| `diversity.py` | 构造高/低重合度的候选对 | 完全 |
| `resolver.py` | 录制真实高德响应存 fixture，回放 | 完全 |
| `providers/` | 同上 + 限流/超时路径必须覆盖（`ProviderError`） | 完全 |
| `orchestrator.py` | 注入 `FakeLlm` 按脚本返回 | 完全 |
| prompts | 少量真实调用，只断言**格式**不断言**质量**，标 slow，不进 CI 默认 | 部分 |
| 端到端 | 一个 golden case，人工看 | 靠眼 |

`rules.py` 之所以能进"完全确定性"这一栏，靠的是 §5.1 把触网部分切给了 resolver。
早先版本一边声称规则是纯函数、一边让规则 #2 现场调高德，那张表是虚的。

`orchestrator` 必须覆盖的分支（都是这次 review 暴露出来的）：等待态空输入 →
返回 `NeedInput` 而非死循环；`expected_revision` 过期 → `Rejected`；命令与阶段
不匹配 → `Rejected`；单个 slot 抛异常 → 另外两个正常返回；撞轮数/资源上限 →
`EXHAUSTED` 且带 `detail`；需求 patch 后 `seeds` 正确传递。

TDD 落地顺序：`models` → `rules`（纯函数，最直接）→ `resolver` / `providers`
（fixture）→ `orchestrator`（FakeLlm）→ `prompts`（最后，靠迭代）→ `cli` / `render`。

代码格式化：每次改动后用 `black`（虚拟环境内优先，否则 `/opt/homebrew/bin/black`）。

## 11. 成本

三条线各跑一次生成 + 平均 2 轮修订 + 1 次 critic，单次完整规划粗估
**30-50 万输入 token**。这是"三份都打磨到收敛"的代价；wall time 靠并发压到
单份的 1.2 倍左右。

这个估算只有在**有硬上限兜底**时才有意义。`SlotLimits`（§3.5）给每条线设了
tool call 数、输出 token 数和 deadline 三道闸；没有它们，一条跑飞的候选线足以
吃掉整个预算，而且是安静地吃。上限是保证而非估算——撞上就产出残缺候选并
如实说明，不是无限重试。

如果实测成本过高，最有效的旋钮依次是：`SlotLimits.max_rounds` 从 3 降到 2、
候选数从 3 降到 2、critic 换更便宜的模型。

## 12. 未来扩展

三种交付形态共用同一个 `advance(state, input) -> Outcome`——**一套编排，多个 driver**。
这是 §3.2 那个接口形状最大的回报。

```
                    ┌──────────────────────────┐
   CLI driver ────▶ │                          │
                    │  advance(state, input)   │  唯一的编排逻辑
 skill driver ────▶ │  强制校验 / 循环上限 / 存盘 │  唯一的校验保证
                    │                          │  唯一的测试目标
  web driver  ────▶ │                          │
                    └──────────────────────────┘
```

### 12.1 Web 界面

`models` / `rules` / `providers` / `tools` / `agents` / `orchestrator` 原样复用
（约 80% 代码）。新增工作全在 web 层本身：HTTP 框架、后台任务队列（三条线并行要跑
几分钟，不能同步等）、前端、认证、多用户配额。`save/load` 从文件换 DB 就两个函数，
届时再抽 Repository 接口。HTML renderer 可直接充当服务端渲染的起点。

### 12.2 Skill 形态

比预想的便宜得多。做法是给 CLI 加两个非交互子命令，把单步接口暴露出来：

```bash
$ trip start "十一想去京都玩5天" --dir ./trips/kyoto
{"outcome":"need_input","kind":"confirm_requirements","payload":{...}}

$ trip advance ./trips/kyoto --text "预算1万5，不爱走路"
{"outcome":"need_input","kind":"choose_or_feedback","payload":{...}}
```

`SKILL.md` 只需三条指令：调 `trip` 子命令、把返回的 payload 讲成人话、把用户原话
通过 `--text` 转发回去。约 60 行，**没有一行流程逻辑**。

关键约束：**宿主 agent 不参与任何决策**——不做需求抽取、不做规划、不做反馈分类，
那些全在 CLI 内部完成。一旦让 agent 参与决策（哪怕只是"反馈分类交给它，对话上下文
更全"），CLI 模式和 skill 模式就分叉成两套逻辑。放弃那点准确率，换行为一致。

skill 形态的真正价值不在省掉 API key（做 LLM 应用躲不掉），而在：零启动摩擦；
payload 是 JSON，agent 能讲成人话；以及**追问能力**——用户问"方案 A 和 B 差在哪"、
"为什么第二天不去岚山"，agent 直接读 `state.json` 回答（结构化数据的唯一来源，
见 §9），不走流程。这是纯 CLI 给不了的。

被否决的替代做法：让 skill 自己编排（SKILL.md 管流程，Python 只提供
validate / route / render 脚本）。它会把流程保证从"代码强制"降级为"指令遵守"，
且编排逻辑无法单测。可以用"`render.py` 校验产物缺失或 hash 对不上就拒绝渲染"这种
脚本间门禁把"应该"变成"必须"，但既然 CLI 版本已经存在，没有理由退而求其次。

### 12.3 真实 API

新增 Provider 实现即可，见 §7。接入携程内部机票/酒店 API 后，规则 #9（营业时间）
可从 WARNING 升到 BLOCKING。

### 12.4 多城市

需要 `Itinerary` 加 city 维度、城际交通段、住宿切换规则。属于独立的一轮设计。

### 12.5 零碎

`trip resume` 命令、ics 导出，都是小增量。
