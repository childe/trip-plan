# Web 界面替代 CLI —— 设计

日期：2026-09-15
状态：已与用户确认；已按 r1 / r2 评审意见修订，待复评

## 1. 背景与动机

现在的交互入口是 `trip plan` / `trip resume`，两者都通过 `cli.terminal_ask()` 的
`input()` 向用户提问。这条路在中文场景下体验很差，**具体症状是终端 IME 输入错乱**：
用 IME 敲中文时光标位置算错、退格删掉半个字、长句折行后彻底花掉。这不是渲染宽度
问题，也不是编码问题，是 readline 与 IME 的交互问题——在终端里绕不过去。

浏览器的 `<textarea>` 天然没有这个问题，并且顺带解决了「一行 `input()` 装不下一段
完整需求」的限制。

方向已确定：**Web 为主，CLI 降级为非交互命令**。

## 2. 范围

### 做

- 行程列表页（浏览 `trips/` 下已有行程，点进去即接续）
- 核心闭环：新建行程 → 确认/修改需求 → 选方案/提意见 → 看成稿
- 实时进度展示（后台线程 + 1 秒轮询）
- 成稿页内嵌地图（直接 serve 已生成的 `itinerary.html`）
- 产物重建（`DONE` 但产物缺失/过期时的出路，不碰 LLM）
- 取消正在进行的规划
- Basic Auth 访问口令 + CSRF，支持局域网共享

### 不做（v1）

- **token 级流式输出**（像 ChatGPT 那样逐字吐规划与 critic 的过程）。
  用户已明确这是**下一期**的需求，本期不实现，但**必须留好扩展位**——
  §5.3 专门讲这件事，它实质影响了 v1 的事件模型设计。
- 下载 md/html 的按钮（产物仍照常落盘到行程目录）
- 用户体系、多租户、HTTPS
- SSE（接口形状为它预留，见 §5.3）

### 部署形态

本机启动，绑 `0.0.0.0` 供局域网访问（手机、家人）。不做公网部署。

## 3. 架构

Web 层是 `orchestrator` 的**第二个 driver**，与 CLI 平级。

```
            ┌──────────────┐
            │  web/app.py  │  路由、模板
            └──────┬───────┘
                   │
            ┌──────▼───────┐
            │  web/jobs.py │  run_command()：后台线程、per-trip 互斥、
            └──────┬───────┘  全局上限、取消令牌、CAS 与产物发布
                   │  advance(state, deps, cmd, emit, cancel)
        ┌──────────▼──────────┐
        │     orchestrator    │
        └─────────────────────┘
```

### 3.1 新增模块

| 文件 | 职责 |
|---|---|
| `src/tripplan/web/app.py` | Flask 应用工厂 + 路由。只做「鉴权 → 校验参数 → 调 registry → 渲染」 |
| `src/tripplan/web/jobs.py` | `JobRegistry` / `TripJob` / `run_command()`：起线程、per-trip 互斥、全局上限、取消令牌、job 生命周期 |
| `src/tripplan/web/events.py` | `EventLog`：序号分配、`events.jsonl` 追加与回读（历史）、内存 live ring（增量轮询）；对外两个出口 `snapshot()` / `since(n)`（见 §5.5） |
| `src/tripplan/web/view.py` | 结构化 view model：把 `Requirements` / `CandidateSlot` 摊成模板能直接遍历的字段（见 §6.1） |
| `src/tripplan/web/templates/` | Jinja2：列表页、详情页 |
| `src/tripplan/web/static/` | CSS + 轮询用的原生 JS |

`jobs.py` 里的 `run_command()` 是**框架无关**的：不 import flask，不碰 `request` / `session`，
入参是 `(trips_root, tid, cmd, deps, job)`，出参是一个显式的 `JobOutcome`。路由只负责
HTTP（鉴权、CSRF、取字段、302），后台线程只负责调度，「load → advance → CAS →
发布产物 → 记终态」这套提交纪律只有这一个实现（见 §4.1）。

### 3.2 改动面

- `cli.py`：删 `plan` / `resume` / `terminal_ask` / 交互版 `drive` / `_resolve_candidate_key`；
  新增 `trip web`；`render` 原样保留
- `write_artifacts()` 从 `cli.py` 抽到 `src/tripplan/artifacts.py`，并拆成
  `stage_artifacts(state, dir, provider, stage_id)` / `publish()` / `discard()` 三步
  （见 §4.1）。抽出去是为了避免 `web/` 反向依赖 `cli/`；`trip render` 改调同一套，
  两条路的发布语义不分叉。`stage_id` 让每个写者只碰 `.staging/<stage_id>/`，
  Web 侧传 `job_id`，`trip render` 自己生成一个 uuid
- `agents/limits.py`：新增 `Cancelled` 异常（**不继承** `LimitExceeded`，理由见 §4.3）；
  `SlotContext` 加取消令牌
- `orchestrator.advance()` / `slot.run_slot()`：各加一个可选的取消令牌参数
  （默认 `None`，现有调用与测试不受影响）
- `slot.py` / `orchestrator.py` / `validation/diversity.py`：每一处宽泛捕获前显式
  `except Cancelled: raise`（见 §4.3）；三处裸 `emit` 换成 `_safe_emit`（见 §5.1）
- `slot.py` 的 `_safe_emit` 抽到 `agents/_emit.py`，供上述三处共用
- `orchestrator.py`：补 4-5 个 `emit` 点（纯增量，不改签名）
- `pyproject.toml`：`[project.optional-dependencies] web = ["flask", "waitress"]`

`state` / `repo` / `render` / `llm` 的逻辑不改。

### 3.3 技术选型理由

**Flask + waitress**，服务端渲染 HTML，前端零构建、零框架。

- `advance()` 是**同步阻塞**的，同步 WSGI 框架与之直接对上。FastAPI / Starlette 的
  异步优势吃不到（`def` 端点会被扔进线程池，绕一圈回到同样的模型）。
- FastAPI 的核心卖点是 pydantic 的校验与序列化，而本项目已有 `models/`（dataclasses）
  \+ `wire.py`（手写 codec）。引入 pydantic 等于并存两套模型体系。
- 项目已经有 `render/itinerary_html.py` 产出 HTML 串，服务端渲染能直接复用；
  NiceGUI / Streamlit 这类纯 Python UI 框架反而与之冲突（Streamlit 的「每次交互
  重跑整个脚本」与有状态长时任务 + 后台线程正面冲突）。
- **不用 Flask 自带的开发服务器**。它明确不适合对外提供服务，而本设计要绑 `0.0.0.0`。
  waitress 是纯 Python、跨平台、无需编译工具链的生产级 WSGI 服务器。
- 选 waitress 而非 gunicorn 是**硬约束推导出来的，不是偏好**：见 §6.4。

## 4. 并发模型

### 4.1 job 不是循环

这是与 CLI 最关键的差异。`cli.drive()` 是 `while` 循环——跑一段、阻塞在 `input()`
问人、再跑一段。Web 版把这个循环**拆开交给 HTTP**：

**每一次用户动作 = 恰好一次 `advance()`**，跑到下一个暂停点就结束，线程退出。
下一次动作由下一个 POST 触发一个新线程。

job 线程体的完整逻辑（`web/jobs.py` 的 `run_command()`，框架无关）：

