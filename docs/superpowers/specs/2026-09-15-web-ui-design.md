# Web 界面替代 CLI —— 设计

日期：2026-09-15
状态：已与用户确认，待评审

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
- 取消正在进行的规划
- Basic Auth 访问口令，支持局域网共享

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
            │  web/jobs.py │  后台线程 + per-trip 互斥
            └──────┬───────┘
                   │  advance(state, deps, cmd, emit, cancel)
        ┌──────────▼──────────┐
        │     orchestrator    │
        └─────────────────────┘
```

### 3.1 新增模块

| 文件 | 职责 |
|---|---|
| `src/tripplan/web/app.py` | Flask 应用工厂 + 路由。只做「校验参数 → 调 registry → 渲染」 |
| `src/tripplan/web/jobs.py` | `JobRegistry` / `TripJob`：起线程、per-trip 互斥、持有取消令牌 |
| `src/tripplan/web/events.py` | `EventLog`：序号分配、内存环形缓冲、`events.jsonl` 落盘与回读 |
| `src/tripplan/web/templates/` | Jinja2：列表页、详情页 |
| `src/tripplan/web/static/` | CSS + 轮询用的原生 JS |

### 3.2 改动面

- `cli.py`：删 `plan` / `resume` / `terminal_ask` / 交互版 `drive` / `_resolve_candidate_key`；
  新增 `trip web`；`render` 原样保留
- `orchestrator.advance()` / `slot.run_slot()` / `agents.limits.SlotContext`：
  各加一个可选的取消令牌参数（默认 `None`，现有调用与测试不受影响）
- `slot.py` / `orchestrator.py`：补 4-5 个 `emit` 点（纯增量，不改签名）
- `pyproject.toml`：`[project.optional-dependencies] web = ["flask", "waitress"]`

`state` / `repo` / `render` / `agents`（除 limits 的取消参数）/ `llm` 的逻辑不改。

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

job 线程体的完整逻辑：

```python
state = repo.load()            # 每次从盘上重读，命令之间不在内存留 state
persisted = state.revision     # CAS 的 expected
outcome = advance(state, deps, cmd, job.emit, cancel=job.cancel_token)

if isinstance(outcome, Rejected):
    job.rejected = outcome.reason
    return          # state 未变、revision 未变 → 不落盘。
                    # 写盘只会白占一次 CAS 窗口，让无辜的并发调用被误杀
                    # （见 cli.drive() 里同一处的注释）

if not repo.save_if_revision(state, persisted):
    job.error = "另一个进程改动了这个行程"
    return          # 绝不写产物：盘上的结局不是我们手上这份

if isinstance(outcome, Done):
    write_artifacts(state, repo.dir, deps.provider)
