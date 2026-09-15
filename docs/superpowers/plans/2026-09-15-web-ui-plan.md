# Web 界面替代 CLI 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把交互入口从终端 `input()` 搬到浏览器，`orchestrator` 获得第二个与 CLI 平级的 driver，CLI 只保留 `trip web` 与 `trip render` 两个非交互命令。

**Architecture:** Flask（服务端渲染 HTML）+ waitress（单进程多线程）。CLI 的 `while` 循环被拆开交给 HTTP：**每一次用户动作 = 恰好一次 `advance()`**，跑完就线程退出，下一次动作由下一个 POST 起一个新线程。`web/jobs.py` 是框架无关的调度层（不 import flask），负责「load → advance → 暂存产物 → CAS → 原子发布 → 记终态」这一套提交纪律；`web/app.py` 只做鉴权、校验、调 registry、渲染。进度靠 `web/events.py` 的 `EventLog`（磁盘 durable 历史 + 内存 live ring）+ 1 秒轮询。

**Tech Stack:** Python 3.12、Flask、waitress、Jinja2（autoescape 全开）、原生 JS（零构建、零框架）、pytest、dataclasses、`threading`。

**Spec:** `docs/superpowers/specs/2026-09-15-web-ui-design.md`

---

## Global Constraints

这些约束适用于**每一个**任务，不再在任务内重复。

1. **`Cancelled` 不许被吞。** 新异常 `Cancelled` **不继承 `LimitExceeded`、不继承任何被现有代码捕获的类型**。每一处宽泛捕获（`except Exception` / `except LimitExceeded` / `except ProviderError` / `except (ValueError, ProviderError, LimitExceeded)`）前面都必须先 `except Cancelled: raise`。漏一处的后果是：用户点了取消，系统给他**写进盘里**一份「候选全部生成失败」的行程，revision 还涨一格（spec §4.3）。
2. **`active` 谓词只有一个定义**：`job.status in {"running", "cancelling"}`。**不许有任何一处写成只判断 `running`**——`cancelling` 的线程还活着、还在烧钱、还握着那份 state（spec §4.2）。互斥、全局上限、产物重建前置、终态清理，四处共用这一个谓词。
3. **产物纪律**：慢且可能失败的渲染 + 拉图必须发生在 CAS **之前**，写进 `<trip>/.staging/<stage_id>/`（按写者隔离，不是 trip 共享）；CAS 成功后只剩一串 `os.replace` + 最后写 `artifacts.json`。CAS 输掉 / 已取消 → `discard()`，**绝不发布**（spec §4.1）。
4. **Jinja autoescape 全程开着，`web/templates/` 里不出现任何 `|safe` / `Markup(...)`。** 模板只吃 `web/view.py` 摊出来的结构化对象，绝不把 `render/requirement_card.py` / `render/candidates.py` 的 Markdown 串塞进页面（spec §6.1）。唯一例外是 `send_file` 那份独立的 `itinerary.html` 文档，它根本不过模板。
5. **`web/jobs.py` 与 `web/events.py` 不 import flask**，不碰 `request` / `session`。测试直接单测，不起服务器（spec §9）。
6. **`job.emit()` 是不抛异常的边界**：内部 `try/except Exception` + `logging.exception`，绝不外抛。事件日志是给人看的进度历史，**不是权威状态**，权威永远是 `state.json`（spec §5.1.1 / §5.4）。
7. **`tid` 校验**：必须是单个路径段（不含 `/`、`\`、`..`、`\0`），且 `(trips_root/tid).resolve()` 必须是 `trips_root.resolve()` 的直接子目录（spec §6.0）。这是对局域网暴露的服务的硬要求。
8. **输入体积上限**：`MAX_CONTENT_LENGTH = 64 KiB`；`request` / `text` 各限 8000 字符；`dir` 限 80 字符；`angle_key` 限 64 字符（spec §6.5）。
9. **不做的事**：token 级流式输出、SSE、下载按钮、用户体系、HTTPS。但 `Event` 的 `durable` / `stream_id` 字段和 `EventLog.since()` 的 `first_seq` / `stream_epoch` / `reset_required` **这一期就要有**，它们实质影响 v1 的事件模型（spec §5.3）。
10. **命令的工作目录与解释器**。下面每一条 `Run:` / bash 块都按这两条执行，任务里不再重复：
    - **cwd 一律是 worktree 根目录** `/Users/jialiu/Projects/trip-plan/.worktrees/20260915-015213-web-ui`（`src` / `tests` 这些相对参数都从这里算）；
    - **解释器与 black 一律用主仓库虚拟环境的绝对路径**：
      `PYTHON=/Users/jialiu/Projects/trip-plan/.venv/bin/python`、
      `BLACK=/Users/jialiu/Projects/trip-plan/.venv/bin/black`。

    **worktree 下没有也不要建 `.venv`**，所以计划里不出现 `.venv/bin/...` 这种相对写法——照着敲会直接
    `no such file or directory`。用主仓库的 venv 跑 worktree 的代码是安全的，不是将就：`pyproject.toml` 里
    `[tool.pytest.ini_options] pythonpath = ["src"]` 是**相对 rootdir** 的，而 cwd 在 worktree 时 rootdir 就是
    worktree，pytest 会把 `<worktree>/src` 插到 `sys.path[0]`，压过那个 venv 里指向主仓库 `src` 的
    editable `.pth`。已实测：在 worktree 下用该解释器跑 pytest，`tripplan.__file__` 落在
    `<worktree>/src/tripplan/__init__.py`，`620 passed, 1 deselected` 与基线一致。**不要脱离 pytest 直接
    `python -c "import tripplan"`**——那条路没有 pytest 的 pythonpath，会 import 到主仓库的旧代码。
11. **格式化**：每次改完代码跑 `$BLACK src tests`（CLAUDE.md 要求）。
12. **基线**：当前 `620 passed, 1 deselected`。每个任务结束时，除该任务有意改写的测试外全部保持绿。
13. **TDD**：先写测试、看它以正确理由失败、再写最小实现、再看它通过、然后提交。

---

## 文件结构

**新建**

| 文件 | 职责 |
|---|---|
| `src/tripplan/artifacts.py` | `stage_artifacts()` / `publish()` / `discard()` / `artifact_ready()` / `sweep_stale_staging()`。从 `cli.write_artifacts` 抽出来并拆成三步，避免 `web/` 反向依赖 `cli/` |
| `src/tripplan/naming.py` | `slugify()`。同样是为了 `web/` 不 import `cli` |
| `src/tripplan/agents/_emit.py` | `safe_emit(emit, event)`。从 `slot.py` 抽出，供 orchestrator / diversity 共用 |
| `src/tripplan/web/__init__.py` | 空 |
| `src/tripplan/web/events.py` | `Event` / `EventLog` / `EventLogStore` / `Snapshot` / `SinceResult` |
| `src/tripplan/web/jobs.py` | `JobOutcome` / `TripJob` / `JobRegistry` / `run_command()` / `rebuild_artifacts()` / `TripBusy` / `ServerBusy` |
| `src/tripplan/web/view.py` | `ReqCardVM` / `CandidateVM` / `TripRowVM` / `IssueVM` / `event_text()` |
| `src/tripplan/web/app.py` | Flask 应用工厂 + 全部路由 |
| `src/tripplan/web/templates/` | `base.html` / `index.html` / `detail.html` / `notice.html` |
| `src/tripplan/web/static/` | `app.css` / `app.js` |

**修改**

| 文件 | 内容 |
|---|---|
| `src/tripplan/agents/limits.py` | 新增 `Cancelled`、`raise_if_cancelled()`；`SlotContext` 收外部取消令牌 |
| `src/tripplan/slot.py` | `_safe_emit` 迁到 `agents/_emit.py`；`run_slot` 收 `cancel`；两处宽泛捕获前加 `except Cancelled: raise` |
| `src/tripplan/orchestrator.py` | `advance` / `_apply` / `_run_to_pause` / `_step_ctx` / `_safe_slot` 一路传 `cancel`；三处宽泛捕获前加 `except Cancelled: raise`；裸 `emit` 换 `safe_emit`；补 5 个 emit 点 |
| `src/tripplan/validation/diversity.py` | 裸 `emit` 换 `safe_emit` |
| `src/tripplan/render/requirement_card.py` | `_LABELS` 改名为公开的 `FIELD_LABELS`（只改名，渲染逻辑一个字不动），供 `web/view.py` 复用同一份标签数据 |
| `src/tripplan/cli.py` | 先加后删，分两个任务：Task 15 新增 `trip web`（`plan` / `resume` 原样留着），Task 16 才删 `plan` / `resume` / `terminal_ask` / `drive` / `_resolve_candidate_key` / `write_artifacts`；`render` 改调 `artifacts.*`（Task 4） |
| `pyproject.toml` | `[project.optional-dependencies] web = ["flask", "waitress"]`，`dev` 追加这两项 |
| `uv.lock` | 跟着 `pyproject.toml` 一起 `uv lock` 重新生成并提交——只改声明不改锁文件，`uv lock --check` / `uv sync --locked` 当场就红（Task 9） |

**测试**

| 文件 | 内容 |
|---|---|
| `tests/test_artifacts.py` | 新建，含从 `test_cli.py` 搬过来的产物用例 |
| `tests/web/__init__.py` | 新建，空 |
| `tests/web/test_events.py` | 新建 |
| `tests/web/test_jobs.py` | 新建 |
| `tests/web/test_view.py` | 新建 |
| `tests/web/conftest.py` | 新建：`app` / `client` / `trips_root` 夹具 |
| `tests/web/test_app_auth.py` | 新建 |
| `tests/web/test_routes.py` | 新建 |
| `tests/test_cli.py` | 删交互相关用例，保留 `render` 与凭据装配 |

---

## Task 1: `Cancelled` 异常与 `SlotContext` 的外部取消令牌

**Files:**
- Modify: `src/tripplan/agents/limits.py`
- Test: `tests/agents/test_limits.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `class Cancelled(Exception)` —— 不继承 `LimitExceeded`
  - `def raise_if_cancelled(token) -> None` —— `token` 是任何带 `.is_set() -> bool` 的对象（`threading.Event`），`None` 表示没有取消通道
  - `SlotContext(limits, clock=time.monotonic, emit=_noop, cancel=None)` —— 新增第四个关键字参数 `cancel`
  - `SlotContext.check()` 取消时抛 `Cancelled("已取消")`，其余额度仍抛 `LimitExceeded`

- [ ] **Step 1: 写失败的测试**

把 `tests/agents/test_limits.py` 里现有的 `test_cancel_stops_the_slot` 整段替换成下面这组（它原来断言 `LimitExceeded`，正是这一期要推翻的行为）：

```python
def test_cancel_raises_cancelled_not_limit_exceeded():
    """取消必须是独立信号。继承或复用 LimitExceeded 会让它在 slot.py:76
    被转成一个 EXHAUSTED 候选，用户按了「取消」却被写进盘里一份
    「候选全部生成失败」的行程（spec §4.3）。"""
    from tripplan.agents.limits import Cancelled

    ctx = SlotContext(SlotLimits())
    ctx.cancel()
    with pytest.raises(Cancelled, match="已取消"):
        ctx.check()


def test_cancelled_is_not_a_limit_exceeded_subclass():
    """这条不是重复：上一条用 pytest.raises(Cancelled) 断言类型，
    而 Cancelled 如果继承了 LimitExceeded，上一条照样通过。"""
    from tripplan.agents.limits import Cancelled

    assert not issubclass(Cancelled, LimitExceeded)


def test_external_cancel_token_stops_the_slot():
    """Web 层拿不到 SlotContext 的句柄（它在 orchestrator 内部现场创建），
    所以取消必须靠一个从外面传进来的令牌（spec §4.3）。"""
    import threading

    from tripplan.agents.limits import Cancelled

    token = threading.Event()
    ctx = SlotContext(SlotLimits(), cancel=token)
    ctx.check()  # 未取消：不抛
    token.set()
    with pytest.raises(Cancelled):
        ctx.check()


def test_budget_limits_still_raise_limit_exceeded_when_a_token_is_present():
    """反证：带取消令牌不会把额度错误也改成 Cancelled。"""
    import threading

    from tripplan.agents.limits import Cancelled

    ctx = SlotContext(SlotLimits(max_output_tokens=100), cancel=threading.Event())
    ctx.charge(Usage(0, 500))
    with pytest.raises(LimitExceeded) as exc:
        ctx.check()
    assert not isinstance(exc.value, Cancelled)


def test_raise_if_cancelled_tolerates_a_missing_token():
    """None 表示「没有取消通道」——CLI 与现有测试都走这条路，不能崩。"""
    import threading

    from tripplan.agents.limits import Cancelled, raise_if_cancelled

    raise_if_cancelled(None)  # 不抛
    raise_if_cancelled(threading.Event())  # 未 set：不抛
    token = threading.Event()
    token.set()
    with pytest.raises(Cancelled):
        raise_if_cancelled(token)
```

- [ ] **Step 2: 跑测试确认它失败**

Run: `cd /Users/jialiu/Projects/trip-plan/.worktrees/20260915-015213-web-ui && /Users/jialiu/Projects/trip-plan/.venv/bin/python -m pytest tests/agents/test_limits.py -v`
Expected: FAIL，`ImportError: cannot import name 'Cancelled' from 'tripplan.agents.limits'`

- [ ] **Step 3: 写最小实现**

在 `src/tripplan/agents/limits.py` 的 `LimitExceeded` 之后插入：

```python
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
```

`SlotContext.__init__` 改成：

```python
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
```

`check()` 的第一段改成：

```python
    def check(self) -> None:
        if self._cancelled or (self._cancel is not None and self._cancel.is_set()):
            raise Cancelled("已取消")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `/Users/jialiu/Projects/trip-plan/.venv/bin/python -m pytest tests/agents/test_limits.py -v && /Users/jialiu/Projects/trip-plan/.venv/bin/python -m pytest -q`
Expected: `tests/agents/test_limits.py` 全绿；全量 `620 passed`（本任务只改写了一条既有用例并新增四条，净增 4 条 → `624 passed, 1 deselected`）

- [ ] **Step 5: 格式化并提交**

```bash
cd /Users/jialiu/Projects/trip-plan/.worktrees/20260915-015213-web-ui
/Users/jialiu/Projects/trip-plan/.venv/bin/black src tests
git add src/tripplan/agents/limits.py tests/agents/test_limits.py
git commit -m "feat(limits): 取消改成独立的 Cancelled 信号 + 外部取消令牌"
```

---

## Task 2: `agents/_emit.py` —— 把 `emit` 变成不拖垮主干的旁路

**Files:**
- Create: `src/tripplan/agents/_emit.py`
- Modify: `src/tripplan/slot.py:20-27`（删掉本地 `_safe_emit`，改 import）
- Modify: `src/tripplan/orchestrator.py:201`、`src/tripplan/orchestrator.py:289`
- Modify: `src/tripplan/validation/diversity.py:87`
- Test: `tests/agents/test_emit.py`（新建）、`tests/test_advance_flow.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `Cancelled`
- Produces: `def safe_emit(emit, event) -> None` —— 吞掉 emit 自己抛的一切 `Exception`，但 `Cancelled` 原样放行

- [ ] **Step 1: 写失败的测试**

新建 `tests/agents/test_emit.py`：

```python
import pytest

from tripplan.agents._emit import safe_emit
from tripplan.agents.limits import Cancelled


def test_safe_emit_swallows_exceptions_from_the_callback():
    def boom(_event):
        raise RuntimeError("磁盘满")

    safe_emit(boom, ("generating", "A"))  # 不抛


def test_safe_emit_passes_the_event_through_when_the_callback_works():
    seen = []
    safe_emit(seen.append, ("generating", "A"))
    assert seen == [("generating", "A")]


def test_safe_emit_lets_cancelled_through():
    """宽泛捕获前必须先放行 Cancelled（Global Constraint 1）。
    safe_emit 自己也是一处 except Exception，同样受这条纪律约束。"""

    def cancelled(_event):
        raise Cancelled("已取消")

    with pytest.raises(Cancelled):
        safe_emit(cancelled, ("generating", "A"))
```

在 `tests/test_advance_flow.py` 末尾追加（spec §9 回归 13）：

```python
def test_a_throwing_emit_never_derails_the_state_machine(wire, tmp_path):
    """spec §5.1.1 / §9 回归 13。

    orchestrator.py:201 的裸 emit 尤其要命：它在 _patch_requirements 里，
    此刻 state.requirements 已经被 patch 进去、state.revision 还没递增。
    异常从这里穿出去，留下的是一个既不算「拒绝」也不算「修改」的半吊子
    state——正是 orchestrator.py 开头那段 docstring 明令不许出现的东西。
    磁盘满不该有能力把状态机搞歪。
    """

    def boom(_event):
        raise RuntimeError("events.jsonl 只读")

    wire(_Fakes(delta=FeedbackDelta(True, {"destination": "巴黎"}, Scale.REWRITE)))
    s = _at_choice(chosen="A")
    before = s.revision

    outcome = advance(s, _deps(), GiveFeedback(before, "A", "改去巴黎"), boom)

    assert not isinstance(outcome, Rejected)
    assert s.revision == before + 1                   # 递增照常发生
    assert s.requirements.destination.value == "巴黎"  # patch 落到位
    assert s.stage is Stage.AWAIT_CHOICE               # 没有卡在半吊子状态


def test_a_throwing_emit_does_not_break_angle_failure_or_diversity_paths(wire):
    """另外两处裸 emit：orchestrator.py:289（角度生成失败）与
    validation/diversity.py:87（重跑提示）。"""

    def boom(_event):
        raise RuntimeError("磁盘满")

    fakes = _Fakes()
    fakes.pick_angles = lambda reqs, deps, ctx=None, n=3: (_ for _ in ()).throw(
        ValueError("一个角度都没解析出来")
    )
    wire(fakes)

    s = _at_choice()
    s.stage = Stage.GENERATE
    s.candidates = []

    outcome = advance(s, _deps(), None, boom)

    assert isinstance(outcome, NeedInput)
    assert s.stage is Stage.AWAIT_CHOICE
    assert s.revision == 6                      # _at_choice 起点是 5
    assert s.candidates[0].status is SlotStatus.FAILED
```

- [ ] **Step 2: 跑测试确认它失败**

Run: `/Users/jialiu/Projects/trip-plan/.venv/bin/python -m pytest tests/agents/test_emit.py tests/test_advance_flow.py -v`
Expected: `test_emit.py` 全部 `ModuleNotFoundError: No module named 'tripplan.agents._emit'`；两条 `advance_flow` 用例 FAIL with `RuntimeError`（裸 emit 直接把异常放穿了）

- [ ] **Step 3: 写最小实现**

新建 `src/tripplan/agents/_emit.py`：

```python
"""进度回调的安全外壳。

emit 点散布在业务主干上，而回调实现（Web 的 EventLog）要做 JSON 序列化、
追加写、flush，每一步都可能抛：payload 里混进不可序列化的对象、磁盘满、
events.jsonl 被删或被改成只读。事件日志是给人看的进度历史，不是权威状态
（spec §5.4），它没有资格决定一次规划算不算数。

两道防线是刻意的：Web 侧的 job.emit() 自己就是不抛异常的边界（spec §5.1.1），
这里再兜一层——即使将来有人换上一个会抛的 emit 实现，主干也不被拖下水。
"""

from tripplan.agents.limits import Cancelled


def safe_emit(emit, event) -> None:
    try:
        emit(event)
    except Cancelled:
        raise  # ★ 宽泛捕获前先放行取消（Global Constraint 1）
    except Exception:
        pass
```

`src/tripplan/slot.py`：删掉本地 `_safe_emit` 定义（第 20-27 行），改成从新模块导入并沿用原名，调用点一个字不动：

```python
from tripplan.agents._emit import safe_emit as _safe_emit
```

`src/tripplan/orchestrator.py`：顶部加 `from tripplan.agents._emit import safe_emit`，把两处裸调用换掉：

```python
    safe_emit(emit, ("requirements_patched", delta.patch))  # 原第 201 行
```

```python
                    safe_emit(emit, ("angle_generation_failed", str(e)))  # 原第 289 行
```

`src/tripplan/validation/diversity.py`：顶部加 `from tripplan.agents._emit import safe_emit`，第 87 行换成：

```python
        safe_emit(emit, ("diversity_retry", result[j].angle.key, sorted(overlap)))
```

- [ ] **Step 4: 跑测试确认通过**

Run: `/Users/jialiu/Projects/trip-plan/.venv/bin/python -m pytest tests/agents/test_emit.py tests/test_advance_flow.py tests/test_slot.py tests/validation/test_diversity.py -v && /Users/jialiu/Projects/trip-plan/.venv/bin/python -m pytest -q`
Expected: 全绿，`629 passed, 1 deselected`

- [ ] **Step 5: 格式化并提交**

```bash
/Users/jialiu/Projects/trip-plan/.venv/bin/black src tests
git add src/tripplan/agents/_emit.py src/tripplan/slot.py src/tripplan/orchestrator.py \
        src/tripplan/validation/diversity.py tests/agents/test_emit.py tests/test_advance_flow.py
git commit -m "fix(emit): 三处裸 emit 改走 safe_emit，进度回调不再能搞歪状态机"
```

---

## Task 3: 取消令牌贯穿 `run_slot` / `advance`，并补齐阶段级 emit 点

**Files:**
- Modify: `src/tripplan/slot.py`（`run_slot` 签名 + 两处捕获）
- Modify: `src/tripplan/orchestrator.py`（`advance` / `_apply` / `_step_ctx` / `_safe_slot` / `_run_to_pause`）
- Test: `tests/test_cancel.py`（新建）、`tests/test_advance_flow.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `Cancelled` / `raise_if_cancelled`，Task 2 的 `safe_emit`
- Produces:
  - `run_slot(angle, seed, reqs, tz, deps, issues=(), limits=SlotLimits(), emit=_noop, avoid_poi_ids=(), cancel=None)`
  - `advance(state, deps, cmd=None, emit=_noop, cancel=None)`
  - 5 个新 emit 事件：`("stage_started", <stage 名>)`、`("angles_picked", [key…])`、`("paused", <stage 名>, <revision>)`

- [ ] **Step 1: 写失败的测试**

新建 `tests/test_cancel.py`：

```python
"""取消必须一路逃出 advance，不能被任何一层宽泛捕获吞掉（spec §4.3 / §9 回归 11）。"""

import threading
from datetime import date

import pytest

from tripplan.agents.limits import Cancelled, SlotLimits
from tripplan.deps import Deps
from tripplan.models.common import Field, Origin
from tripplan.models.itinerary import Angle, Itinerary
from tripplan.models.requirements import DateRange, Party, Requirements
from tripplan.orchestrator import advance
from tripplan.providers.fake import FakeProvider
from tripplan.slot import run_slot
from tripplan.state import Stage, TripState

D1 = date(2026, 10, 1)


def _reqs():
    return Requirements(
        destination=Field("京都", Origin.USER),
        dates=Field(DateRange(D1, D1), Origin.USER),
        party=Field(Party(adults=2), Origin.USER),
    )


def _generate_state():
    s = TripState.new("去京都", run_id="r1")
    s.stage, s.revision, s.requirements = Stage.GENERATE, 2, _reqs()
    s.trip_timezone = "Asia/Tokyo"
    return s


def test_run_slot_lets_cancelled_escape_its_two_broad_catches(monkeypatch):
    """slot.py 的 except LimitExceeded / except ProviderError 都在 Cancelled
    之后——漏掉哪一个，取消都会变成一个带 detail 的 EXHAUSTED/FAILED 候选。"""

    def cancelled_generate(reqs, angle, deps, ctx, avoid_poi_ids=()):
        raise Cancelled("已取消")

    monkeypatch.setattr("tripplan.slot.generate", cancelled_generate)

    with pytest.raises(Cancelled):
        run_slot(
            angle=Angle("A", "方案A", ""),
            seed=None,
            reqs=_reqs(),
            tz="Asia/Tokyo",
            deps=Deps(client=None, provider=FakeProvider()),
        )


def test_the_token_reaches_the_slot_context(monkeypatch):
    """令牌要真的一路传到 SlotContext，而不是只在 advance 的签名上摆着。"""
    seen = {}

    def spy_generate(reqs, angle, deps, ctx, avoid_poi_ids=()):
        seen["cancelled"] = True
        ctx.check()  # 令牌已 set → 必须抛 Cancelled
        return Itinerary(angle=angle)

    monkeypatch.setattr("tripplan.slot.generate", spy_generate)

    token = threading.Event()
    token.set()
    with pytest.raises(Cancelled):
        run_slot(
            angle=Angle("A", "方案A", ""),
            seed=None,
            reqs=_reqs(),
            tz="Asia/Tokyo",
            deps=Deps(client=None, provider=FakeProvider()),
            cancel=token,
        )
    assert seen["cancelled"]


def test_cancel_escapes_advance_and_leaves_no_failed_candidates(monkeypatch):
    """§9 回归 11 的正题：Cancelled 必须穿过 slot.py 的 except LimitExceeded
    与 _safe_slot 的 except Exception 逃出 advance；state.candidates 不能
    被写成一串 FAILED，revision 不能涨。"""

    def cancelled_run_slot(**kw):
        raise Cancelled("已取消")

    monkeypatch.setattr(
        "tripplan.orchestrator.pick_angles",
        lambda reqs, deps, ctx=None, n=3: [Angle(k, f"方案{k}", "") for k in "ABC"],
    )
    monkeypatch.setattr("tripplan.orchestrator.run_slot", cancelled_run_slot)

    state = _generate_state()
    with pytest.raises(Cancelled):
        advance(state, Deps(client=None, provider=FakeProvider()), None)

    assert state.revision == 2          # 没有递增
    assert state.candidates == []       # 没有一串 FAILED 占位
    assert state.stage is Stage.GENERATE


def test_cancel_is_checked_between_candidates(monkeypatch):
    """检查点之一：候选与候选之间。否则取消要等当前这条候选线彻底跑完
    （含 revise + critic）才可能生效（spec §4.3）。"""
    token = threading.Event()
    calls = []

    def one_then_cancel(**kw):
        calls.append(kw["angle"].key)
        token.set()  # 第一条候选跑完就取消
        from tripplan.state import CandidateSlot, SlotStatus

        return CandidateSlot(kw["angle"], Itinerary(angle=kw["angle"]), None, SlotStatus.OK)

    monkeypatch.setattr(
        "tripplan.orchestrator.pick_angles",
        lambda reqs, deps, ctx=None, n=3: [Angle(k, f"方案{k}", "") for k in "ABC"],
    )
    monkeypatch.setattr("tripplan.orchestrator.run_slot", one_then_cancel)

    state = _generate_state()
    with pytest.raises(Cancelled):
        advance(state, Deps(client=None, provider=FakeProvider()), None, cancel=token)

    assert calls == ["A"]  # 第二条根本没起跑


def test_cancel_before_angles_escapes_the_pick_angles_catch(monkeypatch):
    """_run_to_pause 里 pick_angles 的 except (ValueError, ProviderError,
    LimitExceeded) 也得先放行 Cancelled，否则取消会被收敛成一个
    「角度生成失败」的 FAILED 占位候选。"""

    def cancelled_pick(reqs, deps, ctx=None, n=3):
        raise Cancelled("已取消")

    monkeypatch.setattr("tripplan.orchestrator.pick_angles", cancelled_pick)

    state = _generate_state()
    with pytest.raises(Cancelled):
        advance(state, Deps(client=None, provider=FakeProvider()), None)

    assert state.candidates == []
    assert state.revision == 2
```

在 `tests/test_advance_flow.py` 末尾追加：

```python
def test_advance_emits_stage_milestones(wire):
    """spec §5.2：v1 在 _run_to_pause 的阶段边界补 emit 点，不动 agent 内部。"""
    wire(_Fakes())
    events = []

    state = TripState.new("去京都", run_id="r1")
    state.stage, state.revision = Stage.COLLECT, 0

    advance(state, Deps(client=None, provider=FakeProvider()), None, events.append)

    types = [e[0] for e in events]
    assert "stage_started" in types
    assert ("stage_started", "COLLECT") in events
    assert ("paused", "AWAIT_REQ_CONFIRM", 1) in events


def test_advance_emits_angles_picked(wire):
    wire(_Fakes(angles=("A", "B", "C")))
    events = []

    state = TripState.new("去京都", run_id="r1")
    state.stage, state.revision, state.requirements = Stage.GENERATE, 2, _reqs()
    state.trip_timezone = "Asia/Tokyo"

    advance(state, Deps(client=None, provider=FakeProvider()), None, events.append)

    assert ("stage_started", "GENERATE") in events
    assert ("angles_picked", ["A", "B", "C"]) in events
    assert ("paused", "AWAIT_CHOICE", 3) in events
```

- [ ] **Step 2: 跑测试确认它失败**

Run: `/Users/jialiu/Projects/trip-plan/.venv/bin/python -m pytest tests/test_cancel.py tests/test_advance_flow.py -v`
Expected: `test_cancel.py` 里 `test_run_slot_lets_cancelled_escape...` FAIL（Cancelled 被 `except LimitExceeded`/`except ProviderError` 之外的路径吞成候选，实际返回 `CandidateSlot` 而非抛出）；`cancel=` 相关的 FAIL with `TypeError: run_slot() got an unexpected keyword argument 'cancel'`；两条 emit 用例 FAIL（事件不存在）

- [ ] **Step 3: 写最小实现**

`src/tripplan/slot.py`：`run_slot` 加 `cancel=None`，`ctx` 带上它，两处捕获前加放行：

```python
from tripplan.agents.limits import Cancelled, LimitExceeded, SlotContext, SlotLimits
```

```python
def run_slot(
    angle,
    seed,
    reqs,
    tz,
    deps,
    issues=(),
    limits: SlotLimits = SlotLimits(),
    emit=_noop,
    avoid_poi_ids=(),
    cancel=None,
) -> CandidateSlot:
    ctx = SlotContext(limits, emit=emit, cancel=cancel)
```

```python
    except Cancelled:
        # ★ 必须排在下面两个 except 前面。掉进 except LimitExceeded 就会
        # 变成一个 EXHAUSTED 候选，取消被静默翻译成「生成失败」（spec §4.3）。
        raise
    except LimitExceeded as e:
        ...
```

`src/tripplan/orchestrator.py`：

```python
from tripplan.agents._emit import safe_emit
from tripplan.agents.limits import (
    Cancelled,
    LimitExceeded,
    SlotContext,
    SlotLimits,
    raise_if_cancelled,
)
```

```python
def advance(state, deps, cmd=None, emit=_noop, cancel=None):
    if state.stage is Stage.DONE:
        return Done(state.chosen().itinerary)

    if state.stage in AWAITING:
        if cmd is None:
            return _pending(state)
        if (bad := _validate(state, cmd)) is not None:
            return Rejected(bad, _pending(state))
    elif cmd is not None:
        return Rejected(RejectReason.WRONG_COMMAND_FOR_STAGE, _pending(state))

    if cmd is not None:
        _apply(state, cmd, deps, emit, cancel)
    outcome = _run_to_pause(state, deps, emit, cancel)
    state.revision += 1  # ★ 唯一的递增点
    safe_emit(emit, ("paused", state.stage.value, state.revision))
    return outcome