```python
def run_command(trips_root, tid, cmd, deps, job) -> JobOutcome:
    repo = open_trip(trips_root, tid)
    state = repo.load()            # 每次从盘上重读，命令之间不在内存留 state
    persisted = state.revision     # CAS 的 expected

    try:
        outcome = advance(state, deps, cmd, job.emit, cancel=job.cancel_token)
    except Cancelled:
        return JobOutcome.cancelled()
        # 不落盘：取消不是一种规划结果。盘上仍是上一个暂停点
    except (ProviderError, LimitExceeded) as e:
        return JobOutcome.failed(type(e).__name__, str(e))
        # 这两个分支接的正是 cli.main() 原来那两个 except 的职责

    if isinstance(outcome, Rejected):
        return JobOutcome.rejected(outcome.reason)
        # state 未变、revision 未变 → 不落盘。写盘只会白占一次 CAS 窗口，
        # 让无辜的并发调用被误杀（见 cli.drive() 里同一处的注释）

    staged = None
    if isinstance(outcome, Done):
        staged = stage_artifacts(state, repo.dir, deps.provider, job.job_id)
        # 写进 <trip>/.staging/<job_id>/ —— 这一次 job 私有的目录，不是最终路径，
        # 也不是同一 trip 共享的暂存区（理由见下）。这一步慢（要拉高德静态图）
        # 也可能失败；失败只记进 JobOutcome，绝不影响下面的 CAS 判定

    if job.cancel_token.is_set():
        discard(staged)
        return JobOutcome.cancelled()   # CAS 前最后一道检查：已取消就别写盘

    if not repo.save_if_revision(state, persisted):
        discard(staged)
        return JobOutcome.failed("Conflict", "另一个进程改动了这个行程")
        # 绝不发布产物：盘上的结局不是我们手上这份

    if staged is not None:
        publish(staged)   # 逐个 os.replace 原子改名，最后写 artifacts.json
    return JobOutcome.ok(state.revision)
```

「每次从盘上重读 state」让命令之间**完全无内存状态**：CAS 纪律自动成立，
服务器重启后的接续也自动成立。

**为什么产物要「先暂存、CAS 之后再原子发布」**：原来的顺序是 CAS 成功后才开始
`write_artifacts()`。但前端一看到 revision 变了 / stage 变成 `DONE` 就会刷新并亮出成稿
链接，而那一刻 `itinerary.html` 可能还没开始写、或正在被非原子地覆盖写到一半——
用户点进去看到的是 404 或半截文件。更糟的是进程在这中间崩掉：`state.json` 已经是
`DONE`，产物却永远不存在，之后每次进详情页都是一个死链。

所以：**慢且可能失败的那部分（渲染 + 拉图）挪到 CAS 之前**，写进
`.staging/<job_id>/`；CAS 成功后只剩一串 `os.replace`（同文件系统内原子改名）+ 写一个
`artifacts.json`（`{"revision": N, "files": [...]}`）。代价是 CAS 窗口被拉长了一次
产物生成的时间——但 CAS 输掉本来就是罕见情况，而「DONE 却 404」是每次崩溃都会
永久留下的伤。

**暂存目录必须按 job 隔离，不能是同一 trip 共享的 `.staging/`**：`.staging/` 里的文件名
是固定的（`itinerary.html`、地图图片……），共享一个目录等于让两个写者互相踩。§4.2 的
per-trip 互斥**管不到这件事**——它是**进程内**的一把锁，而同一个行程目录下同时会有
第二个写者：

- `trip render <dir>`（§7）走的正是同一条 `stage_artifacts()` + `publish()`，它是**另一个
  进程**，Web 进程的 registry 对它一无所知；
- 用户完全可能对同一个 `trips/` 起两个 `trip web`（§6.4 的单进程是纪律，不是强制）。

共享目录下的两种坏结局都是静默的：**(a)** 写者 A 暂存完、还没 CAS，写者 B 用另一份
state 把 `.staging/itinerary.html` 覆盖掉，A 的 CAS 成功后 `publish()` 发布的是 B 的成稿——
`artifacts.json` 的 revision 与 `state.json` 完全对得上，就绪判定（`artifact_ready`）一路
绿灯，但 HTML 讲的是另一份行程；**(b)** 败者的 `discard()` 删掉的是胜者刚放进去的文件，
`publish()` 于是搬了个空。两种情况都不会报错，也检测不出来。

因此纪律是：`stage_artifacts()` 收一个调用方给的 `stage_id`（Web 侧就是 `TripJob.job_id`，
`trip render` 自己现生成一个 uuid），只往 `.staging/<stage_id>/` 里写；`publish()` / `discard()`
**只能操作自己那个子目录**，并在结束时删掉它。`publish()` 里 `artifacts.json` **最后写**，
它是整个发布动作的提交点。`.staging/` 下的孤儿子目录（进程崩在中途留下的）由 `trip web` **启动时**扫一次删掉，
**只删 mtime 超过 1 小时的**——启动那一刻本进程没有任何 job，但别的进程（一个正在跑的
`trip render`）可能正往自己的子目录里写，按时间设个下限就不会误删活人的暂存件。
删错的代价本来也有限：暂存内容不是任何权威状态，大不了重建一次。

**产物就绪的判定与重建**：详情页不靠 `stage is DONE` 决定要不要给链接，靠
`artifact_ready` = `artifacts.json` 存在 **且** 其 `revision` 等于 `state.revision`
**且** 文件都在。不就绪时页面显示「产物待重建」+ 一个重建按钮（走
`POST /trips/<tid>/artifacts`，与 `trip render` 同一条代码路径，不碰 LLM）。
崩溃恢复、旧版本产物残留、手工删文件，三种情况共用这一条出路。

### 4.1.1 job 生命周期

`TripJob` 不是「跑完就没人管的一个线程」——详情页和轮询都要能回答「刚才那次命令
怎么样了」，所以它有一个明确的状态机：

```
running ──┬─→ succeeded
          ├─→ rejected     （advance 返回 Rejected，不是 HTTP 错误）
          ├─→ failed       （ProviderError / LimitExceeded / Conflict / 未预料异常）
          └─→ cancelling ─→ cancelled
```

- **没有 `queued` 态**：全局满载时直接 503，不排队（§4.2）。排队要额外引入队列超时、
  取消排队中的 job、以及「按钮点了但什么都没发生」的解释成本，对一个家用服务不值。
- `job_id`：创建 job 时生成的 uuid4 hex，**永不复用**。它同时是暂存目录名（§4.1）
  和前端的身份判据（下一条）。
- `status_version`：单调递增整数，**在单个 job 内部**每次状态变化 +1，从 1 开始。
  **它单独不足以当变化判据**：它是 per-job 的，新 job 从 1 重新开始，于是「老 job 的
  running(1)」与「另一个新 job 的 running(1)」在前端看来一模一样。真实的坏场景是：
  某个标签页挂在后台（浏览器把定时器节流到分钟级），这期间 job A 失败了、用户在另一个
  标签页又发了一条命令起了 job B——老标签页醒来一看还是 `running(1)`，判定「没变化」，
  于是既不刷新也不显示 A 的失败，停在一个过期页面上。失败/拒绝/取消这三种终态**不改
  `revision`**，所以 `revision` 那一路也兜不住。
  因此对外暴露的身份是 **`(job_id, status_version)` 这个二元组**，前端比较二元组而不是
  单个整数（§6.3）；`job_id` 变了就一定是另一次命令，无条件刷新。
- **终态 durable 事件带 `job_id`**：重启后回读 `events.jsonl` 也能把终态对到具体某一次
  命令上，而不是只知道「有过一次失败」。
