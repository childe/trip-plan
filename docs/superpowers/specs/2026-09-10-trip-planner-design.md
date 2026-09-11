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
- `trip resume` 断点续跑

### v1 不包含

| 不做 | 原因 |
|---|---|
| 多城市行程 | 引入城际交通、住宿切换、行李寄存等一整套约束，数据模型要加 city 维度 |
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

一次完整规划粗估 30-50 万 token。崩在第三轮（网断、限流、Ctrl-C、机器睡眠）
若只能从头再来，代价远高于实现成本——给定 `advance()` 的形状，
`trip resume <dir>` 就是 `repo.load()` + 进入与 `trip plan` 完全相同的 driver
循环，十行左右。开发期尤其受用：调 prompt 时不必每次重跑前面几个阶段。

它几乎是白送的，因为三样前置条件早已就位：`TripState` 完全可序列化（§3.3）、
`advance()` 无隐藏状态（§3.2）、每个暂停点原子写盘（§3.8）。

```python
def cmd_resume(trip_dir):
    state = repo.load(trip_dir)
    persisted = state.revision
    outcome = advance(state, emit=print_progress)   # cmd=None：等待态原样重问
    return drive(state, outcome, persisted)         # 与 trip plan 共用
```

**等待态传 `cmd=None` 返回当前 `NeedInput` 而非死循环**（§3.4 修掉的第一个缺陷），
正是这里能复用同一段 driver 的原因：resume 不需要知道自己停在哪个阶段。

一个后果值得记下：`state.json` 从"调试用的副产品"变成**用户可见的契约**。
它的 `format_version` 与迁移策略（§3.3）因此不是可选项——用户会持有跨版本的
旧目录并期望 resume 得动。

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
    reason: RejectReason   # STALE_REVISION | WRONG_COMMAND_FOR_STAGE
                           # | MISSING_REQUIRED | UNKNOWN_CANDIDATE
                           # | UNSELECTABLE_CANDIDATE
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
- **`expected_revision` 挡的是"拿着旧问题作答"，不是并发写。** 这两件事必须分清：
  它能识别"用户看到的还是 rev 5 的候选列表，但状态已经推进到 rev 7"，
  这在 CLI 和 web 下都有效；但**它不构成乐观并发控制**——两个并发请求可以都读到
  rev 5、都通过校验、都写盘，后写的覆盖先写的。原子写只防半个文件，不防
  lost update。真正的 CAS 在仓储层，见 §3.8。

CLI 和未来的 Web 各写一个 driver，共用同一个 `advance`：

两个 driver 都遵守同一条纪律：**调 `advance` 之前记下盘上的 revision，
之后拿它作 `expected` 提交**。`advance` 在暂停时会自增 `revision`，
拿自增后的值去 CAS 必然失败（盘上还是旧值）。

```python
# CLI driver —— 阻塞发生在这一层
state = TripState.new(raw_request)
repo.create(state)                            # 新 trip 的创建语义，独立于 CAS
persisted = state.revision                    # 盘上是什么，初始为 0
outcome = advance(state, emit=print_progress)

while True:
    match outcome:
        case Done(itinerary):
            if not repo.save_if_revision(state, persisted):
                return conflict()      # 不能报成功却没写盘
            return itinerary
        case Rejected(reason, current):
            warn(reason)                      # 如"这份候选没跑出结果，换一个"
            outcome = current                 # 直接复用，不重新构造；不写盘
        case NeedInput():
            assert repo.save_if_revision(state, persisted)   # 先落盘再问人
            persisted = state.revision                       # ★ 提交后才更新
            outcome = advance(state, terminal_ask(outcome), emit=print_progress)

# Web driver —— 同一个 advance，同一条纪律
@app.post("/trips/{id}/input")
def submit(id, payload):
    state = repo.load(id)
    persisted = state.revision                # ★ 调用前记录
    outcome = advance(state, parse_command(payload), emit=sse_push)
    if isinstance(outcome, Rejected):
        return outcome                        # 状态未变，无需写盘
    if not repo.save_if_revision(state, persisted):   # CAS 失败 = 有人抢先
        return conflict(repo.load(id))                # 让前端重新渲染
    return outcome
```

两条由此确立的不变量，都要有测试：

- **`Rejected` ⇒ 状态未被修改。** 所有拒绝分支都在 `_apply` 之前返回，
  因此拒绝路径永远不需要写盘。
- **`persisted` 只在 CAS 成功后前进。** 它跟踪的是"盘上是什么"，不是"内存里是
  什么"；把两者混为一谈正是上一版 CLI 那行必然失败的 CAS 的成因。