```

```python
def _step_ctx(emit, cancel=None) -> SlotContext:
    return SlotContext(
        SlotLimits(max_tool_calls=0, max_output_tokens=20_000, deadline_s=120),
        emit=emit,
        cancel=cancel,
    )
```

`_apply(state, cmd, deps, emit, cancel=None)`，两处 `_step_ctx(emit)` 改成 `_step_ctx(emit, cancel)`。

`_safe_slot` 加 `cancel=None` 并放行：

```python
def _safe_slot(
    angle, seed, reqs, tz, deps, emit, issues=(), avoid_poi_ids=(), cancel=None
):
    try:
        return run_slot(
            angle=angle,
            seed=seed,
            reqs=reqs,
            tz=tz,
            deps=deps,
            issues=issues,
            emit=emit,
            avoid_poi_ids=avoid_poi_ids,
            cancel=cancel,
        )
    except Cancelled:
        raise  # ★ 必须排在 except Exception 前面（spec §4.3）
    except Exception as e:  # noqa: BLE001 — 故意兜底：详见上面的说明
        ...
```

`_run_to_pause(state, deps, emit, cancel=None)`，五个改动点：

```python
            case Stage.COLLECT:
                safe_emit(emit, ("stage_started", "COLLECT"))
                state.requirements = collect(
                    state.raw_request, deps, _step_ctx(emit, cancel)
                )
                state.stage = Stage.AWAIT_REQ_CONFIRM
                return _pending(state)

            case Stage.GENERATE:
                safe_emit(emit, ("stage_started", "GENERATE"))
                tz = _ensure_timezone(state, deps)
                try:
                    angles = pick_angles(
                        state.requirements, deps, _step_ctx(emit, cancel)
                    )
                except Cancelled:
                    raise  # ★ 必须排在下面那个元组捕获前面
                except (ValueError, ProviderError, LimitExceeded) as e:
                    ...（原样不动）

                safe_emit(emit, ("angles_picked", [a.key for a in angles]))
                slots = []
                for a in angles:
                    # 检查点之二：候选与候选之间。否则取消要等当前这条线
                    # 彻底跑完（含 revise + critic）才可能生效（spec §4.3）。
                    raise_if_cancelled(cancel)
                    slots.append(
                        _safe_slot(
                            a,
                            state.seeds.get(a.key),
                            state.requirements,
                            tz,
                            deps,
                            emit,
                            cancel=cancel,
                        )
                    )
                state.candidates = slots
                state.candidates = enforce_diversity(
                    state.candidates,
                    lambda slot, avoid: _safe_slot(
                        slot.angle,
                        None,
                        state.requirements,
                        tz,
                        deps,
                        emit,
                        avoid_poi_ids=avoid,
                        cancel=cancel,
                    ),
                    emit=emit,
                )
                state.seeds = {}
                state.stage = Stage.AWAIT_CHOICE
                return _pending(state)

            case Stage.REFINE:
                safe_emit(emit, ("stage_started", "REFINE"))
                tz = _ensure_timezone(state, deps)
                slot = state.chosen()
                refreshed = _safe_slot(
                    slot.angle,
                    slot.itinerary,
                    state.requirements,
                    tz,
                    deps,
                    emit,
                    issues=state.issues,
                    cancel=cancel,
                )
                ...（其余原样）
```

`enforce_diversity` 自身不需要放行 `Cancelled`：它没有宽泛捕获，重跑回调就是 `_safe_slot`，已经 `raise` 了。

- [ ] **Step 4: 跑测试确认通过**

Run: `/Users/jialiu/Projects/trip-plan/.venv/bin/python -m pytest tests/test_cancel.py tests/test_advance_flow.py tests/test_slot.py -v && /Users/jialiu/Projects/trip-plan/.venv/bin/python -m pytest -q`
Expected: 全绿，`636 passed, 1 deselected`

- [ ] **Step 5: 格式化并提交**

```bash
/Users/jialiu/Projects/trip-plan/.venv/bin/black src tests
git add src/tripplan/slot.py src/tripplan/orchestrator.py tests/test_cancel.py tests/test_advance_flow.py
git commit -m "feat(cancel): 取消令牌贯穿 advance/run_slot，并补齐阶段级 emit 点"
```

---

## Task 4: `artifacts.py` —— 暂存 / 原子发布 / 丢弃 / 就绪判定

**Files:**
- Create: `src/tripplan/artifacts.py`
- Create: `tests/test_artifacts.py`
- Modify: `src/tripplan/cli.py`（`write_artifacts` 改成薄壳转调，`_cmd_render` 改走 stage+publish）
- Modify: `tests/test_cli.py`（产物相关用例搬到 `tests/test_artifacts.py`）

**Interfaces:**
- Consumes: 无
- Produces:
  - `@dataclass(frozen=True) class Staged: trip_dir: Path; stage_dir: Path; revision: int; names: tuple[str, ...]`
  - `def stage_artifacts(state, trip_dir, provider, stage_id, fmt="both") -> Staged`
  - `def publish(staged: Staged | None) -> None`
  - `def discard(staged: Staged | None) -> None`
  - `def artifact_ready(trip_dir, revision: int) -> bool`
  - `def sweep_stale_staging(trips_root, max_age_s: float = 3600.0) -> int`
  - 常量 `ARTIFACTS_JSON = "artifacts.json"`、`STAGING_DIR = ".staging"`、`FINAL_HTML = "itinerary.html"`

- [ ] **Step 1: 写失败的测试**

新建 `tests/test_artifacts.py`：

```python
"""产物的暂存与原子发布（spec §4.1 / §9 回归 14、24）。"""

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from tripplan.artifacts import (
    ARTIFACTS_JSON,
    STAGING_DIR,
    artifact_ready,
    discard,
    publish,
    stage_artifacts,
    sweep_stale_staging,
)
from tripplan.models.common import Field, Origin
from tripplan.models.facts import FactSnapshot
from tripplan.models.itinerary import Angle, Itinerary
from tripplan.models.requirements import DateRange, Party, Requirements
from tripplan.providers.fake import FakeProvider
from tripplan.state import CandidateSlot, SlotStatus, Stage, TripState

D1 = date(2026, 10, 1)
_JST = timezone(timedelta(hours=9))


def _facts():
    return FactSnapshot(
        poi_by_activity={},
        constraint_pois={},
        routes=[],
        weather={},
        trip_timezone="Asia/Tokyo",
        resolved_at=datetime(2026, 9, 1, tzinfo=_JST),
        gaps=[],
    )


def _done_state(rev=7, title="古寺"):
    s = TripState.new("去京都", run_id="r1")
    s.revision = rev
    s.requirements = Requirements(
        destination=Field("京都", Origin.USER),
        dates=Field(DateRange(D1, D1), Origin.USER),
        party=Field(Party(adults=2), Origin.USER),
    )
    angle = Angle("A", title, "")
    s.candidates = [CandidateSlot(angle, Itinerary(angle=angle), _facts(), SlotStatus.OK)]
    s.stage, s.chosen_key = Stage.DONE, "A"
    return s


def test_stage_writes_into_its_own_subdirectory_only(tmp_path):
    staged = stage_artifacts(_done_state(), tmp_path, FakeProvider(), "job-1")
    assert staged.stage_dir == tmp_path / STAGING_DIR / "job-1"
    assert (staged.stage_dir / "itinerary.html").exists()
    # 最终路径此刻必须还是空的——CAS 还没发生
    assert not (tmp_path / "itinerary.html").exists()
    assert not (tmp_path / ARTIFACTS_JSON).exists()


def test_publish_moves_files_and_writes_artifacts_json_last(tmp_path):
    state = _done_state(rev=7)
    staged = stage_artifacts(state, tmp_path, FakeProvider(), "job-1")
    publish(staged)

    assert (tmp_path / "itinerary.html").exists()
    assert (tmp_path / "itinerary.md").exists()
    data = json.loads((tmp_path / ARTIFACTS_JSON).read_text(encoding="utf-8"))
    assert data["revision"] == 7
    assert "itinerary.html" in data["files"]
    assert not staged.stage_dir.exists()  # 发布完删掉自己的暂存目录


def test_discard_removes_only_its_own_subdirectory(tmp_path):
    """§9 回归 24 的后半条。"""
    a = stage_artifacts(_done_state(title="A 的成稿"), tmp_path, FakeProvider(), "job-a")
    b = stage_artifacts(_done_state(title="B 的成稿"), tmp_path, FakeProvider(), "job-b")
    discard(a)
    assert not a.stage_dir.exists()
    assert b.stage_dir.exists()
    assert (b.stage_dir / "itinerary.html").exists()


def test_two_writers_do_not_clobber_each_other(tmp_path):
    """§9 回归 24：两个写者（Web job 与另一个进程的 trip render）从**不同的
    state** 各自暂存。最终 itinerary.html 的内容必须来自 publish() 那一方，
    而不是「revision 对得上但内容是另一份」。"""
    loser = stage_artifacts(_done_state(rev=7, title="输家的成稿"), tmp_path, FakeProvider(), "job-a")
    winner = stage_artifacts(_done_state(rev=9, title="赢家的成稿"), tmp_path, FakeProvider(), "job-b")

    discard(loser)
    publish(winner)

    html = (tmp_path / "itinerary.html").read_text(encoding="utf-8")
    assert "赢家的成稿" in html
    assert "输家的成稿" not in html
    assert json.loads((tmp_path / ARTIFACTS_JSON).read_text(encoding="utf-8"))["revision"] == 9


def test_artifact_ready_requires_matching_revision_and_existing_files(tmp_path):
    state = _done_state(rev=7)
    publish(stage_artifacts(state, tmp_path, FakeProvider(), "job-1"))

    assert artifact_ready(tmp_path, 7)
    assert not artifact_ready(tmp_path, 8)          # 旧版本产物残留
    (tmp_path / "itinerary.html").unlink()
    assert not artifact_ready(tmp_path, 7)          # 手工删了文件


def test_artifact_ready_is_false_without_any_manifest(tmp_path):
    assert not artifact_ready(tmp_path, 1)


def test_artifact_ready_survives_a_corrupt_manifest(tmp_path):
    (tmp_path / ARTIFACTS_JSON).write_text("not json", encoding="utf-8")
    assert not artifact_ready(tmp_path, 1)


def test_an_empty_manifest_is_not_ready(tmp_path):
    """`all([])` 是 True —— 照 spec §4.1 的字面写法，一个 files 为空的
    manifest 会拿到假绿灯，详情页于是亮出一个指向不存在文件的成稿链接
    （spec §6.1「不给死链」）。"""
    s = TripState.new("去京都", run_id="r1")
    s.revision = 3
    publish(stage_artifacts(s, tmp_path, FakeProvider(), "job-1"))

    assert json.loads((tmp_path / ARTIFACTS_JSON).read_text(encoding="utf-8"))["files"] == []
    assert not (tmp_path / "itinerary.html").exists()
    assert not artifact_ready(tmp_path, 3)


def test_a_markdown_only_manifest_is_not_ready(tmp_path):
    """`trip render <dir> --format md`（§7 支持的第二个写者）发布的 manifest
    里压根没有 HTML。revision 对得上、列出的文件也都在，但成稿页给不出东西。"""
    state = _done_state(rev=7)
    publish(stage_artifacts(state, tmp_path, FakeProvider(), "job-1", fmt="md"))

    assert (tmp_path / "itinerary.md").exists()
    assert not artifact_ready(tmp_path, 7)


def test_a_markdown_only_manifest_does_not_bless_a_leftover_html(tmp_path):
    """这条是上一条里真正危险的那一半，必须单独钉住：`--format md` **不会删掉**
    上一版留下的 itinerary.html。若就绪判定只看「files 里的都在」，它会给出
    一份 rev 7 的旧成稿，却宣称这是 rev 9 —— manifest 的 revision 与 state.json
    严丝合缝对得上，没有任何报错，谁也查不出来。"""
    publish(stage_artifacts(_done_state(rev=7, title="上一版的成稿"), tmp_path, FakeProvider(), "job-a"))
    assert "上一版的成稿" in (tmp_path / "itinerary.html").read_text(encoding="utf-8")

    publish(stage_artifacts(_done_state(rev=9, title="新版成稿"), tmp_path, FakeProvider(), "job-b", fmt="md"))

    assert (tmp_path / "itinerary.html").exists()          # 旧文件还在
    assert "上一版的成稿" in (tmp_path / "itinerary.html").read_text(encoding="utf-8")
    assert json.loads((tmp_path / ARTIFACTS_JSON).read_text(encoding="utf-8"))["revision"] == 9
    assert not artifact_ready(tmp_path, 9)                 # ★ 绝不能放行


def test_publish_and_discard_tolerate_none(tmp_path):
    publish(None)
    discard(None)


def test_stage_is_safe_before_any_candidates(tmp_path):
    s = TripState.new("去京都", run_id="r1")
    staged = stage_artifacts(s, tmp_path, FakeProvider(), "job-1")
    assert staged.names == ()
    publish(staged)
    assert json.loads((tmp_path / ARTIFACTS_JSON).read_text(encoding="utf-8"))["files"] == []


def test_stage_never_embeds_a_fake_placeholder_map(tmp_path):
    """provider=None 表示「跳过地图」，不是「用假地图顶替」。"""
    staged = stage_artifacts(_done_state(), tmp_path, None, "job-1")
    assert "<img" not in (staged.stage_dir / "itinerary.html").read_text(encoding="utf-8")


def test_format_md_only_does_not_stage_html(tmp_path):
    staged = stage_artifacts(_done_state(), tmp_path, FakeProvider(), "job-1", fmt="md")
    assert "itinerary.md" in staged.names
    assert "itinerary.html" not in staged.names


def test_format_html_only_does_not_stage_markdown(tmp_path):
    staged = stage_artifacts(_done_state(), tmp_path, FakeProvider(), "job-1", fmt="html")
    assert "itinerary.html" in staged.names
    assert "itinerary.md" not in staged.names
    assert not any(n.startswith("plan-") for n in staged.names)


def test_sweep_removes_only_old_orphans(tmp_path):
    """启动时扫一次 .staging/ 的孤儿子目录，但只删 mtime 超过 1 小时的——
    别的进程（一个正在跑的 trip render）可能正往自己的子目录里写（spec §4.1）。"""
    import os
    import time

    trip = tmp_path / "kyoto"
    old = trip / STAGING_DIR / "old-job"
    fresh = trip / STAGING_DIR / "fresh-job"
    old.mkdir(parents=True)
    fresh.mkdir(parents=True)
    ancient = time.time() - 7200
    os.utime(old, (ancient, ancient))

    assert sweep_stale_staging(tmp_path, max_age_s=3600) == 1
    assert not old.exists()
    assert fresh.exists()


def test_sweep_tolerates_a_missing_trips_root(tmp_path):
    assert sweep_stale_staging(tmp_path / "nope") == 0
```

- [ ] **Step 2: 跑测试确认它失败**

Run: `/Users/jialiu/Projects/trip-plan/.venv/bin/python -m pytest tests/test_artifacts.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'tripplan.artifacts'`

- [ ] **Step 3: 写最小实现**

新建 `src/tripplan/artifacts.py`：

```python
"""产物的暂存与原子发布。Web 与 `trip render` 共用同一条路径。

为什么不是「CAS 成功后再 write_artifacts」（spec §4.1）：前端一看到 revision
变了 / stage 变成 DONE 就会刷新并亮出成稿链接，而那一刻 itinerary.html 可能
还没开始写、或正在被非原子地覆盖写到一半——用户点进去看到 404 或半截文件。
更糟的是进程在这中间崩掉：state.json 已经是 DONE，产物却永远不存在，之后
每次进详情页都是一个死链。

所以慢且可能失败的那部分（渲染 + 拉高德静态图）挪到 CAS 之前，写进
`<trip>/.staging/<stage_id>/`；CAS 成功后只剩一串 os.replace（同文件系统内
原子改名）+ 写一个 artifacts.json。

暂存目录必须**按写者隔离**，不能是同一 trip 共享的 .staging/：文件名是固定的，
共享一个目录等于让两个写者互相踩，而 Web 的 per-trip 互斥是**进程内**的锁，
管不到另一个进程里的 `trip render`。两种坏结局都是静默的——(a) 发布出去的
artifacts.json revision 与 state.json 完全对得上，但 HTML 讲的是另一份行程；
(b) 败者的 discard() 删掉胜者刚放进去的文件，publish() 搬了个空。
"""

import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from tripplan.maps import fetch_day_maps
from tripplan.render.itinerary_html import render_itinerary_html
from tripplan.render.itinerary_md import render_itinerary_md
from tripplan.state import Stage

ARTIFACTS_JSON = "artifacts.json"
STAGING_DIR = ".staging"


@dataclass(frozen=True)
class Staged:
    """一次暂存的结果。names 的顺序就是 publish 时 os.replace 的顺序。"""

    trip_dir: Path
    stage_dir: Path
    revision: int
    names: tuple[str, ...]


def stage_artifacts(state, trip_dir, provider, stage_id: str, fmt: str = "both") -> Staged:
    """把产物渲染进 `<trip_dir>/.staging/<stage_id>/`，**不碰最终路径**。

    provider=None 表示「跳过地图」，不是「用假地图顶替」——FakeProvider 的
    占位图结构合法但与目的地毫无关系，混进这份最终要转发给同行者的 HTML 里
    比压根没有图更糟。
    """
    trip_dir = Path(trip_dir)
    stage_dir = trip_dir / STAGING_DIR / stage_id
    stage_dir.mkdir(parents=True, exist_ok=True)

    write_md = fmt in ("md", "both")
    write_html = fmt in ("html", "both")
    names: list[str] = []

    if write_md:
        for slot in state.candidates:
            if slot.itinerary is None or slot.facts is None:
                continue
            name = f"plan-{slot.angle.key}.md"
            (stage_dir / name).write_text(
                render_itinerary_md(slot.itinerary, slot.facts, state.requirements),
                encoding="utf-8",
            )
            names.append(name)

    slot = state.chosen() if state.stage is Stage.DONE else None
    if slot is not None and slot.itinerary is not None and slot.facts is not None:
        if write_md:
            (stage_dir / "itinerary.md").write_text(
                render_itinerary_md(slot.itinerary, slot.facts, state.requirements),
                encoding="utf-8",
            )
            names.append("itinerary.md")
        if write_html:
            day_maps = (
                fetch_day_maps(slot.itinerary, slot.facts, provider)
                if provider is not None
                else {}
            )
            (stage_dir / "itinerary.html").write_text(
                render_itinerary_html(
                    slot.itinerary, slot.facts, state.requirements, day_maps
                ),
                encoding="utf-8",
            )
            names.append("itinerary.html")

    return Staged(trip_dir, stage_dir, state.revision, tuple(names))


def publish(staged: Staged | None) -> None:
    """提交点。artifacts.json **最后写**——它在就代表整组文件都到位了。"""
    if staged is None:
        return
    for name in staged.names:
        os.replace(staged.stage_dir / name, staged.trip_dir / name)
    _atomic_write_json(
        staged.trip_dir / ARTIFACTS_JSON,
        {"revision": staged.revision, "files": list(staged.names)},
    )
    shutil.rmtree(staged.stage_dir, ignore_errors=True)


def discard(staged: Staged | None) -> None:
    """只删自己那个子目录，绝不碰兄弟写者的（spec §4.1）。"""
    if staged is None:
        return
    shutil.rmtree(staged.stage_dir, ignore_errors=True)


FINAL_HTML = "itinerary.html"


def artifact_ready(trip_dir, revision: int) -> bool:
    """详情页不靠 `stage is DONE` 决定要不要给链接，靠这个（spec §4.1）。

    崩溃恢复、旧版本产物残留、手工删文件，三种情况共用这一条判定。

    **它回答的是一个很具体的问题：「`/trips/<tid>/itinerary` 现在点进去，
    拿到的是不是这一版 revision 的成稿？」** 所以 manifest 里必须**明确列出**
    `itinerary.html` 且该文件真的在——`revision` 对得上 + `files` 里的东西都在，
    这两条加起来并不蕴含它。

    spec §4.1 的字面表述是「`artifacts.json` 存在且 revision 相等且文件都在」，
    照字面写成 `all(files 都存在)` 有一个致命的空集陷阱：**`all([])` 是 `True`**。
    于是两种 manifest 会拿到假绿灯，而两种都是现实路径：

    - **空 manifest**：`stage_artifacts()` 在没有可发布候选时返回 `names=()`
      （非 DONE、或 chosen 那条候选 `itinerary`/`facts` 是 None），`publish()`
      照样写下 `{"revision": N, "files": []}`；
    - **只有 Markdown 的 manifest**：`trip render <dir> --format md`（§7 明确
      支持的第二个写者）发布 `{"revision": N, "files": ["plan-A.md",
      "itinerary.md"]}`，压根没碰 HTML。

    两种情况下 `artifact_ready` 若返回 `True`，详情页就会亮出成稿链接（spec §6.1
    那张表的 `DONE + artifact_ready` 行），而点进去只有两种结局：**(a)** 文件不在 →
    成稿页 409「产物需要重建」，详情页刚刚才承诺过它就绪，自相矛盾；**(b)** 更糟，
    上一版的 `itinerary.html` 还躺在目录里没人删 —— `--format md` 不会清理它 ——
    于是**静默给出一份过期成稿**，manifest 的 revision 还和 `state.json` 严丝合缝
    对得上，谁也查不出来。这正是 spec §6.1「**不给死链**」和 §9 回归 15 要堵的洞。

    所以这里比 §4.1 的字面表述**更严**一档：强制要求 `itinerary.html` 在册。
    宁可多显示一次「产物待重建」（点一下重建按钮就好，不碰 LLM），也不要给一个
    404 或一份看不出来的旧成稿。
    """
    trip_dir = Path(trip_dir)
    path = trip_dir / ARTIFACTS_JSON
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict) or data.get("revision") != revision:
        return False
    files = data.get("files")
    if not isinstance(files, list) or FINAL_HTML not in files:
        # ★ 空 manifest 与「只有 md」的 manifest 都到此为止：all([]) 是 True，
        #   少了这一行它们全都是假绿灯。
        return False
    return all(isinstance(n, str) and (trip_dir / n).exists() for n in files)


def sweep_stale_staging(trips_root, max_age_s: float = 3600.0) -> int:
    """`trip web` 启动时扫一次，删掉进程崩在中途留下的孤儿暂存目录。

    只删 mtime 超过 max_age_s 的：启动那一刻本进程没有任何 job，但别的进程
    （一个正在跑的 trip render）可能正往自己的子目录里写。删错的代价本来也
    有限——暂存内容不是任何权威状态，大不了重建一次。
    """
    root = Path(trips_root)
    if not root.is_dir():
        return 0
    cutoff = time.time() - max_age_s
    removed = 0
    for staging in root.glob(f"*/{STAGING_DIR}"):
        if not staging.is_dir():
            continue
        for child in staging.iterdir():
            try:
                if child.is_dir() and child.stat().st_mtime < cutoff:
                    shutil.rmtree(child, ignore_errors=True)
                    removed += 1
            except OSError:
                continue
    return removed