- **线程体必须 `try/except/finally` 收尾**。`except BaseException` 兜住任何未预料的
  异常并转成 `failed`；`finally` 里无条件置终态 + flush `EventLog`。少了这层，一个
  没想到的异常会让 job 永远停在 `running`，详情页的按钮就永久置灰了，用户除了重启
  服务没有任何出路。
- **终态写 durable event**：`job_succeeded` / `job_rejected` / `job_failed` /
  `job_cancelled`（带 kind 与 message）追加进 `events.jsonl`。`JobRegistry` 是进程内
  内存对象，服务器一重启就空了；有这条落盘记录，重启后进详情页仍能解释「上次那步
  发生了什么」，而不是一片空白。
- **保留与替换**：终态 job 留在 registry 里供轮询读取，直到 (a) 同一 trip 的下一个命令
  到来时被新 job 替换，或 (b) 终态超过 30 分钟被清理线程丢弃（丢了也不损失信息，
  终态已经在 `events.jsonl` 里）。**active 的 job（见下面 `active` 的定义）永远不会被
  清理**——`cancelling` 也算 active，它的线程还活着。

### 4.2 并发约束

先定死一个谓词，下面所有约束都用它，**不许有任何一处写成只判断 `running`**：

```python
active = job.status in {"running", "cancelling"}
```

**为什么 `cancelling` 必须算 active**：取消不是立刻生效的，也没有时延上界（§4.3）——
一个 `cancelling` 的 job，它的线程可能还卡在一次已经发出去的 LLM 请求上，还在烧钱，
还握着那个 trip 的 state。如果互斥和全局计数只看 `running`，那么用户一点「取消」，
两个保证会**同时**失效：同一个 trip 立刻能起第二个 job（两个线程对同一份 state 跑
`advance`，正是互斥要防的浪费），全局名额也立刻被释放出来（点三下取消再点三下确认，
六个线程一起烧）。`cancelling` 是**正在退出**，不是**已经退出**。

| 约束 | 保证方式 |
|---|---|
| 同一 trip 同时只有一个 job | `JobRegistry` 用一把锁护住 dict；该 trip 已有 **active** job 则拒绝（409），页面按钮同时置灰 |
| 不同 trip 可并行，但**有全局上限** | 同一把锁里数 **active** job；达到 `TRIPPLAN_WEB_MAX_JOBS`（默认 3）则拒绝（503） |
| 名额只在线程真正退出时归还 | 线程体 `finally` 里置终态（§4.1.1），**那一刻**才从 active 集合里移出——不是在收到取消请求那一刻 |
| 后台线程不占 waitress 线程池 | 自己 `threading.Thread`；请求线程只负责登记后立刻返回 |
| Web 层必须单进程 | 见 §6.4 |

同一个谓词还管着另外两处，一并写死：**产物重建的前置条件**（§6.2 的
`POST /trips/<tid>/artifacts` 要求该 trip 无 active job）和**终态 job 的清理**
（§4.1.1，active 的不清理）。

为什么 per-trip 必须互斥：用户手快点两下「确认」会让两个线程同时对同一个 state 跑
`advance()`。`repo` 的 CAS 保证数据不会坏——但输掉的那个线程**已经把 LLM 的钱烧完了**
才发现自己白干。互斥是为了省钱，不是为了数据一致性。

为什么还需要一个**全局**上限：per-trip 互斥只管住了「同一个行程」，`SlotLimits` 也只是
**per-slot** 的额度。两者叠起来，「十个行程同时开跑」既不违反互斥、也不触发任何额度——
线程数和并发 LLM 请求数完全由用户点按钮的手速决定，而每个 job 内部还会并发跑三条候选
线。这是现有单-slot 保护的一个真空。上限用一个计数器实现（不是 `ThreadPoolExecutor`：
我们不排队，满载即拒），满载时 `POST` 返回 `503` + 「服务器正忙，稍后再试」，页面给
明确提示而不是静默失败。

### 4.3 取消

`SlotContext.cancel()` 与 `ctx.check()` 已经存在，`check()` 在 `runner.py` 的 agent
循环里每个 LLM turn 调一次。但 `SlotContext` 是在 `orchestrator._step_ctx()` 和
`slot.run_slot()` 内部**现场创建**的，Web 层拿不到句柄。

因此需要一个贯穿的取消令牌：`advance()` 加一个可选参数，一路传到 `SlotContext`，
`check()` 里多查一个条件。默认 `None`，不影响任何现有调用。

#### 取消必须是独立信号，不能复用 `LimitExceeded`

`SlotContext.check()` 现在是 `raise LimitExceeded("已取消")`。**这条路在 Web 下是坏的**，
而且坏得很安静：

- `slot.py:76` 的 `except LimitExceeded` 把它转成 `SlotStatus.EXHAUSTED` 的候选；
- 就算漏网，`orchestrator._safe_slot`（`orchestrator.py:254`）还有一层
  `except Exception` 兜底，转成 `SlotStatus.FAILED` 的候选；
- `_run_to_pause` 拿到这堆「失败候选」若无其事地把 stage 推到 `AWAIT_CHOICE`，
  `advance` 走到 `state.revision += 1`，job 体照常 CAS 落盘。

净效果：用户按了「取消」，系统给他**写进盘里**一份「候选全部生成失败」的行程，
revision 还涨了一格。取消被悄悄翻译成了「生成失败」。

所以引入 `Cancelled`（`agents/limits.py`），**不继承 `LimitExceeded`、不继承任何被现有
代码捕获的类型**——继承就等于重新掉进上面那两层捕获里。纪律是：

1. `SlotContext.check()` 取消时抛 `Cancelled`，其余额度仍抛 `LimitExceeded`；
2. **每一处宽泛捕获前面都先 `except Cancelled: raise`**——`slot.run_slot` 的
   `except LimitExceeded` / `except ProviderError`、`orchestrator._safe_slot` 的
   `except Exception`、`_run_to_pause` 里 `pick_angles` 的
   `except (ValueError, ProviderError, LimitExceeded)`、`validation/diversity.py`
   的重试回调。将来新增宽泛捕获时同理，§9 有一条测试守着这件事；
3. `Cancelled` 一路逃出 `advance()`，由 `run_command()` 接住（§4.1），
   **不 CAS、不发布产物**：盘上停在上一个暂停点，刷新页面回到取消前的样子。

#### 检查点

- 每个 LLM turn 一次：`runner.py:144` / `:150` 已有的 `ctx.check()`；
- 候选与候选之间：`_run_to_pause` 推导候选列表时每轮开头查一次——否则取消要等
  当前这条候选线彻底跑完（含 revise + critic）才可能生效；
- CAS 之前最后查一次（§4.1）：都已经取消了就别再写盘。

#### 时延要如实说，不给数字

`check()` 只发生在 turn 之间。**一个已经发出去的 LLM HTTP 请求没有办法被打断**，而
`llm/backends/` 目前**没有配置任何请求超时**（全库只有 `providers/amap.py` 设了
`timeout=10.0`）——所以原稿里「典型十几秒」是一句没有依据的承诺，一次挂住的上游调用
可以让它变成几分钟。真实上界只能这样描述：**到当前这次 provider 调用返回或失败为止**。

页面文案因此是「已请求停止，将在当前这一步结束后生效」，不带秒数，也不假装已停。