```

「每次从盘上重读 state」让命令之间**完全无内存状态**：CAS 纪律自动成立，
服务器重启后的接续也自动成立。

### 4.2 并发约束

| 约束 | 保证方式 |
|---|---|
| 同一 trip 同时只有一个 job | `JobRegistry` 用一把锁护住 dict；已有 running job 则拒绝（409），页面按钮同时置灰 |
| 不同 trip 可并行 | 各自一个线程，互不相干 |
| 后台线程不占 waitress 线程池 | 自己 `threading.Thread`；请求线程只负责登记后立刻返回 |
| Web 层必须单进程 | 见 §6.4 |

为什么 per-trip 必须互斥：用户手快点两下「确认」会让两个线程同时对同一个 state 跑
`advance()`。`repo` 的 CAS 保证数据不会坏——但输掉的那个线程**已经把 LLM 的钱烧完了**
才发现自己白干。互斥是为了省钱，不是为了数据一致性。

### 4.3 取消

`SlotContext.cancel()` 与 `ctx.check()` 已经存在，`check()` 在 `runner.py` 的 agent
循环里每个 LLM turn 调一次。但 `SlotContext` 是在 `orchestrator._step_ctx()` 和
`slot.run_slot()` 内部**现场创建**的，Web 层拿不到句柄。

因此需要一个贯穿的取消令牌：`advance()` 加一个可选参数，一路传到 `SlotContext`，
`check()` 里多查一个条件。默认 `None`，不影响任何现有调用。

**语义要如实告诉用户**：取消不是立刻生效，要等到下一个 `ctx.check()` 检查点
（典型十几秒）。页面显示「正在停止…」，而不是立刻宣布已停止。

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
3. **`EventLog.since(n)` 是唯一读出口** —— 下期若要加 SSE，只是给同一个 `EventLog`
   再开一个出口（浏览器的 `Last-Event-ID` 就是 `seq`），轮询那条路一行不用动。

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

### 5.5 刷新与续传

「浏览器刷新后接着上次」的实现**不靠前端缓存，靠服务端有日志**：

- 详情页服务端渲染时，就把 `EventLog` 里的**全部历史事件**写进 HTML。刷新后一进来
  就是完整的，不是空白等推送。
- 前端从最后一条的 `seq` 往后接。

这个能力与传输方式无关：轮询用 `?since=N`，SSE 用 `Last-Event-ID`，同一个 buffer
两种取法。

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
| `AWAIT_REQ_CONFIRM` | `render_requirement_card()` + 「确认」按钮 + 「说要改什么」textarea |
| `AWAIT_CHOICE` | `render_candidates()` + 每条候选一个「选它」按钮 + 提意见 textarea |
| `COLLECT` / `GENERATE` / `REFINE` | 「正在工作」+ 取消按钮 + 进度区（其余按钮置灰） |
| `DONE` | 成稿摘要 + 跳 `/trips/<tid>/itinerary` 的链接 |

页面底部固定一块**进度区**，服务端渲染时写入全部历史事件（见 §5.5）。

#### `GET /trips/<tid>/itinerary` —— 成稿页（内嵌地图）

直接 `send_file(trips/<tid>/itinerary.html)`。该文件在 job 跑到 `DONE` 时由后台线程
调 `write_artifacts()` 写好，所以页面秒开，**不会在请求里现算高德静态地图**。

### 6.2 动作类（表单 POST，成功一律 302）

#### `POST /trips` —— 新建行程

| 字段 | 类型 | 说明 |
|---|---|---|
| `request` | str，必填 | 自然语言需求，多行 textarea |
| `dir` | str，可选 | 目录名；留空则用现有的 `slugify(request)` |

- 成功 → `302 /trips/<tid>`，后台线程立刻跑 `advance(state, deps, None, emit)`
- `request` 为空 → `400`，回列表页并保留已输入内容
- `TripExists` → `409`，提示「已存在」并给直达链接

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

- 成功 → `302 /trips/<tid>`，后台线程跑 `advance(state, deps, cmd, emit, cancel)`
- 该行程已有 job 在跑 → `409`（按钮本已置灰，这是兜底）
- `advance` 返回 `Rejected` → **不是 HTTP 错误**，job 记下 `reason`，详情页顶部
  显示告警条

**顺带消除的一个 bug 面**：`angle_key` 来自页面上渲染的按钮 value，是真实 key。
CLI 里 `_resolve_candidate_key()` 那整块「大小写兜底」逻辑（连同它注释里描述的
「用户被困死只能 Ctrl-C」的场景）在 Web 下从根上不存在——用户不再手敲 key。

#### `POST /trips/<tid>/cancel` —— 取消

- 输入：CSRF 隐藏字段
- 行为：设置 job 的取消令牌
- 输出：`302 /trips/<tid>`，页面显示「正在停止…」
- 语义见 §4.3：不是立刻生效

### 6.3 数据类（JSON，供轮询）

#### `GET /trips/<tid>/events?since=<seq>`

输入：`since`，int，默认 0。

输出：

```json
{
  "events": [
    {"seq": 12, "ts": 1757900000.1, "type": "generating",
     "payload": {"args": ["foodie"]}, "stream_id": null}
  ],
  "last_seq": 12,
  "running": true,
  "stage": "GENERATE",
  "revision": 7,
  "error": null
}
```

`error` 非空时形如 `{"kind": "ProviderError", "message": "..."}`——后台线程抛出的
`ProviderError` / `LimitExceeded` 在这里暴露，对应 `cli.main()` 里那两个 except 分支
的职责。

**前端逻辑只有三条**，刻意做得极薄：

1. 每秒拉一次，`since` 带上本地 `last_seq`
2. `events` 里的新事件追加到进度区（认 `stream_id`：同 id 拼进同一个块）
3. 若 `running` 由 true 变 false，或 `revision` 变了，或 `error` 非空 → `location.reload()`

**前端不需要知道任何业务状态怎么渲染**，它只判断「要不要刷新」。所有渲染在服务端
Jinja2。这也让下期接 SSE 时前端改动面极小。

### 6.4 硬约束：Web 层必须单进程

`JobRegistry` 是**进程内单例**。多 worker 会让轮询请求被路由到没有该 job 的进程，
进度页随机失灵、取消按钮随机失效。

**不许换成 gunicorn 开多 worker**。waitress 是单进程多线程，registry 天然是一份。
这是选型的实质理由，不是偏好。

### 6.5 鉴权

**HTTP Basic Auth**：零登录页、浏览器原生记住、一个 `before_request` 搞定。
用户名固定 `trip`，密码取环境变量 `TRIPPLAN_WEB_TOKEN`。

**启动时硬约束**：`TRIPPLAN_WEB_TOKEN` 未设置时，拒绝绑 `0.0.0.0`，只能
`127.0.0.1`，并给一句中文提示说明怎么设。不给「裸奔到局域网」留口子。

表单另带 per-session 的 CSRF 隐藏字段。

**如实记录代价**：局域网上是明文 HTTP，Basic Auth 凭据可被同网段嗅探。考虑到这是
家庭/办公网段 + 自生成口令 + 保护的是行程规划而非资金，判定为可接受。要更强就得
上 HTTPS，那是另一个量级的工作，不在本期范围。

## 7. CLI 最终形态

只剩两个子命令：

| 命令 | 说明 |
|---|---|
| `trip web` | 启动服务。参数：`--host`（默认 `127.0.0.1`）、`--port`（默认 8000）、`--trips-dir`（默认 `trips`） |
| `trip render <dir>` | 从 `state.json` 重新生成产物，不碰 LLM。原样保留，含 `--format` |

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

`jobs.py` 与 `events.py` 是**纯内存对象，不依赖 Flask**——直接单测，不起服务器。
路由层用 `app.test_client()`，并把 `advance` 做成可注入依赖（沿用
`drive(..., advance_fn=)` 已在用的同一手法），测试注入假 advance，不碰 LLM 与高德。

要守住的回归，每条对应上文一个具体决定：

1. **per-trip 互斥**（§4.2）—— 同一 trip 连发两次命令，第二次 409，`advance` 只被调用一次
2. **刷新续传**（§5.5）—— 详情页 HTML 含全部历史事件；`?since=N` 只返回增量且序号连续
3. **`durable=False` 不落盘**（§5.3）—— 瞬时事件在 `since()` 可读到，但不在 `events.jsonl` 里
4. **重启接续**（§5.4）—— 从已有 `events.jsonl` 构造 `EventLog`，新事件 seq 从旧最大值往后接
5. **路径穿越被拒**（§6.0）—— `/trips/..%2f..%2fetc%2fpasswd` 返回 404
6. **裸奔保护**（§6.5）—— 未设 `TRIPPLAN_WEB_TOKEN` 时 `trip web --host 0.0.0.0` 启动失败并给中文提示
7. **CAS 输掉不写产物**（§4.1）—— 模拟 `save_if_revision` 返回 False，确认 `write_artifacts` 未被调用
8. **取消生效**（§4.3）—— 设置取消令牌后 `ctx.check()` 抛 `LimitExceeded("已取消")`
9. **坏目录不炸列表页**（§6.1）—— `trips/` 下放损坏的 `state.json`，列表页仍 200 且标为「损坏」
10. **`Rejected` 不落盘**（§4.1）—— `advance` 返回 `Rejected` 时 `save_if_revision` 未被调用

现有 `tests/test_cli.py` 相应删改：交互相关用例（`terminal_ask`、`EOFError` 分支、
`_resolve_candidate_key`）随代码一起删；`render` 与凭据装配相关的保留。

## 10. 已知限制

1. **取消不是立刻生效**（§4.3）。要等到下一个 `ctx.check()` 检查点，典型十几秒。
   页面必须如实显示「正在停止…」，不能假装已停。
2. **关服务会丢当前这一步**。Ctrl-C 停服时正在跑的 `advance()` 直接消失。损失有限：
   `drive()` 的纪律是每到暂停点先落盘再问人，所以上一个暂停点的进度还在
   `state.json` 里，重启后从列表页点进去就能接着跑。丢的只是正在进行的那一步。
3. **Web 层必须单进程**（§6.4）。
4. **局域网明文 HTTP**（§6.5）。
5. **进度信号偏粗**。v1 是阶段级里程碑，不是 token 级。token 级是下一期（§5.3）。
