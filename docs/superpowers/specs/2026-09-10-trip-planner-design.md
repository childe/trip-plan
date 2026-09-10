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
| `trip resume` 命令 | 状态本来就存盘，加命令成本极低，但 v1 不需要 |
| ics 导出 | 纯渲染层，`Itinerary` 已结构化，随时可加 |
| Skill 形态 | 见 §12.2，架构已为此留路，且比预想的便宜 |
| Web 界面 | 见 §12.1，架构已为此留路 |
| 机票 / 酒店真实预订 | 需要携程内部 API，见 §12.3 |

### 已知局限（诚实记录，不假装）

营业时间没有权威数据源。v1 靠 LLM 填充 + 高德 POI 兜底，因此只能给 WARNING 级别
并在行程单上标注"未核实"。接入真实 API 后才能升为 BLOCKING。

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
            human_input: HumanInput | None = None,
            emit: Callable[[Event], None] = noop) -> Outcome:
    """推进到下一个需要人参与的点，或终点。
    调用方负责：持久化 state、获取人类输入、再次调用。"""

Outcome = Done | NeedInput

@dataclass
class Done:
    itinerary: Itinerary

@dataclass
class NeedInput:
    kind: InputKind        # CONFIRM_REQUIREMENTS | CHOOSE_OR_FEEDBACK
    payload: object        # 渲染所需数据（需求卡 / 候选列表）
```

对应的输入侧：

```python
@dataclass
class HumanInput:
    confirmed: bool = False        # 需求卡：原样确认
    accepted:  bool = False        # 候选：接受定稿
    chosen:    str | None = None   # 目标方案的 angle.key；单份时可省
    text:      str = ""            # 自由文本：补充需求 / 修改意见
```

`chosen` 用 `angle.key` 而非列表下标——重跑后候选顺序可能变，下标会指错方案。
用户在候选并排展示时必须先指明改哪一份，CLI 层负责保证这一点。

CLI 和未来的 Web 各写一个 driver，共用同一个 `advance`：

```python
# CLI driver —— 阻塞发生在这一层
outcome = advance(state, emit=print_progress)
while isinstance(outcome, NeedInput):
    save(state)
    outcome = advance(state, terminal_ask(outcome), emit=print_progress)

# Web driver —— 同一个 advance，不同 driver
@app.post("/trips/{id}/input")
def submit(id, payload):
    state = load(id)
    outcome = advance(state, payload, emit=sse_push)
    save(state)
    return outcome
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
class TripState:
    raw_request: str
    stage: Stage = Stage.COLLECT
    requirements: Requirements | None = None
    candidates: list[Itinerary] = field(default_factory=list)
    itinerary: Itinerary | None = None            # 用户选定的那份
    seeds: dict[str, Itinerary] = field(default_factory=dict)   # angle -> 上一版
    issues: list[Issue] = field(default_factory=list)
```

`TripState` 必须完全可 JSON 序列化——没有栈上的隐藏状态。

### 3.4 主循环

```python
def advance(state, human_input=None, emit=noop) -> Outcome:
    if human_input is not None:
        _apply_human_input(state, human_input, emit)

    while True:
        match state.stage:
            case Stage.COLLECT:
                state.requirements = collect(state.raw_request, emit)
                state.stage = Stage.AWAIT_REQ_CONFIRM
                return NeedInput(CONFIRM_REQUIREMENTS, state.requirements)

            case Stage.GENERATE:
                angles = pick_angles(state.requirements, emit)
                state.candidates = parallel(
                    polish(seed=state.seeds.get(a.key), angle=a,
                           reqs=state.requirements, emit=emit)
                    for a in angles)
                state.stage = Stage.AWAIT_CHOICE
                return NeedInput(CHOOSE_OR_FEEDBACK, state.candidates)

            case Stage.REFINE:
                state.itinerary = polish(seed=state.itinerary,
                                         angle=state.itinerary.angle,
                                         reqs=state.requirements,
                                         issues=state.issues, emit=emit)
                state.stage = Stage.AWAIT_CHOICE
                return NeedInput(CHOOSE_OR_FEEDBACK, [state.itinerary])

            case Stage.DONE:
                return Done(state.itinerary)
```

### 3.5 打磨循环 `polish`

GENERATE 与 REFINE 复用同一个函数：

```python
MAX_ROUNDS = 3

def polish(seed, angle, reqs, issues=(), emit=noop) -> Itinerary:
    itin = seed or generate(reqs, angle)

    for rnd in range(MAX_ROUNDS):
        if issues:                                        # 有待办问题就先改
            emit(RevisionStarted(angle, rnd))
            itin = revise(itin, reqs, issues)

        issues = run_rule_checks(itin, reqs)              # ① 便宜，先跑
        if not has_blocking(issues):
            issues += run_llm_critic(itin, reqs)          # ② 贵，硬伤清完才请它
        if not has_blocking(issues):
            break

    itin.issues = issues     # 未解决的跟着走，展示时标注
    return itin