def _atomic_write_json(path: Path, payload: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".artifacts-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
```

- [ ] **Step 4: 跑测试确认通过**

Run: `/Users/jialiu/Projects/trip-plan/.venv/bin/python -m pytest tests/test_artifacts.py -v`
Expected: PASS（17 条）

- [ ] **Step 5: 让 `trip render` 改走同一条路径**

`src/tripplan/cli.py`：删掉 `write_artifacts` 整个函数体与 `fetch_day_maps` / `render_itinerary_html` / `render_itinerary_md` 三个 import，改成：

```python
from tripplan.artifacts import publish, stage_artifacts
```

`_cmd_render` 的产物那一行改成：

```python
    # 与 Web job 共用同一条原子发布路径（spec §7）：自己现生成一个 stage_id，
    # 所以与正在跑的 Web job 共存也不会互相踩暂存文件。
    publish(
        stage_artifacts(
            state, repo.dir, build_provider(dry_run=False), uuid.uuid4().hex, fmt=args.format
        )
    )
```

`_drive_and_report` 里那一行 `write_artifacts(state, repo.dir, deps.provider)` 暂时改成同样的 stage+publish（Task 16 会连同 `drive` 一起删掉，这里只为保持中间态可跑）：

```python
    publish(stage_artifacts(state, repo.dir, deps.provider, uuid.uuid4().hex))
```

- [ ] **Step 6: 把 `test_cli.py` 里的产物用例搬走并跑全量**

从 `tests/test_cli.py` 删掉这 7 条（它们测的是已经不存在的 `cli.write_artifacts`，等价覆盖已在 `tests/test_artifacts.py`）：
`test_write_artifacts_emits_one_markdown_per_candidate`、
`test_write_artifacts_emits_final_md_and_html_when_done`、
`test_write_artifacts_is_safe_before_any_candidates`、
`test_write_artifacts_never_embeds_a_fake_placeholder_map`、
`test_write_artifacts_embeds_the_real_map_when_a_provider_is_given`、
`test_drive_and_report_skips_artifacts_when_final_save_loses_the_cas_race`（Task 16 会用 `run_command` 的等价回归接管；本任务先删）、
以及 `from tripplan.cli import ... write_artifacts` 这一行里的 `write_artifacts`。

`test_render_*` 全部保留——它们走的是 `main(["render", ...])`，正好验证新路径端到端没坏。

Run: `/Users/jialiu/Projects/trip-plan/.venv/bin/python -m pytest -q`
Expected: `646 passed, 1 deselected`（636 − 7 + 17）。特别确认 `test_render_twice_produces_identical_files`、`test_render_format_md_only_does_not_write_html`、`test_render_uses_no_provider_and_skips_maps_when_amap_key_is_absent` 仍绿

- [ ] **Step 7: 格式化并提交**

```bash
/Users/jialiu/Projects/trip-plan/.venv/bin/black src tests
git add src/tripplan/artifacts.py src/tripplan/cli.py tests/test_artifacts.py tests/test_cli.py
git commit -m "refactor(artifacts): 抽出 stage/publish/discard，产物改成先暂存后原子发布"
```

---

## Task 5: `web/events.py` —— EventLog（磁盘历史 + 内存 live ring）

**Files:**
- Create: `src/tripplan/web/__init__.py`（空）
- Create: `src/tripplan/web/events.py`
- Create: `tests/web/__init__.py`（空）
- Create: `tests/web/test_events.py`

**Interfaces:**
- Consumes: 无（不 import flask，不 import orchestrator）
- Produces:
  - `@dataclass(frozen=True) class Event: seq:int; ts:float; type:str; payload:dict; stream_id:str|None=None; durable:bool=True`，带 `to_json() -> dict`
  - `@dataclass(frozen=True) class Snapshot: events:list[Event]; cursor:int; first_seq:int; stream_epoch:str`
  - `@dataclass(frozen=True) class SinceResult: events:list[Event]; first_seq:int; last_seq:int; stream_epoch:str; reset_required:bool; resume_seq:int|None`
  - `class EventLog(path, ring_size=2000, stream_epoch=..., clock=time.time, flush_every=20)`，方法 `append(type, payload, stream_id=None, durable=True) -> Event` / `flush()` / `snapshot() -> Snapshot` / `since(n:int, epoch:str|None=None) -> SinceResult`
  - `class EventLogStore(trips_root, ring_size=2000)`，方法 `get(tid) -> EventLog`，属性 `stream_epoch: str`

- [ ] **Step 1: 写失败的测试**

新建 `tests/web/__init__.py`（空文件）与 `tests/web/test_events.py`：

```python
"""EventLog：磁盘历史 vs 内存 live ring（spec §5.4 / §5.5 / §9 回归 2、3、4、20、25）。"""

import json

from tripplan.web.events import Event, EventLog, EventLogStore


def _log(tmp_path, **kw):
    return EventLog(tmp_path / "events.jsonl", **kw)


def test_seq_starts_at_one_and_is_monotonic(tmp_path):
    log = _log(tmp_path)
    assert log.append("generating", {"args": ["A"]}).seq == 1
    assert log.append("generating", {"args": ["B"]}).seq == 2


def test_durable_events_land_on_disk_after_flush(tmp_path):
    log = _log(tmp_path)
    log.append("generating", {"args": ["A"]})
    log.flush()
    lines = (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["type"] == "generating"


def test_transient_events_are_readable_but_never_written(tmp_path):
    """§9 回归 3：下期的 token 事件量级是每秒几十上百条，落盘会直接撑爆
    events.jsonl（spec §5.3）。"""
    log = _log(tmp_path)
    log.append("token", {"text": "京"}, stream_id="s1", durable=False)
    log.flush()

    assert [e.type for e in log.since(0).events] == ["token"]
    assert not (tmp_path / "events.jsonl").exists() or (
        tmp_path / "events.jsonl"
    ).read_text(encoding="utf-8") == ""


def test_restart_continues_the_sequence_from_the_file(tmp_path):
    """§9 回归 4：seq 从文件里的最大值往后接，不从 1 重来（spec §5.4）。"""
    first = _log(tmp_path)
    first.append("generating", {"args": ["A"]})
    first.append("generating", {"args": ["B"]})
    first.flush()

    second = _log(tmp_path)
    assert second.append("generating", {"args": ["C"]}).seq == 3


def test_a_reloaded_log_replays_history_in_its_snapshot(tmp_path):
    first = _log(tmp_path)
    first.append("generating", {"args": ["A"]})
    first.flush()

    snap = _log(tmp_path).snapshot()
    assert [e.type for e in snap.events] == ["generating"]
    assert snap.cursor == 1


def test_a_corrupt_line_is_skipped_not_fatal(tmp_path):
    (tmp_path / "events.jsonl").write_text(
        '{"seq":1,"ts":1.0,"type":"a","payload":{}}\nnot json\n', encoding="utf-8"
    )
    log = _log(tmp_path)
    assert [e.seq for e in log.snapshot().events] == [1]
    assert log.append("b", {}).seq == 2


def test_since_returns_only_the_increment_and_keeps_seq_contiguous(tmp_path):
    """§9 回归 2 的后半条。"""
    log = _log(tmp_path)
    for key in "ABCD":
        log.append("generating", {"args": [key]})
    result = log.since(2)
    assert [e.seq for e in result.events] == [3, 4]
    assert result.last_seq == 4
    assert result.reset_required is False


def test_snapshot_merges_disk_history_and_the_live_ring_without_duplicates(tmp_path):
    """spec §5.5：快照 = 磁盘 durable 历史 + ring 当前内容，按 seq 归并去重。
    没有去重的话，已 flush 的事件会在页面上出现两遍。"""
    log = _log(tmp_path)
    log.append("a", {})
    log.flush()                       # 1 号同时在磁盘和 ring 里
    log.append("t", {}, durable=False)  # 2 号只在 ring 里
    snap = log.snapshot()
    assert [e.seq for e in snap.events] == [1, 2]
    assert snap.cursor == 2


def test_snapshot_cursor_is_the_high_water_mark_not_the_disk_max(tmp_path):
    """spec §5.5 的收敛性前提：cursor 是**此刻已分配出去的最大 seq**。
    取「磁盘上的最大 seq」会让 reset→reload→再 reset 变成死循环。"""
    log = _log(tmp_path)
    log.append("a", {})
    log.flush()
    log.append("t", {}, durable=False)
    log.append("t", {}, durable=False)
    assert log.snapshot().cursor == 3


def test_a_cursor_behind_the_ring_demands_a_reset(tmp_path):
    """§9 回归 20 前半条：中间那段已经被挤出内存，再返回 first_seq 之后的
    事件就是默默吞掉一段。"""
    log = _log(tmp_path, ring_size=3)
    for _ in range(10):
        log.append("t", {}, durable=False)
    result = log.since(1)
    assert result.reset_required is True
    assert result.resume_seq == 10
    assert result.events == []


def test_a_stale_epoch_demands_a_reset(tmp_path):
    """§9 回归 20 后半条：重启后新事件会复用客户端已经见过的号段，
    单看 seq 分不出「这是新事件」还是「这是我早就有的那条」。"""
    log = _log(tmp_path)
    log.append("a", {})
    assert log.since(0, epoch="别的进程").reset_required is True
    assert log.since(0, epoch=log.stream_epoch).reset_required is False


def test_reset_converges_after_exactly_one_refresh(tmp_path):
    """§9 回归 25：守的是收敛性，不是单次行为。

    ring 被一批 durable=False 的事件挤爆、这些事件不在 events.jsonl 里时，
    「快照 = 磁盘历史」会让 reload 后的新游标又落在 first_seq 之前——一个
    每秒 reload 一次、永远读不完内容的死循环（spec §5.5）。
    """
    log = _log(tmp_path, ring_size=3)
    log.append("a", {})
    log.flush()
    for _ in range(20):
        log.append("t", {}, durable=False)

    assert log.since(1).reset_required is True          # 旧游标失效
    snap = log.snapshot()
    assert log.since(snap.cursor, epoch=snap.stream_epoch).reset_required is False


def test_a_fresh_log_does_not_demand_a_reset_for_since_zero(tmp_path):
    """空 ring 上的边界：新建行程第一次进详情页，游标是 0。若这里判成
    reset_required，页面会 reload、再拿到 0、再 reset——同一个死循环。"""
    log = _log(tmp_path)
    assert log.since(0).reset_required is False
    assert log.since(0).events == []


def test_a_cursor_exactly_one_behind_the_ring_is_still_servable(tmp_path):
    """off-by-one：ring 首元素 seq = first_seq，游标 first_seq - 1 的客户端
    要的是 first_seq 起的事件，ring 完全服务得了，不该触发 reset。"""
    log = _log(tmp_path, ring_size=3)
    for _ in range(5):
        log.append("t", {}, durable=False)
    result = log.since(2)   # ring 里是 3、4、5，first_seq = 3
    assert result.first_seq == 3
    assert result.reset_required is False
    assert [e.seq for e in result.events] == [3, 4, 5]


def test_event_to_json_has_the_wire_shape_the_frontend_expects(tmp_path):
    ev = Event(seq=12, ts=1757900000.1, type="generating", payload={"args": ["foodie"]})
    assert ev.to_json() == {
        "seq": 12,
        "ts": 1757900000.1,
        "type": "generating",
        "payload": {"args": ["foodie"]},
        "stream_id": None,
    }


def test_store_hands_out_one_log_per_trip_sharing_one_epoch(tmp_path):
    store = EventLogStore(tmp_path)
    (tmp_path / "kyoto").mkdir()
    (tmp_path / "osaka").mkdir()
    a, b = store.get("kyoto"), store.get("osaka")
    assert a is store.get("kyoto")       # 同一 tid 复用同一份
    assert a is not b
    assert a.stream_epoch == b.stream_epoch == store.stream_epoch


def test_store_creates_the_trip_directory_lazily_on_first_write(tmp_path):
    store = EventLogStore(tmp_path)
    log = store.get("kyoto")             # 目录还不存在也不许崩
    log.append("a", {})
    log.flush()
    assert (tmp_path / "kyoto" / "events.jsonl").exists()
```

- [ ] **Step 2: 跑测试确认它失败**

Run: `/Users/jialiu/Projects/trip-plan/.venv/bin/python -m pytest tests/web/test_events.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'tripplan.web'`

- [ ] **Step 3: 写最小实现**

新建 `src/tripplan/web/__init__.py`（空文件）与 `src/tripplan/web/events.py`：

```python
"""事件日志。两个来源，一份接口（spec §5.5）。

| 来源 | 内容 | 服务谁 |
|---|---|---|
| events.jsonl（磁盘） | 全部 durable=True 历史 | snapshot() 的主体 |
| live ring（内存） | 最近 N 条，含 durable=False | since() 的增量轮询；并给 snapshot() 补尾巴 |

ring 有容量上限，装不下全部历史；而把全部历史留在内存里，下期几十上百条/秒
的 token 事件立刻把它撑爆（spec §5.3 的整个前提）。所以两者都要，且必须分开。

本模块不 import flask：`?since=N` 的轮询与下期的 SSE（Last-Event-ID 就是 seq）
是同一份数据的两种取法（spec §5.3 第 3 点）。
"""

import collections
import json
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Event:
    seq: int  # 单调递增，从 1 开始
    ts: float  # epoch 秒
    type: str  # "generating" / "revision" / 下期的 "token"
    payload: dict  # 结构化，JSON 可序列化
    #: 同 id 的事件在前端拼进同一个块。v1 不产生这类事件，但渲染函数现在就认它。
    stream_id: str | None = None
    #: False 只进内存，不落盘。下期 token 事件靠它不撑爆 events.jsonl。
    durable: bool = True

    def to_json(self) -> dict:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "type": self.type,
            "payload": self.payload,
            "stream_id": self.stream_id,
        }


@dataclass(frozen=True)
class Snapshot:
    events: list[Event]
    #: **此刻的 high-water mark**（已分配出去的最大 seq），不是「磁盘上的最大
    #: seq」。收敛性全靠这一点：紧接着的 since(cursor) 在构造上不可能再 reset。
    cursor: int
    first_seq: int
    stream_epoch: str


@dataclass(frozen=True)
class SinceResult:
    events: list[Event]
    first_seq: int
    last_seq: int
    stream_epoch: str
    reset_required: bool
    resume_seq: int | None


class EventLog:
    def __init__(
        self,
        path,
        ring_size: int = 2000,
        stream_epoch: str | None = None,
        clock=time.time,
        flush_every: int = 20,
    ) -> None:
        self._path = Path(path)
        self._clock = clock
        self._flush_every = flush_every
        self._lock = threading.Lock()
        self._ring: collections.deque[Event] = collections.deque(maxlen=ring_size)
        self._pending: list[Event] = []
        self.stream_epoch = stream_epoch or secrets.token_hex(4)
        self._history = self._read_history()
        self._seq = self._history[-1].seq if self._history else 0

    # ---------- 写 ----------

    def append(self, type: str, payload: dict, stream_id=None, durable=True) -> Event:
        """可能抛（序列化失败 / 磁盘满 / 文件只读）。调用方 TripJob.emit 负责
        兜住——那里才是「不抛异常的边界」（spec §5.1.1）。"""
        with self._lock:
            self._seq += 1
            event = Event(self._seq, self._clock(), type, payload, stream_id, durable)
            self._ring.append(event)
            if durable:
                self._history.append(event)
                self._pending.append(event)
                if len(self._pending) >= self._flush_every:
                    self._flush_locked()
            return event

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    # ---------- 读 ----------

    def snapshot(self) -> Snapshot:
        """详情页服务端渲染时的初始快照。三件事必须**在同一个锁内**完成：
        读 durable 历史、取 ring 全部内容、按 seq 归并去重。否则「读完磁盘」
        与「读 ring」之间新写入的事件会掉进缝里，快照和游标对不上——v1 的
        批量 flush 延迟就足以制造这条缝（spec §5.5）。"""
        with self._lock:
            merged: dict[int, Event] = {e.seq: e for e in self._history}
            merged.update({e.seq: e for e in self._ring})
            return Snapshot(
                events=[merged[k] for k in sorted(merged)],
                cursor=self._seq,
                first_seq=self._first_seq_locked(),
                stream_epoch=self.stream_epoch,
            )

    def since(self, n: int, epoch: str | None = None) -> SinceResult:
        with self._lock:
            first = self._first_seq_locked()
            stale_epoch = epoch is not None and epoch != self.stream_epoch
            # off-by-one 是刻意的：ring 首元素 seq = first_seq，所以游标
            # first_seq - 1 的客户端要的正好是 ring 的全部内容，服务得了。
            # 写成 `n < first` 会让「空 ring + since=0」也判成失效，而 reload
            # 之后拿到的还是 0 —— 一个每秒 reload 一次的死循环。
            if stale_epoch or n < first - 1:
                return SinceResult(
                    [], first, self._seq, self.stream_epoch, True, self._seq
                )
            return SinceResult(
                [e for e in self._ring if e.seq > n],
                first,
                self._seq,
                self.stream_epoch,
                False,
                None,
            )

    # ---------- 内部 ----------

    def _first_seq_locked(self) -> int:
        """ring 能服务的最早 seq。ring 空时是「下一个事件将拿到的号」。"""
        return self._ring[0].seq if self._ring else self._seq + 1

    def _flush_locked(self) -> None:
        if not self._pending:
            return
        lines = "".join(
            json.dumps(e.to_json(), ensure_ascii=False, default=str) + "\n"
            for e in self._pending
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "a", encoding="utf-8") as fh:
            fh.write(lines)
        # 不每条 fsync：这份日志是给人看的进度历史，不是权威状态（spec §5.4）。
        self._pending.clear()

    def _read_history(self) -> list[Event]:
        if not self._path.exists():
            return []
        out: list[Event] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
                out.append(
                    Event(
                        seq=int(raw["seq"]),
                        ts=float(raw["ts"]),
                        type=str(raw["type"]),
                        payload=raw.get("payload") or {},
                        stream_id=raw.get("stream_id"),
                        durable=True,
                    )
                )
            except (ValueError, KeyError, TypeError):
                continue  # 崩在半行上留下的残片：跳过，不是致命错误
        out.sort(key=lambda e: e.seq)
        return out


class EventLogStore:
    """进程内的 tid → EventLog 映射。

    stream_epoch 是**进程级**的：它回答的是「这还是同一条流吗」，重启后
    所有 trip 的流都换了（spec §5.5）。
    """

    def __init__(self, trips_root, ring_size: int = 2000) -> None:
        self._root = Path(trips_root)
        self._ring_size = ring_size
        self._lock = threading.Lock()
        self._logs: dict[str, EventLog] = {}
        self.stream_epoch = secrets.token_hex(4)

    def get(self, tid: str) -> EventLog:
        with self._lock:
            log = self._logs.get(tid)
            if log is None:
                log = EventLog(
                    self._root / tid / "events.jsonl",
                    ring_size=self._ring_size,
                    stream_epoch=self.stream_epoch,
                )
                self._logs[tid] = log
            return log
```

- [ ] **Step 4: 跑测试确认通过**

Run: `/Users/jialiu/Projects/trip-plan/.venv/bin/python -m pytest tests/web/test_events.py -v && /Users/jialiu/Projects/trip-plan/.venv/bin/python -m pytest -q`
Expected: `tests/web/test_events.py` 17 条全绿；全量 `663 passed, 1 deselected`

- [ ] **Step 5: 格式化并提交**

```bash
/Users/jialiu/Projects/trip-plan/.venv/bin/black src tests
git add src/tripplan/web/__init__.py src/tripplan/web/events.py tests/web/__init__.py tests/web/test_events.py
git commit -m "feat(web): EventLog —— 磁盘 durable 历史 + 内存 live ring，游标失效可检测"
```

---

## Task 6: `web/jobs.py` 之一 —— `run_command()` 的提交纪律

**Files:**
- Create: `src/tripplan/web/jobs.py`
- Create: `tests/web/test_jobs_run_command.py`

**Interfaces:**
- Consumes: Task 1 的 `Cancelled`、Task 4 的 `stage_artifacts` / `publish` / `discard`
- Produces（全部框架无关，不 import flask）：
  - `@dataclass(frozen=True) class JobOutcome: status:str; kind:str|None=None; message:str|None=None; revision:int|None=None`，类方法 `ok(revision)` / `rejected(reason)` / `failed(kind, message)` / `cancelled()`
  - `def open_trip(trips_root, tid) -> FileRepo`
  - `def run_command(trips_root, tid, cmd, deps, job, *, advance_fn=advance, stage_fn=stage_artifacts, publish_fn=publish, discard_fn=discard, open_fn=open_trip) -> JobOutcome`
  - `def rebuild_artifacts(trips_root, tid, deps, job, *, stage_fn=..., publish_fn=..., discard_fn=..., open_fn=...) -> JobOutcome`
  - 本任务只需要 `job` 具备 `.job_id` / `.cancel_token` / `.emit`；`TripJob` 在 Task 7 里做，测试用一个最小替身

- [ ] **Step 1: 写失败的测试**

新建 `tests/web/test_jobs_run_command.py`：

```python
"""run_command 的提交纪律（spec §4.1 / §9 回归 7、10、12、14）。

这一层框架无关：advance / stage / publish / discard 都可注入，所以「CAS 输了」
「产物生成失败」「中途取消」这些分支全部可以纯内存地故障注入，不碰 LLM 与高德。
"""

import threading
from datetime import date

import pytest

from tripplan.agents.limits import Cancelled
from tripplan.artifacts import ARTIFACTS_JSON
from tripplan.deps import Deps
from tripplan.models.common import Field, Origin
from tripplan.models.itinerary import Angle, Itinerary
from tripplan.models.requirements import DateRange, Party, Requirements
from tripplan.providers.base import ProviderError
from tripplan.providers.fake import FakeProvider
from tripplan.repo import FileRepo
from tripplan.state import (
    CandidateSlot,
    ConfirmRequirements,
    Done,
    InputKind,
    NeedInput,
    Rejected,
    RejectReason,
    SlotStatus,
    Stage,
    TripState,
)
from tripplan.web.jobs import JobOutcome, run_command

D1 = date(2026, 10, 1)


class _FakeJob:
    """run_command 只需要这三样。完整的 TripJob 在 Task 7。"""

    def __init__(self, job_id="job-1"):
        self.job_id = job_id
        self.cancel_token = threading.Event()
        self.events = []

    def emit(self, event):
        self.events.append(event)


def _reqs():
    return Requirements(
        destination=Field("京都", Origin.USER),
        dates=Field(DateRange(D1, D1), Origin.USER),
        party=Field(Party(adults=2), Origin.USER),
    )


def _seed(trips_root, tid="kyoto", stage=Stage.AWAIT_REQ_CONFIRM, rev=1):
    repo = FileRepo(trips_root / tid)
    state = TripState.new("去京都", run_id="r1")
    state.stage, state.revision, state.requirements = stage, rev, _reqs()
    angle = Angle("A", "古寺", "")
    state.candidates = [CandidateSlot(angle, Itinerary(angle=angle), None, SlotStatus.OK)]
    repo.create(state)
    return repo


def _pending(state):
    return NeedInput(InputKind.CONFIRM_REQUIREMENTS, state.requirements, state.revision)


def test_a_successful_command_advances_once_and_saves(tmp_path):
    repo = _seed(tmp_path)
    calls = []

    def fake_advance(state, deps, cmd=None, emit=None, cancel=None):
        calls.append(cmd)
        state.revision += 1
        return _pending(state)

    job = _FakeJob()
    outcome = run_command(
        tmp_path, "kyoto", ConfirmRequirements(1),
        Deps(client=None, provider=FakeProvider()), job, advance_fn=fake_advance,
    )

    assert outcome.status == "succeeded"
    assert outcome.revision == 2
    assert repo.load().revision == 2
    assert len(calls) == 1


def test_state_is_reread_from_disk_every_time(tmp_path):
    """「每次从盘上重读 state」让命令之间完全无内存状态：CAS 纪律自动成立，
    服务器重启后的接续也自动成立（spec §4.1）。"""
    _seed(tmp_path, rev=5)
    seen = []

    def fake_advance(state, deps, cmd=None, emit=None, cancel=None):
        seen.append(state.revision)
        state.revision += 1
        return _pending(state)

    run_command(tmp_path, "kyoto", None, Deps(client=None, provider=FakeProvider()),
                _FakeJob(), advance_fn=fake_advance)
    assert seen == [5]


def test_rejected_is_not_an_http_error_and_never_hits_the_disk(tmp_path):
    """§9 回归 10：state 未变、revision 未变 → 不落盘。写盘只会白占一次
    CAS 窗口，让无辜的并发调用被误杀。"""
    repo = _seed(tmp_path, rev=1)
    saves = []
    original = FileRepo.save_if_revision

    def counting(self, state, expected):
        saves.append(expected)
        return original(self, state, expected)

    def fake_advance(state, deps, cmd=None, emit=None, cancel=None):
        return Rejected(RejectReason.STALE_REVISION, _pending(state))

    import tripplan.repo

    tripplan.repo.FileRepo.save_if_revision = counting
    try:
        outcome = run_command(tmp_path, "kyoto", ConfirmRequirements(0),
                              Deps(client=None, provider=FakeProvider()),
                              _FakeJob(), advance_fn=fake_advance)
    finally:
        tripplan.repo.FileRepo.save_if_revision = original

    assert outcome.status == "rejected"
    assert outcome.message == RejectReason.STALE_REVISION.value
    assert saves == []
    assert repo.load().revision == 1


def test_losing_the_cas_race_never_publishes_artifacts(tmp_path):
    """§9 回归 7 + 14：stage_artifacts 成功但 CAS 返回 False 时，行程目录里
    **不出现** itinerary.html——盘上的结局不是我们手上这份。"""
    _seed(tmp_path, stage=Stage.AWAIT_CHOICE, rev=1)
    published = []

    def fake_advance(state, deps, cmd=None, emit=None, cancel=None):
        state.stage, state.chosen_key = Stage.DONE, "A"
        state.revision += 1
        return Done(state.chosen().itinerary)

    class _AlwaysLoses(FileRepo):
        def save_if_revision(self, state, expected):
            return False

    outcome = run_command(
        tmp_path, "kyoto", None, Deps(client=None, provider=FakeProvider()), _FakeJob(),
        advance_fn=fake_advance,
        open_fn=lambda root, tid: _AlwaysLoses(root / tid),
        publish_fn=published.append,
    )

    assert outcome.status == "failed"
    assert outcome.kind == "Conflict"
    assert published == []
    assert not (tmp_path / "kyoto" / "itinerary.html").exists()
    assert not (tmp_path / "kyoto" / ARTIFACTS_JSON).exists()


def test_a_won_cas_publishes_and_the_manifest_matches_state(tmp_path):
    """§9 回归 14 的正面：CAS 成功时文件出现，且 artifacts.json.revision
    与 state.revision 对齐。"""
    import json

    repo = _seed(tmp_path, stage=Stage.AWAIT_CHOICE, rev=1)

    def fake_advance(state, deps, cmd=None, emit=None, cancel=None):
        state.stage, state.chosen_key = Stage.DONE, "A"
        state.candidates[0].facts = _facts_stub()
        state.revision += 1
        return Done(state.chosen().itinerary)

    outcome = run_command(tmp_path, "kyoto", None,
                          Deps(client=None, provider=FakeProvider()), _FakeJob(),
                          advance_fn=fake_advance)

    assert outcome.status == "succeeded"
    assert (tmp_path / "kyoto" / "itinerary.html").exists()
    manifest = json.loads((tmp_path / "kyoto" / ARTIFACTS_JSON).read_text(encoding="utf-8"))
    assert manifest["revision"] == repo.load().revision


def _facts_stub():
    from datetime import datetime, timedelta, timezone

    from tripplan.models.facts import FactSnapshot

    return FactSnapshot(
        poi_by_activity={}, constraint_pois={}, routes=[], weather={},
        trip_timezone="Asia/Tokyo",
        resolved_at=datetime(2026, 9, 1, tzinfo=timezone(timedelta(hours=9))), gaps=[],
    )


def test_cancelled_does_not_touch_the_disk(tmp_path):
    """取消不是一种规划结果：不落盘，盘上仍是上一个暂停点（spec §4.1）。"""
    repo = _seed(tmp_path, rev=3)

    def cancelling_advance(state, deps, cmd=None, emit=None, cancel=None):
        state.revision += 1  # 已经改了内存里的 state，但仍然不许落盘
        raise Cancelled("已取消")

    outcome = run_command(tmp_path, "kyoto", None,
                          Deps(client=None, provider=FakeProvider()), _FakeJob(),
                          advance_fn=cancelling_advance)

    assert outcome.status == "cancelled"
    assert repo.load().revision == 3


def test_cancelling_between_staging_and_cas_discards_the_artifacts(tmp_path):
    """§9 回归 12：CAS 前那道取消检查命中时，publish 未被调用、discard 被调用。"""
    _seed(tmp_path, stage=Stage.AWAIT_CHOICE, rev=1)
    job = _FakeJob()
    published, discarded = [], []

    def fake_advance(state, deps, cmd=None, emit=None, cancel=None):
        state.stage, state.chosen_key = Stage.DONE, "A"
        state.candidates[0].facts = _facts_stub()
        state.revision += 1
        return Done(state.chosen().itinerary)

    def staging_then_cancel(state, trip_dir, provider, stage_id, fmt="both"):
        job.cancel_token.set()  # 暂存刚做完，用户点了取消
        return "STAGED"

    outcome = run_command(tmp_path, "kyoto", None,
                          Deps(client=None, provider=FakeProvider()), job,
                          advance_fn=fake_advance, stage_fn=staging_then_cancel,
                          publish_fn=published.append, discard_fn=discarded.append)

    assert outcome.status == "cancelled"
    assert published == []
    assert discarded == ["STAGED"]
    assert not (tmp_path / "kyoto" / "itinerary.html").exists()


def test_provider_and_limit_errors_become_failed_outcomes(tmp_path):
    """接的正是 cli.main() 原来那两个 except 的职责（spec §4.1）。"""
    from tripplan.agents.limits import LimitExceeded

    for exc, kind in ((ProviderError("高德限流"), "ProviderError"),
                      (LimitExceeded("超时（700s > 600s）"), "LimitExceeded")):
        _seed(tmp_path, tid=kind.lower())

        def boom(state, deps, cmd=None, emit=None, cancel=None, _e=exc):
            raise _e

        outcome = run_command(tmp_path, kind.lower(), None,
                              Deps(client=None, provider=FakeProvider()), _FakeJob(),
                              advance_fn=boom)
        assert outcome.status == "failed"
        assert outcome.kind == kind
        assert str(exc) in outcome.message


def test_staging_failure_is_recorded_but_does_not_block_the_cas(tmp_path):
    """产物生成慢也可能失败；失败只记进 JobOutcome，绝不影响 CAS 判定
    （spec §4.1）——否则拉图超时会让一次已经付过钱的规划整个作废。"""
    repo = _seed(tmp_path, stage=Stage.AWAIT_CHOICE, rev=1)

    def fake_advance(state, deps, cmd=None, emit=None, cancel=None):
        state.stage, state.chosen_key = Stage.DONE, "A"
        state.revision += 1
        return Done(state.chosen().itinerary)

    def exploding_stage(state, trip_dir, provider, stage_id, fmt="both"):
        raise OSError("磁盘满")

    outcome = run_command(tmp_path, "kyoto", None,
                          Deps(client=None, provider=FakeProvider()), _FakeJob(),
                          advance_fn=fake_advance, stage_fn=exploding_stage)

    assert repo.load().revision == 2          # CAS 照常发生
    assert outcome.status == "succeeded"
    assert outcome.kind == "ArtifactError"    # 但如实记下产物没做出来
    assert "磁盘满" in outcome.message


def test_a_no_op_advance_does_not_waste_a_cas_window(tmp_path):
    """cmd=None 落在等待态时 advance 只是重新问一遍，revision 没动。
    再写一次盘只会白占一次 CAS 窗口（cli.drive 里同一处的理由）。"""
    _seed(tmp_path, rev=4)
    saves = []
    original = FileRepo.save_if_revision

    def counting(self, state, expected):
        saves.append(expected)
        return original(self, state, expected)

    import tripplan.repo

    tripplan.repo.FileRepo.save_if_revision = counting
    try:
        outcome = run_command(tmp_path, "kyoto", None,
                              Deps(client=None, provider=FakeProvider()), _FakeJob(),
                              advance_fn=lambda s, d, cmd=None, emit=None, cancel=None: _pending(s))
    finally:
        tripplan.repo.FileRepo.save_if_revision = original

    assert outcome.status == "succeeded"
    assert saves == []


def test_a_missing_or_corrupt_trip_becomes_a_failed_outcome(tmp_path):
    outcome = run_command(tmp_path, "nope", None,
                          Deps(client=None, provider=FakeProvider()), _FakeJob())
    assert outcome.status == "failed"
    assert outcome.kind == "TripNotFound"


def test_rebuild_artifacts_publishes_without_touching_state(tmp_path):
    """spec §6.2：不碰 LLM，与 trip render 同一条代码路径，revision 不变。"""
    import json

    from tripplan.web.jobs import rebuild_artifacts

    repo = _seed(tmp_path, stage=Stage.AWAIT_CHOICE, rev=6)
    state = repo.load()
    state.stage, state.chosen_key = Stage.DONE, "A"
    state.candidates[0].facts = _facts_stub()
    repo.save_if_revision(state, 6)

    outcome = rebuild_artifacts(tmp_path, "kyoto",
                                Deps(client=None, provider=FakeProvider()), _FakeJob())

    assert outcome.status == "succeeded"
    assert repo.load().revision == 6
    assert json.loads((tmp_path / "kyoto" / ARTIFACTS_JSON).read_text(encoding="utf-8"))["revision"] == 6
```

- [ ] **Step 2: 跑测试确认它失败**

Run: `/Users/jialiu/Projects/trip-plan/.venv/bin/python -m pytest tests/web/test_jobs_run_command.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'tripplan.web.jobs'`

- [ ] **Step 3: 写最小实现**

新建 `src/tripplan/web/jobs.py`（Task 7 会在同一文件里追加 `TripJob` / `JobRegistry`）：

```python
"""后台 job：调度、提交纪律、生命周期。**框架无关**——不 import flask，
不碰 request / session（spec §3.1）。

与 CLI 最关键的差异（spec §4.1）：cli.drive() 是一个 while 循环——跑一段、
阻塞在 input() 问人、再跑一段。Web 版把这个循环拆开交给 HTTP：**每一次用户
动作 = 恰好一次 advance()**，跑到下一个暂停点就结束，线程退出。
"""

import logging
from dataclasses import dataclass
from pathlib import Path

from tripplan.agents.limits import Cancelled, LimitExceeded
from tripplan.artifacts import discard as _discard
from tripplan.artifacts import publish as _publish
from tripplan.artifacts import stage_artifacts as _stage_artifacts
from tripplan.orchestrator import advance as _advance
from tripplan.providers.base import ProviderError
from tripplan.repo import FileRepo, TripCorrupt, TripNotFound
from tripplan.state import Done, Rejected, Stage

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class JobOutcome:
    status: str  # "succeeded" | "rejected" | "failed" | "cancelled"
    kind: str | None = None
    message: str | None = None
    revision: int | None = None

    @classmethod
    def ok(cls, revision: int, kind=None, message=None) -> "JobOutcome":
        return cls("succeeded", kind, message, revision)

    @classmethod
    def rejected(cls, reason) -> "JobOutcome":
        return cls("rejected", "Rejected", getattr(reason, "value", str(reason)))

    @classmethod
    def failed(cls, kind: str, message: str) -> "JobOutcome":
        return cls("failed", kind, message)

    @classmethod
    def cancelled(cls) -> "JobOutcome":
        return cls("cancelled", "Cancelled", "已取消")


def open_trip(trips_root, tid: str) -> FileRepo:
    return FileRepo(Path(trips_root) / tid)


def run_command(
    trips_root,
    tid: str,
    cmd,
    deps,
    job,
    *,
    advance_fn=_advance,
    stage_fn=_stage_artifacts,
    publish_fn=_publish,
    discard_fn=_discard,
    open_fn=open_trip,
) -> JobOutcome:
    try:
        repo = open_fn(trips_root, tid)
        state = repo.load()  # 每次从盘上重读，命令之间不在内存留 state
    except (TripNotFound, TripCorrupt) as e:
        return JobOutcome.failed(type(e).__name__, str(e))

    persisted = state.revision  # CAS 的 expected

    try:
        outcome = advance_fn(state, deps, cmd, job.emit, cancel=job.cancel_token)
    except Cancelled:
        # 不落盘：取消不是一种规划结果。盘上仍是上一个暂停点。
        return JobOutcome.cancelled()
    except (ProviderError, LimitExceeded) as e:
        # 这两个分支接的正是 cli.main() 原来那两个 except 的职责。
        return JobOutcome.failed(type(e).__name__, str(e))

    if isinstance(outcome, Rejected):
        # state 未变、revision 未变 → 不落盘。写盘只会白占一次 CAS 窗口，
        # 让无辜的并发调用被误杀（见 cli.drive() 里同一处的注释）。
        return JobOutcome.rejected(outcome.reason)

    if state.revision == persisted:
        # advance 什么也没改（等待态收到空命令＝重新问一遍）。同上，不落盘。
        return JobOutcome.ok(state.revision)

    staged, artifact_error = None, None
    if isinstance(outcome, Done):
        # 写进 <trip>/.staging/<job_id>/ —— 这一次 job 私有的目录，不是最终
        # 路径，也不是同一 trip 共享的暂存区（spec §4.1）。这一步慢（要拉高德
        # 静态图）也可能失败；失败只记进 JobOutcome，绝不影响下面的 CAS 判定。
        try:
            staged = stage_fn(state, repo.dir, deps.provider, job.job_id)
        except Exception as e:  # noqa: BLE001
            _log.exception("产物暂存失败")
            artifact_error = f"{type(e).__name__}: {e}"

    if job.cancel_token.is_set():
        discard_fn(staged)
        return JobOutcome.cancelled()  # CAS 前最后一道检查：已取消就别写盘

    if not repo.save_if_revision(state, persisted):
        discard_fn(staged)
        # 绝不发布产物：盘上的结局不是我们手上这份。
        return JobOutcome.failed("Conflict", "另一个进程改动了这个行程")

    if staged is not None:
        try:
            publish_fn(staged)  # 逐个 os.replace 原子改名，最后写 artifacts.json
        except Exception as e:  # noqa: BLE001
            _log.exception("产物发布失败")
            artifact_error = f"{type(e).__name__}: {e}"

    if artifact_error is not None:
        # 规划本身成功了（state 已落盘），只是产物没做出来——详情页会显示
        # 「产物待重建」+ 重建按钮，不给死链（spec §4.1）。
        return JobOutcome.ok(state.revision, "ArtifactError", artifact_error)
    return JobOutcome.ok(state.revision)


def rebuild_artifacts(
    trips_root,
    tid: str,
    deps,
    job,
    *,
    stage_fn=_stage_artifacts,
    publish_fn=_publish,
    discard_fn=_discard,
    open_fn=open_trip,
) -> JobOutcome:
    """从 state.json 重跑 stage + publish，**不碰 LLM、不碰 CAS**（spec §6.2）。

    崩溃恢复、旧版本产物残留、手工删文件，三种情况共用这一条出路。
    """
    try:
        repo = open_fn(trips_root, tid)
        state = repo.load()
    except (TripNotFound, TripCorrupt) as e:
        return JobOutcome.failed(type(e).__name__, str(e))

    if state.stage is not Stage.DONE:
        return JobOutcome.failed("NotDone", "行程还没定稿，没有可重建的成稿产物")

    staged = None
    try:
        staged = stage_fn(state, repo.dir, deps.provider, job.job_id)
        if job.cancel_token.is_set():
            discard_fn(staged)
            return JobOutcome.cancelled()
        publish_fn(staged)
    except Exception as e:  # noqa: BLE001
        _log.exception("产物重建失败")
        discard_fn(staged)
        return JobOutcome.failed(type(e).__name__, str(e))

    return JobOutcome.ok(state.revision)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `/Users/jialiu/Projects/trip-plan/.venv/bin/python -m pytest tests/web/test_jobs_run_command.py -v && /Users/jialiu/Projects/trip-plan/.venv/bin/python -m pytest -q`
Expected: 13 条全绿；全量 `676 passed, 1 deselected`

- [ ] **Step 5: 格式化并提交**

```bash
/Users/jialiu/Projects/trip-plan/.venv/bin/black src tests
git add src/tripplan/web/jobs.py tests/web/test_jobs_run_command.py
git commit -m "feat(web): run_command —— load/advance/暂存/CAS/发布 的唯一一份提交纪律"
```

---

## Task 7: `web/jobs.py` 之二 —— `TripJob` / `JobRegistry` 生命周期

**Files:**
- Modify: `src/tripplan/web/jobs.py`（追加，不改 Task 6 写好的部分）
- Create: `tests/web/test_jobs_registry.py`

**Interfaces:**
- Consumes: Task 5 的 `EventLog`，Task 6 的 `JobOutcome`
- Produces:
  - `ACTIVE = frozenset({"running", "cancelling"})`
  - `class TripBusy(Exception)` / `class ServerBusy(Exception)`
  - `class TripJob(tid, log, clock=time.time)`：属性 `job_id`（uuid4 hex，永不复用）、`tid`、`cancel_token`（`threading.Event`）、`status`、`status_version`（从 1 开始）、`kind`、`message`、`thread`、`finished_at`；方法 `active` (property)、`emit(event)`、`request_cancel() -> bool`、`finish(outcome)`、`emit_terminal(outcome)`、`snapshot() -> dict`
  - `class JobRegistry(max_jobs=3, retain_s=1800, clock=time.time)`：方法 `get(tid) -> TripJob|None`、`active_count() -> int`、`start(tid, log, target) -> TripJob`（`target(job) -> JobOutcome`）

- [ ] **Step 1: 写失败的测试**

新建 `tests/web/test_jobs_registry.py`：

```python
"""job 生命周期、per-trip 互斥、全局上限（spec §4.1.1 / §4.2 / §9 回归 18、21、22、23）。"""

import json
import threading

import pytest

from tripplan.web.events import EventLog
from tripplan.web.jobs import JobOutcome, JobRegistry, ServerBusy, TripBusy, TripJob


def _log(tmp_path, tid="kyoto"):
    return EventLog(tmp_path / tid / "events.jsonl")


def _blocking_target(gate):
    def target(job):
        gate.wait(5)
        return JobOutcome.ok(1)
    return target


def test_a_finished_job_reports_its_terminal_status(tmp_path):
    reg = JobRegistry()
    job = reg.start("kyoto", _log(tmp_path), lambda j: JobOutcome.ok(7))
    job.thread.join(5)
    assert job.status == "succeeded"
    assert job.active is False
    assert reg.get("kyoto") is job


def test_status_version_starts_at_one_and_increments_on_every_change(tmp_path):
    job = TripJob("kyoto", _log(tmp_path))
    assert (job.status, job.status_version) == ("running", 1)
    job.request_cancel()
    assert (job.status, job.status_version) == ("cancelling", 2)
    job.finish(JobOutcome.cancelled())
    assert (job.status, job.status_version) == ("cancelled", 3)


def test_repeated_cancel_is_a_no_op_and_does_not_bump_the_version(tmp_path):
    """否则每点一下取消都让所有标签页白刷一次（spec §6.2）。"""
    job = TripJob("kyoto", _log(tmp_path))
    assert job.request_cancel() is True
    assert job.request_cancel() is False
    assert job.status_version == 2
    assert job.cancel_token.is_set()


def test_cancelling_counts_as_active(tmp_path):
    """spec §4.2：cancelling 是**正在退出**，不是**已经退出**。"""
    job = TripJob("kyoto", _log(tmp_path))
    job.request_cancel()
    assert job.active is True


def test_the_same_trip_cannot_start_two_jobs(tmp_path):
    """§9 回归 1 的 registry 层。互斥是为了省钱：输掉 CAS 的那个线程
    已经把 LLM 的钱烧完了才发现自己白干（spec §4.2）。"""
    reg = JobRegistry()
    gate = threading.Event()
    reg.start("kyoto", _log(tmp_path), _blocking_target(gate))
    with pytest.raises(TripBusy):
        reg.start("kyoto", _log(tmp_path), lambda j: JobOutcome.ok(1))
    gate.set()


def test_a_cancelling_job_still_blocks_the_same_trip(tmp_path):
    """§9 回归 23 的一部分：那个线程还没退出，放第二个进来就是两个线程
    同时对一份 state 跑 advance。"""
    reg = JobRegistry()
    gate = threading.Event()
    job = reg.start("kyoto", _log(tmp_path), _blocking_target(gate))
    job.request_cancel()
    with pytest.raises(TripBusy):
        reg.start("kyoto", _log(tmp_path), lambda j: JobOutcome.ok(1))
    gate.set()
    job.thread.join(5)
    reg.start("kyoto", _log(tmp_path), lambda j: JobOutcome.ok(1))  # 退出后恢复正常


def test_a_cancelling_job_still_occupies_a_global_slot(tmp_path):
    """§9 回归 23 的另一部分：名额只在线程真正退出时归还。提前释放等于
    允许两个线程同时跑 advance，既烧双份钱又让互斥形同虚设（spec §4.2）。"""
    reg = JobRegistry(max_jobs=1)
    gate = threading.Event()
    job = reg.start("kyoto", _log(tmp_path), _blocking_target(gate))
    job.request_cancel()
    with pytest.raises(ServerBusy):
        reg.start("osaka", _log(tmp_path, "osaka"), lambda j: JobOutcome.ok(1))
    gate.set()
    job.thread.join(5)
    reg.start("osaka", _log(tmp_path, "osaka"), lambda j: JobOutcome.ok(1))


def test_the_global_cap_rejects_a_second_trip(tmp_path):
    """§9 回归 18 的 registry 层：满载即拒，不排队（spec §4.2）。"""
    reg = JobRegistry(max_jobs=1)
    gate = threading.Event()
    reg.start("kyoto", _log(tmp_path), _blocking_target(gate))
    with pytest.raises(ServerBusy):
        reg.start("osaka", _log(tmp_path, "osaka"), lambda j: JobOutcome.ok(1))
    gate.set()


def test_an_unexpected_exception_never_leaves_a_job_running(tmp_path):
    """§9 回归 21：少了 try/except/finally 这层，一个没想到的异常会让 job
    永远停在 running，详情页的按钮就永久置灰了，用户除了重启服务没有任何
    出路（spec §4.1.1）。"""
    reg = JobRegistry()
    job = reg.start("kyoto", _log(tmp_path), lambda j: (_ for _ in ()).throw(KeyError("boom")))
    job.thread.join(5)
    assert job.status == "failed"
    assert job.kind == "KeyError"


def test_a_terminal_durable_event_lands_on_disk_with_the_job_id(tmp_path):
    """§9 回归 21 后半条 + spec §4.1.1：JobRegistry 是进程内内存对象，
    服务器一重启就空了；有这条落盘记录，重启后进详情页仍能解释
    「上次那步发生了什么」，而不是一片空白。"""
    reg = JobRegistry()
    log = _log(tmp_path)
    job = reg.start("kyoto", log, lambda j: (_ for _ in ()).throw(KeyError("boom")))
    job.thread.join(5)

    lines = (tmp_path / "kyoto" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    terminal = [json.loads(x) for x in lines if json.loads(x)["type"].startswith("job_")]
    assert terminal[-1]["type"] == "job_failed"
    assert terminal[-1]["payload"]["job_id"] == job.job_id


def test_the_terminal_event_is_written_before_the_status_flips(tmp_path):
    """顺序是刻意的：前端看到终态就停止轮询，终态事件必须先在流里。"""
    reg = JobRegistry()
    log = _log(tmp_path)
    seen = []
    original = log.append

    def spy(type, payload, stream_id=None, durable=True):
        if type.startswith("job_"):
            seen.append(reg.get("kyoto").status)
        return original(type, payload, stream_id, durable)

    log.append = spy
    job = reg.start("kyoto", log, lambda j: JobOutcome.ok(1))
    job.thread.join(5)
    assert seen == ["running"]  # 写终态事件的那一刻，status 还没翻


def test_emit_never_raises_even_when_the_log_is_broken(tmp_path):
    """spec §5.1.1：job.emit() 自己是不抛异常的边界。"""
    log = _log(tmp_path)

    def boom(*a, **kw):
        raise OSError("磁盘满")

    log.append = boom
    job = TripJob("kyoto", log)
    job.emit(("generating", "A"))       # 不抛
    job.emit(("requirements_patched", {"destination": "大阪"}))


def test_emit_normalises_the_tuple_envelope(tmp_path):
    """现有 emit 收的是变长 tuple；Web 层装一个 adapter 归一化，
    **不修改 orchestrator 的 emit 契约**（spec §5.1）。"""
    log = _log(tmp_path)
    job = TripJob("kyoto", log)
    job.emit(("revision", "foodie", 2))
    ev = log.since(0).events[-1]
    assert ev.type == "revision"
    assert ev.payload == {"args": ["foodie", 2]}


def test_a_new_job_replaces_the_previous_terminal_one(tmp_path):
    """§9 回归 22 的 registry 层：job_id 永不复用，status_version 从 1 重来。"""
    reg = JobRegistry()
    first = reg.start("kyoto", _log(tmp_path), lambda j: JobOutcome.failed("X", "y"))
    first.thread.join(5)
    second = reg.start("kyoto", _log(tmp_path), lambda j: JobOutcome.ok(1))
    second.thread.join(5)

    assert first.job_id != second.job_id
    assert reg.get("kyoto") is second


def test_terminal_jobs_are_reaped_after_the_retention_window(tmp_path):
    clock = {"t": 1000.0}
    reg = JobRegistry(retain_s=1800, clock=lambda: clock["t"])
    job = reg.start("kyoto", _log(tmp_path), lambda j: JobOutcome.ok(1))
    job.thread.join(5)

    clock["t"] += 1801
    reg.start("osaka", _log(tmp_path, "osaka"), lambda j: JobOutcome.ok(1)).thread.join(5)
    assert reg.get("kyoto") is None


def test_active_jobs_are_never_reaped(tmp_path):
    """spec §4.1.1：cancelling 也算 active，它的线程还活着。"""
    clock = {"t": 1000.0}
    reg = JobRegistry(retain_s=1, clock=lambda: clock["t"])
    gate = threading.Event()
    job = reg.start("kyoto", _log(tmp_path), _blocking_target(gate))
    job.request_cancel()
    clock["t"] += 9999
    reg.start("osaka", _log(tmp_path, "osaka"), lambda j: JobOutcome.ok(1))
    assert reg.get("kyoto") is job
    gate.set()


def test_snapshot_has_the_wire_shape_the_polling_endpoint_expects(tmp_path):
    job = TripJob("kyoto", _log(tmp_path))
    snap = job.snapshot()
    assert set(snap) == {"id", "status", "status_version", "kind", "message"}
    assert snap["status"] == "running"
    assert snap["id"] == job.job_id
```

- [ ] **Step 2: 跑测试确认它失败**

Run: `/Users/jialiu/Projects/trip-plan/.venv/bin/python -m pytest tests/web/test_jobs_registry.py -v`
Expected: FAIL，`ImportError: cannot import name 'JobRegistry' from 'tripplan.web.jobs'`

- [ ] **Step 3: 写最小实现**

在 `src/tripplan/web/jobs.py` 顶部补 import：

```python
import threading
import time
import uuid
```

在文件末尾追加：

```python
#: **唯一的 active 定义。不许有任何一处写成只判断 "running"**（spec §4.2）。
#: 取消不是立刻生效的，也没有时延上界：一个 cancelling 的 job，它的线程可能
#: 还卡在一次已经发出去的 LLM 请求上，还在烧钱，还握着那个 trip 的 state。
ACTIVE = frozenset({"running", "cancelling"})

_TERMINAL_EVENT = {
    "succeeded": "job_succeeded",
    "rejected": "job_rejected",
    "failed": "job_failed",
    "cancelled": "job_cancelled",
}


class TripBusy(Exception):
    """该 trip 已有 active job。路由层转成 409。"""


class ServerBusy(Exception):
    """全局 active job 数已满。路由层转成 503——不排队（spec §4.1.1）。"""


class TripJob:
    """不是「跑完就没人管的一个线程」：详情页和轮询都要能回答
    「刚才那次命令怎么样了」，所以它有一个明确的状态机（spec §4.1.1）。

        running ──┬─→ succeeded
                  ├─→ rejected
                  ├─→ failed
                  └─→ cancelling ─→ cancelled
    """

    def __init__(self, tid: str, log, clock=time.time) -> None:
        #: uuid4 hex，**永不复用**。它同时是暂存目录名（spec §4.1）和前端的
        #: 身份判据：status_version 是 per-job 的，新 job 从 1 重新开始，于是
        #: 「老 job 的 running(1)」与「新 job 的 running(1)」在前端看来一模
        #: 一样。对外暴露的身份必须是 (job_id, status_version) 这个二元组。
        self.job_id = uuid.uuid4().hex
        self.tid = tid
        self.cancel_token = threading.Event()
        self.status = "running"
        self.status_version = 1
        self.kind: str | None = None
        self.message: str | None = None
        self.thread: threading.Thread | None = None
        self.finished_at: float | None = None
        self._log = log
        self._clock = clock
        self._lock = threading.Lock()

    @property
    def active(self) -> bool:
        return self.status in ACTIVE

    # ---------- 事件 ----------

    def emit(self, event) -> None:
        """**不抛异常的边界**（spec §5.1.1）。事件日志坏了就少几行进度，
        它没有资格决定一次规划算不算数。

        顺带做 §5.1 的信封归一：现有 emit 收的是变长 tuple，这里摊成
        type + {"args": [...]}，**不修改 orchestrator 的 emit 契约**。
        """
        try:
            parts = tuple(event)
            self._log.append(str(parts[0]), {"args": list(parts[1:])})
        except Exception:  # noqa: BLE001
            _log.exception("事件记录失败，已忽略")

    def emit_terminal(self, outcome: JobOutcome) -> None:
        """终态写 durable event，并 flush 一次。

        带 job_id：重启后回读 events.jsonl 也能把终态对到具体某一次命令上，
        而不是只知道「有过一次失败」（spec §4.1.1）。
        """
        try:
            self._log.append(
                _TERMINAL_EVENT.get(outcome.status, "job_failed"),
                {
                    "job_id": self.job_id,
                    "kind": outcome.kind,
                    "message": outcome.message,
                },
            )
            self._log.flush()
        except Exception:  # noqa: BLE001
            _log.exception("终态事件记录失败，已忽略")

    # ---------- 状态迁移 ----------

    def request_cancel(self) -> bool:
        """幂等：已经是 cancelling / 终态时是 no-op，**不再 +1
        status_version**（否则每点一下都让所有标签页白刷一次，spec §6.2）。"""
        with self._lock:
            if self.status != "running":
                return False
            self.cancel_token.set()
            self.status = "cancelling"
            self.status_version += 1
            return True

    def finish(self, outcome: JobOutcome) -> None:
        with self._lock:
            self.status = outcome.status
            self.kind = outcome.kind
            self.message = outcome.message
            self.status_version += 1
            self.finished_at = self._clock()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "id": self.job_id,
                "status": self.status,
                "status_version": self.status_version,
                "kind": self.kind,
                "message": self.message,
            }


class JobRegistry:
    """**进程内单例**（spec §6.4）。多 worker 会让轮询请求被路由到没有该 job
    的进程，进度页随机失灵、取消按钮随机失效——这正是选 waitress（单进程
    多线程）而不是 gunicorn 的实质理由。"""

    def __init__(self, max_jobs: int = 3, retain_s: float = 1800.0, clock=time.time):
        self._max_jobs = max_jobs
        self._retain_s = retain_s
        self._clock = clock
        self._lock = threading.Lock()
        self._jobs: dict[str, TripJob] = {}

    def get(self, tid: str) -> TripJob | None:
        with self._lock:
            return self._jobs.get(tid)

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for j in self._jobs.values() if j.active)

    def start(self, tid: str, log, target) -> TripJob:
        """target(job) -> JobOutcome。两个约束在同一把锁里判，避免竞态。"""
        with self._lock:
            self._reap_locked()
            existing = self._jobs.get(tid)
            if existing is not None and existing.active:
                raise TripBusy(tid)
            if sum(1 for j in self._jobs.values() if j.active) >= self._max_jobs:
                # 不排队：排队要额外引入队列超时、取消排队中的 job，以及
                # 「按钮点了但什么都没发生」的解释成本（spec §4.1.1）。
                raise ServerBusy()

            job = TripJob(tid, log, clock=self._clock)
            self._jobs[tid] = job
            job.thread = threading.Thread(
                target=self._run, args=(job, target), name=f"trip-job-{tid}", daemon=True
            )
            job.thread.start()
            return job

    @staticmethod
    def _run(job: TripJob, target) -> None:
        """线程体必须 try/except/finally 收尾（spec §4.1.1）。

        except BaseException 兜住任何未预料的异常并转成 failed；finally 里
        无条件置终态 + flush EventLog。少了这层，一个没想到的异常会让 job
        永远停在 running，详情页的按钮就永久置灰了。
        """
        outcome = JobOutcome.failed("UnknownError", "job 线程异常退出")
        try:
            outcome = target(job)
        except BaseException as e:  # noqa: BLE001 — 故意兜底，见 docstring
            _log.exception("job 线程未预料异常")
            outcome = JobOutcome.failed(type(e).__name__, str(e) or type(e).__name__)
        finally:
            # 顺序刻意：前端看到终态就停止轮询，终态事件必须先在流里。
            job.emit_terminal(outcome)
            job.finish(outcome)

    def _reap_locked(self) -> None:
        """终态 job 留在 registry 里供轮询读取，直到被新 job 替换或超过保留
        窗口。**active 的 job 永远不会被清理**——cancelling 也算 active。
        丢了也不损失信息，终态已经在 events.jsonl 里（spec §4.1.1）。"""
        now = self._clock()
        for tid, job in list(self._jobs.items()):
            if job.active or job.finished_at is None:
                continue
            if now - job.finished_at > self._retain_s:
                del self._jobs[tid]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `/Users/jialiu/Projects/trip-plan/.venv/bin/python -m pytest tests/web/test_jobs_registry.py -v && /Users/jialiu/Projects/trip-plan/.venv/bin/python -m pytest -q`
Expected: 17 条全绿；全量 `693 passed, 1 deselected`

- [ ] **Step 5: 格式化并提交**

```bash
/Users/jialiu/Projects/trip-plan/.venv/bin/black src tests
git add src/tripplan/web/jobs.py tests/web/test_jobs_registry.py
git commit -m "feat(web): TripJob/JobRegistry —— 终态必达、cancelling 算 active、满载即拒"
```

---

## Task 8: `web/view.py` —— 结构化 view model

**Files:**
- Create: `src/tripplan/web/view.py`
- Create: `tests/web/test_view.py`
- Modify: `src/tripplan/render/requirement_card.py`（`_LABELS` → 公开的 `FIELD_LABELS`，渲染逻辑不动）

**Interfaces:**
- Consumes: Task 7 的 `JobRegistry`（`trip_rows` 用它判「是否正在跑」）、Task 4 的 `artifact_ready`
- Produces:
  - `@dataclass(frozen=True) class FieldVM: name:str; label:str; value:str; is_inferred:bool; rationale:str`
  - `@dataclass(frozen=True) class ReqCardVM: fields:list[FieldVM]; missing:list[str]`
  - `@dataclass(frozen=True) class IssueVM: mark:str; severity:str; message:str`
  - `@dataclass(frozen=True) class CandidateVM: key:str; title:str; description:str; selectable:bool; days:int; activities:int; status:str; detail:str; issues:list[IssueVM]`
  - `@dataclass(frozen=True) class TripRowVM: tid:str; summary:str; stage:str; stage_text:str; revision:int; mtime:float; running:bool; corrupt:bool; error:str`（`stage` 是机器值如 `AWAIT_CHOICE`，`stage_text` 是中文「等你选方案」；模板显示后者、测试断言前者）
  - `def req_card_vm(reqs) -> ReqCardVM`
  - `def candidate_vms(slots) -> list[CandidateVM]`
  - `def trip_rows(trips_root, registry) -> list[TripRowVM]`
  - `def event_text(ev: dict) -> str`

- [ ] **Step 1: 写失败的测试**

新建 `tests/web/test_view.py`：

```python
"""模板只吃结构化对象，绝不吃 Markdown 串（spec §6.1）。"""

from datetime import date

from tripplan.models.common import Field, Origin
from tripplan.models.issue import Issue, Severity, Source
from tripplan.models.itinerary import Angle, Day, Itinerary
from tripplan.models.requirements import DateRange, Party, Requirements
from tripplan.repo import FileRepo
from tripplan.state import CandidateSlot, SlotStatus, Stage, TripState
from tripplan.web.jobs import JobRegistry
from tripplan.web.view import candidate_vms, event_text, req_card_vm, trip_rows

D1 = date(2026, 10, 1)


def test_req_card_vm_splits_fields_into_independent_attributes():
    """把 Requirements 摊成模板能直接遍历的字段：label、值、is_inferred、
    rationale 各自独立，模板自己出 HTML 结构。"""
    reqs = Requirements(
        destination=Field("京都", Origin.USER),
        dates=Field(DateRange(D1, D1), Origin.USER),
        party=Field(Party(adults=2), Origin.USER),
        pace=Field(None, None),
        lodging_area=Field("四条", Origin.MODEL, rationale="按预算推断"),
    )
    vm = req_card_vm(reqs)
    by_name = {f.name: f for f in vm.fields}

    assert by_name["destination"].label == "目的地"
    assert by_name["destination"].value == "京都"
    assert by_name["destination"].is_inferred is False
    assert by_name["lodging_area"].is_inferred is True
    assert by_name["lodging_area"].rationale == "按预算推断"
    assert "pace" not in by_name          # 无取值的字段不出现
    assert vm.missing == []


def test_req_card_vm_lists_missing_required_labels():
    vm = req_card_vm(Requirements(destination=Field("京都", Origin.USER)))
    assert vm.missing == ["日期", "人员"]


def test_req_card_vm_never_returns_markdown():
    """反面断言：这是把 render_requirement_card() 塞进 Jinja 的那条路
    被禁掉的原因——autoescape 开着就显示 `**目的地**` 的星号，用 |safe
    就等于把模型输出与用户输入当可信 HTML 注入（spec §6.1）。"""
    vm = req_card_vm(
        Requirements(destination=Field("京都", Origin.USER))
    )
    blob = "".join(f.label + f.value + f.rationale for f in vm.fields)
    assert "**" not in blob
    assert "- " not in blob


def test_candidate_vm_carries_counts_status_and_issues():
    angle = Angle("foodie", "吃遍京都", "从早市到居酒屋")
    itin = Itinerary(
        angle=angle,
        days=[Day(id="d1", date=D1, activities=[]), Day(id="d2", date=D1, activities=[])],
        issues=[Issue(Severity.WARNING, Source.CRITIC, "C1", "第二天略赶")],
    )
    [vm] = candidate_vms([CandidateSlot(angle, itin, None, SlotStatus.EXHAUSTED, "修订 3 次后仍有 1 个硬伤")])

    assert vm.key == "foodie"
    assert vm.title == "吃遍京都"
    assert vm.days == 2
    assert vm.activities == 0
    assert vm.selectable is True
    assert vm.status == "EXHAUSTED"
    assert vm.detail == "修订 3 次后仍有 1 个硬伤"
    assert [i.message for i in vm.issues] == ["第二天略赶"]
    assert vm.issues[0].mark == "🟡"


def test_a_candidate_without_an_itinerary_is_not_selectable():
    angle = Angle("_error", "角度生成失败", "")
    [vm] = candidate_vms([CandidateSlot(angle, None, None, SlotStatus.FAILED, "角度生成失败：限流")])
    assert vm.selectable is False
    assert vm.detail == "角度生成失败：限流"


def test_trip_rows_marks_a_corrupt_directory_instead_of_blowing_up(tmp_path):
    """§9 回归 9 的数据层：一个坏目录不能把整页炸掉（spec §6.1）。"""
    good = FileRepo(tmp_path / "kyoto")
    state = TripState.new("十一想去京都玩5天", run_id="r1")
    state.stage, state.revision = Stage.AWAIT_CHOICE, 3
    good.create(state)

    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "state.json").write_text("not json at all", encoding="utf-8")

    (tmp_path / "not-a-trip").mkdir()  # 连 state.json 都没有：直接跳过

    rows = {r.tid: r for r in trip_rows(tmp_path, JobRegistry())}
    assert rows["kyoto"].corrupt is False
    assert rows["kyoto"].stage == "AWAIT_CHOICE"
    assert rows["kyoto"].revision == 3
    assert "京都" in rows["kyoto"].summary
    assert rows["broken"].corrupt is True
    assert "not-a-trip" not in rows