- **每一次 `save_if_revision` 的返回值都要看，`Done` 分支尤其。** 定稿是用户唯一
  真正在意的那次写入；CAS 失败却打印"✅ 已定稿"，等于告诉用户一件没发生的事。

`repo.create()` 与 `save_if_revision()` 分开：新 trip 盘上无物，没有可比较的
revision，用 CAS 表达"创建"只能靠约定一个哨兵值，不如给它一个自己的方法——
顺带天然防住 `run_id` 撞车（已存在则报错，不覆盖）。

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
    trip_timezone: str | None = None          # IANA 时区，见 §4.3
    seeds: dict[str, Itinerary] = field(default_factory=dict)   # angle.key -> 上一版
    issues: list[Issue] = field(default_factory=list)
```

`TripState` 必须完全可 JSON 序列化——没有栈上的隐藏状态。但"可序列化"不是自动
成立的：模型里用了 `datetime` / `date` / `time` / `Decimal` / `Enum` / `frozenset`，
以及以元组为键的字典，**这些都不能直接 `json.dumps`**。wire format 现在定死，
不留给 `save/load` 实现时临场发挥：

| 内存类型 | JSON 表示 | 说明 |
|---|---|---|
| `datetime` | `"2026-10-02T09:30:00+09:00"` | ISO 8601，**带时区**（跨境行程必需） |
| `date` / `time` | `"2026-10-02"` / `"09:30"` | ISO 8601 |
| `Decimal` | `"1240.00"`（字符串） | 绝不能走 float，钱不能有二进制舍入 |
| `Enum` | 成员名字符串 `"BLOCKING"` | 不用序号——重排枚举不会悄悄改变旧文件语义 |
| `frozenset` | 排序后的 list | 排序保证同一状态的字节输出稳定，diff 才有意义 |
| `dict[tuple, T]` | list of object | JSON 对象键只能是字符串；`routes` 因此直接定义成 `list[RouteFact]`（§5.1），键信息已在字段里 |
| `Field[T]` | `{"value":…,"origin":"MODEL","confirmed":true,"rationale":…}` | `origin` 与 `confirmed` 都必须落盘：前者是"谁给的值"，后者是"认没认"，重载后缺一个就分不清用户说的和模型猜的 |
| 判别式联合 | 带 `"kind"` 标签：`{"kind":"Ambiguous","candidates":[…]}` | `PoiResolution` 这类联合必须显式打标，靠字段形状去猜会在字段可选时崩 |

```json
{
  "format_version": 1,
  "run_id": "…", "revision": 7, "stage": "AWAIT_CHOICE",
  "requirements": {...}, "candidates": [...], "chosen_key": null
}
```

**`format_version` 从第一天就写。** 迁移策略：读到低版本先跑迁移函数升到当前版本
再解析；读到高版本直接报错退出，不尝试猜测——宁可让用户升级工具，也不要用旧代码
去解析新结构，那会静默丢字段。v1 只有版本 1，迁移表为空，但**入口分支必须存在**，
否则第一次改结构时所有历史 `state.json` 一起变砖。

序列化用显式的 encoder/decoder 函数对，不依赖 `pydantic` 的隐式行为——
`state.json` 是跨版本的持久契约，值得手写一层。

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
    # ⓪ 终态幂等：已定稿的行程反复查询只回同一个答案
    if state.stage is Stage.DONE:
        return Done(state.chosen().itinerary)

    # ① 校验：所有拒绝与空查询都在这里返回 —— 不改状态、不递增 revision
    if state.stage in AWAITING:
        if cmd is None:
            return _pending(state)                          # 空调用 = 重新问一遍
        if (bad := _validate(state, cmd)) is not None:
            return Rejected(bad, _pending(state))
    elif cmd is not None:
        return Rejected(WRONG_COMMAND_FOR_STAGE, _pending(state))

    # ② 过了这道线，状态必然改变
    if cmd is not None:
        _apply(state, cmd, emit)                            # 只有这里能改 stage
    outcome = _run_to_pause(state, emit)
    state.revision += 1                                     # ★ 唯一的递增点
    return outcome


def _validate(state, cmd) -> RejectReason | None:
    if cmd.expected_revision != state.revision:
        return STALE_REVISION
    if type(cmd) not in ALLOWED_COMMANDS[state.stage]:
        return WRONG_COMMAND_FOR_STAGE
    if isinstance(cmd, ConfirmRequirements) and missing_required(state.requirements):
        return MISSING_REQUIRED
    return _check_candidate(state, cmd)


def _run_to_pause(state, emit) -> Outcome:
    """工作态：一路向前，直到再次需要人或结束。不碰 revision。"""
    while True:
        match state.stage:
            case Stage.COLLECT:
                state.requirements = collect(state.raw_request, emit)
                state.stage = Stage.AWAIT_REQ_CONFIRM
                return _pending(state)

            case Stage.GENERATE:
                tz = _ensure_timezone(state)                # §4.3
                angles = pick_angles(state.requirements, emit)
                state.candidates = parallel(
                    run_slot(a, seed=state.seeds.get(a.key),
                             reqs=state.requirements, tz=tz, emit=emit)
                    for a in angles)
                enforce_diversity(state, emit)              # §6.1
                state.stage = Stage.AWAIT_CHOICE
                return _pending(state)

            case Stage.REFINE:
                tz = _ensure_timezone(state)
                slot = state.chosen()
                state.candidates = [run_slot(slot.angle, seed=slot.itinerary,
                                             reqs=state.requirements, tz=tz,
                                             issues=state.issues, emit=emit)]
                state.stage = Stage.AWAIT_CHOICE
                return _pending(state)

            case Stage.DONE:
                return Done(state.chosen().itinerary)


AWAITING = {Stage.AWAIT_REQ_CONFIRM, Stage.AWAIT_CHOICE}

ALLOWED_COMMANDS = {
    Stage.AWAIT_REQ_CONFIRM: {ConfirmRequirements, AmendRequirements},
    Stage.AWAIT_CHOICE:      {ChooseCandidate, GiveFeedback, AmendRequirements},
}

def _ensure_timezone(state) -> str:
    """幂等：destination 变更时由 _patch_requirements 置空，这里按需重解析。"""
    if state.trip_timezone is None:
        state.trip_timezone = resolve_timezone(state.requirements)
    return state.trip_timezone

def _check_candidate(state, cmd) -> RejectReason | None:
    """ChooseCandidate / GiveFeedback 携带的 angle_key 必须真实且可用。"""
    if not isinstance(cmd, (ChooseCandidate, GiveFeedback)):
        return None
    slot = state.slot(cmd.angle_key)
    if slot is None:
        return UNKNOWN_CANDIDATE
    if slot.itinerary is None:            # FAILED 且没跑出任何东西
        return UNSELECTABLE_CANDIDATE
    return None

def _pending(state) -> NeedInput:
    """当前等待态对应的 NeedInput —— 纯函数，可反复调用。"""
```