（`llm/backends/` 缺请求超时是一个独立的、同时影响 CLI 的既有问题。本期只如实描述
它对取消时延的影响，不顺手改——见 §10。）

Python 没有安全强杀线程的手段，这是唯一诚实的取消方式。

## 5. 事件模型

### 5.1 信封

```python
@dataclass(frozen=True)
class Event:
    seq: int                      # 单调递增，从 1 开始
    ts: float                     # epoch 秒
    type: str                     # "generating" / "revision" / 下期的 "token"
    payload: dict                 # 结构化，JSON 可序列化
    stream_id: str | None = None  # 同 id 的事件在前端拼进同一个块
    durable: bool = True          # False 只进内存，不落盘
```

现有 `emit` 收的是变长 tuple（如 `("generating", "foodie")`）。Web 层装一个 adapter
归一化：`type = t[0]`，`payload = {"args": list(t[1:])}`。

**不修改 orchestrator 的 emit 契约**——改它会波及一批现有测试，收益为零。

### 5.1.1 `emit` 是不抛异常的边界

`EventLog` 要做 JSON 序列化、追加写、flush，每一步都可能抛：payload 里混进不可序列化
的对象、磁盘满、`events.jsonl` 被删或被改成只读。而 emit 点是**散布在业务主干上**的，
其中三处是**裸调用**，没有 `slot.py` 里那层 `_safe_emit` 保护：

- `orchestrator.py:201` —— `emit(("requirements_patched", patch))`
- `orchestrator.py:289` —— `emit(("angle_generation_failed", str(e)))`
- `validation/diversity.py:87` —— `emit(("diversity_retry", key, overlap))`

201 那处尤其要命：它在 `_patch_requirements` 里，此刻 `state.requirements` 已经被
patch 进去、`state.revision` 还没递增。异常从这里穿出去，留下的是一个既不算「拒绝」
也不算「修改」的半吊子 state——正是 `orchestrator.py` 开头那段 docstring 明令不许出现
的东西。磁盘满不该有能力把状态机搞歪。

因此明确三条：

1. **`job.emit()` 自己是不抛异常的边界**：内部 `try/except Exception`，序列化或落盘
   失败只 `logging.exception` 到服务端日志，绝不外抛。事件日志坏了就少几行进度，
   它没有资格决定一次规划算不算数（§5.4 已经写明它不是权威状态）。
2. 上述三处裸 `emit` 一并换成 `_safe_emit`（从 `slot.py` 抽到 `agents/_emit.py` 共用）。
   两道防线是刻意的：即使将来有人换上一个会抛的 emit 实现，主干也不被拖下水。
3. 终态在线程体的 `finally` 里 flush 一次（§4.1.1），批量缓冲不会让最后几条进度、
   尤其是终态事件，永远卡在内存里。

§9 有一条对应测试：注入一个「每次都抛」的 emit，`advance` 仍正常返回、CAS 仍照常发生。

### 5.2 现有 emit 点

全库共 5 处：

| 位置 | 事件 | 性质 |
|---|---|---|
| `slot.py:48` | `("generating", angle.key)` | 进度里程碑 |
| `slot.py:53` | `("revision", angle.key, rnd)` | 进度里程碑 |
| `orchestrator.py:201` | `("requirements_patched", patch)` | 旁路提示 |
| `orchestrator.py:289` | `("angle_generation_failed", str(e))` | 异常 |
| `validation/diversity.py:87` | `("diversity_retry", key, overlap)` | 异常 |

进度信号本来就有，但偏粗，缺「开始收集需求」「正在校验事实」「critic 点评中」
这一层。v1 在 `_run_to_pause` 的阶段边界补 4-5 个 emit 点即可，**不需要动 agent 内部**。

### 5.3 为下期 token 流式留的扩展位

用户已明确下一期要做 **ChatGPT 式的 token 流式输出，覆盖规划与 critic 全过程**。
以下三点是 v1 现在就要留、否则下期必须重构的地方：

1. **`durable` 字段** —— token 事件量级是每秒几十上百条。若 v1 把 `events.jsonl`
   写成「所有事件都落盘」，下期会直接把它撑爆。有了这个字段，token 事件
   `durable=False`，只进内存环形缓冲。
2. **`stream_id` 字段** —— 前端把同 id 的 token 拼进同一个 DOM 块，而不是刷出几百行。
   v1 不产生这类事件，但**渲染函数现在就认这个字段**。
3. **`EventLog.since(n)` 是增量的唯一读出口** —— 下期若要加 SSE，只是给同一个
   `EventLog` 再开一个出口（浏览器的 `Last-Event-ID` 就是 `seq`），轮询那条路一行
   不用动。它返回的不只是事件列表，还有 `first_seq` / `stream_epoch` /
   `reset_required`（§5.5）——正是 `durable=False` 出现之后判断「客户端的游标还有效
   吗」所必需的，两种传输方式都要用。初始快照走另一个出口 `snapshot()`，见 §5.5。

**下期的实现路径（本期不实现，但设计不挡它）**：`ctx.emit` 已经一路传到
`run_agent()` 的调用现场——`steps._plan()` 与 `steps.run_llm_critic()` 都持有 `ctx`。
下期只需让 `run_agent` 把 `ctx.emit` 透给 `client.chat`，再让 backends 支持 streaming。
因为规划与 critic 走的是**同一个 `run_agent`**，两条线天然都覆盖到。函数签名的
形状不用动。

### 5.4 落盘

`durable=True` 的事件追加到 `trips/<行程>/events.jsonl`，**批量 flush，不每条 fsync**。

这份日志是**给人看的进度历史，不是权威状态**——权威永远是 `state.json`。崩溃时丢掉
最后几条事件不影响任何正确性。

服务器重启后，`EventLog` 从 `events.jsonl` 回读，新事件的 `seq` 从文件里的最大值
往后接，不从 1 重来。

### 5.5 两个来源：磁盘历史 vs 内存 live ring

§3.1 说 `EventLog` 有内存 ring，§5.5 又说详情页写入「全部历史事件」——**这两者不是同
一个东西**，必须分开说，否则实现时只能二选一：ring 有容量上限，装不下全部历史；而
把全部历史留在内存里，下期几十上百条/秒的 token 事件立刻把它撑爆（§5.3 的整个前提）。

分工：

| 来源 | 内容 | 服务谁 |
|---|---|---|
| `events.jsonl`（磁盘） | 全部 `durable=True` 历史 | `snapshot()` 的主体 |
| live ring（内存，容量 N=2000） | 最近 N 条事件，含 `durable=False` | `?since=N` 的**增量轮询**；并给 `snapshot()` 补上尾巴 |

两个出口：`EventLog.snapshot()`（详情页服务端渲染时的**初始快照**，磁盘历史 + ring
当前内容归并，锁内完成，见下）和 `EventLog.since(n)`（增量）。

「浏览器刷新后接着上次」的实现**不靠前端缓存，靠服务端有日志**：详情页渲染时调
`snapshot()` 把历史写进 HTML，刷新后一进来就是完整的；前端拿快照给出的 `cursor`
往后接增量（**不是**自己数页面上最后一条的 `seq`——理由见下一小节第 2 点）。

#### 游标可能失效，不许静默漏事件

`since=N` 有两种失效方式，`?since` 这个形状本身表达不了，所以响应里必须带出来：