def test_trip_rows_reports_a_running_job(tmp_path):
    import threading

    from tripplan.web.events import EventLog
    from tripplan.web.jobs import JobOutcome

    FileRepo(tmp_path / "kyoto").create(TripState.new("去京都", run_id="r1"))
    reg = JobRegistry()
    gate = threading.Event()
    job = reg.start("kyoto", EventLog(tmp_path / "kyoto" / "events.jsonl"),
                    lambda j: (gate.wait(5), JobOutcome.ok(1))[1])
    try:
        assert trip_rows(tmp_path, reg)[0].running is True
    finally:
        gate.set()
        job.thread.join(5)


def test_event_text_renders_every_known_event_type_in_chinese():
    """事件文案在**服务端**渲染（spec §6.3 结尾），前端只负责追加 DOM。"""
    cases = [
        ({"type": "stage_started", "payload": {"args": ["GENERATE"]}}, "生成候选"),
        ({"type": "angles_picked", "payload": {"args": [["A", "B"]]}}, "A"),
        ({"type": "generating", "payload": {"args": ["foodie"]}}, "foodie"),
        ({"type": "revision", "payload": {"args": ["foodie", 0]}}, "foodie"),
        ({"type": "requirements_patched", "payload": {"args": [{"destination": "大阪"}]}}, "需求"),
        ({"type": "angle_generation_failed", "payload": {"args": ["限流"]}}, "限流"),
        ({"type": "diversity_retry", "payload": {"args": ["B", ["B001"]]}}, "B"),
        ({"type": "paused", "payload": {"args": ["AWAIT_CHOICE", 4]}}, "等你"),
        ({"type": "job_failed", "payload": {"job_id": "x", "kind": "ProviderError", "message": "高德限流"}}, "高德限流"),
        ({"type": "job_cancelled", "payload": {"job_id": "x", "kind": "Cancelled", "message": "已取消"}}, "取消"),
    ]
    for ev, needle in cases:
        assert needle in event_text(ev), ev["type"]


def test_event_text_degrades_gracefully_on_malformed_payloads():
    """事件是从磁盘回读的，可能是旧版本写的、也可能残缺。渲染函数不许崩。"""
    assert event_text({"type": "generating", "payload": {}}) == "generating"
    assert event_text({"type": "未来的类型", "payload": {"args": [1]}}) == "未来的类型"
    assert event_text({}) == ""
```

- [ ] **Step 2: 跑测试确认它失败**

Run: `/Users/jialiu/Projects/trip-plan/.venv/bin/python -m pytest tests/web/test_view.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'tripplan.web.view'`

- [ ] **Step 3: 先把标签常量改成公开名**

`src/tripplan/render/requirement_card.py`：把 `_LABELS` 改名为 `FIELD_LABELS`（定义处与函数体内两处引用），其余一个字不动。

```python
#: 字段名 → 中文标签。**是数据不是渲染串**，所以可以被 web/view.py 复用；
#: 本模块产出的 Markdown 串则不共享（spec §6.1）。
FIELD_LABELS = {
    "destination": "目的地",
    ...
}
```

- [ ] **Step 4: 写 `web/view.py`**

```python
"""结构化 view model。模板遍历字段、自己出 HTML 结构。

**绝不复用 CLI 的 Markdown 渲染器**（spec §6.1）：
render_requirement_card() / render_candidates() 返回的是 Markdown 串
（"## 需求确认"、"- **目的地**：…"）。塞进 Jinja 只有两种结局，都不能要——
autoescape 开着就在页面上显示 Markdown 源文；用 |safe 当 HTML 就等于把
field.rationale / slot.detail / angle.title / Issue.message / raw_request
这些**模型输出或用户输入的自由文本**当可信 HTML 注入，直接开一个 XSS 面，
而这个服务还要暴露在局域网上给别人访问。
"""

from dataclasses import dataclass
from pathlib import Path

from tripplan.artifacts import artifact_ready  # noqa: F401  （详情页要用，这里一并导出）
from tripplan.models.common import Origin
from tripplan.models.requirements import describe_value, missing_required
from tripplan.render import SEVERITY_MARK
from tripplan.render.requirement_card import FIELD_LABELS
from tripplan.repo import FileRepo, TripCorrupt, TripNotFound
from tripplan.wire import UnsupportedVersion