**`EXHAUSTED` 的候选是可选的，`FAILED` 且无行程的不可选。** 带着遗留硬伤定稿是
用户的权利（问题都摆在他面前了）；但选一个根本没跑出东西的 slot 只会让
`Done(state.chosen().itinerary)` 拿到 `None`——早先版本 `chosen_key` 完全不校验，
任意字符串都能写进去，崩溃点被推迟到了 `Done` 那一行。

修掉的三个缺陷：

1. **`while True` 在等待态无出口。** 原版 `match` 没有 `AWAIT_*` 分支，无输入调用
   时三个 case 全不命中，直接死循环。现在等待态在进入循环**之前**就被拦下并返回，
   循环体只处理工作态——`AWAITING` 与 `match` 的分支集合互不相交，结构上不可能
   再漏。
2. **错误阶段的输入被静默吞掉。** 原版 `_apply_human_input` 用 `match state.stage`
   分派，不匹配就什么也不做，用户以为提交成功了。现在返回 `Rejected` 并附上当前
   真正在等的问题。
3. **候选 key 不校验。** 任意字符串都能写进 `chosen_key`，故障延迟到 `Done` 或
   `REFINE` 取 slot 时才爆。现在在入口挡掉。
4. **`revision` 在直接定稿路径上不递增。** 递增点原先藏在 `_pause()` 里，而
   `ChooseCandidate → DONE` 不经过 `_pause`——两个并发请求分别选 A 和 B，都以
   `expected=3` 提交，后者仍能覆盖前者。**依赖"所有路径碰巧都会走到 `_pause`"
   是靠不住的**，现在递增收敛到 `advance` 里唯一一处，位于校验之后、返回之前，
   结构上覆盖包括 DONE 在内的每一条修改路径。
5. **终态不幂等。** `DONE` 不在 `AWAITING` 里，于是无命令调用会一路走到
   `_run_to_pause` 拿到 `Done`，再照样 `revision += 1`——重复 `trip resume` 一个
   已定稿的行程会不断改版本号，还会让别人手里的 `expected_revision` 平白失效。
   带命令重放更糟：落到 `Rejected(..., _pending(state))`，而 `_pending` 对 DONE
   只能硬凑一个 `CHOOSE_OR_FEEDBACK`，**伪造一个根本不存在的待答问题**。
   ⓪ 号分支因此必须在最前面。