def generate(reqs, angle) -> Itinerary:
    return run_agent(PLAN_PROMPT, render(reqs, angle=angle), role=PLANNER,
                     tools=PLANNING_TOOLS, output_schema=ItinerarySchema)

def revise(itin, reqs, issues) -> Itinerary:
    return run_agent(PLAN_PROMPT, render(reqs, issues=issues, seed=itin), role=PLANNER,
                     tools=PLANNING_TOOLS, output_schema=ItinerarySchema)
```

入参 `issues` 统一了两种入口：GENERATE 首轮传空（先生成再校验），REFINE 带着人工
意见或上一轮遗留问题进来（先按意见改再校验）。两条路径共用同一段代码。

`①②` 的顺序是成本控制的关键：让 critic 点评一份时间都对不上的行程没有意义。
硬伤修订阶段完全不烧 critic。

撞到 `MAX_ROUNDS` 上限时不报错，带着未解决的 issue 交给用户——遗留问题本身就是
用户挑选方案的重要依据。

### 3.6 内层 tool loop

```python
def run_agent(system_prompt, user_prompt, tools, output_schema, role) -> dict:
    """LLM 想调几次工具就调几次，想什么顺序就什么顺序。
    唯一要求：最后交出符合 output_schema 的结构化结果。"""
    messages = [user(user_prompt)]
    while True:
        resp = llm.chat(role, system_prompt, messages, tools=tools)
        if resp.stop_reason != "tool_use":
            return parse_and_validate(resp.content, output_schema)   # 失败则重试
        for call in resp.tool_calls:
            messages.append(tool_result(call, TOOLS[call.name](**call.args)))
```

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

```python
@dataclass
class Requirements:
    destination: str
    start_date: date
    end_date: date
    party: Party                    # 人数 / 成人·儿童·老人构成
    budget: Budget | None           # 总额或人均，标明含不含大交通
    styles: list[str]               # 自由标签："美食"、"历史"、"不爱走路"
    pace: Pace                      # RELAXED / NORMAL / PACKED
    must_visit: list[str]
    avoid: list[str]
    lodging_area: str | None
    constraints: list[str]          # 自由文本兜底："第2天下午有个电话会"
    inferred: set[str]              # 哪些字段是推断而非用户明说的
```

`inferred` 服务于需求卡的展示：推断字段标灰加 `?`，用户一眼知道该盯哪几行，
不必通读全表。这是"抽取+推断，一次性确认"能成立的前提。

```python
@dataclass
class Activity:
    name: str
    poi_id: str | None              # 高德 POI id
    coords: LatLng | None
    start: time
    end: time
    category: Category              # SIGHT / MEAL / REST / SHOPPING
    cost: Money | None
    indoor: bool                    # 雨天备选、季节适配要用
    note: str                       # LLM 写给用户的一句话理由

@dataclass
class Day:
    date: date
    activities: list[Activity]
    lodging: str | None

@dataclass
class Leg:                          # 交通段：代码算出来的，LLM 不写
    from_activity: int
    to_activity: int
    mode: TravelMode
    duration_min: int
    distance_m: int

@dataclass
class Itinerary:
    angle: Angle                    # 切入角度，展示时当方案标题
    days: list[Day]
    legs: list[Leg]
    issues: list[Issue]
```

**交通段由代码计算，不由 LLM 输出。** LLM 只排活动，校验器用高德算相邻活动的真实
耗时，渲染行程单时代码把"→ 地铁 40 分钟"插进去。理由：交通耗时是纯客观数据，
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

### 5.1 确定性规则（`validation/rules.py`）

纯函数，不依赖 LLM，全部可单测。

| # | 规则 | 级别 |
|---|---|---|
| 1 | 同日活动不重叠、时间递增 | BLOCKING |
| 2 | 相邻活动间隙 ≥ 高德实测耗时 × 1.2 | BLOCKING |
| 3 | 日期覆盖完整，首末日扣掉抵离占用 | BLOCKING |
| 4 | `must_visit` 全部出现 | BLOCKING |
| 5 | `avoid` 未出现 | BLOCKING |
| 6 | 花费合计 ≤ budget | BLOCKING |
| 7 | 日均强度符合 pace（活动数 / 步行距离 / 在外时长） | WARNING |
| 8 | 每天有午餐、晚餐时段 | WARNING |
| 9 | 营业时间冲突（数据不可靠，见 §2） | WARNING |

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

```python
def _apply_human_input(state, inp, emit):
    match state.stage:
        case Stage.AWAIT_REQ_CONFIRM:
            if inp.confirmed:
                state.stage = Stage.GENERATE
            else:
                state.raw_request += f"\n用户补充：{inp.text}"
                state.stage = Stage.COLLECT

        case Stage.AWAIT_CHOICE:
            if inp.accepted:
                state.itinerary = pick(state.candidates, inp.chosen)
                state.stage = Stage.DONE
                return

            delta = classify_feedback(inp.text, state.requirements)

            if delta.patches_requirements:
                state.requirements = apply_patch(state.requirements, delta.patch)
                emit(RequirementsPatched(delta.patch))   # 非阻塞提示，不拦流程
                state.issues = []                         # 旧 issue 基于旧需求，作废
                if state.itinerary is None:               # 尚未选定 → 三份各自重跑
                    state.seeds = ({c.angle.key: c for c in state.candidates}
                                   if delta.scale is INCREMENTAL else {})
                    state.stage = Stage.GENERATE
                else:
                    state.stage = Stage.REFINE
            else:
                state.itinerary = pick(state.candidates, inp.chosen)  # 提意见 = 选定它
                state.issues = [Issue.from_human(inp.text)]
                state.stage = Stage.REFINE