_SUMMARY_CHARS = 60

_STAGE_TEXT = {
    "COLLECT": "收集需求",
    "AWAIT_REQ_CONFIRM": "等你确认需求",
    "GENERATE": "生成候选",
    "AWAIT_CHOICE": "等你选方案",
    "REFINE": "按意见打磨",
    "DONE": "已定稿",
}


@dataclass(frozen=True)
class FieldVM:
    name: str
    label: str
    value: str
    is_inferred: bool
    rationale: str


@dataclass(frozen=True)
class ReqCardVM:
    fields: list[FieldVM]
    missing: list[str]


@dataclass(frozen=True)
class IssueVM:
    mark: str
    severity: str
    message: str


@dataclass(frozen=True)
class CandidateVM:
    key: str
    title: str
    description: str
    selectable: bool
    days: int
    activities: int
    status: str
    detail: str
    issues: list[IssueVM]


@dataclass(frozen=True)
class TripRowVM:
    tid: str
    summary: str
    stage: str
    stage_text: str
    revision: int
    mtime: float
    running: bool
    corrupt: bool
    error: str


def req_card_vm(reqs) -> ReqCardVM:
    fields = []
    for name, label in FIELD_LABELS.items():
        field = getattr(reqs, name)
        if field.value is None:
            continue
        fields.append(
            FieldVM(
                name=name,
                label=label,
                value=describe_value(field.value),
                is_inferred=field.origin is Origin.MODEL,
                rationale=field.rationale or "",
            )
        )
    return ReqCardVM(fields, [FIELD_LABELS[n] for n in missing_required(reqs)])


def candidate_vms(slots) -> list[CandidateVM]:
    out = []
    for slot in slots or []:
        itin = slot.itinerary
        out.append(
            CandidateVM(
                key=slot.angle.key,
                title=slot.angle.title,
                description=slot.angle.description or "",
                # 一份「主体已生成、critic 挂了」的行程仍然可选，比强行剥夺
                # 选择更合理——与 render/candidates.py 的判据保持一致。
                selectable=itin is not None,
                days=len(itin.days) if itin else 0,
                activities=sum(len(d.activities) for d in itin.days) if itin else 0,
                status=slot.status.value,
                detail=slot.detail or "",
                issues=[
                    IssueVM(SEVERITY_MARK[i.severity], i.severity.value, i.message)
                    for i in (itin.issues if itin else [])
                ],
            )
        )
    return out


def trip_rows(trips_root, registry) -> list[TripRowVM]:
    """单个目录的 TripCorrupt / TripNotFound 要单独标记为「损坏」并继续，
    不能让一个坏目录把整页炸掉（spec §6.1）。"""
    root = Path(trips_root)
    rows: list[TripRowVM] = []
    if not root.is_dir():
        return rows
    for child in sorted(root.iterdir()):
        state_path = child / "state.json"
        if not child.is_dir() or not state_path.exists():
            continue
        job = registry.get(child.name)
        running = job is not None and job.active
        try:
            state = FileRepo(child).load()
        except (TripCorrupt, TripNotFound, UnsupportedVersion) as e:
            rows.append(
                TripRowVM(child.name, "", "", "", 0, state_path.stat().st_mtime,
                          running, True, str(e))
            )
            continue
        rows.append(
            TripRowVM(
                tid=child.name,
                summary=_summary(state.raw_request),
                stage=state.stage.value,
                stage_text=_STAGE_TEXT.get(state.stage.value, state.stage.value),
                revision=state.revision,
                mtime=state_path.stat().st_mtime,
                running=running,
                corrupt=False,
                error="",
            )
        )
    rows.sort(key=lambda r: r.mtime, reverse=True)
    return rows


def _summary(raw: str) -> str:
    text = " ".join((raw or "").split())
    return text if len(text) <= _SUMMARY_CHARS else text[:_SUMMARY_CHARS] + "…"


# ---------- 事件文案（服务端渲染，前端只负责追加 DOM，spec §6.3） ----------

_EVENT_TEXT = {
    "stage_started": lambda a: f"开始{_STAGE_TEXT.get(a[0], a[0])}",
    "angles_picked": lambda a: "已确定切入角度：" + "、".join(str(k) for k in a[0]),
    "generating": lambda a: f"正在生成候选 {a[0]}",
    "revision": lambda a: f"候选 {a[0]} 第 {int(a[1]) + 1} 轮修订",
    "requirements_patched": lambda a: "需求已更新",
    "angle_generation_failed": lambda a: f"角度生成失败：{a[0]}",
    "diversity_retry": lambda a: f"候选 {a[0]} 与其它候选重合，重跑一次",
    "paused": lambda a: f"暂停：{_STAGE_TEXT.get(a[0], a[0])}",
}

_JOB_TEXT = {
    "job_succeeded": "这一步完成",
    "job_rejected": "命令被拒绝",
    "job_failed": "这一步失败",
    "job_cancelled": "已取消",
}


def event_text(ev: dict) -> str:
    """事件是从磁盘回读的，可能是旧版本写的、也可能残缺——渲染函数不许崩。"""
    etype = (ev or {}).get("type", "")
    payload = (ev or {}).get("payload") or {}
    render = _EVENT_TEXT.get(etype)
    if render is not None:
        try:
            return render(payload.get("args") or [])
        except (IndexError, KeyError, TypeError, ValueError):
            return etype
    if etype in _JOB_TEXT:
        message = payload.get("message")
        return _JOB_TEXT[etype] + (f"：{message}" if message else "")
    return etype
```

- [ ] **Step 5: 跑测试确认通过**

Run: `/Users/jialiu/Projects/trip-plan/.venv/bin/python -m pytest tests/web/test_view.py tests/render/test_requirement_card.py -v && /Users/jialiu/Projects/trip-plan/.venv/bin/python -m pytest -q`
Expected: 10 条新用例绿，`render` 的既有用例不受影响；全量 `703 passed, 1 deselected`

- [ ] **Step 6: 格式化并提交**

```bash
/Users/jialiu/Projects/trip-plan/.venv/bin/black src tests
git add src/tripplan/web/view.py src/tripplan/render/requirement_card.py tests/web/test_view.py
git commit -m "feat(web): 结构化 view model —— 模板只吃对象，不碰 Markdown 串"
```

---

## Task 9: `web/app.py` —— 应用工厂、Basic Auth、CSRF、tid 校验、列表页

**Files:**
- Create: `src/tripplan/web/app.py`
- Create: `src/tripplan/web/templates/base.html`
- Create: `src/tripplan/web/templates/index.html`
- Create: `src/tripplan/web/templates/notice.html`
- Create: `src/tripplan/web/static/app.css`
- Create: `tests/web/conftest.py`
- Create: `tests/web/test_app_auth.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`（**和 `pyproject.toml` 一起改、一起提交**，理由见 Step 1）

**Interfaces:**
- Consumes: Task 5 的 `EventLogStore`、Task 7 的 `JobRegistry`、Task 8 的 `trip_rows`
- Produces:
  - `def create_app(trips_root, deps, *, token=None, secret=None, registry=None, store=None, max_jobs=3, run_command_fn=run_command, rebuild_fn=rebuild_artifacts) -> Flask`
  - `def resolve_trip_dir(trips_root, tid) -> Path`（越界一律 `abort(404)`）
  - Jinja 全局 `csrf_token()`
  - 路由 `GET /`（endpoint 名 `index`）
  - `app.extensions["tripplan"]` 里挂 `{"trips_root", "deps", "registry", "store", "run_command_fn", "rebuild_fn"}`，供后续任务的路由取用

- [ ] **Step 1: 加依赖、更新锁文件、装上**

`pyproject.toml`：

```toml
[project.optional-dependencies]
openai = ["openai>=3.13"]
web = ["flask>=3", "waitress>=3"]
dev = ["pytest>=8", "pytest-cov>=5", "black>=24", "openai>=3.13", "flask>=3", "waitress>=3"]
```

**仓库里有一份 `uv.lock`（35 个包，当前 `uv lock --check` 是过的），它必须跟着一起改。**
只动 `pyproject.toml` 会让锁文件和声明当场对不上：`uv lock --check` 立刻报
`The lockfile at uv.lock needs to be updated`，`uv sync --locked` / `uv sync --frozen` 直接失败，
全新检出的人复现不出这个环境，而 `uv pip install 'flask>=3'` 这种命令是**绕过锁文件**的——它只
改了本机这一个 venv，锁文件依旧不知道 flask 的存在。锁文件是这个仓库对「依赖到底是哪几个、
哪些版本」的唯一权威记录，漏掉它等于把本机 venv 当成了事实来源。

```bash
# ① 重新解析并写回 uv.lock（只动锁文件，不建任何 venv）
uv lock

# ② 验收：锁文件与 pyproject 一致（这条命令在 ① 之前必然失败，之后必然通过）
uv lock --check

# ③ 装进主仓库的 venv（Global Constraint 10：本项目的开发 venv 就是它，
#    worktree 下不建 .venv，所以这里用 uv pip install --python 指名道姓，
#    而不是会在 cwd 新建 .venv 的 uv sync）
uv pip install --python /Users/jialiu/Projects/trip-plan/.venv/bin/python 'flask>=3' 'waitress>=3'
/Users/jialiu/Projects/trip-plan/.venv/bin/python -c "import flask, waitress; print(flask.__version__, waitress.__version__)"
```

预期：`uv lock` 输出 `Added flask / waitress / werkzeug / jinja2 / markupsafe / itsdangerous /
blinker / ...`（35 → 42 个包），随后 `uv lock --check` 静默通过。

**验收（三条都要过，缺一条这一步就没做完）**：
- `uv lock --check` 退出码 0；
- `git status --porcelain` 里 `uv.lock` 与 `pyproject.toml` **同时**出现（Step 7 会一起 `git add`）；
- `import flask, waitress` 打印出版本号。

（旁人换一台机器复现时走 `uv sync --extra web --extra dev`，它读的就是这份锁文件——
这也是「必须把 `uv.lock` 一起提交」的落点。Task 15 还会让 `trip web` 在没装这个 extra 时
打印一句可读的安装提示，而不是甩一个 `ModuleNotFoundError` 堆栈。）

- [ ] **Step 2: 写失败的测试**

新建 `tests/web/conftest.py`：

```python
import pytest

from tripplan.deps import Deps
from tripplan.providers.fake import FakeProvider
from tripplan.web.jobs import JobOutcome


@pytest.fixture
def trips_root(tmp_path):
    root = tmp_path / "trips"
    root.mkdir()
    return root


class RecordingRunner:
    """记录 run_command / rebuild_artifacts 有没有被调用、被调了几次。

    §9 的多条回归要断言「advance 没被碰过」，而 advance 藏在 run_command
    里面——在这一层拦住即可，路由测试不需要真的跑状态机。
    """

    def __init__(self, outcome=None):
        self.calls = []
        self.rebuilds = []
        self.outcome = outcome or JobOutcome.ok(2)

    def run_command(self, trips_root, tid, cmd, deps, job, **kw):
        self.calls.append((tid, cmd))
        return self.outcome

    def rebuild(self, trips_root, tid, deps, job, **kw):
        self.rebuilds.append(tid)
        return self.outcome


@pytest.fixture
def runner():
    return RecordingRunner()


@pytest.fixture
def make_app(trips_root, runner):
    from tripplan.web.app import create_app

    def _make(**kw):
        kw.setdefault("deps", Deps(client=None, provider=FakeProvider()))
        kw.setdefault("run_command_fn", runner.run_command)
        kw.setdefault("rebuild_fn", runner.rebuild)
        kw.setdefault("secret", "test-secret")
        app = create_app(trips_root, **kw)
        app.config["TESTING"] = True
        return app

    return _make


@pytest.fixture
def app(make_app):
    return make_app()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def csrf(client):
    """先 GET 一次拿到 session 里的 token —— 与真实浏览器同一条路。"""

    def _get():
        client.get("/")
        with client.session_transaction() as sess:
            return sess["_csrf"]

    return _get
```

新建 `tests/web/test_app_auth.py`：

```python
"""鉴权、CSRF、tid 校验、体积上限（spec §6.0 / §6.5 / §9 回归 5、9、19）。"""

import pytest

from tripplan.repo import FileRepo
from tripplan.state import Stage, TripState


def _seed(trips_root, tid="kyoto", raw="十一想去京都玩5天"):
    state = TripState.new(raw, run_id="r1")
    state.stage, state.revision = Stage.AWAIT_CHOICE, 3
    FileRepo(trips_root / tid).create(state)


# ---------- Basic Auth ----------


def test_without_a_token_the_service_is_open(client):
    assert client.get("/").status_code == 200


def test_with_a_token_an_anonymous_request_gets_401_and_a_challenge(make_app):
    c = make_app(token="hunter2").test_client()
    resp = c.get("/")
    assert resp.status_code == 401
    assert "Basic" in resp.headers["WWW-Authenticate"]


def test_the_right_credentials_get_through(make_app):
    import base64

    c = make_app(token="hunter2").test_client()
    cred = base64.b64encode(b"trip:hunter2").decode()
    assert c.get("/", headers={"Authorization": f"Basic {cred}"}).status_code == 200


def test_a_wrong_password_or_username_is_rejected(make_app):
    import base64

    c = make_app(token="hunter2").test_client()
    for raw in (b"trip:wrong", b"admin:hunter2"):
        cred = base64.b64encode(raw).decode()
        assert c.get("/", headers={"Authorization": f"Basic {cred}"}).status_code == 401


# ---------- CSRF（中间件层；四个真实 POST 路由的覆盖在 Task 12） ----------


def test_every_post_is_guarded_by_default_not_by_remembering_a_decorator(app, client):
    """spec §6.5：before_request 里对所有 POST 统一拦，默认全拦。
    这条用一个临时注册的路由证明「默认就拦」，而不是逐个路由记得加装饰器。"""
    app.add_url_rule("/_probe", "_probe", lambda: "ok", methods=["POST"])
    assert client.post("/_probe").status_code == 403


def test_a_matching_token_passes(app, client, csrf):
    app.add_url_rule("/_probe", "_probe", lambda: "ok", methods=["POST"])
    assert client.post("/_probe", data={"_csrf": csrf()}).status_code == 200


def test_a_wrong_token_is_rejected(app, client, csrf):
    app.add_url_rule("/_probe", "_probe", lambda: "ok", methods=["POST"])
    csrf()
    assert client.post("/_probe", data={"_csrf": "别的值"}).status_code == 403


def test_the_csrf_token_is_stable_within_a_session(client, csrf):
    assert csrf() == csrf()


def test_auth_is_checked_before_csrf(make_app):
    """顺序要对：未鉴权的请求应该拿 401 去登录，而不是一头雾水的 403。"""
    c = make_app(token="hunter2").test_client()
    assert c.post("/trips", data={"request": "去京都"}).status_code == 401


# ---------- tid 校验 ----------


@pytest.mark.parametrize(
    "path",
    [
        "/trips/..%2f..%2fetc%2fpasswd",
        "/trips/%2e%2e",
        "/trips/%2e%2e%2f%2e%2e%2fetc",
    ],
)
def test_path_traversal_is_refused(client, path):
    """§9 回归 5。对局域网暴露的服务这是硬要求（spec §6.0）。"""
    assert client.get(path).status_code == 404


def test_a_chinese_directory_name_works(client, trips_root):
    """中文目录名可正常工作——这一期存在的全部理由就是中文（spec §6.0）。"""
    _seed(trips_root, "十一去京都")
    resp = client.get("/trips/%E5%8D%81%E4%B8%80%E5%8E%BB%E4%BA%AC%E9%83%BD")
    assert resp.status_code == 200


def test_an_unknown_trip_is_404(client):
    assert client.get("/trips/nope").status_code == 404


# ---------- 体积上限 ----------


def test_an_oversized_body_is_refused_by_flask(client, csrf):
    """MAX_CONTENT_LENGTH = 64 KiB（spec §6.5）。局域网暴露的服务不能
    任由请求体撑爆内存。"""
    token = csrf()
    resp = client.post("/trips", data={"_csrf": token, "request": "去" * 40_000})
    assert resp.status_code == 413


# ---------- 列表页 ----------


def test_the_index_lists_trips_with_stage_and_revision(client, trips_root):
    _seed(trips_root)
    body = client.get("/").get_data(as_text=True)
    assert "kyoto" in body
    assert "十一想去京都玩5天" in body
    assert "等你选方案" in body


def test_a_corrupt_directory_does_not_blow_up_the_index(client, trips_root):
    """§9 回归 9：列表页仍 200 且标为「损坏」。"""
    _seed(trips_root)
    (trips_root / "broken").mkdir()
    (trips_root / "broken" / "state.json").write_text("not json at all", encoding="utf-8")

    resp = client.get("/")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "kyoto" in body
    assert "损坏" in body


def test_the_index_escapes_user_text(client, trips_root):
    """autoescape 全程开着（Global Constraint 4）。"""
    _seed(trips_root, raw="<script>alert(1)</script>")
    body = client.get("/").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


def test_no_template_uses_safe_or_markup():
    """§9 回归 16 后半条：全库扫一遍 web/templates/。这条测试是纪律本身——
    将来任何人往模板里写 |safe，它当场红（spec §6.1）。"""
    from pathlib import Path

    import tripplan.web

    root = Path(tripplan.web.__file__).parent / "templates"
    for path in root.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        assert "|safe" not in text, path
        assert "| safe" not in text, path
        assert "Markup" not in text, path
```

- [ ] **Step 3: 跑测试确认它失败**

Run: `/Users/jialiu/Projects/trip-plan/.venv/bin/python -m pytest tests/web/test_app_auth.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'tripplan.web.app'`

- [ ] **Step 4: 写 `web/app.py`**

```python
"""Flask 应用工厂 + 路由。只做「鉴权 → 校验参数 → 调 registry → 渲染」。

选 Flask + waitress、服务端渲染、前端零构建零框架的理由见 spec §3.3：
advance() 是同步阻塞的，同步 WSGI 框架与之直接对上；项目已有 models/ +
wire.py，引入 pydantic 等于并存两套模型体系；render/itinerary_html.py 已经
产出 HTML 串，服务端渲染能直接复用。

**必须单进程**（spec §6.4）：JobRegistry 是进程内单例，多 worker 会让轮询
请求被路由到没有该 job 的进程，进度页随机失灵、取消按钮随机失效。
"""

import hmac
import os
import secrets
from pathlib import Path

from flask import Flask, abort, render_template, request, session

from tripplan.web.events import EventLogStore
from tripplan.web.jobs import JobRegistry, rebuild_artifacts, run_command
from tripplan.web.view import trip_rows

#: 用户名固定，只有口令是秘密（spec §6.5）。
BASIC_AUTH_USER = "trip"
MAX_CONTENT_LENGTH = 64 * 1024
MAX_REQUEST_CHARS = 8000
MAX_DIR_CHARS = 80
MAX_ANGLE_KEY_CHARS = 64


def create_app(
    trips_root,
    deps,
    *,
    token: str | None = None,
    secret: str | None = None,
    registry=None,
    store=None,
    max_jobs: int = 3,
    run_command_fn=run_command,
    rebuild_fn=rebuild_artifacts,
) -> Flask:
    trips_root = Path(trips_root)
    app = Flask(__name__)

    # secret_key：未设置就现生成。重启即所有旧表单失效，对单进程本机服务
    # 可接受——**比硬编码一个默认值安全得多**，那种默认值一定会被原样带到
    # 局域网上（spec §6.5）。
    app.secret_key = (
        secret or os.environ.get("TRIPPLAN_WEB_SECRET") or secrets.token_urlsafe(32)
    )
    app.config.update(
        MAX_CONTENT_LENGTH=MAX_CONTENT_LENGTH,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        # 明文 HTTP，见 spec §6.5 的「如实记录代价」一节。
        SESSION_COOKIE_SECURE=False,
        SESSION_COOKIE_PATH="/",
    )

    app.extensions["tripplan"] = {
        "trips_root": trips_root,
        "deps": deps,
        "registry": registry or JobRegistry(max_jobs=max_jobs),
        "store": store or EventLogStore(trips_root),
        "run_command_fn": run_command_fn,
        "rebuild_fn": rebuild_fn,
        "token": token,
    }

    app.jinja_env.globals["csrf_token"] = _csrf_token

    @app.before_request
    def _guard():
        # 顺序刻意：未鉴权的请求应该拿 401 去登录，而不是一头雾水的 403。
        if not _auth_ok(request.authorization, token):
            return (
                render_template("notice.html", title="需要口令", message="请输入访问口令。"),
                401,
                {"WWW-Authenticate": 'Basic realm="tripplan"'},
            )
        if request.method == "POST":
            # **默认全拦**，不是逐个路由自己记得加装饰器——将来新增的任何
            # POST 自动被覆盖（spec §6.5）。
            expected = session.get("_csrf", "")
            supplied = request.form.get("_csrf", "")
            # 两个 not 不能省：compare_digest("", "") 是 True，少了这一道，
            # 「session 里还没有 token 且表单也没带」会被判成通过——正是
            # 攻击者的跨站表单最容易构造出来的那种请求。
            if not expected or not supplied or not hmac.compare_digest(expected, supplied):
                return (
                    render_template(
                        "notice.html",
                        title="表单已过期",
                        message="请刷新页面后重试（服务重启会让旧表单失效）。",
                    ),
                    403,
                )
        return None

    @app.get("/")
    def index():
        cfg = app.extensions["tripplan"]
        return render_template(
            "index.html",
            rows=trip_rows(cfg["trips_root"], cfg["registry"]),
            form={"request": "", "dir": ""},
            error=None,
        )

    return app


# ---------- 共用工具（后续任务的路由都调它们） ----------


def _csrf_token() -> str:
    tok = session.get("_csrf")
    if not tok:
        tok = secrets.token_urlsafe(32)
        session["_csrf"] = tok
    return tok


def _auth_ok(auth, token: str | None) -> bool:
    """用 hmac.compare_digest 而不是 ==：口令比较不留计时侧信道，
    反正一行的事（spec §6.5）。"""
    if token is None:
        return True
    if auth is None or (auth.type or "").lower() != "basic":
        return False
    ok_user = hmac.compare_digest(auth.username or "", BASIC_AUTH_USER)
    ok_pass = hmac.compare_digest(auth.password or "", token)
    return ok_user and ok_pass


def resolve_trip_dir(trips_root, tid: str) -> Path:
    """tid 必须是单个路径段，且解析后必须是 trips_root 的**直接子目录**。

    对局域网暴露的服务这是硬要求（spec §6.0）。werkzeug 不会把 %2F 合并进
    路径段，所以 `/trips/..%2f..%2fetc%2fpasswd` 到这里 tid 就是
    `../../etc/passwd`，正好被下面这两道挡住。
    """
    if not tid or tid in (".", "..") or any(c in tid for c in ("/", "\\", "\0")):
        abort(404)
    root = Path(trips_root).resolve()
    target = (root / tid).resolve()
    if target.parent != root or not target.is_dir():
        abort(404)
    return target
```

- [ ] **Step 5: 写模板与样式**

`src/tripplan/web/templates/base.html`：

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{% block title %}行程规划{% endblock %}</title>
  <link rel="stylesheet" href="{{ url_for('static', filename='app.css') }}">
</head>
<body>
  <header class="topbar"><a href="{{ url_for('index') }}">← 全部行程</a></header>
  <main>{% block main %}{% endblock %}</main>
  {% block scripts %}{% endblock %}
</body>
</html>
```

`src/tripplan/web/templates/notice.html`：

```html
{% extends "base.html" %}
{% block title %}{{ title }}{% endblock %}
{% block main %}
<h1>{{ title }}</h1>
<p>{{ message }}</p>
{% if link_tid %}<p><a href="{{ url_for('detail', tid=link_tid) }}">进入这个行程 →</a></p>{% endif %}
{% endblock %}
```

（`link_tid` 在 Task 10 才会有调用方传入；`url_for('detail')` 在 Task 11 才注册，所以本任务的模板里先不引用 —— 把上面 `{% if link_tid %}` 那一行整块留到 Task 11 再加，本任务只保留 `<h1>` 与 `<p>` 两行。）

`src/tripplan/web/templates/index.html`：

```html
{% extends "base.html" %}
{% block title %}全部行程{% endblock %}
{% block main %}
<h1>全部行程</h1>

{% if error %}<p class="error">{{ error }}</p>{% endif %}

<table class="trips">
  <thead><tr><th>行程</th><th>需求</th><th>阶段</th><th>rev</th><th>状态</th></tr></thead>
  <tbody>
  {% for row in rows %}
    <tr>
      <td>{{ row.tid }}</td>
      <td>{% if row.corrupt %}<span class="bad">损坏：{{ row.error }}</span>{% else %}{{ row.summary }}{% endif %}</td>
      <td>{{ row.stage_text }}</td>
      <td>{{ row.revision }}</td>
      <td>{% if row.running %}进行中{% endif %}</td>
    </tr>
  {% else %}
    <tr><td colspan="5">还没有任何行程。</td></tr>
  {% endfor %}
  </tbody>
</table>

<h2>新建行程</h2>
<form method="post" action="/trips">
  <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
  <label for="request">想去哪、什么时候、几个人、有什么讲究——随便写，多长都行。</label>
  <textarea id="request" name="request" rows="8" maxlength="8000" required>{{ form.request }}</textarea>
  <label for="dir">目录名（可留空，自动按需求生成）</label>
  <input id="dir" name="dir" maxlength="80" value="{{ form.dir }}">
  <button type="submit">开始规划</button>
</form>
{% endblock %}
```

注意上面 `action="/trips"` 写的是字面量、行程名也没有链接：`create_trip` 与 `detail` 两个端点要到 Task 10 / 11 才注册，现在写 `url_for` 会让整页渲染不出来。Task 10 加上 `create_trip` 后把 action 换成 `{{ url_for('create_trip') }}`，Task 11 再把 `row.tid` 包成 `<a href="{{ url_for('detail', tid=row.tid) }}">`。这样每一步都是可跑的。

`src/tripplan/web/static/app.css`：

```css
:root { color-scheme: light dark; }
body { margin: 0 auto; padding: 1rem; max-width: 52rem;
       font: 16px/1.6 -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; }
.topbar { margin-bottom: 1rem; }
h1 { font-size: 1.4rem; }
h2 { font-size: 1.15rem; margin-top: 2rem; }
table.trips { width: 100%; border-collapse: collapse; }
table.trips th, table.trips td { text-align: left; padding: .4rem .5rem;
                                 border-bottom: 1px solid #8884; vertical-align: top; }
textarea, input[type=text], input:not([type]) { width: 100%; box-sizing: border-box;
                                                font: inherit; padding: .5rem; }
label { display: block; margin: .8rem 0 .3rem; }
button { font: inherit; padding: .5rem 1.2rem; margin-top: .8rem; cursor: pointer; }
button[disabled] { opacity: .5; cursor: not-allowed; }
.error, .bad { color: #b00020; }
.notice { padding: .6rem .8rem; border: 1px solid #b0802088; background: #ffd70022;
          margin-bottom: 1rem; }
.card { border: 1px solid #8884; padding: .8rem 1rem; margin: .8rem 0; }
.inferred { color: #806000; font-size: .9em; }
#event-feed { list-style: none; padding-left: 0; font-size: .92rem; max-height: 22rem;
              overflow-y: auto; border: 1px solid #8884; padding: .5rem; }
#event-feed li { padding: .15rem 0; border-bottom: 1px dotted #8883; }
```

- [ ] **Step 6: 跑测试确认通过**

Run: `/Users/jialiu/Projects/trip-plan/.venv/bin/python -m pytest tests/web/test_app_auth.py -v && /Users/jialiu/Projects/trip-plan/.venv/bin/python -m pytest -q`
Expected: 除 `test_a_chinese_directory_name_works` / `test_an_unknown_trip_is_404` / `test_an_oversized_body_is_refused_by_flask` / `test_auth_is_checked_before_csrf` 四条（依赖 Task 10/11 的路由）外全绿

**把这四条先标成 `@pytest.mark.xfail(reason="路由在 Task 10/11", strict=True)`**，Task 10、11 完成时把标记摘掉——这是刻意的：让它们以「确实还没实现」的方式红，而不是被悄悄删掉又忘了补。

Run: `/Users/jialiu/Projects/trip-plan/.venv/bin/python -m pytest -q`
Expected: `718 passed, 4 xfailed, 1 deselected`

- [ ] **Step 7: 格式化并提交**

```bash
/Users/jialiu/Projects/trip-plan/.venv/bin/black src tests
git add pyproject.toml uv.lock src/tripplan/web/app.py src/tripplan/web/templates src/tripplan/web/static \
        tests/web/conftest.py tests/web/test_app_auth.py
git commit -m "feat(web): Flask 应用工厂 —— Basic Auth、默认全拦的 CSRF、tid 校验、列表页"
```

---

## Task 10: `POST /trips` —— 新建行程

**Files:**
- Create: `src/tripplan/naming.py`
- Modify: `src/tripplan/cli.py`（`slugify` 改成从 `naming` 重新导出）
- Modify: `src/tripplan/web/app.py`（新增 `create_trip` 路由 + 两个共用小工具）
- Modify: `src/tripplan/web/templates/index.html`（`action` 改用 `url_for`）
- Modify: `src/tripplan/web/templates/notice.html`（加 `link_tid` 分支 —— 但 `detail` 端点要到 Task 11 才有，所以这里先用 `"/trips/" + link_tid` 的字面量拼接，Task 11 再改 `url_for`）
- Create: `tests/web/test_create_trip.py`

**Interfaces:**
- Consumes: Task 9 的 `create_app` / `_csrf_token` / `resolve_trip_dir`，Task 7 的 `JobRegistry.start` / `TripBusy` / `ServerBusy`
- Produces:
  - `tripplan.naming.slugify(text) -> str`（从 `cli.py` 原样搬过来）
  - 路由 `POST /trips`（endpoint 名 `create_trip`）
  - `def _start_job(cfg, tid, target)`（内部）：包住 `registry.start` 并把 `TripBusy` / `ServerBusy` 翻译成 409 / 503

- [ ] **Step 1: 写失败的测试**

新建 `tests/web/test_create_trip.py`：