**它只拦「进来时就已经是 DONE」。** `ChooseCandidate` 把 stage 推到 DONE 的那一次
仍然走正常路径并递增 `revision`——否则第 4 条又会回来。

由此得到一条可测的不变量：**`advance` 要么拒绝（状态与 `revision` 均不变），
要么修改（`revision` 恰好 +1），要么在终态幂等返回（同样不变）**。
测试直接断言这个三分，而不是逐条路径去数。

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


def run_slot(angle, seed, reqs, tz, issues=(),
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

            facts  = resolve(itin, reqs, provider, tz)     # 触网，本轮唯一的 I/O
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

### 3.8 仓储层

`save/load` 不能只是两个自由函数——一旦有第二个写者（另一个 CLI 进程开着同一个
目录，或将来的 web），"读-改-写"就会丢更新。协议现在定下来：

```python
class StateRepo(Protocol):
    def create(self, state: TripState) -> None:
        """新建。trip_id 已存在则抛错，不覆盖。"""
    def load(self, trip_id: str) -> TripState: ...
    def save_if_revision(self, state: TripState, expected: int) -> bool:
        """仅当持久化的 revision 仍等于 expected 时写入并返回 True；
        否则不写、返回 False，调用方需重新 load 并告知用户。"""
```

`FileRepo`（v1）：`fcntl.flock` 锁住 trip 目录 → 读盘校验 revision → 写临时文件
→ `os.replace` → 解锁。锁把"检查"和"写入"合成一个临界区，本地文件系统上这就是
一次真正的 compare-and-swap。`DbRepo`（未来）用 `UPDATE … WHERE revision = ?`
的影响行数判断，语义完全一致。

这也修正了 §12.1 早先的说法："`save/load` 从文件换 DB 就两个函数"低估了——
换的是一个带 CAS 语义的接口，不是两个裸函数。接口现在就定好，实现可以先只有
文件版。

## 4. 数据模型

### 4.1 需求

每个字段都带**来源**与**确认状态**，而不是用一个 `inferred: set[str]` 旁挂：

```python
class Origin(Enum):
    USER  = auto()       # 用户明说的
    MODEL = auto()       # 模型推断的

@dataclass(frozen=True)
class Field[T]:
    value:     T | None              # None ⇒ 尚无取值（原先的 MISSING）
    origin:    Origin | None         # value is None 时为 None
    confirmed: bool = False          # 用户是否已签字
    rationale: str = ""              # origin=MODEL 时说明推断依据，显示在需求卡上

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

def missing_required(reqs) -> list[str]:
    return [n for n in REQUIRED if getattr(reqs, n).value is None]
```

**为什么拆成两个字段。** 早先版本用单个 `FieldStatus` 同时表达"谁给的值"和
"用户认没认"，于是 `ConfirmRequirements` 陷入两难：不改 status 就与
`CONFIRMED` 的语义冲突，改成 `CONFIRMED` 又抹掉了"这个值本来是模型猜的"这个
事实——而这正是需求卡标灰、以及后续排查"行程为什么偏了"时最需要的信息。
两者本来就正交，拆开即可：

| 场景 | `value` | `origin` | `confirmed` |
|---|---|---|---|
| 用户明说"10 月 2 日出发" | 有 | `USER` | 确认后 `True` |
| 模型推断"预算按人均 3000" | 有 | `MODEL` | 确认前 `False`，确认后 `True` |
| 目的地没说 | `None` | `None` | `False` |

`ConfirmRequirements` 把**有取值的**字段的 `confirmed` 置 `True`，**不动 `origin`**。
`value is None` 的字段保持 `confirmed=False`——空值的语义是 `origin=None,
confirmed=False`，把一个"用户压根没提"的可选字段标成已确认是自相矛盾的，
下游也就再分不清"确认过不需要"和"从没问过"。
需求卡标灰的判据是 `origin is MODEL and not confirmed`；定稿后回看行程时，
`origin is MODEL` 依然告诉你哪些前提是猜的。

**`REQUIRED` 三项 `value is None` 时不允许进入 GENERATE。** 拦截点在 `advance()` 的
命令校验里：`ConfirmRequirements` 遇到缺失必答项直接 `Rejected(MISSING_REQUIRED)`，
需求卡会把缺的那几项高亮出来继续等。**不能**用"回退到 COLLECT 重新抽取"来处理——
输入没变，抽取结果也不会变，那是个死循环。只有用户通过 `AmendRequirements` 补充了
新信息，才值得重跑 COLLECT。
这三项无中生有地推断出来，后果不是"猜得不准"而是"整个规划建立在虚构约束上"，
而且下游的确定性校验会拿这份虚构去判定 BLOCKING——比不校验更糟。其余字段允许
`origin=MODEL`，但必须在需求卡上标出并给 `rationale`，用户一眼知道该盯哪几行。

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
这一项无法确定时整个 `budget.value` 置 `None` 并追问，而不是默认一个。

### 4.2 行程

```python
@dataclass
class Activity:
    id:        str                  # 稳定 ID，由代码分配（LLM 不产出 ID）
    day_id:    str
    poi_query: str                  # LLM 写的名字，如"清水寺"；解析结果不存这里
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

### 4.3 时区

`Day.date` 与 `Activity.start/end` 是**无时区的本地时间**——这是刻意的，
LLM 排"上午九点到清水寺"时不该、也没法关心 UTC 偏移。但无时区的时间无法直接
参与计算：`RouteFact.depart_at` 需要一个具体时刻去查地铁班次，跨境行程尤其如此。

v1 单目的地，因此定一个行程级时区：

```python
# TripState
trip_timezone: str | None = None      # IANA，如 "Asia/Tokyo"
```

- **来源与唯一性**：由已解析的 `destination` 反查得出（`resolve_timezone`），
  存在 `TripState.trip_timezone`，**只在这里解析一次**。`run_slot` 与 `resolve`
  都以参数 `tz` 接收它，`FactSnapshot.trip_timezone` 只是把当时用的值抄一份下来
  供回放核对——不是第二个解析点。让 resolver 自己去查会得到两条独立的解析路径，
  它们迟早会不一致，而且不一致时没有任何地方会报错。
- **失效**：`destination` 被 patch 时置 `None`，下一次进入工作态由
  `_ensure_timezone` 重解析。
- **语义**：`Day.date + Activity.start` 一律按 `trip_timezone` 解释为具体时刻。
  `depart_at = localize(day.date, activity.end, trip_timezone)`。
- **抵离**：`Transfer` 存**带 offset 的 datetime**——航班时刻本来就是跨时区的，
  出发地和目的地不在同一个时区里，用本地时间表达会错。
- **DST**：用 IANA 时区名而不是固定 offset，夏令时切换自动正确。存 `+09:00`
  这类固定偏移在跨越切换日的行程上会算错一小时。

多城市（§12.4）时这个字段要变成每日或每段一个，属于那一轮设计的一部分。

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
    poi_by_activity:   dict[str, PoiResolution] # activity.id -> 解析结果
    constraint_pois:   dict[str, PoiResolution] # ★ must_visit / avoid 的原文 -> 解析结果
    routes:            list[RouteFact]          # 自带 day_id + 两端活动 ID
    weather:           dict[str, WeatherFact]   # ISO 日期串 -> 天气
    trip_timezone:     str                      # IANA，见 §4.3
    resolved_at:       datetime
    gaps:              list[Gap]                # ★ 没查到的事实，显式记录

PoiResolution = Resolved | Ambiguous | NotFound

@dataclass(frozen=True)
class Resolved:
    fact: PoiFact                               # id / name / coords / 营业时间 / 票价

@dataclass(frozen=True)
class Ambiguous:
    candidates: list[PoiFact]                   # 同名多个，不擅自挑

@dataclass(frozen=True)
class NotFound:
    query: str

def resolve(itin: Itinerary, reqs: Requirements,
            provider, tz: str) -> FactSnapshot: ...
def run_rule_checks(itin: Itinerary, reqs: Requirements,
                    facts: FactSnapshot) -> list[Issue]: ...        # 纯函数
```

**索引键是 `activity.id`，不是解析结果本身。** 早先版本让 `Activity.poi_key` 指向
`FactSnapshot.pois`，而 `pois` 又以 `poi_key` 为键，同时 `resolve()` 只返回快照、
并不回写 itinerary——这个数据流是闭不上的。现在 `Activity` 只保留 LLM 写下的
`poi_query`，解析结果整个活在快照里，按活动 ID 索引。`Itinerary` 保持不可变，
resolver 无副作用。

**同名 POI 不擅自挑一个。** "清水寺"在高德可能匹配到多条，随便取第一条会让
`must_visit`、`avoid` 和整条路线的校验建立在错的坐标上，而且错得无声无息。
`Ambiguous` 与 `NotFound` 都记入 `gaps`：

| Gap | 触发 | 下游影响 |
|---|---|---|
| `AMBIGUOUS_POI` | 匹配到多个同名候选 | 依赖该 POI 的规则降级为 WARNING，列出候选让用户定夺 |
| `POI_NOT_FOUND` | 一个都没匹配上 | 同上；规则 #4/#5 明确说"无法核实" |
| `AMBIGUOUS_CONSTRAINT` | `must_visit`/`avoid` 的原文解析不唯一 | 规则 #4/#5 降级为 WARNING |
| `ROUTE_UNAVAILABLE` | 路线查询失败/限流 | 规则 #2 降级，见 §5.1 |

**约束侧也必须解析，否则"按 POI id 匹配"根本无从谈起。** `must_visit` / `avoid`
在 `Requirements` 里是用户写下的字符串，快照只解析活动是不够的——比较的两端得都
是 id。`constraint_pois` 以约束原文为键存放解析结果，规则 #4/#5 于是变成：

```
must_visit 中每一条 c：
    constraint_pois[c] 是 Resolved(fact)  且  fact.id ∈ {已解析活动的 poi id}
        → 通过
    constraint_pois[c] 是 Ambiguous / NotFound
        → WARNING「无法核实"c"是否已安排」+ 列出候选，不给 BLOCKING
```

约束本身的歧义与活动侧同等对待：解析不出来就如实降级，不拿一个猜的 id 去判
BLOCKING。这一点尤其要紧——规则 #4/#5 是 BLOCKING 级，用错 id 会驱动 planner
反复修改一个本来正确的行程。

`avoid` 同理，方向相反：解析成功且命中才报 BLOCKING。字符串比对一律不用——
"清水寺"和"清水寺（京都）"是同一个地方，字符串比不出来。

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
`departure` 无取值（`value is None`）时同样降级——原版声称"首末日扣掉抵离占用"，但模型里
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

### 5.2 LLM critic（实现在 `agents/steps.run_llm_critic`）

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
    """只有这里能改 stage。调用前 advance() 已校验 revision、阶段与候选 key。"""
    match cmd:
        case ConfirmRequirements():
            state.requirements = mark_all_confirmed(state.requirements)  # origin 不变
            state.stage = Stage.GENERATE

        case AmendRequirements(text=text):
            if state.stage is Stage.AWAIT_REQ_CONFIRM:
                state.raw_request += f"\n用户补充：{text}"
                state.stage = Stage.COLLECT
            else:                                        # 定稿前改需求
                _patch_requirements(state, classify_feedback(text, state.requirements), emit)

        case ChooseCandidate(angle_key=key):
            state.chosen_key = key
            state.stage = Stage.DONE

        case GiveFeedback(angle_key=key, text=text):
            state.chosen_key = key                       # 提意见即选定
            delta = classify_feedback(text, state.requirements)   # ★ 只分类一次
            if delta.patches_requirements:
                _patch_requirements(state, delta, emit)
            else:
                state.issues = [Issue.from_human(text)]
                state.stage = Stage.REFINE


def _patch_requirements(state, delta, emit) -> None:
    """delta 由调用方传入 —— 不在这里重新分类。"""
    state.requirements = apply_patch(state.requirements, delta.patch)
    emit(RequirementsPatched(delta.patch))               # 非阻塞提示，不拦流程
    state.issues = []                                    # 旧 issue 基于旧需求，作废

    if "destination" in delta.patch:
        state.trip_timezone = None                       # ★ 置空 → 下轮重解析

    if delta.scale is REWRITE:                           # ★ 旧行程整体作废
        state.seeds = {}
        for slot in state.candidates:
            slot.itinerary = None                        # 逼 run_slot 重新 generate
            slot.facts = None
            slot.status = SlotStatus.PENDING
    elif state.chosen_key is None:                       # 增量 + 尚未选定
        state.seeds = {c.angle.key: c.itinerary
                       for c in state.candidates if c.itinerary}

    state.stage = Stage.GENERATE if state.chosen_key is None else Stage.REFINE
```

**REWRITE 必须清干净选中方案，而不只是清 `seeds`。** 早先版本只在"尚未选定"
分支里处理 seed，已选定时直接进 REFINE——于是"京都改巴黎"会拿着京都的行程当
seed 去 refine，而 `trip_timezone` 只在 GENERATE 重算，会一路停在 `Asia/Tokyo`。
两处都与文档声称的"REWRITE seed 置空"相矛盾。

**清的是行程，不是角度。** 目的地全换了，但用户选中的切入角度（"博物馆主题"）
通常仍然成立，而且他已经表达过这个偏好——所以保留 `slot.angle`，重新 generate。
这也和"选定之后不回到多候选"一致：不因为换了目的地就重新甩三个方案给他。

`trip_timezone` 用**置空 + 按需重解析**而不是就地重算：patch 发生在 `_apply` 阶段，
此时不该触网；`_ensure_timezone` 在工作态需要它时才解析，两处职责不混。

**`delta` 必须由调用方传入。** 早先版本 `GiveFeedback` 先 `classify_feedback` 一次，
转手调 `_patch_requirements` 又分类一次：多烧一次 LLM 调用是小事，
**两次结果可能不一致**才是真问题——第一次判定"要改需求"进了分支，第二次却给出
一个空 patch 或不同的 scale，状态就此走歪，而且没有任何地方会报错。分类是不确定
操作，同一次决策里只允许发生一次。

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
已解析 POI id 集合，取自 `FactSnapshot.poi_by_activity`；未解析的活动不参与比较）。
不采用 review 建议的"主题 / 核心 POI / 每日区域 / 节奏
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
│   ├── state.py                   # TripState、Stage、Command/Outcome
│   ├── repo.py                    # StateRepo Protocol + FileRepo（CAS，§3.8）
│   ├── wire.py                    # JSON encoder/decoder + format_version 迁移
│   ├── maps.py                    # 静态地图字节（渲染链路唯一触网点，不进 FactSnapshot）
│   ├── models/
│   │   ├── common.py              # Field / Money / Origin / Confidence / LatLng
│   │   ├── facts.py               # FactSnapshot / PoiFact / RouteFact / Gap
│   │   ├── requirements.py
│   │   ├── itinerary.py
│   │   └── issue.py
│   ├── llm/
│   │   ├── client.py              # 按角色分发的多模型 client
│   │   └── config.py
│   ├── agents/
│   │   ├── runner.py              # run_agent：tool loop + schema 校验重试
│   │   ├── schemas.py             # 各角色输出 schema（type 由 runner 递归执行）
│   │   ├── steps.py               # collect / pick_angles / generate / critic / classify
│   │   ├── tools.py               # 给 LLM 调的工具 + 注册表
│   │   └── prompts/               # collect / angle / plan / critic / classify.md
│   ├── providers/
│   │   ├── base.py                # Protocol
│   │   ├── amap.py
│   │   ├── cache.py
│   │   └── fake.py
│   ├── validation/
│   │   ├── resolver.py            # Itinerary → FactSnapshot（校验链路唯一触网点）
│   │   ├── rules.py               # 9 条规则，纯函数，只消费 FactSnapshot
│   │   ├── budget.py              # 分层账单，§5.1
│   │   └── diversity.py           # 候选差异度检查，§6.1
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

$ trip resume ./trips/kyoto        # 中断后接着上次的进度
  ✓ rev 4，阶段 AWAIT_CHOICE
  三份成品并排 + 各自遗留问题       # 与 plan 走同一段 driver

$ trip render ./trips/kyoto --format html
```

三个子命令的分工：`plan` 建新 trip（`repo.create`），`resume` 载入既有 trip，
两者之后共用同一个 driver 循环；`render` 只读 `state.json` 重新生成投影，
不推进状态、不写回 `state.json`；有 `AMAP_KEY` 时会为当天地图取一次图
（`maps.py`，见 §8.1），没有就整份跳过地图——**绝不拿占位图顶替**。

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
| `wire.py` | 往返测试：每种类型 encode→decode 等值；跨版本迁移用例；**低版本 `state.json` 能被 resume 读起来** | 完全 |
| `repo.py` | CAS 语义：`save_if_revision` 在 revision 变化后返回 False；并发两写只有一个成功；`create` 撞 id 报错不覆盖 | 完全 |
| `orchestrator.py` | 注入 `FakeLlm` 按脚本返回 | 完全 |
| prompts | 少量真实调用，只断言**格式**不断言**质量**，标 slow，不进 CI 默认 | 部分 |
| 端到端 | 一个 golden case，人工看 | 靠眼 |

`rules.py` 之所以能进"完全确定性"这一栏，靠的是 §5.1 把触网部分切给了 resolver。
早先版本一边声称规则是纯函数、一边让规则 #2 现场调高德，那张表是虚的。

`orchestrator` 必须覆盖的分支（都是两轮 review 暴露出来的）：等待态空输入 →
返回 `NeedInput` 而非死循环；`expected_revision` 过期 → `Rejected`；命令与阶段
不匹配 → `Rejected`；未知 / 不可选候选 key → `Rejected`；必答项缺失时
`ConfirmRequirements` → `Rejected` 且不回退 COLLECT；单个 slot 抛异常 → 另外两个
正常返回；撞轮数 / 资源上限 → `EXHAUSTED` 且带 `detail`；需求 patch 后 `seeds`
正确传递；`GiveFeedback` 全程只调一次 `classify_feedback`（用计数 fake 断言）；
**任何 `Rejected` 路径都不修改 state**（比对调用前后的序列化结果）；
driver 的 `persisted` 只在 CAS 成功后前进（连续多轮不出现 CAS 失败）。

两条覆盖全部路径的不变量，用参数化测试跑遍每个 `(stage, command)` 组合，
而不是逐条路径手写：

- **`advance` 的二分律**：返回 `Rejected` ⇒ `revision` 与序列化状态均不变；
  否则 `revision` 恰好 +1。**`ChooseCandidate → DONE` 必须包含在内**——
  它是唯一不经过工作态的修改路径，也正是上一版漏掉递增的地方。
- **REWRITE 清理彻底**：`delta.scale is REWRITE` 后，所有 slot 的
  `itinerary` / `facts` 为 `None`、`seeds` 为空；若 patch 含 `destination`，
  `trip_timezone` 为 `None`。断言"目的地换成另一个时区的城市后，
  新行程的 `FactSnapshot.trip_timezone` 已随之改变"。

另有一条跨模块的用例值得单列：**中断后 resume 得到等价状态**——在任意暂停点
序列化、丢弃内存对象、重新 `load`，后续行为与不中断时一致。这条同时守住
`wire` 的完整性和 `advance` 的无隐藏状态，比分别测两者更有力。

TDD 落地顺序：`models` / `wire`（往返测试先立住持久化契约）→ `rules`（纯函数，
最直接）→ `repo`（CAS）→ `resolver` / `providers`（fixture）→ `orchestrator`
（FakeLlm）→ `prompts`（最后，靠迭代）→ `cli` / `render`。

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
几分钟，不能同步等）、前端、认证、多用户配额。存储层换 DB 就是给 `StateRepo`
（§3.8）加一个实现——CAS 语义已经在接口里，`UPDATE … WHERE revision = ?` 直接对上。
HTML renderer 可直接充当服务端渲染的起点。

需要注意的是**并发在这里才真正出现**：v1 CLI 是单写者，`expected_revision` 只用来
识别"拿旧问题作答"；多用户 web 下必须依赖 §3.8 的 CAS 才能防住 lost update。

但 CAS 只保证**最终状态不丢**，不保证**工作不白做**：两个并发请求可以各自跑完
一整轮 LLM 调用（几分钟、几十万 token、各自往前端推进度），最后才由 CAS 判掉一个。
CLI 下无所谓，web 下这是真金白银。届时要升级成"抢运行权"模型：

```
CAS 抢占（stage → RUNNING，写入 run_id）→ worker 执行 → CAS 提交结果
```

抢不到的请求立刻收到"该行程正在生成中"，而不是先烧完再被拒。`run_id` 字段
（§3.3）已经在状态里，就是为这个留的；`RUNNING` 阶段和超时回收留给 web 那一轮设计。

### 12.2 Skill 形态

比预想的便宜得多。做法是给 CLI 加两个非交互子命令，把单步接口暴露出来：

子命令必须完整表达 §3.2 的命令联合——命令类型、`expected_revision`、候选 key
一个都不能少，否则 agent 就在猜状态机的意图：

```bash
$ trip start "十一想去京都玩5天" --dir ./trips/kyoto
{"outcome":"need_input","kind":"confirm_requirements","revision":1,"payload":{...}}

$ trip advance ./trips/kyoto --amend "预算1万5，不爱走路" --revision 1
{"outcome":"need_input","kind":"confirm_requirements","revision":2,"payload":{...}}

$ trip advance ./trips/kyoto --confirm --revision 2
{"outcome":"need_input","kind":"choose_or_feedback","revision":3,"payload":{...}}

$ trip advance ./trips/kyoto --feedback B --text "第2天太赶了" --revision 3
$ trip advance ./trips/kyoto --choose B --revision 4
```

`--revision` 从上一次返回的 `revision` 字段原样带回；对不上就拿到
`{"outcome":"rejected","reason":"STALE_REVISION","current":{...}}`，agent 据
`current` 重新问一遍。**这道校验存在的意义正是防止 agent 拿着几轮之前的候选列表
作答**——它比人更容易犯这个错，因为它的"上一屏"可能已经被压缩掉了。

`SKILL.md` 只需三条指令：调 `trip` 子命令、把返回的 payload 讲成人话、把用户原话
按命令类型转发回去。约 60 行，**没有一行流程逻辑**。

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

ics 导出、行程分享链接，都是小增量。