```

`pick(candidates, key)` 在已选定单份的情况下（`candidates` 只有一项或为空）
直接返回 `state.itinerary`。

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

## 7. 数据 provider 层

```python
class GeoProvider(Protocol):
    def search_poi(self, query: str, city: str) -> list[Poi]: ...
    def route(self, origin: LatLng, dest: LatLng, mode: TravelMode) -> Leg: ...
    def static_map(self, points: list[LatLng], path: bool) -> bytes: ...   # PNG
```

- `AmapProvider` — 高德实现，带磁盘缓存（按坐标对 + mode 去重）
- `FakeProvider` — 测试用，返回确定性假数据

**Provider 和 Tool 是两层，不合并。** Provider 是数据访问；Tool 是包给 LLM 的壳
（参数校验 + 结果转 LLM 友好文本）。分开的实际理由：`route()` 同时被 LLM 的工具
和规则 #2 的校验器调用，共用 Provider 就共用一份缓存——LLM 规划时查过的路线，
校验时不必再查。合成一层做不到这点。

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
│   ├── orchestrator.py            # ★ advance() / polish() / 阶段推进
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
│   │   ├── rules.py               # 9 条确定性规则，纯函数
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

版式：每天一张卡片，卡片内是活动时间轴，代码算出的交通段（`Leg`）插在相邻活动
之间，遗留 `Issue` 按 severity 标色（BLOCKING 红 / WARNING 黄 / SUGGESTION 灰）。
每天顶部一张高德静态地图，标出当天所有 POI 并连出路线。

地图通过 `GeoProvider.static_map()` 获取，与 POI、路线共用同一层缓存。
`FakeProvider` 返回一张占位 PNG，使 HTML 渲染测试不触网。

## 9. CLI 与产物

```
$ trip plan "十一想去京都玩5天，两个人，预算1万5"

  需求卡（推断项标灰加 ?）→ 回车确认或直接改
  ⠋ 三条线并行打磨中…  A✓  B(修订 2/3)  C✓
  三份成品并排 + 各自遗留问题
  选一份 / 对某份提意见 / 改需求
```

产物落在 `./trips/<slug>/`：

- `state.json` — 完整状态，每个暂停点写盘
- `plan-{A,B,C}.md` — 三份候选
- `itinerary.md` — 定稿
- `itinerary.html` — 定稿的自包含 HTML（`trip render <dir> --format html`）

## 10. 测试策略

| 层 | 方法 | 确定性 |
|---|---|---|
| `rules.py`、`render/` | 普通单测，每条规则一正一负 | 完全 |
| `providers/` | 录制真实高德响应存 fixture，回放 | 完全 |
| `orchestrator.py` | 注入 `FakeLlm` 按脚本返回；测阶段推进、循环上限、需求 patch、seed 传递、反馈分类分支 | 完全 |
| prompts | 少量真实调用，只断言**格式**不断言**质量**，标 slow，不进 CI 默认 | 部分 |
| 端到端 | 一个 golden case，人工看 | 靠眼 |

前三层能完全自动测——这正是混合架构相对全自主 loop 换来的东西。

TDD 落地顺序：`models` → `rules`（纯函数，最直接）→ `providers`（fixture）
→ `orchestrator`（FakeLlm）→ `prompts`（最后，靠迭代）→ `cli` / `render`。

代码格式化：每次改动后用 `black`（虚拟环境内优先，否则 `/opt/homebrew/bin/black`）。

## 11. 成本

三条线各跑一次生成 + 平均 2 轮修订 + 1 次 critic，单次完整规划粗估
**30-50 万输入 token**。这是"三份都打磨到收敛"的代价；wall time 靠并发压到
单份的 1.2 倍左右。

如果实测成本过高，最有效的旋钮依次是：`MAX_ROUNDS` 从 3 降到 2、候选数从 3 降到 2、
critic 换更便宜的模型。

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
"为什么第二天不去岚山"，agent 直接读 `state.json` 和 `plan-*.json` 回答，不走流程。
这是纯 CLI 给不了的。

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