```python
"""POST /trips（spec §6.2）。"""

import pytest

from tripplan.repo import FileRepo
from tripplan.state import Stage, TripState


def test_a_new_trip_is_created_and_redirects_to_its_detail_page(client, csrf, trips_root, runner):
    resp = client.post("/trips", data={"_csrf": csrf(), "request": "十一想去京都玩5天"})
    assert resp.status_code == 302
    assert "/trips/" in resp.headers["Location"]
    assert (trips_root / "十一想去京都玩5天").joinpath("state.json").exists()


def test_the_directory_name_defaults_to_the_existing_slugify(client, csrf, trips_root):
    client.post("/trips", data={"_csrf": csrf(), "request": "十一想去京都玩5天！"})
    assert (trips_root / "十一想去京都玩5天").is_dir()


def test_an_explicit_dir_is_honoured(client, csrf, trips_root):
    client.post("/trips", data={"_csrf": csrf(), "request": "去京都", "dir": "kyoto-2026"})
    assert (trips_root / "kyoto-2026" / "state.json").exists()


def test_the_first_command_is_dispatched_with_cmd_none(client, csrf, runner):
    """新建即开跑：后台线程跑 run_command(..., cmd=None, ...)（spec §6.2）。"""
    client.post("/trips", data={"_csrf": csrf(), "request": "去京都"})
    _wait(runner)
    assert runner.calls and runner.calls[0][1] is None


def _wait(runner, n=1, timeout=5):
    import time

    deadline = time.time() + timeout
    while len(runner.calls) + len(runner.rebuilds) < n and time.time() < deadline:
        time.sleep(0.01)


def test_an_empty_request_is_400_and_keeps_what_was_typed(client, csrf):
    resp = client.post("/trips", data={"_csrf": csrf(), "request": "   ", "dir": "kyoto"})
    body = resp.get_data(as_text=True)
    assert resp.status_code == 400
    assert "kyoto" in body  # 已输入内容被保留


def test_an_overlong_request_is_400_and_keeps_what_was_typed(client, csrf):
    long_text = "去京都" * 3000  # 9000 字 > 8000 上限，但远小于 64 KiB
    resp = client.post("/trips", data={"_csrf": csrf(), "request": long_text})
    assert resp.status_code == 400
    assert "8000" in resp.get_data(as_text=True)


def test_an_overlong_dir_is_400(client, csrf):
    resp = client.post("/trips", data={"_csrf": csrf(), "request": "去京都", "dir": "x" * 81})
    assert resp.status_code == 400


@pytest.mark.parametrize("bad", ["../escape", "a/b", "..", "."])
def test_a_dir_that_is_not_a_single_path_segment_is_400(client, csrf, bad, trips_root):
    resp = client.post("/trips", data={"_csrf": csrf(), "request": "去京都", "dir": bad})
    assert resp.status_code == 400
    assert not (trips_root.parent / "escape").exists()


def test_an_existing_directory_is_409_with_a_direct_link(client, csrf, trips_root):
    FileRepo(trips_root / "kyoto").create(TripState.new("去京都", run_id="r1"))
    resp = client.post("/trips", data={"_csrf": csrf(), "request": "去京都", "dir": "kyoto"})
    body = resp.get_data(as_text=True)
    assert resp.status_code == 409
    assert "已存在" in body
    assert "/trips/kyoto" in body


def test_a_full_server_still_keeps_the_trip_and_says_so(make_app, trips_root):
    """spec §6.2：此时**行程目录已经建好**，提示「已创建，但服务器正忙，
    稍后进去点『继续』」并给直达链接——不静默丢掉用户刚敲的那段需求。"""
    import threading

    from tripplan.web.events import EventLog
    from tripplan.web.jobs import JobOutcome, JobRegistry

    reg = JobRegistry(max_jobs=1)
    gate = threading.Event()
    app = make_app(registry=reg)
    c = app.test_client()
    c.get("/")
    with c.session_transaction() as sess:
        token = sess["_csrf"]

    busy = reg.start("占位", EventLog(trips_root / "占位" / "events.jsonl"),
                     lambda j: (gate.wait(5), JobOutcome.ok(1))[1])
    try:
        resp = c.post("/trips", data={"_csrf": token, "request": "去京都", "dir": "kyoto"})
        body = resp.get_data(as_text=True)
        assert resp.status_code == 503
        assert (trips_root / "kyoto" / "state.json").exists()   # 需求没被丢掉
        assert "正忙" in body
        assert "/trips/kyoto" in body
    finally:
        gate.set()
        busy.thread.join(5)
```

- [ ] **Step 2: 跑测试确认它失败**

Run: `/Users/jialiu/Projects/trip-plan/.venv/bin/python -m pytest tests/web/test_create_trip.py -v`
Expected: 全部 FAIL，`405 METHOD NOT ALLOWED`（`/trips` 还没注册）

- [ ] **Step 3: 把 `slugify` 搬出 `cli.py`**

新建 `src/tripplan/naming.py`：

```python
"""行程目录名。放在这里而不是 cli.py，是为了让 web/ 不必反向依赖 cli/。"""

import re

_SLUG_STRIP = re.compile(r"[^\w一-鿿\s-]", re.U)


def slugify(text: str) -> str:
    cleaned = _SLUG_STRIP.sub("", text).strip()
    cleaned = re.sub(r"\s+", "-", cleaned)
    return cleaned[:40] or "trip"
```

`src/tripplan/cli.py`：删掉 `_SLUG_STRIP` 与 `slugify` 的定义，改成重新导出（`tests/test_cli.py` 里 `from tripplan.cli import slugify` 因此仍然有效）：

```python
from tripplan.naming import slugify  # noqa: F401  （重新导出，保持 trip.cli.slugify 可用）
```

- [ ] **Step 4: 写路由**

`src/tripplan/web/app.py` 顶部补 import：

```python
import uuid

from flask import redirect, url_for

from tripplan.naming import slugify
from tripplan.repo import FileRepo, TripExists
from tripplan.state import TripState
from tripplan.web.jobs import ServerBusy, TripBusy
```

在 `create_app` 内部、`index` 之后注册：

```python
    @app.post("/trips")
    def create_trip():
        cfg = app.extensions["tripplan"]
        raw = (request.form.get("request") or "").strip()
        wanted_dir = (request.form.get("dir") or "").strip()

        if not raw:
            return _index_error(cfg, "请先写点什么——想去哪、什么时候、几个人。", raw, wanted_dir)
        if len(raw) > MAX_REQUEST_CHARS:
            return _index_error(
                cfg, f"需求太长了（{len(raw)} 字，上限 {MAX_REQUEST_CHARS} 字）。", raw, wanted_dir
            )
        if len(wanted_dir) > MAX_DIR_CHARS:
            return _index_error(
                cfg, f"目录名太长了（上限 {MAX_DIR_CHARS} 字）。", raw, wanted_dir
            )

        tid = wanted_dir or slugify(raw)
        if not _is_single_segment(tid):
            return _index_error(cfg, "目录名必须是单个名字，不能带 / 或 ..。", raw, wanted_dir)

        repo = FileRepo(cfg["trips_root"] / tid)
        try:
            repo.create(TripState.new(raw, run_id=uuid.uuid4().hex[:12]))
        except TripExists:
            return (
                render_template(
                    "notice.html",
                    title="行程已存在",
                    message=f"{tid} 已存在。换个目录名，或者直接进去接着上次的进度。",
                    link_tid=tid,
                ),
                409,
            )

        # 目录已经建好了。下面这一步就算被拒，用户敲的那段需求也没丢。
        return _start_command(cfg, tid, None)
```

在模块级追加三个工具：

```python
def _is_single_segment(tid: str) -> bool:
    return bool(tid) and tid not in (".", "..") and not any(
        c in tid for c in ("/", "\\", "\0")
    )


def _index_error(cfg, message: str, raw: str, wanted_dir: str):
    return (
        render_template(
            "index.html",
            rows=trip_rows(cfg["trips_root"], cfg["registry"]),
            form={"request": raw, "dir": wanted_dir},
            error=message,
        ),
        400,
    )


def _start_command(cfg, tid: str, cmd):
    """起一个后台 job 跑一次 advance。TripBusy → 409，ServerBusy → 503。

    请求线程只负责登记后立刻返回，不占 waitress 线程池（spec §4.2）。
    """
    log = cfg["store"].get(tid)
    run = cfg["run_command_fn"]
    try:
        cfg["registry"].start(
            tid, log, lambda job: run(cfg["trips_root"], tid, cmd, cfg["deps"], job)
        )
    except TripBusy:
        return _detail_with_notice(cfg, tid, "这个行程正在跑上一步，等它结束再操作。", 409)
    except ServerBusy:
        return (
            render_template(
                "notice.html",
                title="服务器正忙",
                message="同时进行的规划已达上限。行程已经创建好了，稍后进去点「继续」即可。",
                link_tid=tid,
            ),
            503,
        )
    return redirect(url_for("detail", tid=tid), code=302)
```

`_detail_with_notice` 与 `detail` 端点在 Task 11 实现；**本任务先让 `_start_command` 的 `TripBusy` 分支返回一个 `notice.html` + 409**，Task 11 再换成带横幅的详情页。同理 `url_for("detail", ...)` 在 Task 11 前不存在——本任务里先写成 `redirect(f"/trips/{quote(tid)}", code=302)`（`from urllib.parse import quote`），Task 11 注册 `detail` 后统一换成 `url_for`。

`src/tripplan/web/templates/index.html`：`action="/trips"` 改成 `action="{{ url_for('create_trip') }}"`。

`src/tripplan/web/templates/notice.html`：加上链接分支（`detail` 端点还没有，先用字面量）：

```html
{% if link_tid %}<p><a href="/trips/{{ link_tid | urlencode }}">进入这个行程 →</a></p>{% endif %}
```

- [ ] **Step 5: 跑测试确认通过**

摘掉 `tests/web/test_app_auth.py` 里 `test_an_oversized_body_is_refused_by_flask` 与 `test_auth_is_checked_before_csrf` 的 `xfail` 标记。

Run: `/Users/jialiu/Projects/trip-plan/.venv/bin/python -m pytest tests/web/ -v && /Users/jialiu/Projects/trip-plan/.venv/bin/python -m pytest -q`
Expected: `tests/web/test_create_trip.py` 12 条全绿；全量 `732 passed, 2 xfailed, 1 deselected`

- [ ] **Step 6: 格式化并提交**

```bash
/Users/jialiu/Projects/trip-plan/.venv/bin/black src tests
git add src/tripplan/naming.py src/tripplan/cli.py src/tripplan/web/app.py \
        src/tripplan/web/templates tests/web/test_create_trip.py tests/web/test_app_auth.py
git commit -m "feat(web): POST /trips —— 建目录、起首次 job，满载也不丢用户敲的需求"
```

---

## Task 11: 详情页 `GET /trips/<tid>` —— 按 stage 渲染 + 进度区

**Files:**
- Modify: `src/tripplan/web/app.py`（`detail` 路由 + `_detail_context` / `_detail_with_notice`）
- Create: `src/tripplan/web/templates/detail.html`
- Modify: `src/tripplan/web/templates/index.html`（行程名加链接）
- Modify: `src/tripplan/web/templates/notice.html`（链接改 `url_for`）
- Create: `tests/web/test_detail.py`

**Interfaces:**
- Consumes: Task 8 的 `req_card_vm` / `candidate_vms` / `event_text`，Task 5 的 `EventLog.snapshot()`，Task 4 的 `artifact_ready`
- Produces:
  - 路由 `GET /trips/<tid>`（endpoint 名 `detail`）
  - `def _detail_context(cfg, tid) -> dict`（内部；键：`tid` `state` `stage` `revision` `req_card` `candidates` `job` `active` `artifact_ready` `events` `cursor` `epoch` `notice`）
  - `def _detail_with_notice(cfg, tid, message, status)`（内部）
  - 页面上供轮询起步的 `#progress` 元素，带 `data-cursor` / `data-epoch` / `data-job-id` / `data-status-version` / `data-revision` / `data-artifact-ready` / `data-events-url`

- [ ] **Step 1: 写失败的测试**

新建 `tests/web/test_detail.py`：

```python
"""详情页按 stage 渲染（spec §6.1 / §9 回归 2、15、16）。"""

from datetime import date, datetime, timedelta, timezone

import pytest

from tripplan.artifacts import publish, stage_artifacts
from tripplan.models.common import Field, Origin
from tripplan.models.facts import FactSnapshot
from tripplan.models.issue import Issue, Severity, Source
from tripplan.models.itinerary import Angle, Day, Itinerary
from tripplan.models.requirements import DateRange, Party, Requirements
from tripplan.providers.fake import FakeProvider
from tripplan.repo import FileRepo
from tripplan.state import CandidateSlot, SlotStatus, Stage, TripState

D1 = date(2026, 10, 1)
_JST = timezone(timedelta(hours=9))


def _facts():
    return FactSnapshot(poi_by_activity={}, constraint_pois={}, routes=[], weather={},
                        trip_timezone="Asia/Tokyo",
                        resolved_at=datetime(2026, 9, 1, tzinfo=_JST), gaps=[])


def _reqs(rationale=""):
    return Requirements(
        destination=Field("京都", Origin.USER),
        dates=Field(DateRange(D1, D1), Origin.USER),
        party=Field(Party(adults=2), Origin.USER),
        lodging_area=Field("四条", Origin.MODEL, rationale=rationale),
    )


def _seed(trips_root, tid="kyoto", stage=Stage.AWAIT_REQ_CONFIRM, rev=3,
          raw="去京都", detail="", rationale=""):
    state = TripState.new(raw, run_id="r1")
    state.stage, state.revision, state.requirements = stage, rev, _reqs(rationale)
    angle = Angle("foodie", "吃遍京都", "从早市到居酒屋")
    itin = Itinerary(angle=angle, days=[Day(id="d1", date=D1, activities=[])],
                     issues=[Issue(Severity.WARNING, Source.CRITIC, "C1", "第二天略赶")])
    state.candidates = [CandidateSlot(angle, itin, _facts(), SlotStatus.OK, detail)]
    if stage is Stage.DONE:
        state.chosen_key = "foodie"
    FileRepo(trips_root / tid).create(state)
    return state


def test_await_req_confirm_shows_the_requirement_card_and_two_actions(client, trips_root):
    _seed(trips_root, rationale="按预算推断")
    body = client.get("/trips/kyoto").get_data(as_text=True)
    assert "目的地" in body and "京都" in body
    assert "按预算推断" in body
    assert "确认" in body
    assert 'name="kind" value="confirm"' in body
    assert 'name="kind" value="amend"' in body
    assert 'name="expected_revision" value="3"' in body


def test_the_requirement_card_is_not_markdown_source(client, trips_root):
    """§9 回归 16 的一半：autoescape 开着时 render_requirement_card() 的
    Markdown 串会原样带星号显示出来（spec §6.1）。"""
    _seed(trips_root)
    body = client.get("/trips/kyoto").get_data(as_text=True)
    assert "**目的地**" not in body
    assert "## 需求确认" not in body


def test_await_choice_lists_candidates_with_a_button_each(client, trips_root):
    _seed(trips_root, stage=Stage.AWAIT_CHOICE, detail="修订 3 次后仍有 1 个硬伤")
    body = client.get("/trips/kyoto").get_data(as_text=True)
    assert "吃遍京都" in body
    assert 'value="foodie"' in body
    assert "修订 3 次后仍有 1 个硬伤" in body
    assert "第二天略赶" in body
    assert 'name="kind" value="choose"' in body
    assert 'name="kind" value="feedback"' in body


@pytest.mark.parametrize("payload", ["<script>alert(1)</script>"])
def test_model_and_user_text_is_escaped_everywhere(client, trips_root, payload):
    """§9 回归 16 的正题：rationale / slot.detail / raw_request 全是模型输出
    或用户输入的自由文本。标成 safe 等于开一个 XSS 面，而这个服务还要暴露
    在局域网上给别人访问（spec §6.1）。"""
    _seed(trips_root, stage=Stage.AWAIT_CHOICE, raw=payload, detail=payload, rationale=payload)
    body = client.get("/trips/kyoto").get_data(as_text=True)
    assert payload not in body
    assert "&lt;script&gt;" in body


def test_a_working_stage_with_an_active_job_shows_a_cancel_button(client, trips_root, app, runner):
    import threading

    from tripplan.web.events import EventLog
    from tripplan.web.jobs import JobOutcome

    _seed(trips_root, stage=Stage.GENERATE)
    cfg = app.extensions["tripplan"]
    gate = threading.Event()
    job = cfg["registry"].start("kyoto", EventLog(trips_root / "kyoto" / "events.jsonl"),
                                lambda j: (gate.wait(5), JobOutcome.ok(1))[1])
    try:
        body = client.get("/trips/kyoto").get_data(as_text=True)
        assert "正在工作" in body
        assert "取消" in body
    finally:
        gate.set()
        job.thread.join(5)


def test_a_cancelling_job_greys_out_the_cancel_button(client, trips_root, app):
    import threading

    from tripplan.web.events import EventLog
    from tripplan.web.jobs import JobOutcome

    _seed(trips_root, stage=Stage.GENERATE)
    cfg = app.extensions["tripplan"]
    gate = threading.Event()
    job = cfg["registry"].start("kyoto", EventLog(trips_root / "kyoto" / "events.jsonl"),
                                lambda j: (gate.wait(5), JobOutcome.ok(1))[1])
    job.request_cancel()
    try:
        body = client.get("/trips/kyoto").get_data(as_text=True)
        assert "已请求停止" in body
        assert "秒" not in body  # 不给秒数，不假装已停（spec §4.3）
    finally:
        gate.set()
        job.thread.join(5)


def test_a_working_stage_without_a_job_offers_a_way_out(client, trips_root):
    """spec §6.1：服务重启、503 被拒、线程异常挂掉，三种情况都落在这里——
    必须有出路，不能是一个永远转圈的假进度条。"""
    _seed(trips_root, stage=Stage.GENERATE)
    body = client.get("/trips/kyoto").get_data(as_text=True)
    assert "没有跑完" in body
    assert "继续" in body


def test_done_with_ready_artifacts_links_to_the_itinerary(client, trips_root):
    state = _seed(trips_root, stage=Stage.DONE, rev=5)
    publish(stage_artifacts(state, trips_root / "kyoto", FakeProvider(), "job-1"))
    body = client.get("/trips/kyoto").get_data(as_text=True)
    assert "/trips/kyoto/itinerary" in body
    assert "待重建" not in body


def test_done_without_artifacts_offers_a_rebuild_instead_of_a_dead_link(client, trips_root):
    """§9 回归 15 的一半：崩溃恢复、旧版本残留、手工删文件，共用这条出路
    （spec §4.1）。"""
    _seed(trips_root, stage=Stage.DONE, rev=5)
    body = client.get("/trips/kyoto").get_data(as_text=True)
    assert "待重建" in body
    assert "/trips/kyoto/itinerary" not in body


def test_a_stale_manifest_also_counts_as_not_ready(client, trips_root):
    import json

    state = _seed(trips_root, stage=Stage.DONE, rev=5)
    publish(stage_artifacts(state, trips_root / "kyoto", FakeProvider(), "job-1"))
    path = trips_root / "kyoto" / "artifacts.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["revision"] = 4
    path.write_text(json.dumps(data), encoding="utf-8")

    assert "待重建" in client.get("/trips/kyoto").get_data(as_text=True)


def test_the_page_carries_the_whole_event_history_and_a_cursor(client, trips_root, app):
    """§9 回归 2 的前半条：「刷新后接着上次」不靠前端缓存，靠服务端有日志
    （spec §5.5）。"""
    _seed(trips_root)
    log = app.extensions["tripplan"]["store"].get("kyoto")
    log.append("generating", {"args": ["foodie"]})
    log.append("revision", {"args": ["foodie", 0]})

    body = client.get("/trips/kyoto").get_data(as_text=True)
    assert "正在生成候选 foodie" in body
    assert "第 1 轮修订" in body
    assert 'data-cursor="2"' in body
    assert f'data-epoch="{log.stream_epoch}"' in body


def test_the_page_exposes_the_polling_baseline(client, trips_root):
    """前端比的是 (job.id, status_version, revision, artifact_ready) 四者
    （spec §6.3 第 3 条），所以四个基线都要写进 HTML。"""
    _seed(trips_root, rev=3)
    body = client.get("/trips/kyoto").get_data(as_text=True)
    assert 'data-job-id=""' in body
    assert 'data-status-version="0"' in body
    assert 'data-revision="3"' in body
    assert 'data-artifact-ready="0"' in body


def test_a_corrupt_trip_shows_a_readable_page_not_a_500(client, trips_root):
    (trips_root / "broken").mkdir()
    (trips_root / "broken" / "state.json").write_text("not json", encoding="utf-8")
    resp = client.get("/trips/broken")
    assert resp.status_code == 200
    assert "损坏" in resp.get_data(as_text=True)
```

- [ ] **Step 2: 跑测试确认它失败**

Run: `/Users/jialiu/Projects/trip-plan/.venv/bin/python -m pytest tests/web/test_detail.py -v`
Expected: 全部 FAIL，`404 NOT FOUND`

- [ ] **Step 3: 写路由**

`src/tripplan/web/app.py`：补 import

```python
from tripplan.repo import TripCorrupt, TripNotFound
from tripplan.web.view import artifact_ready, candidate_vms, event_text, req_card_vm
from tripplan.wire import UnsupportedVersion
```

在 `create_app` 里注册：

```python
    @app.get("/trips/<tid>")
    def detail(tid):
        cfg = app.extensions["tripplan"]
        return render_template("detail.html", **_detail_context(cfg, tid))
```

模块级追加：

```python
def _detail_context(cfg, tid: str, notice: str | None = None) -> dict:
    trip_dir = resolve_trip_dir(cfg["trips_root"], tid)
    job = cfg["registry"].get(tid)
    snap = cfg["store"].get(tid).snapshot()
    base = {
        "tid": tid,
        "job": job.snapshot() if job is not None else None,
        "active": job is not None and job.active,
        "notice": notice,
        "events": [
            {"seq": e.seq, "stream_id": e.stream_id, "text": event_text(e.to_json())}
            for e in snap.events
        ],
        "cursor": snap.cursor,
        "epoch": snap.stream_epoch,
    }

    try:
        state = FileRepo(trip_dir).load()
    except (TripCorrupt, TripNotFound, UnsupportedVersion) as e:
        # 一个坏目录不该是 500。给一句读得懂的话（repo.py 的既定承诺）。
        return {**base, "corrupt": str(e), "state": None, "stage": "", "revision": 0,
                "req_card": None, "candidates": [], "artifact_ready": False}

    return {
        **base,
        "corrupt": None,
        "state": state,
        "stage": state.stage.value,
        "revision": state.revision,
        "req_card": req_card_vm(state.requirements) if state.requirements else None,
        "candidates": candidate_vms(state.candidates),
        # 详情页不靠 `stage is DONE` 决定要不要给链接（spec §4.1）。
        "artifact_ready": artifact_ready(trip_dir, state.revision),
    }


def _detail_with_notice(cfg, tid: str, message: str, status: int):
    return render_template("detail.html", **_detail_context(cfg, tid, notice=message)), status
```

把 Task 10 里 `_start_command` 的 `TripBusy` 分支换成真正的 `_detail_with_notice(cfg, tid, ..., 409)`，把 `redirect(f"/trips/{quote(tid)}", ...)` 换成 `redirect(url_for("detail", tid=tid), code=302)`，并删掉 `quote` 的 import。

- [ ] **Step 4: 写 `detail.html`**

```html
{% extends "base.html" %}
{% block title %}{{ tid }}{% endblock %}
{% block main %}
<h1>{{ tid }}</h1>

{% if corrupt %}
  <p class="bad">这个行程的 state.json 已损坏：{{ corrupt }}</p>
{% else %}

{% if notice %}<p class="notice">{{ notice }}</p>{% endif %}
{% if job and job.status == "rejected" %}
  <p class="notice">上一条命令被拒绝：{{ job.message }}</p>
{% elif job and job.status == "failed" %}
  <p class="notice">上一步失败了（{{ job.kind }}）：{{ job.message }}
    上一次保存到磁盘的进度还在，行程目录没有损坏。</p>
{% elif job and job.status == "cancelled" %}
  <p class="notice">已取消。盘上停在上一个暂停点，刷新即回到取消前的样子。</p>
{% endif %}

<p class="meta">阶段 {{ stage }} · rev {{ revision }}</p>
<p class="meta">{{ state.raw_request }}</p>

{% if active %}
  <section class="card">
    <h2>正在工作</h2>
    {% if job.status == "cancelling" %}
      <p>已请求停止，将在当前这一步结束后生效。</p>
      <button disabled>已请求停止…</button>
    {% else %}
      <form method="post" action="{{ url_for('cancel', tid=tid) }}">
        <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
        <button type="submit">取消</button>
      </form>
    {% endif %}
  </section>

{% elif stage == "AWAIT_REQ_CONFIRM" and req_card %}
  <section class="card">
    <h2>需求确认</h2>
    <dl>
      {% for f in req_card.fields %}
        <dt>{{ f.label }}</dt>
        <dd>{{ f.value }}
          {% if f.is_inferred %}<span class="inferred">推断{% if f.rationale %}（{{ f.rationale }}）{% endif %}</span>{% endif %}
        </dd>
      {% endfor %}
    </dl>
    {% if req_card.missing %}
      <p class="bad">还缺：{{ req_card.missing | join("、") }}。
        这几项无法推断——猜出来会让整个规划建立在假约束上。</p>
    {% endif %}
  </section>

  <form method="post" action="{{ url_for('commands', tid=tid) }}">
    <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
    <input type="hidden" name="expected_revision" value="{{ revision }}">
    <button type="submit" name="kind" value="confirm"
            {% if req_card.missing %}disabled{% endif %}>就按这个来</button>
  </form>

  <form method="post" action="{{ url_for('commands', tid=tid) }}">
    <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
    <input type="hidden" name="expected_revision" value="{{ revision }}">
    <label for="amend-text">要改什么？</label>
    <textarea id="amend-text" name="text" rows="4" maxlength="8000"></textarea>
    <button type="submit" name="kind" value="amend">按这个改</button>
  </form>

{% elif stage == "AWAIT_CHOICE" %}
  <h2>候选方案</h2>
  {% for c in candidates %}
    <section class="card">
      <h3>{{ c.title }}<span class="meta"> [{{ c.key }}]</span></h3>
      {% if c.description %}<p class="meta">{{ c.description }}</p>{% endif %}
      {% if c.selectable %}
        <p>{{ c.days }} 天 / {{ c.activities }} 项安排</p>
      {% else %}
        <p class="bad">未能生成——无法选择</p>
      {% endif %}
      {% if c.detail %}<p class="bad">{{ c.detail }}</p>{% endif %}
      {% if c.issues %}
        <ul>{% for i in c.issues %}<li>{{ i.mark }} {{ i.message }}</li>{% endfor %}</ul>
      {% endif %}
      {% if c.selectable %}
        <form method="post" action="{{ url_for('commands', tid=tid) }}">
          <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
          <input type="hidden" name="expected_revision" value="{{ revision }}">
          <input type="hidden" name="angle_key" value="{{ c.key }}">
          <button type="submit" name="kind" value="choose">选它</button>
        </form>
        <form method="post" action="{{ url_for('commands', tid=tid) }}">
          <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
          <input type="hidden" name="expected_revision" value="{{ revision }}">
          <input type="hidden" name="angle_key" value="{{ c.key }}">
          <label for="fb-{{ loop.index }}">对这一份提意见</label>
          <textarea id="fb-{{ loop.index }}" name="text" rows="3" maxlength="8000"></textarea>
          <button type="submit" name="kind" value="feedback">按意见改</button>
        </form>
      {% endif %}
    </section>
  {% endfor %}

{% elif stage == "DONE" %}
  <section class="card">
    <h2>已定稿</h2>
    {% if artifact_ready %}
      <p><a href="{{ url_for('itinerary', tid=tid) }}">看成稿（含每天地图） →</a></p>
    {% else %}
      <p>产物待重建：成稿文件缺失或与当前 rev 对不上。</p>
      <form method="post" action="{{ url_for('rebuild', tid=tid) }}">
        <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
        <button type="submit">重建产物</button>
      </form>
    {% endif %}
  </section>

{% else %}
  <section class="card">
    <h2>上一步没有跑完</h2>
    <p>服务重启、被拒、或者线程意外退出都会停在这里。点「继续」接着跑。</p>
    <form method="post" action="{{ url_for('commands', tid=tid) }}">
      <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
      <input type="hidden" name="expected_revision" value="{{ revision }}">
      <button type="submit">继续</button>
    </form>
  </section>
{% endif %}

<h2>进度</h2>
<div id="progress"
     data-tid="{{ tid }}"
     data-events-url="{{ url_for('events', tid=tid) }}"
     data-cursor="{{ cursor }}"
     data-epoch="{{ epoch }}"
     data-job-id="{{ job.id if job else '' }}"
     data-status-version="{{ job.status_version if job else 0 }}"
     data-revision="{{ revision }}"
     data-artifact-ready="{{ '1' if artifact_ready else '0' }}">
  <ol id="event-feed">
    {% for e in events %}<li{% if e.stream_id %} data-stream-id="{{ e.stream_id }}"{% endif %}>{{ e.text }}</li>{% endfor %}
  </ol>
</div>
{% endif %}
{% endblock %}

{% block scripts %}<script src="{{ url_for('static', filename='app.js') }}"></script>{% endblock %}
```

模板引用了 `cancel` / `commands` / `rebuild` / `itinerary` / `events` 五个还不存在的端点。**本任务先在 `create_app` 里为这五个注册占位路由**，让 `url_for` 可用、页面渲染得出来：

```python
    # 占位：真正的实现分别在 Task 12（commands/cancel/rebuild）、
    # Task 13（events）、Task 14（itinerary）。留在这里是为了让 detail.html
    # 的 url_for 现在就解析得了，每个任务都能独立跑测试。
    @app.post("/trips/<tid>/commands")
    def commands(tid):
        abort(501)

    @app.post("/trips/<tid>/cancel")
    def cancel(tid):
        abort(501)

    @app.post("/trips/<tid>/artifacts")
    def rebuild(tid):
        abort(501)

    @app.get("/trips/<tid>/events")
    def events(tid):
        abort(501)

    @app.get("/trips/<tid>/itinerary")
    def itinerary(tid):
        abort(501)
```

`index.html`：行程名加链接

```html
      <td>{% if row.corrupt %}{{ row.tid }}{% else %}<a href="{{ url_for('detail', tid=row.tid) }}">{{ row.tid }}</a>{% endif %}</td>
```

`notice.html`：链接改成 `{{ url_for('detail', tid=link_tid) }}`。

`app.css` 补一行：`.meta { color: #666; font-size: .9em; }`

- [ ] **Step 5: 跑测试确认通过**

摘掉 `tests/web/test_app_auth.py` 里剩下两条的 `xfail` 标记。

Run: `/Users/jialiu/Projects/trip-plan/.venv/bin/python -m pytest tests/web/ -v && /Users/jialiu/Projects/trip-plan/.venv/bin/python -m pytest -q`
Expected: `tests/web/test_detail.py` 13 条全绿；全量 `745 passed, 1 deselected`

- [ ] **Step 6: 格式化并提交**

```bash
/Users/jialiu/Projects/trip-plan/.venv/bin/black src tests
git add src/tripplan/web/app.py src/tripplan/web/templates src/tripplan/web/static tests/web/
git commit -m "feat(web): 详情页按 stage 渲染 —— 结构化卡片、出路兜底、进度区与轮询基线"
```