1. **客户端落后于 ring**：`since < ring.first_seq`（标签页挂后台太久、或 token 事件
   刷得太快）。中间那段已经被挤出内存，再返回 `first_seq` 之后的事件就是**默默吞掉
   一段**，前端拼出来的进度是错的还不自知。
2. **服务器重启过**：seq 仍从 `events.jsonl` 最大值续号（§5.4 不变），但下期一旦有
   `durable=False` 事件，它们消耗掉的 seq 号不在磁盘上——重启后新事件会**复用客户端
   已经见过的号段**。单看 seq 分不出「这是新事件」还是「这是我早就有的那条」。

两者都用同一个出口解决：响应带 `first_seq` 与 `stream_epoch`（进程启动时生成的随机
串），并在 `since < first_seq` 或客户端持有的 epoch 与当前不符时置 `reset_required: true`。
前端见到它就 `location.reload()` 重新取一份完整快照，**宁可多刷一次，不接受静默漏事件**。

#### reset 必须保证收敛：快照的游标要落在 high-water mark 上

「reset → reload → 重取快照」这条路**只有在新快照给出的游标一定不再触发 reset 时才成立**，
而「快照 = 磁盘 durable 历史」给不出这个保证。反例（下期 token 事件一开就是常态）：ring
被一批 `durable=False` 的事件挤爆，`first_seq` 涨到 5000；这些事件不在 `events.jsonl` 里，
所以 reload 后新页面从磁盘拿到的最大 seq 还是 4000；前端带 `since=4000` 再问一次，
`4000 < 5000` 又是 `reset_required` ——**一个每秒 reload 一次、永远读不完内容的死循环**，
比静默漏事件更糟。

所以初始快照的定义收紧为，且这是 `EventLog` 的接口契约而不是调用方的自觉：

1. `EventLog.snapshot()` **在 `EventLog` 自己的锁内**完成三件事：读 durable 历史、取当前
   live ring 的全部内容、按 `seq` 归并去重，返回 `(events, cursor, first_seq, stream_epoch)`。
   `cursor` 是**此刻的 high-water mark**（已分配出去的最大 seq），不是「磁盘上的最大 seq」。
   在锁内取是必要的：否则「读完磁盘」与「读 ring」之间新写入的事件会掉进缝里，
   快照和游标对不上，v1 的批量 flush 延迟就足以制造这条缝。
2. 详情页把 `cursor` 写进 HTML，前端从它往后要增量。因为 `cursor ≥ ring.last_seq ≥
   first_seq`，紧接着的那次 `?since=cursor` **在构造上不可能再触发 reset**。唯一还能触发
   的是「这中间进程又重启了」（epoch 变），那是真的换了一条流，reload 本来就该发生。
3. 轮询接口在置 `reset_required: true` 时**一并返回 `resume_seq`**（= 当前 high-water mark）。
   前端不想整页 reload 时（下期 SSE）可以直接跳到 `resume_seq` 续上——代价是承认中间那段
   丢了，但这是**明示**的丢，不是静默吞掉。v1 前端简单起见仍走 `location.reload()`。

**收敛性就是这样保证的**：任何一次 reset 之后，客户端的游标都被推到当前 high-water mark，
而 high-water mark 永远 ≥ `first_seq`。最多刷新一次，然后恢复正常轮询。

这个能力与传输方式无关：轮询用 `?since=N`，SSE 用 `Last-Event-ID`，同一份数据两种取法
（SSE 下 epoch 变化对应重新 `retry` 并丢弃 `Last-Event-ID`）。

## 6. HTTP 接口

### 6.0 行程标识与校验

`<tid>` **就是 `trips/` 下的目录名**，URL 编码后进路径。中文目录名可正常工作
（`/trips/%E6%88%91%E4%B8%8B...`），Flask 自动解码。