---

## Task 12: 三个动作路由 —— `commands` / `cancel` / `artifacts`

**Files:**
- Modify: `src/tripplan/web/app.py`（三个占位路由换成真实现）
- Create: `tests/web/test_commands.py`

**Interfaces:**
- Consumes: Task 10 的 `_start_command`，Task 7 的 `TripJob.request_cancel` / `JobRegistry`，Task 6 的 `rebuild_artifacts`
- Produces:
  - `POST /trips/<tid>/commands`（`commands`）：`kind` ∈ `confirm|amend|choose|feedback|""`，`expected_revision`、`text`、`angle_key`
  - `POST /trips/<tid>/cancel`（`cancel`）：幂等
  - `POST /trips/<tid>/artifacts`（`rebuild`）
  - `def _build_command(form) -> (cmd, error)`（内部）

- [ ] **Step 1: 写失败的测试**

新建 `tests/web/test_commands.py`：

```python
"""三个动作路由（spec §6.2 / §9 回归 1、18、19、23）。"""

import threading
import time
from datetime import date, datetime, timedelta, timezone

import pytest

from tripplan.models.common import Field, Origin
from tripplan.models.facts import FactSnapshot
from tripplan.models.itinerary import Angle, Itinerary
from tripplan.models.requirements import DateRange, Party, Requirements
from tripplan.repo import FileRepo
from tripplan.state import (
    AmendRequirements,
    CandidateSlot,
    ChooseCandidate,
    ConfirmRequirements,
    GiveFeedback,
    SlotStatus,
    Stage,
    TripState,
)
from tripplan.web.events import EventLog
from tripplan.web.jobs import JobOutcome

D1 = date(2026, 10, 1)


def _seed(trips_root, tid="kyoto", stage=Stage.AWAIT_CHOICE, rev=3):
    state = TripState.new("去京都", run_id="r1")
    state.stage, state.revision = stage, rev
    state.requirements = Requirements(
        destination=Field("京都", Origin.USER),
        dates=Field(DateRange(D1, D1), Origin.USER),
        party=Field(Party(adults=2), Origin.USER),
    )
    angle = Angle("foodie", "吃遍京都", "")
    facts = FactSnapshot(poi_by_activity={}, constraint_pois={}, routes=[], weather={},
                         trip_timezone="Asia/Tokyo",
                         resolved_at=datetime(2026, 9, 1, tzinfo=timezone(timedelta(hours=9))),
                         gaps=[])
    state.candidates = [CandidateSlot(angle, Itinerary(angle=angle), facts, SlotStatus.OK)]
    if stage is Stage.DONE:
        state.chosen_key = "foodie"
    FileRepo(trips_root / tid).create(state)
    return state


def _wait(runner, n=1, timeout=5):
    deadline = time.time() + timeout
    while len(runner.calls) + len(runner.rebuilds) < n and time.time() < deadline:
        time.sleep(0.01)


# ---------- 命令映射 ----------

@pytest.mark.parametrize(
    "form,expected",
    [
        ({"kind": "confirm"}, ConfirmRequirements(3)),
        ({"kind": "amend", "text": "改成四天"}, AmendRequirements(3, "改成四天")),
        ({"kind": "choose", "angle_key": "foodie"}, ChooseCandidate(3, "foodie")),
        ({"kind": "feedback", "angle_key": "foodie", "text": "第2天太赶"},
         GiveFeedback(3, "foodie", "第2天太赶")),
    ],
)
def test_each_kind_maps_to_the_state_command(client, csrf, trips_root, runner, form, expected):
    _seed(trips_root)
    client.post("/trips/kyoto/commands", data={"_csrf": csrf(), "expected_revision": "3", **form})
    _wait(runner)
    assert runner.calls == [("kyoto", expected)]


def test_an_empty_kind_means_continue(client, csrf, trips_root, runner):
    """spec §6.2：kind 留空 = 「继续」，对应 cmd=None。"""
    _seed(trips_root, stage=Stage.GENERATE)
    client.post("/trips/kyoto/commands", data={"_csrf": csrf(), "expected_revision": "3"})
    _wait(runner)
    assert runner.calls == [("kyoto", None)]


def test_a_successful_post_redirects(client, csrf, trips_root):
    _seed(trips_root)
    resp = client.post("/trips/kyoto/commands", data={"_csrf": csrf(), "kind": "confirm",
                                                     "expected_revision": "3"})
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/trips/kyoto")


def test_a_bad_expected_revision_is_400(client, csrf, trips_root, runner):
    _seed(trips_root)
    resp = client.post("/trips/kyoto/commands",
                       data={"_csrf": csrf(), "kind": "confirm", "expected_revision": "abc"})
    assert resp.status_code == 400
    assert runner.calls == []


def test_an_unknown_kind_is_400(client, csrf, trips_root, runner):
    _seed(trips_root)
    resp = client.post("/trips/kyoto/commands",
                       data={"_csrf": csrf(), "kind": "drop-table", "expected_revision": "3"})
    assert resp.status_code == 400
    assert runner.calls == []


def test_overlong_text_is_400_and_keeps_the_input(client, csrf, trips_root, runner):
    _seed(trips_root)
    resp = client.post("/trips/kyoto/commands",
                       data={"_csrf": csrf(), "kind": "amend", "expected_revision": "3",
                             "text": "改" * 9000})
    assert resp.status_code == 400
    assert runner.calls == []


def test_an_overlong_angle_key_is_400(client, csrf, trips_root, runner):
    _seed(trips_root)
    resp = client.post("/trips/kyoto/commands",
                       data={"_csrf": csrf(), "kind": "choose", "expected_revision": "3",
                             "angle_key": "k" * 65})
    assert resp.status_code == 400
    assert runner.calls == []


def test_choose_without_an_angle_key_is_400(client, csrf, trips_root, runner):
    _seed(trips_root)
    resp = client.post("/trips/kyoto/commands",
                       data={"_csrf": csrf(), "kind": "choose", "expected_revision": "3"})
    assert resp.status_code == 400
    assert runner.calls == []


# ---------- 互斥与上限 ----------

def test_a_second_command_on_the_same_trip_is_409_and_advance_runs_once(
    client, csrf, trips_root, app, runner
):
    """§9 回归 1：互斥是为了省钱——输掉 CAS 的那个线程已经把 LLM 的钱
    烧完了才发现自己白干（spec §4.2）。"""
    _seed(trips_root)
    gate = threading.Event()
    runner.run_command = lambda *a, **kw: (
        runner.calls.append((a[1], a[2])), gate.wait(5), JobOutcome.ok(4)
    )[2]
    app.extensions["tripplan"]["run_command_fn"] = runner.run_command

    token = csrf()
    first = client.post("/trips/kyoto/commands",
                        data={"_csrf": token, "kind": "confirm", "expected_revision": "3"})
    _wait(runner)
    second = client.post("/trips/kyoto/commands",
                         data={"_csrf": token, "kind": "confirm", "expected_revision": "3"})
    gate.set()
    app.extensions["tripplan"]["registry"].get("kyoto").thread.join(5)

    assert first.status_code == 302
    assert second.status_code == 409
    assert len(runner.calls) == 1


def test_a_cancelling_job_also_blocks_the_same_trip(client, csrf, trips_root, app, runner):
    """§9 回归 23：**cancelling 也挡**——那个线程还没退出，放第二个进来
    就是两个线程同时对一份 state 跑 advance（spec §6.2）。"""
    _seed(trips_root)
    gate = threading.Event()
    reg = app.extensions["tripplan"]["registry"]
    job = reg.start("kyoto", EventLog(trips_root / "kyoto" / "events.jsonl"),
                    lambda j: (gate.wait(5), JobOutcome.ok(1))[1])
    job.request_cancel()
    try:
        resp = client.post("/trips/kyoto/commands",
                           data={"_csrf": csrf(), "kind": "confirm", "expected_revision": "3"})
        assert resp.status_code == 409
        assert runner.calls == []
    finally:
        gate.set()
        job.thread.join(5)


def test_the_global_cap_returns_503_and_does_not_touch_the_second_trip(
    make_app, trips_root, runner
):
    """§9 回归 18：上限设为 1，对**两个不同的 trip** 连发命令，第二个拿到
    503，且第二个 trip 的 advance 没被调用。"""
    from tripplan.web.jobs import JobRegistry

    _seed(trips_root, "kyoto")
    _seed(trips_root, "osaka")
    reg = JobRegistry(max_jobs=1)
    app = make_app(registry=reg)
    c = app.test_client()
    c.get("/")
    with c.session_transaction() as sess:
        token = sess["_csrf"]

    gate = threading.Event()
    busy = reg.start("kyoto", EventLog(trips_root / "kyoto" / "events.jsonl"),
                     lambda j: (gate.wait(5), JobOutcome.ok(1))[1])
    try:
        resp = c.post("/trips/osaka/commands",
                      data={"_csrf": token, "kind": "confirm", "expected_revision": "3"})
        assert resp.status_code == 503
        assert "正忙" in resp.get_data(as_text=True)
        assert runner.calls == []
    finally:
        gate.set()
        busy.thread.join(5)


# ---------- 取消 ----------

def test_cancel_flips_a_running_job_to_cancelling(client, csrf, trips_root, app):
    _seed(trips_root, stage=Stage.GENERATE)
    reg = app.extensions["tripplan"]["registry"]
    gate = threading.Event()
    job = reg.start("kyoto", EventLog(trips_root / "kyoto" / "events.jsonl"),
                    lambda j: (gate.wait(5), JobOutcome.ok(1))[1])
    try:
        resp = client.post("/trips/kyoto/cancel", data={"_csrf": csrf()})
        assert resp.status_code == 302
        assert job.status == "cancelling"
        assert job.cancel_token.is_set()
        assert job.status_version == 2
    finally:
        gate.set()
        job.thread.join(5)


def test_cancel_is_idempotent_and_does_not_bump_the_version_twice(client, csrf, trips_root, app):
    """重复点不报错，也**不再 +1 status_version**——否则每点一下都让所有
    标签页白刷一次（spec §6.2）。"""
    _seed(trips_root, stage=Stage.GENERATE)
    reg = app.extensions["tripplan"]["registry"]
    gate = threading.Event()
    job = reg.start("kyoto", EventLog(trips_root / "kyoto" / "events.jsonl"),
                    lambda j: (gate.wait(5), JobOutcome.ok(1))[1])
    try:
        token = csrf()
        client.post("/trips/kyoto/cancel", data={"_csrf": token})
        client.post("/trips/kyoto/cancel", data={"_csrf": token})
        assert job.status_version == 2
    finally:
        gate.set()
        job.thread.join(5)


def test_cancel_without_a_job_is_a_no_op_redirect(client, csrf, trips_root):
    _seed(trips_root)
    resp = client.post("/trips/kyoto/cancel", data={"_csrf": csrf()})
    assert resp.status_code == 302


# ---------- 重建产物 ----------

def test_rebuild_dispatches_a_job(client, csrf, trips_root, runner):
    _seed(trips_root, stage=Stage.DONE, rev=5)
    resp = client.post("/trips/kyoto/artifacts", data={"_csrf": csrf()})
    _wait(runner)
    assert resp.status_code == 302
    assert runner.rebuilds == ["kyoto"]


def test_rebuild_is_refused_while_a_job_is_active(client, csrf, trips_root, app, runner):
    """§9 回归 23 的第三条：那个还没退出的线程可能正在往自己的暂存目录里写、
    马上要 publish()，此时插一次重建就是两个发布者抢同一份最终产物（spec §6.2）。"""
    _seed(trips_root, stage=Stage.DONE, rev=5)
    reg = app.extensions["tripplan"]["registry"]
    gate = threading.Event()
    job = reg.start("kyoto", EventLog(trips_root / "kyoto" / "events.jsonl"),
                    lambda j: (gate.wait(5), JobOutcome.ok(1))[1])
    job.request_cancel()  # cancelling 同样要挡
    try:
        resp = client.post("/trips/kyoto/artifacts", data={"_csrf": csrf()})
        assert resp.status_code == 409
        assert runner.rebuilds == []
    finally:
        gate.set()
        job.thread.join(5)


def test_rebuild_is_refused_before_the_trip_is_done(client, csrf, trips_root, runner):
    _seed(trips_root, stage=Stage.AWAIT_CHOICE)
    resp = client.post("/trips/kyoto/artifacts", data={"_csrf": csrf()})
    assert resp.status_code == 409
    assert runner.rebuilds == []


# ---------- CSRF 全覆盖 ----------

@pytest.mark.parametrize(
    "path,form",
    [
        ("/trips", {"request": "去京都"}),
        ("/trips/kyoto/commands", {"kind": "confirm", "expected_revision": "3"}),
        ("/trips/kyoto/cancel", {}),
        ("/trips/kyoto/artifacts", {}),
    ],
)
@pytest.mark.parametrize("token", [None, "错的"])
def test_every_post_route_refuses_a_missing_or_wrong_csrf_token(
    client, trips_root, app, runner, path, form, token
):
    """§9 回归 19：四个 POST 路由逐个测，缺 token、错 token 一律 403，
    且此时 advance / 取消令牌都没被碰过。"""
    _seed(trips_root, stage=Stage.DONE, rev=3)
    reg = app.extensions["tripplan"]["registry"]
    gate = threading.Event()
    job = reg.start("kyoto", EventLog(trips_root / "kyoto" / "events.jsonl"),
                    lambda j: (gate.wait(5), JobOutcome.ok(1))[1])
    try:
        client.get("/")  # 建立 session
        data = dict(form)
        if token is not None:
            data["_csrf"] = token
        resp = client.post(path, data=data)
        assert resp.status_code == 403
        assert runner.calls == [] and runner.rebuilds == []
        assert not job.cancel_token.is_set()
    finally:
        gate.set()
        job.thread.join(5)
```

- [ ] **Step 2: 跑测试确认它失败**

Run: `/Users/jialiu/Projects/trip-plan/.venv/bin/python -m pytest tests/web/test_commands.py -v`
Expected: 绝大多数 FAIL，`501 NOT IMPLEMENTED`

- [ ] **Step 3: 写实现**

`src/tripplan/web/app.py`：补 import

```python
from tripplan.state import (
    AmendRequirements,
    ChooseCandidate,
    ConfirmRequirements,
    GiveFeedback,
    Stage,
    TripState,
)
```

把三个占位路由换成：

```python
    @app.post("/trips/<tid>/commands")
    def commands(tid):
        cfg = app.extensions["tripplan"]
        resolve_trip_dir(cfg["trips_root"], tid)  # 越界一律 404
        cmd, error = _build_command(request.form)
        if error is not None:
            return _detail_with_notice(cfg, tid, error, 400)
        return _start_command(cfg, tid, cmd)

    @app.post("/trips/<tid>/cancel")
    def cancel(tid):
        cfg = app.extensions["tripplan"]
        resolve_trip_dir(cfg["trips_root"], tid)
        job = cfg["registry"].get(tid)
        if job is not None:
            # 幂等：没有 job 在跑、或已经是 cancelling 时都是 no-op，
            # 重复点不报错也不 +1 status_version（spec §6.2）。
            job.request_cancel()
        return redirect(url_for("detail", tid=tid), code=302)

    @app.post("/trips/<tid>/artifacts")
    def rebuild(tid):
        cfg = app.extensions["tripplan"]
        trip_dir = resolve_trip_dir(cfg["trips_root"], tid)
        try:
            state = FileRepo(trip_dir).load()
        except (TripCorrupt, TripNotFound, UnsupportedVersion) as e:
            return _detail_with_notice(cfg, tid, str(e), 409)
        if state.stage is not Stage.DONE:
            return _detail_with_notice(cfg, tid, "行程还没定稿，没有可重建的成稿产物。", 409)

        job_holder = cfg["registry"]
        rebuild_target = cfg["rebuild_fn"]
        try:
            job_holder.start(
                tid,
                cfg["store"].get(tid),
                lambda job: rebuild_target(cfg["trips_root"], tid, cfg["deps"], job),
            )
        except TripBusy:
            # 那个还没退出的线程可能正要 publish()，插一次重建就是两个
            # 发布者抢同一份最终产物（spec §6.2）。
            return _detail_with_notice(cfg, tid, "这个行程正在跑上一步，等它结束再重建。", 409)
        except ServerBusy:
            return _detail_with_notice(cfg, tid, "服务器正忙，稍后再试。", 503)
        return redirect(url_for("detail", tid=tid), code=302)
```

模块级追加：

```python
_KINDS = {"", "confirm", "amend", "choose", "feedback"}


def _build_command(form):
    """把表单摊成 state.py 现成的命令类型。

    顺带消除的一个 bug 面（spec §6.2）：angle_key 来自页面上渲染的按钮 value，
    是真实 key。CLI 里 _resolve_candidate_key() 那整块「大小写兜底」逻辑
    （连同它注释里描述的「用户被困死只能 Ctrl-C」的场景）在 Web 下从根上
    不存在——用户不再手敲 key。
    """
    kind = (form.get("kind") or "").strip()
    if kind not in _KINDS:
        return None, f"不认识的操作：{kind}"

    raw_rev = (form.get("expected_revision") or "").strip()
    try:
        expected = int(raw_rev)
    except ValueError:
        return None, "表单已过期，请刷新页面后重试。"

    text = (form.get("text") or "").strip()
    angle_key = (form.get("angle_key") or "").strip()
    if len(text) > MAX_REQUEST_CHARS:
        return None, f"内容太长了（{len(text)} 字，上限 {MAX_REQUEST_CHARS} 字）。"
    if len(angle_key) > MAX_ANGLE_KEY_CHARS:
        return None, "候选标识不合法。"

    if kind == "":
        return None, None  # 「继续」
    if kind == "confirm":
        return ConfirmRequirements(expected), None
    if kind == "amend":
        if not text:
            return None, "要改什么？写一句再提交。"
        return AmendRequirements(expected, text), None
    if not angle_key:
        return None, "没有指定是哪一份候选。"
    if kind == "choose":
        return ChooseCandidate(expected, angle_key), None
    if not text:
        return None, "意见写一句再提交，或者直接点「选它」。"
    return GiveFeedback(expected, angle_key, text), None
```

`_start_command` 的 `ServerBusy` 分支在 `/trips/<tid>/commands` 下应该是**带横幅的详情页 + 503**（spec §6.2：按钮不置灰，可以再点），而新建行程时是 `notice.html`。所以给 `_start_command` 加一个参数：

```python
def _start_command(cfg, tid: str, cmd, *, busy_page=None):
    ...
    except ServerBusy:
        if busy_page is not None:
            return busy_page()
        return _detail_with_notice(cfg, tid, "服务器正忙，稍后再试。", 503)
```

`create_trip` 调用时传 `busy_page=lambda: (render_template("notice.html", ...), 503)`。

- [ ] **Step 4: 跑测试确认通过**

Run: `/Users/jialiu/Projects/trip-plan/.venv/bin/python -m pytest tests/web/test_commands.py -v && /Users/jialiu/Projects/trip-plan/.venv/bin/python -m pytest -q`
Expected: 全绿；全量约 `776 passed, 1 deselected`

- [ ] **Step 5: 格式化并提交**

```bash
/Users/jialiu/Projects/trip-plan/.venv/bin/black src tests
git add src/tripplan/web/app.py tests/web/test_commands.py
git commit -m "feat(web): commands/cancel/artifacts 三个动作路由，cancelling 一律挡住"
```

---

## Task 13: `GET /trips/<tid>/events` + 前端轮询

**Files:**
- Modify: `src/tripplan/web/app.py`（`events` 占位换成真实现）
- Create: `src/tripplan/web/static/app.js`
- Create: `tests/web/test_events_endpoint.py`

**Interfaces:**
- Consumes: Task 5 的 `EventLog.since()`，Task 7 的 `TripJob.snapshot()`，Task 4 的 `artifact_ready`
- Produces:
  - `GET /trips/<tid>/events?since=<int>&epoch=<str>` → JSON：`events` / `first_seq` / `last_seq` / `stream_epoch` / `reset_required` / `resume_seq` / `job` / `stage` / `revision` / `artifact_ready`
  - `events[]` 每项：`seq` / `ts` / `type` / `payload` / `stream_id` / `text`（`text` 是服务端渲染好的中文——前端不需要知道任何业务状态怎么渲染，spec §6.3）
  - `job` 无 job 时是 `{"id": null, "status": "none", "status_version": 0, "kind": null, "message": null}`
  - `static/app.js`：四条前端逻辑

- [ ] **Step 1: 写失败的测试**

新建 `tests/web/test_events_endpoint.py`：

```python
"""轮询接口（spec §6.3 / §9 回归 2、17、20、22、25）。"""

import threading
import time
from datetime import date

from tripplan.artifacts import publish, stage_artifacts
from tripplan.providers.fake import FakeProvider
from tripplan.repo import FileRepo
from tripplan.state import Stage, TripState
from tripplan.web.events import EventLog
from tripplan.web.jobs import JobOutcome


def _seed(trips_root, tid="kyoto", stage=Stage.GENERATE, rev=3):
    """带一个可发布的候选：没有它，DONE 状态下 stage_artifacts 产出空
    names，artifacts.json 的 files 是 []，而 all([]) 是 True —— artifact_ready
    照样返回 True，下面那条测试就会以错误的理由通过。"""
    from datetime import datetime, timedelta, timezone

    from tripplan.models.common import Field, Origin
    from tripplan.models.facts import FactSnapshot
    from tripplan.models.itinerary import Angle, Day, Itinerary
    from tripplan.models.requirements import DateRange, Party, Requirements
    from tripplan.state import CandidateSlot, SlotStatus

    d1 = date(2026, 10, 1)
    state = TripState.new("去京都", run_id="r1")
    state.stage, state.revision = stage, rev
    state.requirements = Requirements(
        destination=Field("京都", Origin.USER),
        dates=Field(DateRange(d1, d1), Origin.USER),
        party=Field(Party(adults=2), Origin.USER),
    )
    angle = Angle("foodie", "吃遍京都", "")
    facts = FactSnapshot(
        poi_by_activity={}, constraint_pois={}, routes=[], weather={},
        trip_timezone="Asia/Tokyo",
        resolved_at=datetime(2026, 9, 1, tzinfo=timezone(timedelta(hours=9))), gaps=[],
    )
    state.candidates = [CandidateSlot(
        angle, Itinerary(angle=angle, days=[Day(id="d1", date=d1, activities=[])]),
        facts, SlotStatus.OK)]
    if stage is Stage.DONE:
        state.chosen_key = "foodie"
    FileRepo(trips_root / tid).create(state)
    return state


def test_the_envelope_has_every_field_the_frontend_reads(client, trips_root, app):
    _seed(trips_root)
    app.extensions["tripplan"]["store"].get("kyoto").append("generating", {"args": ["foodie"]})

    body = client.get("/trips/kyoto/events?since=0").get_json()
    assert set(body) == {
        "events", "first_seq", "last_seq", "stream_epoch", "reset_required",
        "resume_seq", "job", "stage", "revision", "artifact_ready",
    }
    assert body["stage"] == "GENERATE"
    assert body["revision"] == 3
    assert body["artifact_ready"] is False
    assert body["job"] == {"id": None, "status": "none", "status_version": 0,
                           "kind": None, "message": None}


def test_events_carry_server_rendered_text(client, trips_root, app):
    """前端不需要知道任何业务状态怎么渲染，它只判断「要不要刷新」（spec §6.3）。"""
    _seed(trips_root)
    app.extensions["tripplan"]["store"].get("kyoto").append("generating", {"args": ["foodie"]})
    [ev] = client.get("/trips/kyoto/events?since=0").get_json()["events"]
    assert ev["seq"] == 1
    assert ev["type"] == "generating"
    assert ev["payload"] == {"args": ["foodie"]}
    assert ev["stream_id"] is None
    assert ev["text"] == "正在生成候选 foodie"


def test_since_returns_only_the_increment(client, trips_root, app):
    """§9 回归 2 的后半条。"""
    _seed(trips_root)
    log = app.extensions["tripplan"]["store"].get("kyoto")
    for key in "ABCD":
        log.append("generating", {"args": [key]})

    body = client.get("/trips/kyoto/events?since=2").get_json()
    assert [e["seq"] for e in body["events"]] == [3, 4]
    assert body["last_seq"] == 4
    assert body["reset_required"] is False


def test_a_stale_cursor_or_epoch_demands_a_reset_with_a_resume_point(client, trips_root, make_app):
    """§9 回归 20：宁可多刷一次，不接受静默漏事件（spec §5.5）。"""
    from tripplan.web.events import EventLogStore

    _seed(trips_root)
    store = EventLogStore(trips_root, ring_size=3)
    app = make_app(store=store)
    c = app.test_client()
    log = store.get("kyoto")
    for _ in range(10):
        log.append("t", {}, durable=False)

    stale = c.get("/trips/kyoto/events?since=1").get_json()
    assert stale["reset_required"] is True
    assert stale["resume_seq"] == 10

    wrong_epoch = c.get("/trips/kyoto/events?since=10&epoch=别的进程").get_json()
    assert wrong_epoch["reset_required"] is True


def test_reset_converges_after_one_snapshot(client, trips_root, make_app):
    """§9 回归 25：用详情页给出的 cursor 再轮询，这一次**必须**
    reset_required=false。守的是收敛性，不是单次行为。"""
    from tripplan.web.events import EventLogStore

    _seed(trips_root)
    store = EventLogStore(trips_root, ring_size=3)
    app = make_app(store=store)
    c = app.test_client()
    log = store.get("kyoto")
    log.append("a", {})
    log.flush()
    for _ in range(20):
        log.append("t", {}, durable=False)

    assert c.get("/trips/kyoto/events?since=1").get_json()["reset_required"] is True

    html = c.get("/trips/kyoto").get_data(as_text=True)
    cursor = int(html.split('data-cursor="')[1].split('"')[0])
    again = c.get(f"/trips/kyoto/events?since={cursor}&epoch={log.stream_epoch}").get_json()
    assert again["reset_required"] is False


def test_a_terminal_job_keeps_a_stable_status_version(client, trips_root, app):
    """§9 回归 17：error 是**留在 job 上的终态字段**，不是一次性事件。
    照原稿「error 非空就 reload」会每秒刷一次，用户连错误信息都读不完
    （spec §6.3）。"""
    _seed(trips_root)
    reg = app.extensions["tripplan"]["registry"]
    job = reg.start("kyoto", EventLog(trips_root / "kyoto" / "events.jsonl"),
                    lambda j: JobOutcome.failed("ProviderError", "高德限流"))
    job.thread.join(5)

    first = client.get("/trips/kyoto/events?since=0").get_json()["job"]
    second = client.get("/trips/kyoto/events?since=0").get_json()["job"]
    assert first == second
    assert first["status"] == "failed"
    assert first["kind"] == "ProviderError"
    assert first["message"] == "高德限流"


def test_a_new_job_is_distinguishable_from_the_old_one(client, trips_root, app):
    """§9 回归 22：模拟一个「错过中间终态」的标签页。**只比 status_version
    会相等**，这条测试就是守着这一点；同时断言 revision 在这一串里没变
    （证明 revision 兜不住）。"""
    _seed(trips_root)
    reg = app.extensions["tripplan"]["registry"]
    log = EventLog(trips_root / "kyoto" / "events.jsonl")

    gate = threading.Event()
    job_a = reg.start("kyoto", log, lambda j: (gate.wait(5), JobOutcome.ok(3))[1])
    first = client.get("/trips/kyoto/events?since=0").get_json()
    assert first["job"]["status"] == "running"

    gate.set()
    job_a.thread.join(5)
    job_a.finish(JobOutcome.failed("ProviderError", "高德限流"))  # 标签页错过了这一步

    gate2 = threading.Event()
    job_b = reg.start("kyoto", log, lambda j: (gate2.wait(5), JobOutcome.ok(3))[1])
    try:
        second = client.get("/trips/kyoto/events?since=0").get_json()
        assert second["job"]["status"] == "running"
        assert second["job"]["status_version"] == first["job"]["status_version"]  # 相等！
        assert second["job"]["id"] != first["job"]["id"]                          # 靠它才分得出
        assert second["revision"] == first["revision"]                            # revision 兜不住
    finally:
        gate2.set()
        job_b.thread.join(5)


def test_artifact_ready_flips_when_the_manifest_lands(client, trips_root):
    """前端把 artifact_ready 当作四个刷新判据之一（spec §6.3），所以它必须
    真的会翻。"""
    state = _seed(trips_root, stage=Stage.DONE, rev=4)
    assert client.get("/trips/kyoto/events?since=0").get_json()["artifact_ready"] is False
    publish(stage_artifacts(state, trips_root / "kyoto", FakeProvider(), "job-1"))
    assert (trips_root / "kyoto" / "itinerary.html").exists()  # 确实发布了东西
    assert client.get("/trips/kyoto/events?since=0").get_json()["artifact_ready"] is True


def test_a_bad_since_value_is_treated_as_zero(client, trips_root):
    _seed(trips_root)
    assert client.get("/trips/kyoto/events?since=abc").status_code == 200


def test_the_events_endpoint_refuses_path_traversal(client):
    assert client.get("/trips/..%2f..%2fetc/events").status_code == 404
```

- [ ] **Step 2: 跑测试确认它失败**

Run: `/Users/jialiu/Projects/trip-plan/.venv/bin/python -m pytest tests/web/test_events_endpoint.py -v`
Expected: FAIL，`501 NOT IMPLEMENTED`

- [ ] **Step 3: 写路由**

`src/tripplan/web/app.py`：补 `from flask import jsonify`，把 `events` 占位换成：

```python
    @app.get("/trips/<tid>/events")
    def events(tid):
        cfg = app.extensions["tripplan"]
        trip_dir = resolve_trip_dir(cfg["trips_root"], tid)

        try:
            since = int(request.args.get("since", 0))
        except ValueError:
            since = 0
        epoch = request.args.get("epoch") or None

        result = cfg["store"].get(tid).since(since, epoch)
        job = cfg["registry"].get(tid)

        stage, revision, ready = "", 0, False
        try:
            state = FileRepo(trip_dir).load()
            stage, revision = state.stage.value, state.revision
            ready = artifact_ready(trip_dir, revision)
        except (TripCorrupt, TripNotFound, UnsupportedVersion):
            pass  # 轮询不该因为文件坏了就 500；详情页会把话说清楚

        return jsonify(
            events=[{**e.to_json(), "text": event_text(e.to_json())} for e in result.events],
            first_seq=result.first_seq,
            last_seq=result.last_seq,
            stream_epoch=result.stream_epoch,
            reset_required=result.reset_required,
            resume_seq=result.resume_seq,
            job=job.snapshot()
            if job is not None
            else {"id": None, "status": "none", "status_version": 0,
                  "kind": None, "message": None},
            stage=stage,
            revision=revision,
            artifact_ready=ready,
        )
```

- [ ] **Step 4: 写 `static/app.js`**

```js
/* 前端逻辑只有四条，刻意做得极薄（spec §6.3）：
   1. 每秒拉一次，带上本地游标（初值是快照的 cursor）与 epoch
   2. 新事件追加到进度区（认 stream_id：同 id 拼进同一个块）
   3. 四者之一变化就 location.reload()：job.id、job.status_version、
      revision、artifact_ready。刷新后把新值记为本地基线
   4. job.status 是终态时停止轮询；reset_required 为 true 时 reload 一次

   第 3 条必须是「版本变化」而不是「error 非空」：error 是留在 job 上的终态
   字段，不是一次性事件——reload 之后它还在那儿，于是又 reload，每秒一次，
   用户连错误信息都读不完。
   而光有 status_version 也不够：它是 per-job 的，新 job 从 1 重来，
   「老 job 的 running(1)」和「新 job 的 running(1)」看起来完全一样。
   revision 也补不上这个洞——failed/rejected/cancelled 三种终态不改 revision。
*/
(function () {
  var root = document.getElementById("progress");
  if (!root) return;
  var feed = document.getElementById("event-feed");
  var url = root.dataset.eventsUrl;
  var TERMINAL = ["succeeded", "rejected", "failed", "cancelled", "none"];

  var cursor = Number(root.dataset.cursor || 0);
  var epoch = root.dataset.epoch || "";
  var jobId = root.dataset.jobId || "";
  var statusVersion = Number(root.dataset.statusVersion || 0);
  var revision = Number(root.dataset.revision || 0);
  var artifactReady = root.dataset.artifactReady === "1";
  var timer = null;

  function stop() { if (timer) { clearInterval(timer); timer = null; } }

  function append(ev) {
    if (ev.stream_id) {
      var existing = feed.querySelector('[data-stream-id="' + ev.stream_id.replace(/"/g, "") + '"]');
      if (existing) { existing.textContent += ev.text; return; }
    }
    var li = document.createElement("li");
    if (ev.stream_id) li.setAttribute("data-stream-id", ev.stream_id);
    li.textContent = ev.text;          // textContent，不是 innerHTML
    feed.appendChild(li);
    feed.scrollTop = feed.scrollHeight;
  }

  function tick() {
    fetch(url + "?since=" + cursor + "&epoch=" + encodeURIComponent(epoch),
          { headers: { "Accept": "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data) return;                      // 网络抖动：下一秒再试
        if (data.reset_required) { stop(); window.location.reload(); return; }

        (data.events || []).forEach(append);
        cursor = data.last_seq;

        var job = data.job || {};
        var id = job.id || "";
        var version = Number(job.status_version || 0);
        if (id !== jobId || version !== statusVersion ||
            data.revision !== revision || data.artifact_ready !== artifactReady) {
          stop();
          window.location.reload();
          return;
        }
        if (TERMINAL.indexOf(job.status) >= 0) stop();
      })
      .catch(function () { /* 下一秒再试 */ });
  }

  timer = setInterval(tick, 1000);
})();
```

- [ ] **Step 5: 跑测试确认通过**

Run: `/Users/jialiu/Projects/trip-plan/.venv/bin/python -m pytest tests/web/test_events_endpoint.py -v && /Users/jialiu/Projects/trip-plan/.venv/bin/python -m pytest -q`
Expected: 10 条全绿；全量约 `786 passed, 1 deselected`

- [ ] **Step 6: 格式化并提交**

```bash
/Users/jialiu/Projects/trip-plan/.venv/bin/black src tests
git add src/tripplan/web/app.py src/tripplan/web/static/app.js tests/web/test_events_endpoint.py
git commit -m "feat(web): 轮询接口与前端 —— 二元组判据、终态停轮询、游标失效必刷新"
```

---

## Task 14: 成稿页 `GET /trips/<tid>/itinerary`

**Files:**
- Modify: `src/tripplan/web/app.py`（`itinerary` 占位换成真实现）
- Create: `tests/web/test_itinerary_page.py`

**Interfaces:**
- Consumes: Task 4 的 `artifact_ready`
- Produces: `GET /trips/<tid>/itinerary` → `send_file` 或 409 + 重建入口

- [ ] **Step 1: 写失败的测试**

新建 `tests/web/test_itinerary_page.py`：

```python
"""成稿页（spec §6.1 / §9 回归 15）。"""

from datetime import date, datetime, timedelta, timezone

from tripplan.artifacts import publish, stage_artifacts
from tripplan.models.common import Field, Origin
from tripplan.models.facts import FactSnapshot
from tripplan.models.itinerary import Angle, Day, Itinerary
from tripplan.models.requirements import DateRange, Party, Requirements
from tripplan.providers.fake import FakeProvider
from tripplan.repo import FileRepo
from tripplan.state import CandidateSlot, SlotStatus, Stage, TripState

D1 = date(2026, 10, 1)


def _done(trips_root, tid="kyoto", rev=5):
    state = TripState.new("去京都", run_id="r1")
    state.stage, state.revision = Stage.DONE, rev
    state.requirements = Requirements(
        destination=Field("京都", Origin.USER),
        dates=Field(DateRange(D1, D1), Origin.USER),
        party=Field(Party(adults=2), Origin.USER),
    )
    angle = Angle("foodie", "吃遍京都", "")
    facts = FactSnapshot(poi_by_activity={}, constraint_pois={}, routes=[], weather={},
                         trip_timezone="Asia/Tokyo",
                         resolved_at=datetime(2026, 9, 1, tzinfo=timezone(timedelta(hours=9))),
                         gaps=[])
    state.candidates = [CandidateSlot(
        angle, Itinerary(angle=angle, days=[Day(id="d1", date=D1, activities=[])]),
        facts, SlotStatus.OK)]
    state.chosen_key = "foodie"
    FileRepo(trips_root / tid).create(state)
    return state


def test_a_ready_itinerary_is_served_as_a_standalone_document(client, trips_root):
    """页面秒开，**不会在请求里现算高德静态地图**（spec §6.1）——文件在 job
    跑到 DONE 时就已经暂存并原子发布了。"""
    state = _done(trips_root)
    publish(stage_artifacts(state, trips_root / "kyoto", FakeProvider(), "job-1"))

    resp = client.get("/trips/kyoto/itinerary")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "<!DOCTYPE html>" in body
    assert "text/html" in resp.headers["Content-Type"]


def test_a_missing_artifact_is_409_with_a_rebuild_entry_not_a_bare_404(client, trips_root):
    """§9 回归 15：详情页在这种状态下本来也不会给出这个链接，这里是直接输
    URL 或用旧书签进来的兜底（spec §6.1）。"""
    _done(trips_root)
    resp = client.get("/trips/kyoto/itinerary")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 409
    assert "重建" in body
    assert "/trips/kyoto" in body


def test_a_stale_manifest_is_also_409(client, trips_root):
    import json

    state = _done(trips_root, rev=5)
    publish(stage_artifacts(state, trips_root / "kyoto", FakeProvider(), "job-1"))
    path = trips_root / "kyoto" / "artifacts.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["revision"] = 4
    path.write_text(json.dumps(data), encoding="utf-8")

    assert client.get("/trips/kyoto/itinerary").status_code == 409


def test_deleting_the_file_by_hand_and_rebuilding_restores_the_page(client, csrf, trips_root, app):
    """§9 回归 15 的完整闭环：删文件 → 详情页显示「待重建」→ 409 →
    重建 → 恢复。这一条用真实的 rebuild_artifacts，不注入替身。"""
    from tripplan.web.jobs import rebuild_artifacts

    state = _done(trips_root)
    publish(stage_artifacts(state, trips_root / "kyoto", FakeProvider(), "job-1"))
    (trips_root / "kyoto" / "itinerary.html").unlink()

    assert "待重建" in client.get("/trips/kyoto").get_data(as_text=True)
    assert client.get("/trips/kyoto/itinerary").status_code == 409

    app.extensions["tripplan"]["rebuild_fn"] = rebuild_artifacts
    client.post("/trips/kyoto/artifacts", data={"_csrf": csrf()})
    app.extensions["tripplan"]["registry"].get("kyoto").thread.join(5)

    assert client.get("/trips/kyoto/itinerary").status_code == 200
    assert "待重建" not in client.get("/trips/kyoto").get_data(as_text=True)


def test_the_itinerary_route_refuses_path_traversal(client):
    assert client.get("/trips/..%2f..%2fetc/itinerary").status_code == 404
```

- [ ] **Step 2: 跑测试确认它失败**

Run: `/Users/jialiu/Projects/trip-plan/.venv/bin/python -m pytest tests/web/test_itinerary_page.py -v`
Expected: FAIL，`501 NOT IMPLEMENTED`

- [ ] **Step 3: 写路由**

`src/tripplan/web/app.py`：补 `from flask import send_file`，把 `itinerary` 占位换成：

```python
    @app.get("/trips/<tid>/itinerary")
    def itinerary(tid):
        cfg = app.extensions["tripplan"]
        trip_dir = resolve_trip_dir(cfg["trips_root"], tid)
        try:
            state = FileRepo(trip_dir).load()
        except (TripCorrupt, TripNotFound, UnsupportedVersion) as e:
            return render_template("notice.html", title="读不出这个行程", message=str(e)), 409

        path = trip_dir / "itinerary.html"
        if not artifact_ready(trip_dir, state.revision) or not path.exists():
            # **不是裸 404**：详情页在这种状态下本来也不会给出这个链接，
            # 这里是直接输 URL 或用旧书签进来的兜底（spec §6.1）。
            return (
                render_template(
                    "notice.html",
                    title="产物需要重建",
                    message="成稿文件缺失，或与当前 rev 对不上。回到行程页点「重建产物」即可。",
                    link_tid=tid,
                ),
                409,
            )
        # send_file 一份**完整的独立 HTML 文档**，根本不过模板——转义责任在
        # render/itinerary_html.py（既有行为，本期不改，spec §6.1）。
        return send_file(path, mimetype="text/html")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `/Users/jialiu/Projects/trip-plan/.venv/bin/python -m pytest tests/web/test_itinerary_page.py -v && /Users/jialiu/Projects/trip-plan/.venv/bin/python -m pytest -q`
Expected: 5 条全绿；全量约 `791 passed, 1 deselected`

- [ ] **Step 5: 格式化并提交**

```bash
/Users/jialiu/Projects/trip-plan/.venv/bin/black src tests
git add src/tripplan/web/app.py tests/web/test_itinerary_page.py
git commit -m "feat(web): 成稿页 —— 秒开已发布的 HTML，未就绪给 409 + 重建入口而非死链"
```

---

## Task 15: 新增 `trip web`（交互命令这一任务里**原样留着**）

**Files:**
- Modify: `src/tripplan/cli.py`（**只加不删**）
- Create: `tests/test_cli_web.py`

**为什么「加 `trip web`」和「删 `plan`/`resume`」拆成两个任务**

这两件事没有技术上的耦合：`_cmd_web` 不碰 `_cmd_plan` 的任何一行，删掉后者也不会让前者
多出或少掉任何能力。合成一个任务只会带来两个坏处：

- **回滚粒度太粗**。真实闭环（Step 5）是这一期唯一一次人眼验收，它一旦不过——中文输入花屏、
  进度区不动、取消点不动——要退回去时，`git revert` 退掉的是「新增 `trip web`」和
  「删掉旧 CLI」打包在一起的一个提交，于是**连唯一能用的旧入口也一并没了**。拆开之后，
  Task 15 的提交可以留着继续调，Task 16 压根还没发生。
- **定位变难**。合并提交里同时有 ~250 行新增和 ~400 行删除，全量测试红了要先分清是新代码
  的问题还是删多了。

代价是显式的、而且很小：Task 15 结束时 `plan` / `resume` / `terminal_ask` / `drive` 和它们那
~24 条测试还在仓库里活着（多留一个任务的周期），全量测试数因此比最终态高一截。这不是遗漏，
是刻意的中间态——Task 16 的职责就是把它清掉，`grep` 清单在最后的「收尾检查」里。

**验收关口：Task 15 的 Step 5（真实中文输入闭环）不过，就不要开始 Task 16。**

**Interfaces:**
- Consumes: Task 9 的 `create_app`、Task 4 的 `sweep_stale_staging`、既有的 `build_deps`
- Produces:
  - `def _serve(app, host, port) -> None`
  - `def _require_web_deps() -> None`
  - `def _cmd_web(args) -> int`
  - `trip web --host 127.0.0.1 --port 8000 --trips-dir trips`

- [ ] **Step 1: 写失败的测试**

新建 `tests/test_cli_web.py`：

```python
"""trip web 的启动纪律（spec §6.5 / §7 / §9 回归 6）。"""

import pytest

from tripplan.cli import main


@pytest.fixture(autouse=True)
def _sealed(monkeypatch, tmp_path):
    for var in ("AMAP_KEY", "ANTHROPIC_API_KEY", "TRIPPLAN_WEB_TOKEN",
                "TRIPPLAN_WEB_SECRET", "TRIPPLAN_CONFIG", "TRIPPLAN_ROLES"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "sealed-home"))


@pytest.fixture
def served(monkeypatch):
    """拦住 waitress.serve，测试只看「有没有走到起服务这一步、参数对不对」。"""
    calls = []
    monkeypatch.setattr("tripplan.cli.build_deps", lambda dry_run=False: object())
    monkeypatch.setattr("tripplan.cli._serve", lambda app, host, port: calls.append((host, port)))
    return calls


def test_binding_to_all_interfaces_without_a_token_is_refused(tmp_path, capsys, served):
    """§9 回归 6：不给「裸奔到局域网」留口子（spec §6.5）。"""
    code = main(["web", "--host", "0.0.0.0", "--trips-dir", str(tmp_path)])
    err = capsys.readouterr().err
    assert code != 0
    assert "TRIPPLAN_WEB_TOKEN" in err
    assert "Traceback" not in err
    assert served == []          # 服务压根没起来


def test_binding_to_localhost_without_a_token_is_fine(tmp_path, served):
    assert main(["web", "--trips-dir", str(tmp_path)]) == 0
    assert served == [("127.0.0.1", 8000)]


def test_a_token_unlocks_binding_to_all_interfaces(tmp_path, monkeypatch, served):
    monkeypatch.setenv("TRIPPLAN_WEB_TOKEN", "hunter2")
    assert main(["web", "--host", "0.0.0.0", "--port", "9000", "--trips-dir", str(tmp_path)]) == 0
    assert served == [("0.0.0.0", 9000)]


def test_startup_sweeps_stale_staging_directories(tmp_path, monkeypatch, served):
    """spec §4.1：.staging/ 下的孤儿子目录由 trip web 启动时扫一次删掉。"""
    import os
    import time

    from tripplan.artifacts import STAGING_DIR

    orphan = tmp_path / "kyoto" / STAGING_DIR / "dead-job"
    orphan.mkdir(parents=True)
    ancient = time.time() - 7200
    os.utime(orphan, (ancient, ancient))

    main(["web", "--trips-dir", str(tmp_path)])
    assert not orphan.exists()


def test_missing_credentials_are_reported_at_startup_not_inside_a_job(tmp_path, capsys, monkeypatch):
    """spec §7：凭据类错误在启动时处理，运行类在 job 里转成 error 字段。"""
    monkeypatch.setattr("tripplan.cli._serve", lambda app, host, port: None)
    code = main(["web", "--trips-dir", str(tmp_path)])
    err = capsys.readouterr().err
    assert code != 0
    assert "AMAP_KEY" in err
    assert "Traceback" not in err


def test_a_missing_web_extra_is_reported_readably_not_as_a_traceback(
    tmp_path, capsys, monkeypatch, served
):
    """flask / waitress 是 **optional-dependency**（Task 9 写进 pyproject +
    uv.lock 的 `[project.optional-dependencies].web`）。只装了基础依赖的人跑
    `trip web`，撞上的是 `ModuleNotFoundError: No module named 'flask'` 一段
    堆栈——它既不说要装什么，也不说怎么装。`trip render` 不需要这两个包，所以
    「没装」是完全正常的状态，不是用户犯错。
    """
    def missing():
        raise ModuleNotFoundError("No module named 'flask'", name="flask")

    monkeypatch.setattr("tripplan.cli._require_web_deps", missing)

    code = main(["web", "--trips-dir", str(tmp_path)])
    err = capsys.readouterr().err
    assert code != 0
    assert "flask" in err
    assert "uv sync --extra web" in err
    assert "Traceback" not in err
    assert served == []          # 没装依赖就别假装起过服务
```

- [ ] **Step 2: 跑测试确认它失败**

Run: `/Users/jialiu/Projects/trip-plan/.venv/bin/python -m pytest tests/test_cli_web.py -v`
Expected: 全部 FAIL（`web` 子命令不存在 → argparse `SystemExit: 2`；
`_require_web_deps` 不存在 → `monkeypatch.setattr` 抛 `AttributeError`）

- [ ] **Step 3: 改 `cli.py`（只新增，不删任何东西）**

新增：

```python
def _serve(app, host: str, port: int) -> None:
    """单独抽出来只为可测：测试要拦住它，不能真的起一个服务器。

    **不用 Flask 自带的开发服务器**：它明确不适合对外提供服务，而本设计
    要绑 0.0.0.0。waitress 是纯 Python、跨平台、无需编译工具链的生产级
    WSGI 服务器，而且**单进程多线程**——JobRegistry 天然是一份（spec §3.3 / §6.4）。
    """
    from waitress import serve

    serve(app, host=host, port=port, threads=8)


def _require_web_deps() -> None:
    """提前把 flask / waitress 的缺席撞出来，好在 `_cmd_web` 里翻译成人话。

    单独一个函数只为两件事：**(a)** 在真正 import `tripplan.web.app`（它会连带
    拉起整个 Flask 应用模块）之前就失败；**(b)** 测试能 monkeypatch 它来模拟
    「这台机器没装 web extra」——总不能为了测一句提示语真去卸载 flask。
    """
    import waitress  # noqa: F401 —— 真正用它在 _serve 里，这里只探测存在性
    from tripplan.web.app import create_app  # noqa: F401


_WEB_EXTRA_HINT = (
    "错误：网页界面需要额外依赖，但没装上（缺 {missing}）。\n"
    "请执行 `uv sync --extra web` 装上 flask 与 waitress（开发环境用 `uv sync --extra dev`）；\n"
    "它们是可选依赖，`trip render` 用不到，所以默认不装。"
)

_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _cmd_web(args) -> int:
    from tripplan.artifacts import sweep_stale_staging

    token = os.environ.get("TRIPPLAN_WEB_TOKEN")
    if args.host not in _LOCAL_HOSTS and not token:
        # 不给「裸奔到局域网」留口子（spec §6.5）。
        print(
            f"错误：--host {args.host} 会把服务暴露到局域网，但没有设置访问口令。\n"
            "请先执行 `export TRIPPLAN_WEB_TOKEN=你自己想的口令` 再启动"
            "（浏览器会弹出登录框，用户名固定是 trip）；\n"
            "或者去掉 --host，只在本机 127.0.0.1 上访问。",
            file=sys.stderr,
        )
        return 1

    try:
        _require_web_deps()
    except ModuleNotFoundError as e:
        print(_WEB_EXTRA_HINT.format(missing=e.name or "flask / waitress"), file=sys.stderr)
        return 1

    from tripplan.web.app import create_app

    trips_root = Path(args.trips_dir)
    trips_root.mkdir(parents=True, exist_ok=True)
    # 启动时扫一次 .staging/ 的孤儿子目录（spec §4.1）。只删够老的：别的
    # 进程（一个正在跑的 trip render）可能正往自己的子目录里写。
    sweep_stale_staging(trips_root)

    deps = build_deps(dry_run=False)  # 凭据问题在这里就炸，不拖到 job 里
    app = create_app(trips_root, deps, token=token)

    where = "本机" if args.host in _LOCAL_HOSTS else "本机与局域网"
    print(f"行程目录：{trips_root.resolve()}")
    print(f"服务已启动（{where}）：http://{args.host}:{args.port}/")
    if token:
        print("访问需要口令：用户名 trip，密码取自 TRIPPLAN_WEB_TOKEN。")
    _serve(app, args.host, args.port)
    return 0
```

`main()` 的 parser 里**追加**（`plan` / `resume` / `render` 三个子命令这一任务里一个不动）：

```python
    w = sub.add_parser("web", help="启动网页界面")
    w.add_argument("--host", default="127.0.0.1", help="绑定地址；0.0.0.0 需要设 TRIPPLAN_WEB_TOKEN")
    w.add_argument("--port", type=int, default=8000)
    w.add_argument("--trips-dir", default="trips", help="行程目录的父目录")
    w.set_defaults(func=_cmd_web)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `/Users/jialiu/Projects/trip-plan/.venv/bin/python -m pytest tests/test_cli_web.py -v && /Users/jialiu/Projects/trip-plan/.venv/bin/python -m pytest -q`
Expected: 6 条全绿；全量约 `797 passed, 1 deselected`（旧交互命令与它那 ~24 条测试**仍然在**，
这是 Task 16 之前的正常中间态）

- [ ] **Step 5: 手动跑一遍真实闭环（人眼验收关口）**

```bash
cd /Users/jialiu/Projects/trip-plan/.worktrees/20260915-015213-web-ui
/Users/jialiu/Projects/trip-plan/.venv/bin/python -m tripplan.cli web --trips-dir /tmp/trip-smoke --port 8765
```

先验两条不需要凭据的：

- 故意不 `uv sync --extra web` 的机器上（或临时 `monkeypatch` 不到的场景下）应打印那句
  「请执行 `uv sync --extra web`」并退出 1，**不是堆栈**；
- 缺 `AMAP_KEY` 时应打印可读的中文提示并退出 1。两条都是有效验证。

有凭据时打开 `http://127.0.0.1:8765/`，**用中文在 textarea 里敲一段真实需求**，逐条确认：

- [ ] 输入不花屏、不吞字、长句折行正常（这正是整期要解决的问题，spec §1）
- [ ] 能建行程，跳到详情页
- [ ] 进度区每秒有新条目，文案是中文、看得懂
- [ ] 取消按钮可点，点完状态变「已请求停止…」，且**盘上没有多出一份候选全失败的行程**（spec §4.3）
- [ ] 确认需求 → 出候选 → 选一个 → 定稿，全程不需要碰终端
- [ ] 成稿页秒开、有地图

**这一步任意一条不过，就停在 Task 15 修，不要进 Task 16**——旧 CLI 还在，随时可以退回去用。

- [ ] **Step 6: 格式化并提交**

```bash
/Users/jialiu/Projects/trip-plan/.venv/bin/black src tests
git add src/tripplan/cli.py tests/test_cli_web.py
git commit -m "feat(cli): 新增 trip web；裸奔到局域网与漏装 web extra 都被挡在可读提示后面"
```

---

## Task 16: CLI 收口 —— 删掉交互命令

**前置：Task 15 的 Step 5 已经人眼验收通过。**

**Files:**
- Modify: `src/tripplan/cli.py`（这一次只删）
- Modify: `tests/test_cli.py`
- Modify: `tests/test_cli_web.py`（追加一条「确实删干净了」）

**Interfaces:**
- Consumes: Task 15 的 `trip web`
- Produces:
  - 删除：`plan`、`resume`、`terminal_ask()`、`drive()`、`_resolve_candidate_key()`、`_drive_and_report()`、`_print_event()`、`_cmd_plan`、`_cmd_resume`

- [ ] **Step 1: 写失败的测试**

在 `tests/test_cli_web.py` 末尾追加：

```python
def test_the_interactive_subcommands_are_gone():
    """spec §7：只剩 web 与 render 两个子命令。"""
    import tripplan.cli as cli

    for name in ("terminal_ask", "drive", "_resolve_candidate_key", "write_artifacts",
                 "_cmd_plan", "_cmd_resume", "_drive_and_report"):
        assert not hasattr(cli, name), name

    with pytest.raises(SystemExit):
        main(["plan", "去京都"])
    with pytest.raises(SystemExit):
        main(["resume", "trips/kyoto"])
```

- [ ] **Step 2: 跑测试确认它失败**

Run: `/Users/jialiu/Projects/trip-plan/.venv/bin/python -m pytest tests/test_cli_web.py -v`
Expected: `test_the_interactive_subcommands_are_gone` FAIL（`terminal_ask` 仍在），其余 6 条仍绿

- [ ] **Step 3: 改 `cli.py`**

删除：`_resolve_candidate_key`、`terminal_ask`、`_print_event`、`drive`、`_drive_and_report`、`_cmd_plan`、`_cmd_resume`，以及随之无用的 import（`render_candidates`、`render_requirement_card`、`Done`、`NeedInput`、`Rejected`、`InputKind`、`AmendRequirements`、`ChooseCandidate`、`ConfirmRequirements`、`GiveFeedback`、`Stage`、`TripState`、`TripExists`、`_advance`）。

`main()` 的 parser 里删掉 `plan` 与 `resume` 两个子命令，只留：

```python
    w = sub.add_parser("web", help="启动网页界面")
    w.add_argument("--host", default="127.0.0.1", help="绑定地址；0.0.0.0 需要设 TRIPPLAN_WEB_TOKEN")
    w.add_argument("--port", type=int, default=8000)
    w.add_argument("--trips-dir", default="trips", help="行程目录的父目录")
    w.set_defaults(func=_cmd_web)

    d = sub.add_parser("render", help="从 state.json 重新生成产物")
    d.add_argument("dir")
    d.add_argument("--format", default="both", help="md | html | both")
    d.set_defaults(func=_cmd_render)
```

`main()` 的 `except EOFError` 分支整段删除（`input()` 已经不存在）。`ProviderError` / `LimitExceeded` 那一段的文案改成不再提 `trip resume`：

```python
    except (ProviderError, LimitExceeded) as e:
        # 现在只有 `trip render` 会走到这里（Web 侧的运行期错误由 job 转成
        # JobOutcome，不冒到这一层）。
        kind = "高德或大模型服务" if isinstance(e, ProviderError) else "资源额度"
        print(f"错误：{kind}出了问题，本次操作已中断：{e}", file=sys.stderr)
        return 1
```

`TripNotFound` / `TripCorrupt` / `ConfigError` / `MissingCredential` 四个分支原样保留——`trip render` 与 `trip web` 的启动期都还会用到。

顶部 docstring 改成：

```python
"""CLI。只剩两个非交互子命令：`trip web` 与 `trip render`。

交互全部搬到浏览器：终端 IME 输入错乱（光标位置算错、退格删掉半个字、
长句折行后彻底花掉）在 readline 里绕不过去，而 <textarea> 天然没有这个问题。
Web 层是 orchestrator 的第二个 driver，与本模块平级（spec §1 / §3）。
"""
```

- [ ] **Step 4: 清理 `tests/test_cli.py`**

删除这些用例（交互相关，随代码一起删）：
`test_driver_saves_with_the_persisted_revision_not_the_new_one`、
`test_driver_reprompts_with_the_current_question_after_reject`、
`test_plan_refuses_to_reuse_an_existing_directory`、
`test_resume_reports_missing_trip`、
`test_resume_dry_run_reports_state_without_calling_llm`、
`test_plan_reports_provider_error_without_traceback`、
`test_resume_reports_limit_exceeded_without_traceback`、
`test_plan_without_amap_key_reports_readable_error`、
`test_plan_without_llm_credential_reports_readable_error`、
`test_driver_does_not_save_on_rejected_round`、
`test_resume_reports_corrupt_state_readably`、
`test_state_corrupted_mid_session_is_reported_readably`、
`test_state_deleted_mid_session_is_reported_readably`、
`test_driver_gives_up_readably_when_a_reject_carries_no_question`、
`test_eof_on_stdin_is_reported_readably_not_as_a_traceback`、
`_run_resume_with_a_hostile_ask`、`_Ask`、
以及全部 `terminal_ask` 相关的 8 条（`test_terminal_ask_*`、
`test_a_lowercase_angle_key_no_longer_livelocks_the_choice_prompt`、`_choice_need`、`_typed`）。

把三条只依赖子命令入口的用例改成走 `web`（等价覆盖，且是现存路径）：

```python
def test_config_error_from_bad_toml_is_readable(tmp_path, monkeypatch, capsys):
    """ConfigError 现在只可能从 `trip web` 的启动期 build_deps 里出来。"""
    monkeypatch.setenv("AMAP_KEY", "test-key-123")
    monkeypatch.setenv("TRIPPLAN_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr("tripplan.cli._serve", lambda app, host, port: None)
    bad = tmp_path / "bad.toml"
    bad.write_text("[models.x\n", encoding="utf-8")
    monkeypatch.setenv("TRIPPLAN_CONFIG", str(bad))

    code = main(["web", "--trips-dir", str(tmp_path / "trips")])
    err = capsys.readouterr().err
    assert code != 0
    assert "Traceback" not in err
    assert str(bad) in err
```

同法改写 `test_config_error_from_unknown_model_ref_is_readable`。
`test_trippan_log_default_leaves_root_logger_untouched` 已经走 `render`，保留。
`slugify`、`build_deps`、`build_provider`、`render` 相关的用例全部保留。

- [ ] **Step 5: 跑测试确认通过**

Run: `/Users/jialiu/Projects/trip-plan/.venv/bin/python -m pytest tests/test_cli.py tests/test_cli_web.py -v && /Users/jialiu/Projects/trip-plan/.venv/bin/python -m pytest -q`
Expected: 全绿。全量最终约 `774 passed, 1 deselected`（在 Task 15 的 ~797 基础上删掉 ~24 条交互用例、新增 1 条）

- [ ] **Step 6: 格式化并提交**

```bash
/Users/jialiu/Projects/trip-plan/.venv/bin/black src tests
git add src/tripplan/cli.py tests/test_cli.py tests/test_cli_web.py
git commit -m "refactor(cli): 删掉交互命令 plan/resume，入口只剩 web 与 render"
```

---

## 收尾检查（不是一个任务，是 Task 16 之后的确认清单）

- [ ] `grep -rn "terminal_ask\|_resolve_candidate_key\|write_artifacts" src/` 无结果
- [ ] `grep -rn "|safe\|Markup" src/tripplan/web/templates/` 无结果
- [ ] `grep -rn "status == .running.\|status != .running." src/tripplan/web/` 只在 `TripJob.request_cancel` 里出现一处（那里判的是「能不能从 running 迁到 cancelling」，不是 active 谓词）
- [ ] `grep -rn "import flask\|from flask" src/tripplan/web/jobs.py src/tripplan/web/events.py src/tripplan/web/view.py` 无结果
- [ ] `uv lock --check` 退出码 0（`pyproject.toml` 与 `uv.lock` 没跑偏）
- [ ] `git status --porcelain` 干净：`uv.lock` 已经跟着 `pyproject.toml` 一起提交了，不是留在工作区里
- [ ] `/Users/jialiu/Projects/trip-plan/.venv/bin/python -m pytest -q` 全绿
- [ ] `/Users/jialiu/Projects/trip-plan/.venv/bin/black --check src tests` 无改动