**校验纪律（对局域网暴露的服务是硬要求）**：`tid` 必须是单个路径段（不含 `/`、`\`、
`..`），且 `(trips_root/tid).resolve()` 必须是 `trips_root.resolve()` 的直接子目录。

### 6.1 页面类（返回 HTML）

#### `GET /` —— 列表页 + 新建表单

- 输入：无
- 输出：每个行程一行（目录名、`raw_request` 摘要、`stage`、`revision`、修改时间、
  是否正在跑）；页面底部一个大 `textarea` 的新建表单
- **单个目录 `TripCorrupt` / `TripNotFound` 要单独标记为「损坏」并继续**，
  不能让一个坏目录把整页炸掉

#### `GET /trips/<tid>` —— 详情页

服务端按 `stage` 渲染不同主区域：

| stage | 主区域 |
|---|---|
| `AWAIT_REQ_CONFIRM` | 需求卡（模板遍历 `ReqCardVM`）+ 「确认」按钮 + 「说要改什么」textarea |
| `AWAIT_CHOICE` | 候选列表（模板遍历 `CandidateVM`）+ 每条候选一个「选它」按钮 + 提意见 textarea |
| `COLLECT` / `GENERATE` / `REFINE`，且有 **active** job（§4.2） | 「正在工作」+ 取消按钮 + 进度区（其余按钮置灰）。`cancelling` 时取消按钮也置灰，文案换成「已请求停止…」 |
| `COLLECT` / `GENERATE` / `REFINE`，但**没有 active** job | 「上一步没有跑完」+ 「继续」按钮（发 `cmd=None` 的命令）。服务重启、`503` 被拒、线程异常挂掉，三种情况都落在这里——必须有出路，不能是一个永远转圈的假进度条 |
| `DONE` + `artifact_ready` | 成稿摘要 + 跳 `/trips/<tid>/itinerary` 的链接 |
| `DONE` + 产物未就绪 | 成稿摘要 + 「产物待重建」+ 重建按钮（§4.1），**不给死链** |

页面底部固定一块**进度区**，服务端渲染时写入 `EventLog.snapshot()` 的结果，并把快照
给出的 `cursor` 一并写进页面供轮询起步（见 §5.5）。

#### 不复用 CLI 的 Markdown 渲染器

`render/requirement_card.py` 的 `render_requirement_card()` 和 `render/candidates.py`
的 `render_candidates()` 返回的是 **Markdown 串**（`"## 需求确认"`、`"- **目的地**：…"`、
`"### [foodie] …"`），不是 HTML。把它们塞进 Jinja 只有两种结局，都不能要：

- autoescape 开着 → 页面上显示的是 Markdown 源文，`**目的地**` 原样带星号；
- 用 `|safe` 让它当 HTML → 这些串里拼进去的是 `field.rationale`、`slot.detail`、
  `angle.title`、`Issue.message`、`raw_request`——**全都是模型输出或用户输入的自由
  文本**。标成 safe 等于把它们当可信 HTML 注入页面，直接开一个 XSS 面，而这个服务
  还要暴露在局域网上给别人访问。

（在浏览器里现跑一个 Markdown→HTML 转换也不行：那只是把同一个注入点挪进转换器，
还得额外引一个依赖。）

所以 Web 模板**只吃结构化对象**：`web/view.py` 把 `Requirements` / `CandidateSlot`
摊成 `ReqCardVM` / `CandidateVM`（label、值、`is_inferred`、rationale、天数、项数、
status、detail、issues……都是独立字段），模板遍历字段、自己出 HTML 结构。

纪律：**Jinja autoescape 全程开着，Web 模板里不出现任何 `|safe` / `Markup(...)`**。
§9 有一条测试守这件事。CLI 的两个 Markdown 渲染器保持原样服务 `trip render`，两套
渲染不共享字符串、只共享数据模型。

唯一的例外是下面的成稿页——它 `send_file` 一份**完整的独立 HTML 文档**，根本不过模板。
该文档由 `render/itinerary_html.py` 生成，转义责任在那个模块（既有行为，本期不改）。

#### `GET /trips/<tid>/itinerary` —— 成稿页（内嵌地图）

`send_file(trips/<tid>/itinerary.html)`。该文件在 job 跑到 `DONE` 时由后台线程暂存并
原子发布（§4.1），所以页面秒开，**不会在请求里现算高德静态地图**。

产物不存在或 `artifacts.json.revision` 与 `state.revision` 不一致时，返回 `409` +
一句「产物需要重建」和重建入口，**不是裸 404**——详情页在这种状态下本来也不会给出
这个链接，这里是直接输 URL 或用旧书签进来的兜底。

### 6.2 动作类（表单 POST，成功一律 302）

#### `POST /trips` —— 新建行程

| 字段 | 类型 | 说明 |
|---|---|---|
| `request` | str，必填 | 自然语言需求，多行 textarea |
| `dir` | str，可选 | 目录名；留空则用现有的 `slugify(request)` |

- 成功 → `302 /trips/<tid>`，后台线程跑 `run_command(..., cmd=None, ...)`
- `request` 为空 → `400`，回列表页并保留已输入内容
- `request` 超长（见 §6.5 的字段上限）→ `400`，同样保留内容
- `TripExists` → `409`，提示「已存在」并给直达链接
- 全局 **active** job 数满 → `503`（§4.2）。此时**行程目录已经建好**，提示「已创建，但服务器
  正忙，稍后进去点『继续』」并给直达链接——不静默丢掉用户刚敲的那段需求

#### `POST /trips/<tid>/commands` —— 提交命令

| 字段 | 类型 | 说明 |
|---|---|---|
| `kind` | `confirm` \| `amend` \| `choose` \| `feedback` | |
| `expected_revision` | int | 隐藏字段，渲染页面时写入 |
| `text` | str | `amend` / `feedback` 用 |
| `angle_key` | str | `choose` / `feedback` 用 |

映射到 `state.py` 现成的命令类型：

```
confirm  → ConfirmRequirements(expected_revision)
amend    → AmendRequirements(expected_revision, text)
choose   → ChooseCandidate(expected_revision, angle_key)
feedback → GiveFeedback(expected_revision, angle_key, text)
```

（`kind` 留空 = 「继续」，对应 `cmd=None`，用于上一行提到的「有 stage、没 job」。）

- 成功 → `302 /trips/<tid>`，后台线程跑 `run_command(...)`
- 该行程已有 **active**（`running` **或 `cancelling`**，§4.2）job → `409`（按钮本已置灰，
  这是兜底）。**`cancelling` 也挡**：那个线程还没退出，放第二个进来就是两个线程同时
  对一份 state 跑 `advance`
- 全局 **active** job 数满 → `503`，详情页顶部提示「服务器正忙」，按钮不置灰（可以再点）
- `text` 超长（§6.5）→ `400`，回详情页并保留输入
- `advance` 返回 `Rejected` → **不是 HTTP 错误**，job 进 `rejected` 终态并记下
  `reason`，详情页顶部显示告警条

**顺带消除的一个 bug 面**：`angle_key` 来自页面上渲染的按钮 value，是真实 key。
CLI 里 `_resolve_candidate_key()` 那整块「大小写兜底」逻辑（连同它注释里描述的
「用户被困死只能 Ctrl-C」的场景）在 Web 下从根上不存在——用户不再手敲 key。

#### `POST /trips/<tid>/cancel` —— 取消

- 输入：CSRF 隐藏字段
- 行为：设置 job 的取消令牌，job 状态 `running` → `cancelling`（`status_version` +1）
- 输出：`302 /trips/<tid>`，页面显示「已请求停止，将在当前这一步结束后生效」
- 没有 job 在跑 → `302` 回详情页，什么也不做（幂等，重复点不报错）；已经是
  `cancelling` 时同样是 no-op，**不再 +1 `status_version`**（否则每点一下都让所有标签页
  白刷一次）
- **`cancelling` 期间该 trip 仍然被互斥挡着**（§4.2）：页面不会因为「已请求停止」就把
  按钮放开，否则用户会以为可以立刻开始下一步，实际拿到 409
- 语义见 §4.3：不是立刻生效，也不承诺时延

#### `POST /trips/<tid>/artifacts` —— 重建产物

- 输入：CSRF 隐藏字段
- 行为：从 `state.json` 重跑 `stage_artifacts()` + `publish()`，**不碰 LLM**，
  与 `trip render` 同一条代码路径（§4.1）
- 前置：`stage is DONE`，且该 trip **没有 active job**（§4.2 的谓词，含 `cancelling`）；
  否则 `409`。理由同上：那个还没退出的线程可能正在往自己的暂存目录里写、马上要
  `publish()`，此时插一次重建就是两个发布者抢同一份最终产物
- 输出：`302 /trips/<tid>`
- 会拉高德静态地图，可能慢：同样走后台线程 + 一个 job（**占全局 active 名额**），
  页面轮询等它

### 6.3 数据类（JSON，供轮询）

#### `GET /trips/<tid>/events?since=<seq>`

输入：`since`，int，默认 0；`epoch`，str，可选（前端回传页面里拿到的那个）。

输出：

```json
{
  "events": [
    {"seq": 12, "ts": 1757900000.1, "type": "generating",
     "payload": {"args": ["foodie"]}, "stream_id": null}
  ],
  "first_seq": 1,
  "last_seq": 12,
  "stream_epoch": "8f3ac1d2",
  "reset_required": false,
  "resume_seq": null,
  "job": {"id": "3f9a1c...", "status": "running", "status_version": 4,
          "kind": null, "message": null},
  "stage": "GENERATE",
  "revision": 7,
  "artifact_ready": false
}
```

- `job.status` ∈ `running` / `cancelling` / `succeeded` / `rejected` / `failed` /
  `cancelled` / `none`（该 trip 当前没有 job，例如服务刚重启）。
- `job.kind` / `job.message`：`failed` 时形如 `"ProviderError"` / `"Conflict"` +
  一句中文；`rejected` 时是 `RejectReason`。后台线程接住的
  `ProviderError` / `LimitExceeded` 在这里暴露，对应 `cli.main()` 里那两个 except 分支
  的职责。
- `job.id` / `job.status_version`：见 §4.1.1。**这两个字段必须一起看**：
  `status_version` 是 per-job 的，换了 job 就从 1 重来。`job.status` 为 `none` 时
  `job.id` 也是 `null`。
- `reset_required` / `resume_seq` / `first_seq` / `stream_epoch`：见 §5.5。
  `resume_seq` 只在 `reset_required` 为 true 时非空。

**前端逻辑只有四条**，刻意做得极薄：

1. 每秒拉一次，带上本地游标（初值是快照的 `cursor`，§5.5）与 `epoch`
2. `events` 里的新事件追加到进度区（认 `stream_id`：同 id 拼进同一个块）
3. 需要刷新时 `location.reload()`，触发条件是**四者之一发生变化**：
   **`job.id`**、`job.status_version`、`revision`、`artifact_ready`。
   **刷新后把新值记为本地基线**
4. `job.status` 是终态（`succeeded` / `rejected` / `failed` / `cancelled`）时
   **停止轮询**；`reset_required` 为 true 时也 reload 一次

第 3 条必须是「**版本变化**」而不是原稿的「`error` 非空」。`error`（现在的 `job.kind`）
是**留在 job 上的终态字段**，不是一次性事件：reload 之后再轮询，它还在那儿，于是又
reload——每秒一次，页面永远刷不停，用户连错误信息都读不完。同理 `running` 由 true 变
false 也只是 `status_version` 变化的一个特例，单独判断反而漏掉「running → 另一个
running」这类情况。改成单调版本号 + 终态停轮询，两个毛病一起消掉。

**但光有 `status_version` 不够，必须连 `job.id` 一起比**：`status_version` 只在一个 job
内部单调，新 job 从 1 重来，「老 job 的 running(1)」和「新 job 的 running(1)」看起来
完全一样——后台标签页因此会漏掉整整一次命令（完整推演见 §4.1.1）。`revision` 也补不上
这个洞，因为 `failed` / `rejected` / `cancelled` 三种终态根本不改 revision。二元组变了
就刷新，是唯一不漏的判据。

**前端不需要知道任何业务状态怎么渲染**，它只判断「要不要刷新」。所有渲染在服务端
Jinja2。这也让下期接 SSE 时前端改动面极小。

### 6.4 硬约束：Web 层必须单进程

`JobRegistry` 是**进程内单例**。多 worker 会让轮询请求被路由到没有该 job 的进程，
进度页随机失灵、取消按钮随机失效。

**不许换成 gunicorn 开多 worker**。waitress 是单进程多线程，registry 天然是一份。
这是选型的实质理由，不是偏好。

### 6.5 鉴权

**HTTP Basic Auth**：零登录页、浏览器原生记住、一个 `before_request` 搞定。
用户名固定 `trip`，密码取环境变量 `TRIPPLAN_WEB_TOKEN`，用 `hmac.compare_digest`
比较（不是 `==`：口令比较不留计时侧信道，反正一行的事）。

**启动时硬约束**：`TRIPPLAN_WEB_TOKEN` 未设置时，拒绝绑 `0.0.0.0`，只能
`127.0.0.1`，并给一句中文提示说明怎么设。不给「裸奔到局域网」留口子。

#### CSRF

原稿只写了「表单另带 per-session 的 CSRF 隐藏字段」，没说密钥哪来、cookie 怎么设、
校验覆盖哪些路由、怎么比较——而依赖表里只有 flask + waitress，没有任何现成 CSRF 组件，
这几件事不写清楚就等于没有。具体形态：

| 项 | 决定 |
|---|---|
| Flask `secret_key` | 取 `TRIPPLAN_WEB_SECRET`；未设置则进程启动时 `secrets.token_urlsafe(32)` 现生成。重启即所有旧表单失效，对单进程本机服务可接受——**比硬编码一个默认值安全得多**，那种默认值一定会被原样带到局域网上 |
| session cookie | `HttpOnly=True`、`SameSite=Lax`、`Secure=False`（明文 HTTP，见下面的代价一节）、`Path=/` |
| token 生成 | `secrets.token_urlsafe(32)`，首次访问时放进 session，per-session 复用 |
| token 传递 | 模板里**每个** `<form>` 都渲染同一个隐藏字段 `_csrf` |
| 校验 | `hmac.compare_digest(session_token, form_token)`；不匹配或缺失 → `403` |
| 覆盖范围 | `before_request` 里**对所有 `POST` 统一拦**（默认全拦，白名单式豁免），不是逐个路由自己记得加装饰器——`/trips`、`/trips/<tid>/commands`、`/trips/<tid>/cancel`、`/trips/<tid>/artifacts`，以及将来新增的任何 POST |

不引入 `flask-wtf`：一个 token + 一次 `compare_digest` + 一个 `before_request`，
为此多一个依赖不划算。

#### 输入体积上限

局域网暴露的服务不能任由请求体撑爆内存，模型也不能拿到一段 50 万字的「需求」：

- `MAX_CONTENT_LENGTH = 64 KiB`（Flask 原生，超限直接 `413`）
- `request` / `text`：各限 8000 字符，超出 → `400` 并回显已输入内容
- `dir`：限 80 字符（还要过 §6.0 的路径段校验）
- `angle_key`：限 64 字符；反正 `_check_candidate` 还会验它是不是真实存在的 key

**如实记录代价**：局域网上是明文 HTTP，Basic Auth 凭据可被同网段嗅探。考虑到这是
家庭/办公网段 + 自生成口令 + 保护的是行程规划而非资金，判定为可接受。要更强就得
上 HTTPS，那是另一个量级的工作，不在本期范围。

## 7. CLI 最终形态

只剩两个子命令：

| 命令 | 说明 |
|---|---|
| `trip web` | 启动服务。参数：`--host`（默认 `127.0.0.1`）、`--port`（默认 8000）、`--trips-dir`（默认 `trips`） |
| `trip render <dir>` | 从 `state.json` 重新生成产物，不碰 LLM。CLI 接口原样保留（含 `--format`），内部改调 `artifacts.stage_artifacts()` + `publish()`，与 Web 共用同一条原子发布路径（§3.2 / §4.1）。它现生成自己的 `stage_id`，因此**与正在跑的 Web job 共存也不会互相踩暂存文件**——这是 §4.1 要求按 job 隔离暂存目录的直接原因 |

删除：`plan`、`resume`、`terminal_ask()`、交互版 `drive()`、`_resolve_candidate_key()`。

`main()` 里的异常处理分支相应调整：`EOFError` 分支随 `input()` 一起删除；
`ProviderError` / `LimitExceeded` / `ConfigError` / `MissingCredential` /
`TripNotFound` / `TripCorrupt` 的处理移交 Web 层对应位置（凭据类在启动时，
运行类在 job 里转成 `error` 字段）。

## 8. 依赖

```toml
[project.optional-dependencies]
web = ["flask", "waitress"]
```

进 optional-dependencies，只装 CLI（`trip render`）的用户不受影响。
`dev` 分组追加这两项，以便跑测试。

## 9. 测试策略

`jobs.py`（含 `run_command()`）与 `events.py` **不依赖 Flask**——直接单测，不起服务器。
`run_command()` 的 `advance` / `stage_artifacts` / `publish` 都是可注入依赖（沿用
即将删掉的 `drive(..., advance_fn=)` 的同一手法），所以「CAS 输了」「产物生成失败」
「emit 抛异常」「中途取消」这些分支全部可以纯内存地故障注入，不碰 LLM 与高德。
路由层用 `app.test_client()`。

要守住的回归，每条对应上文一个具体决定：

1. **per-trip 互斥**（§4.2）—— 同一 trip 连发两次命令，第二次 409，`advance` 只被调用一次
2. **刷新续传**（§5.5）—— 详情页 HTML 含全部历史事件；`?since=N` 只返回增量且序号连续
3. **`durable=False` 不落盘**（§5.3）—— 瞬时事件在 `since()` 可读到，但不在 `events.jsonl` 里
4. **重启接续**（§5.4）—— 从已有 `events.jsonl` 构造 `EventLog`，新事件 seq 从旧最大值往后接
5. **路径穿越被拒**（§6.0）—— `/trips/..%2f..%2fetc%2fpasswd` 返回 404
6. **裸奔保护**（§6.5）—— 未设 `TRIPPLAN_WEB_TOKEN` 时 `trip web --host 0.0.0.0` 启动失败并给中文提示
7. **CAS 输掉不写产物**（§4.1）—— 模拟 `save_if_revision` 返回 False，确认 `write_artifacts` 未被调用
8. **取消生效**（§4.3）—— 设置取消令牌后 `ctx.check()` 抛 `Cancelled`
9. **坏目录不炸列表页**（§6.1）—— `trips/` 下放损坏的 `state.json`，列表页仍 200 且标为「损坏」
10. **`Rejected` 不落盘**（§4.1）—— `advance` 返回 `Rejected` 时 `save_if_revision` 未被调用
11. **取消不被吞成失败候选**（§4.3）—— 在 `run_slot` 内部触发 `Cancelled`，确认它穿过
    `slot.py` 的 `except LimitExceeded` 和 `_safe_slot` 的 `except Exception` 逃出
    `advance`；且 `save_if_revision` 未被调用、盘上 revision 未变、`state.candidates`
    没有被写成一串 FAILED
12. **取消后不发布产物**（§4.1）—— CAS 前那道取消检查命中时，`publish` 未被调用
13. **emit 炸了不影响主干**（§5.1.1）—— 注入一个每次都抛的 emit，走一遍会触发
    `requirements_patched` / `angle_generation_failed` / `diversity_retry` 的路径，
    `advance` 仍正常返回、CAS 仍照常发生、state 不留半吊子改动
14. **产物先暂存后发布**（§4.1）—— `stage_artifacts` 成功但 CAS 返回 False 时，行程目录里
    **不出现** `itinerary.html`；CAS 成功时文件出现且 `artifacts.json.revision` 与
    `state.revision` 对齐
15. **DONE 但产物缺失不给死链**（§6.1）—— 手工删掉 `itinerary.html` 后进详情页，显示
    「产物待重建」而非链接；`GET .../itinerary` 返回 409；重建后恢复
16. **模板不出 Markdown 源文、不注入 HTML**（§6.1）—— 把 `<script>alert(1)</script>` 塞进
    `rationale` / `slot.detail` / `raw_request`，详情页 HTML 里必须是转义后的文本；
    同时全库扫一遍 `web/templates/`，不允许出现 `|safe` / `Markup`
17. **错误不刷新循环**（§6.3）—— job 处于 `failed` 终态时连续轮询两次，`status_version`
    不变（前端据此不再 reload），且响应告诉前端这是终态、可以停轮询
18. **全局 job 上限**（§4.2）—— 上限设为 1，对**两个不同的 trip** 连发命令，第二个拿到
    `503`，且第二个 trip 的 `advance` 没被调用
19. **CSRF 全覆盖**（§6.5）—— 四个 POST 路由逐个测：缺 token、错 token 一律 403，
    且此时 `advance` / 取消令牌都没被碰过
20. **游标失效不静默漏事件**（§5.5）—— `since` 小于 ring 的 `first_seq` 时返回
    `reset_required=true`；换一个 `epoch` 轮询同样返回 `reset_required=true`
21. **线程异常不留永久 running**（§4.1.1）—— 让 `advance` 抛一个设计里没预料的异常
    （例如 `KeyError`），job 最终停在 `failed` 而不是 `running`，且 `events.jsonl` 里
    有对应的终态事件
22. **错过终态也不会把新 job 当成旧 job**（§4.1.1 / §6.3）—— 模拟一个「错过中间终态」的
    标签页：job A 跑到 `running`（取一次轮询响应），随后让 A 进 `failed`，再为同一 trip
    起 job B 并停在 `running`；下一次轮询响应里的 `(job.id, job.status_version)` 必须与
    第一次不同（即前端会刷新）。**只比 `status_version` 会相等**，这条测试就是守着这一点；
    同时断言 `revision` 在这一串里没变（证明 revision 兜不住）
23. **`cancelling` 仍占名额、仍挡住同 trip**（§4.2）—— 取消后让 job 停在 `cancelling`
    （线程不退出），此时：同一 trip 再发命令返回 `409`；把全局上限设为 1 时，**另一个**
    trip 发命令返回 `503`；`POST /trips/<tid>/artifacts` 也返回 `409`。线程真正退出后，
    三者都恢复正常
24. **并发写者不串台**（§4.1）—— 两个写者（模拟 Web job 与 `trip render` 两个进程）从
    **不同的 state** 各自 `stage_artifacts()`，验证它们落在不同的 `.staging/<id>/` 下互不
    覆盖；让其中一个 `discard()`、另一个 `publish()`，最终 `itinerary.html` 的内容必须
    来自 `publish()` 那一方，且 `artifacts.json.revision` 与它对得上（不是「revision 对得上
    但内容是另一份」）。另补一条：`discard()` 只删自己的子目录
25. **reset 最多刷新一次**（§5.5）—— 构造「被挤出 ring 的是 `durable=False` 事件」的场景
    （直接往 `EventLog` 写瞬时事件把 ring 挤爆），先确认 `?since=<旧游标>` 返回
    `reset_required=true`；然后按设计走一次 `snapshot()`，用它返回的 `cursor` 再轮询，
    这一次**必须** `reset_required=false`。守的是收敛性，不是单次行为

现有 `tests/test_cli.py` 相应删改：交互相关用例（`terminal_ask`、`EOFError` 分支、
`_resolve_candidate_key`）随代码一起删；`render` 与凭据装配相关的保留。

## 10. 已知限制

1. **取消不是立刻生效，且时延没有上界**（§4.3）。检查点只在 LLM turn 之间，而
   `llm/backends/` 没有配置请求超时——一次挂住的上游调用能把取消拖到任意长。页面
   如实显示「已请求停止，将在当前这一步结束后生效」，**不给秒数**，不假装已停。
2. **`llm/backends/` 缺请求超时**。这是一个既有问题，同时影响 CLI，本期不改（改它
   要逐个 backend 过一遍构造参数并补测试，属于另一件事）。它在本期的可见后果只有
   上面第 1 条。
3. **关服务会丢当前这一步**。Ctrl-C 停服时正在跑的 `advance()` 直接消失。损失有限：
   纪律是每到暂停点先落盘再问人，所以上一个暂停点的进度还在 `state.json` 里，
   重启后进详情页会看到「上一步没有跑完 + 继续」（§6.1）。丢的只是正在进行的那一步。
4. **重启后 `JobRegistry` 清空**。刚才那次命令的终态在内存里没有了，只能靠
   `events.jsonl` 里的终态事件解释（§4.1.1）。这是刻意的：job 是进程内概念，权威
   状态永远是 `state.json`。
5. **全局同时只跑 3 个 job**（§4.2），满了直接 503 且不排队。家用场景下这是特性
   不是缺陷，但确实意味着「一次性开十个行程」做不到。
6. **取消不会立刻还回名额**（§4.2）。`cancelling` 算 active，名额要等线程真正退出才
   释放，而那受限于第 1 条的时延。这是刻意的：提前释放等于允许两个线程同时对一份
   state 跑 `advance`，既烧双份钱又让互斥形同虚设。页面文案要能解释「为什么点了取消
   还不能马上开始下一步」。
7. **Web 层必须单进程**（§6.4）。
8. **局域网明文 HTTP**（§6.5）。session cookie 因此不能设 `Secure`，CSRF token 与
   Basic Auth 凭据一样可被同网段嗅探——这条限制的根因和缓解判断都在 §6.5。
9. **进度信号偏粗**。v1 是阶段级里程碑，不是 token 级。token 级是下一期（§5.3）。
