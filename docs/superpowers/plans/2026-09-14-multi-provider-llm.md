# 多 provider LLM 接入 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `tripplan` 的 LLM 访问层同时支持 OpenAI 与 Anthropic，每个角色可独立选择 model，model 的 provider / 模型名 / base_url / key 全部可配置。

**Architecture:** 配置分两层——先定义一组 model（各带 provider、name、base_url、key，字符串字段支持 `${VAR:-default}` 展开），角色只引用 model 名。`RoutingClient` 实现现有的 `LlmClient` Protocol，按角色分派到对应 backend，因此 `steps.py` / `deps.py` 完全不动、`runner.py` 只动三处。两个 backend 各自把自家响应归一化成现有的 `LlmResponse`。

**Tech Stack:** Python 3.12、`anthropic>=0.40`（实测于 1.5.0）、`openai>=3.13`（可选依赖，懒加载；实测于 3.13.0）、`tomllib`、pytest、dataclasses。

**Spec:** `docs/superpowers/specs/2026-09-13-multi-provider-llm-design.md`（第 12 稿，经十一轮交叉评审）

---

## Global Constraints

这些约束适用于**每一个**任务，不再在任务内重复。

1. **异常类型铁律（最重要）。** 异常类型决定数据能不能活下来，异常消息决定用户能不能看懂。`run_slot` 只捕获 `LimitExceeded`（`slot.py:76`）与 `ProviderError`（`slot.py:86`）；其余一切落到 `orchestrator.py:254` 的 `_safe_slot`，而它把 `itinerary` 与 `facts` **硬编码成 `None`**——已生成的行程当场丢失。因此：
   - **加载期 / 构造期**可以用 `ConfigError` / `MissingCredential`（那时还没进 slot，`main()` 接得住）；
   - **请求期的一切错误一律 `ProviderError`**，想改善诊断就改**消息内容**，永远不要为了"消息更贴切"去换类型。
2. **请求期捕厂商基类，不捕 `APIError`。** `anthropic.CredentialsError` / `RetryableError` / `IdentityTokenFileError` 都**不是** `APIError` 子类（实测），而它们会在请求期刷新令牌时抛出；`_base_client.py:1296-1302` 明确把 `AnthropicError` 原样穿出不包装。所以捕 `anthropic.AnthropicError` / `openai.OpenAIError`。
3. **永不把 `key` 或整个 `ModelSpec` 交给 logger / repr。** `key_source` 也只有在能识别为 `${NAME}` / `${NAME:-…}` 形态时才可输出（用户写字面量时它就是明文密钥），否则回落到 provider 固定映射的变量名。
4. **全程不触网。** 所有测试用 `patch("anthropic.Anthropic")` / `patch("openai.OpenAI")` 或真实构造（构造不发请求）。
5. **格式化**：每次改完代码跑 `.venv/bin/black src tests`（CLAUDE.md 要求）。
6. **基线**：当前 `505 passed, 1 deselected`。每个任务结束时，除该任务有意改写的测试外全部保持绿。
7. **TDD**：先写测试、看它以正确理由失败、再写最小实现、再看它通过、然后提交。

---

## 文件结构

**新建**

| 文件 | 职责 |
|---|---|
| `src/tripplan/llm/errors.py` | `ConfigError` / `MissingCredential`。下沉到 `llm/` 是因为 backend 不能 import `cli` |
| `src/tripplan/llm/backends/__init__.py` | 空 |
| `src/tripplan/llm/backends/anthropic.py` | Anthropic 适配器（今天的 `AnthropicClient` 迁移并加固） |
| `src/tripplan/llm/backends/openai.py` | OpenAI 适配器 |
| `src/tripplan/llm/router.py` | `RoutingClient`：按角色分派、backend 缓存、加载期构造 |

**重写**

| 文件 | 变化 |
|---|---|
| `src/tripplan/llm/config.py` | `ModelSpec` / `RoleConfig` / `LlmConfig` / `expand()` / `load_config()`。删除 `independent_context` |
| `src/tripplan/llm/client.py` | 删掉 `AnthropicClient`（迁走），保留 Protocol / `Usage` / `ToolCall` / `LlmResponse` / `FakeLlm` |

**局部修改**

| 文件 | 处数 | 内容 |
|---|---|---|
| `src/tripplan/agents/runner.py` | 3 | 补偿一（拍平工具调用）、补偿二（`.strip() or`）、§13 诊断（结构性：包住整个 `while`） |
| `src/tripplan/cli.py` | 7 | 见 Task 8 与 Task 12 |
| `src/tripplan/agents/tools.py` | 1 | 注册期校验"至少一个必填参数" |
| `src/tripplan/render/candidates.py` | 1 | `FAILED` 也显示 `detail` |
| `src/tripplan/agents/limits.py` | 1 | 跨 provider 计量不可比的注释 |
| `pyproject.toml` | 1 | `openai` 可选依赖 + 进 `dev` extra |

**任务依赖**：1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → (9, 10, 11, 12 可并行)

---

## Task 1: `llm/errors.py` 与 `main()` 的配置错误收口

**Files:**
- Create: `src/tripplan/llm/errors.py`
- Modify: `src/tripplan/cli.py:43`（删 `MissingCredential` 定义，改为从 `llm.errors` 导入并重新导出）、`cli.py:363-405`（except 元组加 `ConfigError`）
- Test: `tests/llm/test_errors.py`（新建）、`tests/test_cli.py`

**Interfaces:**
- Produces: `tripplan.llm.errors.ConfigError`、`tripplan.llm.errors.MissingCredential`；`tripplan.cli.MissingCredential` 仍可导入（同一个类对象）

**背景**：今天 `load_config` 抛 `ValueError`，不在 `main()` 的 except 元组里，实测会逃逸成裸 traceback。本设计把配置错误的产生面放大了一个数量级，必须先把收口做好。

- [ ] **Step 1: 写失败测试**

创建 `tests/llm/test_errors.py`：

```python
"""ConfigError 必须能被 main() 接住并打印成中文——今天 load_config 抛的
ValueError 会直接逃成裸 traceback（设计文档 §11 有实测记录）。"""

import pytest

from tripplan.cli import MissingCredential as CliMissingCredential
from tripplan.llm.errors import ConfigError, MissingCredential


def test_missing_credential_is_the_same_class_from_both_paths():
    """cli.py 重新导出它，所以 cli.py:380 的 except 与测试的 import
    必须指向同一个类对象，否则 except 接不住。"""
    assert CliMissingCredential is MissingCredential


def test_config_error_is_not_a_value_error():
    """刻意不继承 ValueError：main() 按类型收口，ConfigError 要有自己的分支，
    不能靠碰巧是 ValueError 子类蹭进别人的 except。"""
    assert not issubclass(ConfigError, ValueError)
    assert issubclass(ConfigError, Exception)
```

在 `tests/test_cli.py` 末尾追加：

```python
# ---------- ConfigError 必须被 main() 收口成可读中文 ----------
#
# 这几条必须先 setenv AMAP_KEY。build_deps 里 AMAP 的前置检查（cli.py:236-242）
# 排在 load_config 之前，而 _no_real_credentials 会把 AMAP_KEY 清掉——不 setenv
# 的话请求根本走不到配置解析，测试拿到的是「缺少环境变量 AMAP_KEY」，
# 退出码非零、也没有 Traceback，两条断言全绿而 TOML 一个字节都没读过。
# 这是设计文档 §15 点名的假绿陷阱。


def test_config_error_from_bad_toml_is_readable(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AMAP_KEY", "test-key-123")
    monkeypatch.setenv("TRIPPLAN_CACHE", str(tmp_path / "cache"))
    bad = tmp_path / "bad.toml"
    bad.write_text('[models.x\n', encoding="utf-8")  # 缺右方括号
    monkeypatch.setenv("TRIPPLAN_CONFIG", str(bad))

    code = main(["plan", "去芜湖", "--dir", str(tmp_path / "t")])
    err = capsys.readouterr().err
    assert code != 0
    assert "Traceback" not in err
    assert str(bad) in err  # 断言确实读到了这个文件，而不是被 AMAP 拦下


def test_config_error_from_unknown_model_ref_is_readable(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("AMAP_KEY", "test-key-123")
    monkeypatch.setenv("TRIPPLAN_CACHE", str(tmp_path / "cache"))
    cfg = tmp_path / "roles.toml"
    cfg.write_text('[roles.critic]\nmodel = "claude-sonnet-5"\n', encoding="utf-8")
    monkeypatch.setenv("TRIPPLAN_CONFIG", str(cfg))

    code = main(["plan", "去芜湖", "--dir", str(tmp_path / "t")])
    err = capsys.readouterr().err
    assert code != 0
    assert "Traceback" not in err
    assert "未知的 model 引用" in err
    assert "claude-sonnet-5" in err  # 报的是用户写的原文
```

- [ ] **Step 2: 跑测试，确认以正确理由失败**

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/llm/test_errors.py -q
```
预期：`ModuleNotFoundError: No module named 'tripplan.llm.errors'`

- [ ] **Step 3: 创建 `src/tripplan/llm/errors.py`**

```python
"""LLM 层的错误类型。

为什么下沉到 llm/ 而不是留在 cli.py：backend 需要抛 MissingCredential，
而让 llm/backends/* 去 import cli 是层次倒置。cli.py 重新导出它，
tests/test_cli.py 的既有导入路径与 cli.py 的 except 都不受影响。

使用纪律（设计文档开篇铁律）：这两个类型**只能在加载期与构造期使用**。
请求期抛出的任何非 ProviderError 异常都会落到 orchestrator._safe_slot，
而它把 itinerary 与 facts 硬编码成 None——已经生成好的行程当场丢失，
用户看到的是「候选线出现未处理异常」而不是真正的原因。
"""


class ConfigError(Exception):
    """配置文件读不懂，或内容非法。加载期抛出。

    刻意不继承 ValueError：main() 是按异常类型收口的，ConfigError 要有自己
    的分支，不能靠碰巧是 ValueError 的子类蹭进别人的 except。
    """


class MissingCredential(Exception):
    """缺少运行所需的凭据。加载期或 backend 构造期抛出。"""
```

- [ ] **Step 4: 改 `cli.py`**

把 `cli.py:43` 那个 `class MissingCredential(Exception): ...` 的定义整段删掉，替换成导入 + 重新导出：

```python
from tripplan.llm.errors import ConfigError, MissingCredential  # noqa: F401

# MissingCredential 从 llm.errors 重新导出：tests/test_cli.py 与下面的
# except 都从 tripplan.cli 拿它，必须是同一个类对象。
```

然后在 `main()` 的 except 链里，紧挨着现有的 `except MissingCredential as e:` 分支之前或之后，加：

```python
    except ConfigError as e:
        # 配置读不懂是用户的输入问题，不是程序 bug——给一句人话，
        # 不要把 TOMLDecodeError / ValueError 的 traceback 糊到脸上。
        print(f"错误：{e}", file=sys.stderr)
        return 1
```

- [ ] **Step 5: 跑测试**

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/llm/test_errors.py -q
```
预期：2 passed。（`tests/test_cli.py` 新增的两条此时仍会失败——`load_config` 还没重写，Task 3 才会让它们变绿。先跳过它们：`-k "not config_error_from"`。）

- [ ] **Step 6: 全量回归**

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -k "not config_error_from"
```
预期：`505 passed`（新增 2 条 → 507，减去跳过的 2 条）。

- [ ] **Step 7: 格式化并提交**

```bash
.venv/bin/black src tests
git add -A
git commit -m "feat(llm): 新增 ConfigError 并让 main() 收口配置错误

MissingCredential 从 cli.py 下沉到 llm/errors.py——backend 要抛它，
让 llm/backends/* import cli 是层次倒置。cli.py 重新导出，既有导入
路径与 cli.py 的 except 不受影响。"
```

---

## Task 2: `${VAR}` / `${VAR:-default}` 展开

**Files:**
- Modify: `src/tripplan/llm/config.py`（新增 `expand()`，暂不动其余部分）
- Test: `tests/llm/test_config.py`

**Interfaces:**
- Produces: `expand(value: str, where: str) -> str`。`where` 是出错时报给用户的位置描述，例如 `"models.gpt5.key"`

- [ ] **Step 1: 写失败测试**

在 `tests/llm/test_config.py` 顶部追加 import，并在文件末尾加：

```python
from tripplan.llm.config import expand
from tripplan.llm.errors import ConfigError


def test_expand_uses_env_when_set(monkeypatch):
    monkeypatch.setenv("TRIP_X", "hello")
    assert expand("${TRIP_X}", "models.m.key") == "hello"


def test_expand_falls_back_to_default_when_unset(monkeypatch):
    monkeypatch.delenv("TRIP_X", raising=False)
    assert expand("${TRIP_X:-gpt-5}", "models.m.name") == "gpt-5"


def test_expand_raises_when_unset_and_no_default(monkeypatch):
    monkeypatch.delenv("TRIP_X", raising=False)
    with pytest.raises(ConfigError) as exc:
        expand("${TRIP_X}", "models.gpt5.key")
    message = str(exc.value)
    assert "models.gpt5.key" in message
    assert "TRIP_X" in message


def test_expand_empty_default_is_legal(monkeypatch):
    """${VAR:-} 的语义是「显式留空」，不是错误。内置默认配置靠它表达
    「交给 SDK 自己解析凭据」。"""
    monkeypatch.delenv("TRIP_X", raising=False)
    assert expand("${TRIP_X:-}", "models.m.base_url") == ""


def test_expand_treats_set_but_empty_env_as_defined(monkeypatch):
    """`export X=` 是用户显式表达「我知道它，但留空」，与「拼写错了」
    是两回事。判据是 os.environ.get(name) is None，不是真值判断。"""
    monkeypatch.setenv("TRIP_X", "")
    assert expand("${TRIP_X}", "models.m.key") == ""


def test_expand_leaves_non_matching_text_literal(monkeypatch):
    assert expand("claude-opus-5", "models.m.name") == "claude-opus-5"
    assert expand("$NOT_A_VAR", "models.m.name") == "$NOT_A_VAR"
    assert expand("price is $5", "models.m.name") == "price is $5"


def test_expand_default_stops_at_first_brace(monkeypatch):
    """不支持嵌套，默认值取到第一个 } 为止。写明规则比发明转义便宜，
    但必须写明——否则三个实现者会发明三套规则。"""
    monkeypatch.delenv("TRIP_A", raising=False)
    monkeypatch.setenv("TRIP_B", "bee")
    assert expand("${TRIP_A:-${TRIP_B}}", "models.m.name") == "${TRIP_B}"
```

- [ ] **Step 2: 跑测试确认失败**

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/llm/test_config.py -q -k expand
```
预期：`ImportError: cannot import name 'expand'`

- [ ] **Step 3: 实现**

在 `src/tripplan/llm/config.py` 顶部加 import 与实现：

```python
import os
import re

from tripplan.llm.errors import ConfigError

#: 只认两个形状：${NAME} 与 ${NAME:-默认值}。默认值取到**第一个** } 为止，
#: 不支持嵌套、不提供转义——`${A:-${B}}` 会取到 `${B` 为止。这些写法在
#: key / url / 模型名里不存在，但规则必须写死，否则每个实现者会发明一套。
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand(value: str, where: str) -> str:
    """展开 ${VAR} 与 ${VAR:-default}。

    `where` 是出错时报给用户的位置（如 "models.gpt5.key"）。

    判「变量是否已定义」用 `os.environ.get(name) is None` 而不是真值判断：
    `export X=` 是用户显式表达"我知道它，但留空"，与"拼写错了"是两回事，
    前者应当放行成空串，后者应当当场报错。
    """

    def _sub(m: re.Match) -> str:
        name, default = m.group(1), m.group(2)
        env = os.environ.get(name)
        if env is not None:
            return env
        if default is not None:
            return default
        raise ConfigError(
            f"{where} 引用了未设置的环境变量 {name}。"
            f"请先 export 它，或在配置里写 ${{{name}:-默认值}} 给一个默认值。"
        )

    return _VAR.sub(_sub, value)
```

- [ ] **Step 4: 跑测试确认通过**

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/llm/test_config.py -q -k expand
```
预期：7 passed

- [ ] **Step 5: 格式化并提交**

```bash
.venv/bin/black src tests
git add -A
git commit -m "feat(llm): 配置值支持 \${VAR} 与 \${VAR:-default} 展开"
```

---

## Task 3: 配置数据结构与 `load_config()` 重写

**Files:**
- Modify: `src/tripplan/llm/config.py`（`ModelSpec` / `RoleConfig` / `LlmConfig` / 内置默认 / `load_config`；删除 `independent_context` 与 `DEFAULT_ROLES`）
- Test: `tests/llm/test_config.py`（大幅改写）

**Interfaces:**
- Consumes: Task 2 的 `expand()`、Task 1 的 `ConfigError`
- Produces:
  - `ModelSpec(provider: str, name: str, base_url: str, key: str, name_source: str, key_source: str)`
  - `RoleConfig(model: str, max_tokens: int, allow_same_model: bool = False)`
  - `LlmConfig(models: dict[str, ModelSpec], roles: dict[Role, RoleConfig])`
  - `load_config(path: Path | None = None) -> LlmConfig`

**加载顺序（七步，必须按此实现，否则错误信息会不一致）：**

1. 读取并解析 TOML —— 捕 `OSError` 族（`FileNotFoundError` / `IsADirectoryError` / `PermissionError`）与 `ValueError` 族（`TOMLDecodeError` / `UnicodeDecodeError`）→ `ConfigError`
2. 合并 `models` 与 `roles`（与内置默认合并，用户同名条目**整条替换**）
3. 校验结构与类型
4. **解析引用**（`roles.*.model` 必须在 `models` 中存在）—— **在展开之前**，所以报错里永远是用户写的原文
5. 计算被引用到的 model 集合
6. 只对该集合校验 `provider`、展开三个字符串字段
7. 校验 critic 约束

- [ ] **Step 1: 写失败测试**

**这是"删三条 + 追加新的"，不是整份替换。** Task 2 刚写进 `tests/llm/test_config.py` 的 7 条 expand 测试必须原样保留——它们覆盖的是 §5 的展开语义。

从 `tests/llm/test_config.py` **删除**下面这三条，其余一律保留：
- `test_defaults_cover_every_role`（第 6-7 行，依赖即将消失的 `DEFAULT_ROLES` 符号；下面有同名新版）
- `test_critic_defaults_to_independent_context`（第 15-17 行，纯 change detector：断言两个常量值，没有任何行为依赖它，只会在有意修改时失败，永远抓不到 bug）
- `test_planner_and_critic_use_different_models`（第 10-12 行，新语义下比的是 `"opus" != "sonnet"`，**会继续变绿但不再测 §8.2 声明的约束**）

然后追加：

```python
from pathlib import Path

import pytest

from tripplan.llm.config import (
    LlmConfig,
    ModelSpec,
    Role,
    RoleConfig,
    expand,
    load_config,
)
from tripplan.llm.errors import ConfigError


@pytest.fixture(autouse=True)
def _sealed_env(monkeypatch):
    """配置解析读环境变量，测试必须不依赖开发机上碰巧 export 过什么。"""
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "TRIP_GPT_MODEL"):
        monkeypatch.delenv(var, raising=False)


def _write(tmp_path, text):
    p = tmp_path / "cfg.toml"
    p.write_text(text, encoding="utf-8")
    return p


# ---------- 开箱默认 ----------


def test_defaults_cover_every_role():
    cfg = load_config(None)
    assert set(cfg.roles) == set(Role)


def test_defaults_match_current_behaviour():
    """不给配置文件时必须与现状一致，否则这是破坏性变更。"""
    cfg = load_config(None)
    expected = {
        Role.PLANNER: ("anthropic", "claude-opus-5", 16000),
        Role.CRITIC: ("anthropic", "claude-sonnet-5", 4000),
        Role.ANGLE: ("anthropic", "claude-sonnet-5", 2000),
        Role.CLASSIFIER: ("anthropic", "claude-haiku-4-5", 1000),
    }
    for role, (provider, name, max_tokens) in expected.items():
        spec = cfg.models[cfg.roles[role].model]
        assert (spec.provider, spec.name, cfg.roles[role].max_tokens) == (
            provider,
            name,
            max_tokens,
        )


def test_default_key_is_empty_so_sdk_can_resolve_credentials():
    """内置默认用 ${ANTHROPIC_API_KEY:-}，展开成空串交给 SDK 自己解析——
    ANTHROPIC_API_KEY 不是唯一凭据来源（还有 AUTH_TOKEN / profile / WIF）。"""
    cfg = load_config(None)
    assert cfg.models[cfg.roles[Role.PLANNER].model].key == ""


# ---------- 合并语义（§4.1） ----------


def test_only_named_roles_are_overridden(tmp_path):
    """承接旧的 test_load_config_overrides_only_named_roles：
    config.py 今天就是 dict(DEFAULT_ROLES) 再按名 replace，改成整份替换
    是无谓的破坏性变更。"""
    path = _write(
        tmp_path,
        '[models.gpt5]\nprovider = "openai"\nname = "gpt-5"\nkey = "k"\n'
        '[roles.critic]\nmodel = "gpt5"\n',
    )
    cfg = load_config(path)
    assert cfg.models[cfg.roles[Role.CRITIC].model].provider == "openai"
    assert cfg.models[cfg.roles[Role.PLANNER].model].name == "claude-opus-5"
    assert cfg.roles[Role.PLANNER].max_tokens == 16000


def test_roles_section_may_be_absent_entirely(tmp_path):
    path = _write(tmp_path, '[models.unused]\nprovider = "openai"\nname = "x"\n')
    cfg = load_config(path)
    assert set(cfg.roles) == set(Role)


def test_max_tokens_is_optional(tmp_path):
    """replace() 只覆盖给出的字段；把 max_tokens 标成必填是破坏性变更。"""
    path = _write(tmp_path, '[roles.planner]\nmax_tokens = 999\n')
    cfg = load_config(path)
    assert cfg.roles[Role.PLANNER].max_tokens == 999
    assert cfg.roles[Role.CRITIC].max_tokens == 4000


def test_same_named_model_is_replaced_wholesale(tmp_path):
    """不做字段级合并——provider="openai" 却继承 name="claude-opus-5"
    只会制造困惑。"""
    path = _write(
        tmp_path,
        '[models.opus]\nprovider = "openai"\nname = "gpt-5"\nkey = "k"\n',
    )
    cfg = load_config(path)
    spec = cfg.models[cfg.roles[Role.PLANNER].model]
    assert (spec.provider, spec.name) == ("openai", "gpt-5")


# ---------- 引用解析先于展开（§5 step 4） ----------


def test_unknown_model_ref_reports_the_literal_the_user_wrote(tmp_path):
    path = _write(tmp_path, '[roles.critic]\nmodel = "claude-sonnet-5"\n')
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert "未知的 model 引用" in str(exc.value)
    assert "claude-sonnet-5" in str(exc.value)


def test_unreferenced_model_does_not_block_startup(tmp_path, monkeypatch):
    """配置里囤着的备用 model 引用了未设的变量、或写了非法 provider，
    都不该阻塞启动——它压根没被任何角色用到。"""
    monkeypatch.delenv("NEVER_SET", raising=False)
    path = _write(
        tmp_path,
        '[models.spare]\nprovider = "openai"\nname = "x"\n'
        'key = "${NEVER_SET}"\n',
    )
    cfg = load_config(path)  # 不抛
    assert "spare" not in cfg.models


# ---------- 结构与类型校验（§5 step 3） ----------


@pytest.mark.parametrize(
    "toml_text, needle",
    [
        ('models = "x"\n', "models"),
        ('[models]\ngpt5 = "x"\n[roles.critic]\nmodel = "gpt5"\n', "gpt5"),
        ('[models.m]\nname = "x"\n[roles.critic]\nmodel = "m"\n', "provider"),
        ('[models.m]\nprovider = "openai"\n[roles.critic]\nmodel = "m"\n', "name"),
        (
            '[models.m]\nprovider = "openai"\nname = 5\n'
            '[roles.critic]\nmodel = "m"\n',
            "name",
        ),
        ('[roles.planner]\nmax_tokens = "16000"\n', "max_tokens"),
        ('[roles.planner]\nmodel = ["a"]\n', "model"),
        ('[roles.critic]\nallow_same_model = "false"\n', "allow_same_model"),
        ('[roles.planner]\nallow_same_model = true\n', "allow_same_model"),
        ('[roles.nosuchrole]\nmax_tokens = 1\n', "nosuchrole"),
        ('[roles.planner]\nmdoel = "x"\n', "mdoel"),
        (
            '[models.m]\nprovider = "nope"\nname = "x"\n'
            '[roles.critic]\nmodel = "m"\n',
            "nope",
        ),
    ],
)
def test_structural_errors_become_config_error(tmp_path, toml_text, needle):
    path = _write(tmp_path, toml_text)
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert needle in str(exc.value)


@pytest.mark.parametrize(
    "make_path",
    [
        lambda tmp: tmp / "nope.toml",  # FileNotFoundError
        lambda tmp: tmp,  # IsADirectoryError
    ],
)
def test_unreadable_file_becomes_config_error(tmp_path, make_path):
    with pytest.raises(ConfigError):
        load_config(make_path(tmp_path))


def test_broken_toml_becomes_config_error(tmp_path):
    path = _write(tmp_path, "[models.x\n")
    with pytest.raises(ConfigError):
        load_config(path)


def test_non_utf8_file_becomes_config_error(tmp_path):
    path = tmp_path / "cfg.toml"
    path.write_bytes(b'[models.m]\nname = "\xff\xfe"\n')
    with pytest.raises(ConfigError):
        load_config(path)


# ---------- critic ≠ planner（§8.2） ----------


def test_critic_and_planner_may_not_share_provider_and_name(tmp_path):
    """判据是展开后的 (provider, name)，不是 model 引用名——用户复制一个
    [models.*] 块通常是为了换 base_url/key，不会意识到自己关掉了这道阀。"""
    path = _write(
        tmp_path,
        '[models.a]\nprovider = "openai"\nname = "gpt-5"\nkey = "k"\n'
        '[models.b]\nprovider = "openai"\nname = "gpt-5"\n'
        'base_url = "https://other/v1"\nkey = "k"\n'
        '[roles.planner]\nmodel = "a"\n[roles.critic]\nmodel = "b"\n',
    )
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert "gpt-5" in str(exc.value)


def test_allow_same_model_opens_the_gate(tmp_path):
    path = _write(
        tmp_path,
        '[models.a]\nprovider = "openai"\nname = "gpt-5"\nkey = "k"\n'
        '[roles.planner]\nmodel = "a"\n'
        '[roles.critic]\nmodel = "a"\nallow_same_model = true\n',
    )
    cfg = load_config(path)  # 不抛
    assert cfg.roles[Role.CRITIC].allow_same_model is True


def test_same_model_error_shows_both_source_and_expanded(tmp_path, monkeypatch):
    """name 可以是 ${TRIP_GPT_MODEL:-gpt-5}，同一份配置在不同机器上会一会儿
    触发一会儿不触发。报错必须同时给原文与展开值，否则用户对不上。"""
    monkeypatch.delenv("TRIP_GPT_MODEL", raising=False)
    path = _write(
        tmp_path,
        '[models.a]\nprovider = "openai"\nname = "gpt-5"\nkey = "k"\n'
        '[models.b]\nprovider = "openai"\n'
        'name = "${TRIP_GPT_MODEL:-gpt-5}"\nkey = "k"\n'
        '[roles.planner]\nmodel = "a"\n[roles.critic]\nmodel = "b"\n',
    )
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    message = str(exc.value)
    assert "${TRIP_GPT_MODEL:-gpt-5}" in message
    assert "gpt-5" in message


# ---------- 展开作用域（§5） ----------


def test_expansion_applies_to_models_string_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIP_GPT_MODEL", "gpt-5-mini")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real")
    path = _write(
        tmp_path,
        '[models.gpt5]\nprovider = "openai"\n'
        'name = "${TRIP_GPT_MODEL:-gpt-5}"\n'
        'base_url = "${TRIP_GW:-https://api.openai.com/v1}"\n'
        'key = "${OPENAI_API_KEY}"\n'
        '[roles.critic]\nmodel = "gpt5"\n',
    )
    spec = load_config(path).models["gpt5"]
    assert spec.name == "gpt-5-mini"
    assert spec.base_url == "https://api.openai.com/v1"
    assert spec.key == "sk-real"


def test_sources_keep_the_literal_text(tmp_path, monkeypatch):
    """name_source / key_source 不是冗余：加载期 backend 构造时抛出的凭据
    错误要报出用户写的那个变量名，那时 load_config 已经返回了。"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real")
    path = _write(
        tmp_path,
        '[models.gpt5]\nprovider = "openai"\nname = "gpt-5"\n'
        'key = "${OPENAI_API_KEY}"\n[roles.critic]\nmodel = "gpt5"\n',
    )
    spec = load_config(path).models["gpt5"]
    assert spec.key_source == "${OPENAI_API_KEY}"
    assert spec.key == "sk-real"


def test_provider_is_not_expanded(tmp_path, monkeypatch):
    """provider 是纯内部枚举，不出现在对外请求里——展开只会让错误信息
    里的值与用户在文件里看到的 ${X} 对不上。"""
    monkeypatch.setenv("TRIP_P", "openai")
    path = _write(
        tmp_path,
        '[models.m]\nprovider = "${TRIP_P}"\nname = "x"\n'
        '[roles.critic]\nmodel = "m"\n',
    )
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert "${TRIP_P}" in str(exc.value)
```

- [ ] **Step 2: 跑测试确认失败**

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/llm/test_config.py -q
```
预期：大量 `ImportError` / `AttributeError`（`ModelSpec` / `LlmConfig` / `load_config` 签名不符）

- [ ] **Step 3: 重写 `src/tripplan/llm/config.py`**

```python
"""按角色配置模型，而不是全局一个。

配置分两层：先定义一组 model（各带 provider / name / base_url / key），
角色再引用 model 名。这样同一个 model 能被多个角色复用，而 max_tokens
按角色定——planner 要 16000、angle 只要 2000。
"""

import os
import re
import tomllib
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path

from tripplan.llm.errors import ConfigError


class Role(Enum):
    PLANNER = "planner"
    CRITIC = "critic"
    ANGLE = "angle"
    CLASSIFIER = "classifier"


PROVIDERS = ("anthropic", "openai")

#: 只认两个形状：${NAME} 与 ${NAME:-默认值}。默认值取到**第一个** } 为止，
#: 不支持嵌套、不提供转义——`${A:-${B}}` 会取到 `${B` 为止。这些写法在
#: key / url / 模型名里不存在，但规则必须写死，否则每个实现者会发明一套。
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand(value: str, where: str) -> str:
    """展开 ${VAR} 与 ${VAR:-default}。见 Task 2 的 docstring。"""

    def _sub(m: re.Match) -> str:
        name, default = m.group(1), m.group(2)
        env = os.environ.get(name)
        if env is not None:
            return env
        if default is not None:
            return default
        raise ConfigError(
            f"{where} 引用了未设置的环境变量 {name}。"
            f"请先 export 它，或在配置里写 ${{{name}:-默认值}} 给一个默认值。"
        )

    return _VAR.sub(_sub, value)


def is_var_reference(text: str) -> bool:
    """整段文本是否就是一个 ${...} 引用。

    只有为真时 `key_source` 才可以进日志或错误消息——用户写字面量 key 时
    `key_source` 就是明文密钥本身（见 ModelSpec.key_source 的注释）。
    """
    return _VAR.fullmatch(text) is not None


@dataclass(frozen=True)
class ModelSpec:
    provider: str  # "anthropic" | "openai"
    name: str  # 展开后的真实模型 ID
    base_url: str  # 展开后；"" = SDK 默认端点
    key: str  # 展开后；"" = 交给 SDK 自行解析凭据
    #: 用户在 TOML 里写的原文，仅用于错误消息。展开前后相同时与上面一致
    #: ——注意 key_source 因此可能就是**明文密钥本身**（用户写字面量时），
    #: 输出前必须先用 is_var_reference() 判形态，不可无条件取用。
    name_source: str
    key_source: str


@dataclass(frozen=True)
class RoleConfig:
    model: str  # ModelSpec 的键名
    max_tokens: int
    allow_same_model: bool = False  # 仅 critic 有意义


@dataclass(frozen=True)
class LlmConfig:
    models: dict[str, ModelSpec]
    roles: dict[Role, RoleConfig]


#: 内置默认。与现状行为一致——不给配置文件时不构成破坏性变更。
#: key 用 ${ANTHROPIC_API_KEY:-} 而不是 ${ANTHROPIC_API_KEY}：展开成空串后
#: backend 会把它归一成 None 交给 SDK，让 SDK 自己走完 API_KEY → AUTH_TOKEN
#: → profile → WIF 的解析链。写成无默认值的形式会把只有 AUTH_TOKEN 或
#: profile 的用户挡在门外。
_DEFAULT_MODELS: dict[str, dict] = {
    "opus": {
        "provider": "anthropic",
        "name": "claude-opus-5",
        "key": "${ANTHROPIC_API_KEY:-}",
    },
    "sonnet": {
        "provider": "anthropic",
        "name": "claude-sonnet-5",
        "key": "${ANTHROPIC_API_KEY:-}",
    },
    "haiku": {
        "provider": "anthropic",
        "name": "claude-haiku-4-5",
        "key": "${ANTHROPIC_API_KEY:-}",
    },
}

_DEFAULT_ROLES: dict[str, dict] = {
    "planner": {"model": "opus", "max_tokens": 16000},
    "critic": {"model": "sonnet", "max_tokens": 4000},
    "angle": {"model": "sonnet", "max_tokens": 2000},
    "classifier": {"model": "haiku", "max_tokens": 1000},
}

_MODEL_FIELDS = ("provider", "name", "base_url", "key")
_ROLE_FIELDS = ("model", "max_tokens", "allow_same_model")


def _read_toml(path: Path) -> dict:
    """step 1。捕两族异常：OSError（文件读不到）与 ValueError（内容解不开）。

    只点名 FileNotFoundError 与 TOMLDecodeError 会漏掉 IsADirectoryError /
    PermissionError / UnicodeDecodeError，它们会一路逃成裸 traceback。
    """
    try:
        return tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as e:
        raise ConfigError(f"读不到配置文件 {path}：{e}") from e
    except ValueError as e:  # TOMLDecodeError 与 UnicodeDecodeError 都是它的子类
        raise ConfigError(f"配置文件 {path} 解析失败：{e}") from e


def _merged_section(raw, key: str, defaults: dict) -> dict:
    """step 2。顶层段必须是 table；用户同名条目整条替换内置条目。"""
    section = raw.get(key)
    if section is None:
        return {k: dict(v) for k, v in defaults.items()}
    if not isinstance(section, dict):
        raise ConfigError(f"配置里的 {key} 必须是一个表（[{key}.xxx]），实际是 {type(section).__name__}")
    merged = {k: dict(v) for k, v in defaults.items()}
    for name, body in section.items():
        if not isinstance(body, dict):
            raise ConfigError(f"{key}.{name} 必须是一个表（[{key}.{name}]），实际是 {type(body).__name__}")
        merged[name] = dict(body)
    return merged


def _check_models(models_raw: dict) -> None:
    """step 3 的 models 部分。显式白名单——不能用 **overrides 那套，
    ModelSpec 带 name_source / key_source 两个内部字段，用户不该写得进去。"""
    for name, body in models_raw.items():
        for field in body:
            if field not in _MODEL_FIELDS:
                raise ConfigError(
                    f"models.{name} 含未知字段：{field}"
                    f"（可用：{', '.join(_MODEL_FIELDS)}）"
                )
        for required in ("provider", "name"):
            if required not in body:
                raise ConfigError(f"models.{name} 缺少必填字段 {required}")
        for field, value in body.items():
            if not isinstance(value, str):
                raise ConfigError(
                    f"models.{name}.{field} 必须是字符串，实际是 {type(value).__name__}"
                )


def _build_roles(roles_raw: dict) -> dict[Role, RoleConfig]:
    """step 3 的 roles 部分 + 构造。"""
    out: dict[Role, RoleConfig] = {}
    for name, body in roles_raw.items():
        try:
            role = Role(name)
        except ValueError as e:
            raise ConfigError(f"未知角色：{name}") from e
        for field in body:
            if field not in _ROLE_FIELDS:
                raise ConfigError(f"roles.{name} 含未知字段：{field}")
        if "allow_same_model" in body and role is not Role.CRITIC:
            raise ConfigError(
                f"allow_same_model 只在 [roles.critic] 下有意义，"
                f"不能写在 roles.{name} 下"
            )
        if "model" in body and not isinstance(body["model"], str):
            raise ConfigError(
                f"roles.{name}.model 必须是字符串，实际是 {type(body['model']).__name__}"
            )
        # bool 是 int 的子类，必须显式排除——否则 max_tokens = true 会被当成 1
        if "max_tokens" in body and (
            isinstance(body["max_tokens"], bool)
            or not isinstance(body["max_tokens"], int)
        ):
            raise ConfigError(
                f"roles.{name}.max_tokens 必须是整数，"
                f"实际是 {type(body['max_tokens']).__name__}"
            )
        if "allow_same_model" in body and not isinstance(
            body["allow_same_model"], bool
        ):
            raise ConfigError(
                f"roles.{name}.allow_same_model 必须是布尔值（true / false，不加引号），"
                f"实际是 {type(body['allow_same_model']).__name__}"
            )
        out[role] = RoleConfig(**body)
    return out


def _build_spec(ref: str, body: dict) -> ModelSpec:
    """step 6。provider 原样比较（不展开），三个字符串字段展开。"""
    provider = body["provider"]
    if provider not in PROVIDERS:
        raise ConfigError(
            f"models.{ref}.provider 不支持：{provider}"
            f"（只能是 {' 或 '.join(PROVIDERS)}）"
        )
    name_source = body["name"]
    key_source = body.get("key", "")
    return ModelSpec(
        provider=provider,
        name=expand(name_source, f"models.{ref}.name"),
        base_url=expand(body.get("base_url", ""), f"models.{ref}.base_url"),
        key=expand(key_source, f"models.{ref}.key"),
        name_source=name_source,
        key_source=key_source,
    )


def _check_critic(roles: dict[Role, RoleConfig], models: dict[str, ModelSpec]) -> None:
    """step 7。判据是展开后的 (provider, name)，base_url 不参与——同一个
    模型放在不同网关后面，盲点不变。

    只比 model 引用名是不够的：用户复制一个 [models.*] 块通常是为了换
    base_url / 换 key（多网关、灰度、区域），不会意识到自己顺手关掉了这道
    安全阀。能被复制粘贴无声关闭的安全阀只提供虚假的保障感。
    """
    critic, planner = roles[Role.CRITIC], roles[Role.PLANNER]
    if critic.allow_same_model:
        return
    c, p = models[critic.model], models[planner.model]
    if (c.provider, c.name) != (p.provider, p.name):
        return
    raise ConfigError(
        f"critic 与 planner 指向同一个模型：{c.provider}/{c.name}"
        f"（critic 的 name 原文为 {c.name_source!r}，planner 的为 {p.name_source!r}）。"
        "同源自审会放过同一个盲点。若确实要这样，在 [roles.critic] 下写 "
        "allow_same_model = true。"
    )


def load_config(path: Path | None = None) -> LlmConfig:
    """按 §5 的七步加载。顺序不能变——引用解析必须先于展开，这样"未知的
    model 引用"报错里出现的永远是用户写的原文。"""
    raw = {} if path is None else _read_toml(path)  # step 1
    models_raw = _merged_section(raw, "models", _DEFAULT_MODELS)  # step 2
    roles_raw = _merged_section(raw, "roles", _DEFAULT_ROLES)
    _check_models(models_raw)  # step 3
    roles = _build_roles(roles_raw)

    for role, rc in roles.items():  # step 4：先于展开
        if rc.model not in models_raw:
            raise ConfigError(
                f"未知的 model 引用：{rc.model}（被角色 {role.value} 使用）。"
                f"请先用 [models.{rc.model}] 定义它。"
                "注意 roles.*.model 现在填的是 model 的引用名，不是真实模型 ID。"
            )

    referenced = {rc.model for rc in roles.values()}  # step 5
    # 只对被引用到的 model 做 provider 校验与展开：配置里囤着的备用 model
    # 若引用了未设置的变量或写了非法 provider，不应阻塞启动。
    models = {ref: _build_spec(ref, models_raw[ref]) for ref in referenced}  # step 6
    _check_critic(roles, models)  # step 7
    return LlmConfig(models=models, roles=roles)
```

- [ ] **Step 4: 跑测试确认通过**

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/llm/test_config.py -q
```
预期：全部 passed

- [ ] **Step 4b: 改写 `tests/test_slot.py:259-284`**

删除 `DEFAULT_ROLES` 会打断 `test_slot.py:268` 的导入，所以这条测试在本任务一并改写。整条替换为：

```python
def test_transport_error_surfaces_as_failed_not_propagating(mk):
    """run_slot 必须把 ProviderError 收成 FAILED，不能让它炸穿这条候选线
    （以及已经跑完的另外两条）。

    「厂商异常转成 ProviderError」那一半不在这里测——它是 backend 的职责，
    由 tests/llm/test_anthropic_backend.py 直接覆盖（APIError 与非 APIError
    的厂商异常各一条）。这里只测 run_slot 这一层的契约，所以用最小 stub
    而不是真实 backend：一条测试只测一件事。
    """
    from tripplan.providers.base import ProviderError

    class _RaisingClient:
        def chat(self, role, system, messages, tools):
            raise ProviderError("连接失败")

    deps = Deps(client=_RaisingClient(), provider=FakeProvider(pois={}))
    slot = run_slot(angle=ANGLE, seed=None, reqs=_reqs(), tz=TZ, deps=deps)

    assert slot.status is SlotStatus.FAILED
    assert slot.itinerary is None
    assert "外部依赖失败" in slot.detail
```

顺手删掉该文件里因此不再使用的 `import anthropic` / `import httpx`（如果没有别的测试用到）。

- [ ] **Step 5: 跑 Task 1 留下的两条**

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_cli.py -q -k config_error_from
```
预期：仍会失败——`cli.py` 还在用旧的 `AnthropicClient(load_config(...))`，Task 8 才接线。**这是预期的，不要在这里硬修。**

- [ ] **Step 6: 格式化并提交**

```bash
.venv/bin/black src tests
git add -A
git commit -m "feat(llm): 配置改为两层——先定义 model，角色再引用

删除死字段 independent_context（全仓零生产读取）及其 change-detector
测试。critic≠planner 的判据从模型 ID 字面量改为展开后的 (provider, name)。"
```

---

## Task 4: Anthropic backend

**Files:**
- Create: `src/tripplan/llm/backends/__init__.py`（空）、`src/tripplan/llm/backends/anthropic.py`
- Test: `tests/llm/test_anthropic_backend.py`（新建）
- **不碰**：`src/tripplan/llm/client.py`、`tests/llm/test_client.py`、`tests/test_slot.py` —— 三者都已在 Task 3 处理完毕（见下面 Step 4/5）

**Interfaces:**
- Consumes: `ModelSpec`、`ConfigError`、`MissingCredential`、`LlmResponse` / `Usage` / `ToolCall`
- Produces: `AnthropicBackend(spec: ModelSpec)`，方法 `chat(role: Role, model_ref: str, system: str, messages: list, tools: list | None, max_tokens: int) -> LlmResponse`

**注意 `tests/test_slot.py:259-284`**（`test_transport_error_surfaces_as_failed_not_propagating`）同时依赖三样即将全部变化的东西：`from tripplan.llm.client import AnthropicClient` 的导入路径、`AnthropicClient(DEFAULT_ROLES, api_key=...)` 的构造签名、以及 `_client` 私有属性名。必须在本任务一并改写。

- [ ] **Step 1: 写失败测试**

创建 `tests/llm/test_anthropic_backend.py`：

```python
"""Anthropic 适配器。全程不触网——构造不发请求，chat 用 patch 挡住。"""

import inspect
from unittest.mock import MagicMock, patch

import anthropic
import httpx
import pytest
from anthropic.resources.messages import Messages

from tripplan.llm.backends.anthropic import AnthropicBackend
from tripplan.llm.config import ModelSpec, Role
from tripplan.llm.errors import ConfigError, MissingCredential
from tripplan.providers.base import ProviderError


def _spec(**over):
    base = dict(
        provider="anthropic",
        name="claude-opus-5",
        base_url="",
        key="sk-test",
        name_source="claude-opus-5",
        key_source="${ANTHROPIC_API_KEY}",
    )
    base.update(over)
    return ModelSpec(**base)


@pytest.fixture(autouse=True)
def _sealed(monkeypatch, tmp_path):
    """凭据解析链必须被密封，否则测试结果随开发机而变。

    HOME 要指向空目录：SDK 的 _config_dir() 在 ANTHROPIC_CONFIG_DIR 未设时
    回落到 ~/.config/anthropic/。而 ANTHROPIC_CONFIG_DIR **不能** setenv 到
    空目录——那会把 profile 解析升级为「显式选择」，构造期直接抛
    CredentialsError（实测，_chain.py:119-129 的注释写明了这个语义）。
    """
    for var in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_PROFILE",
        "ANTHROPIC_CONFIG_DIR",
        "ANTHROPIC_IDENTITY_TOKEN",
        "ANTHROPIC_IDENTITY_TOKEN_FILE",
        "ANTHROPIC_FEDERATION_RULE_ID",
        "ANTHROPIC_ORGANIZATION_ID",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def _resp(*, text="{}", tool_uses=(), stop_reason="end_turn"):
    blocks = []
    if text:
        b = MagicMock()
        b.type = "text"
        b.text = text
        blocks.append(b)
    for tid, tname, tinput in tool_uses:
        b = MagicMock()
        b.type = "tool_use"
        b.id, b.name, b.input = tid, tname, tinput
        blocks.append(b)
    r = MagicMock()
    r.content = blocks
    r.stop_reason = stop_reason
    r.usage.input_tokens, r.usage.output_tokens = 10, 5
    return r


# ---------- 凭据（§6） ----------


def test_empty_key_is_handed_to_sdk_as_none():
    """规则一：空串必须归一成 None。anthropic SDK 用 `api_key is not None`
    判断"是否给了显式凭据"，`"" is not None` 为真，于是空串会被当成显式
    凭据、整条环境解析链（API_KEY → AUTH_TOKEN → profile → WIF）被跳过。"""
    with patch("anthropic.Anthropic") as MockAnthropic:
        MockAnthropic.return_value.api_key = "resolved-by-sdk"
        AnthropicBackend(_spec(key=""))
        assert MockAnthropic.call_args.kwargs["api_key"] is None


def test_empty_base_url_is_handed_to_sdk_as_none():
    with patch("anthropic.Anthropic") as MockAnthropic:
        MockAnthropic.return_value.api_key = "k"
        AnthropicBackend(_spec(base_url=""))
        assert MockAnthropic.call_args.kwargs["base_url"] is None


def test_no_credential_anywhere_raises_missing_credential():
    """规则二：判据是 SDK 是否解析出了任何一种凭据，不是 auth_headers。
    这里打真实的 anthropic.Anthropic（构造不发请求），因为 MagicMock 的
    api_key 恒为 truthy，判据永远为假——那样断言的是 mock 不是 SDK。"""
    with pytest.raises(MissingCredential) as exc:
        AnthropicBackend(_spec(key=""), role=Role.PLANNER, model_ref="opus")
    message = str(exc.value)
    assert "planner" in message
    assert "opus" in message
    assert "ANTHROPIC_API_KEY" in message
    assert "--dry-run" in message


def test_auth_token_alone_is_accepted(monkeypatch):
    """ANTHROPIC_API_KEY 不是唯一凭据来源。只配 AUTH_TOKEN 的用户必须能跑。"""
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "bearer-abc")
    AnthropicBackend(_spec(key=""))  # 不抛


def test_literal_key_is_never_echoed_in_message():
    """key_source 在用户写字面量时就是明文密钥本身。错误消息里只能出现
    provider 的固定映射变量名，不能把它原样吐出来。"""
    with pytest.raises(MissingCredential) as exc:
        AnthropicBackend(
            _spec(key="", key_source="sk-ant-SUPERSECRET"),
            role=Role.PLANNER,
            model_ref="opus",
        )
    assert "SUPERSECRET" not in str(exc.value)
    assert "ANTHROPIC_API_KEY" in str(exc.value)


def test_var_form_key_source_is_reported(monkeypatch):
    with pytest.raises(MissingCredential) as exc:
        AnthropicBackend(
            _spec(key="", key_source="${MY_OWN_VAR:-}"),
            role=Role.PLANNER,
            model_ref="opus",
        )
    assert "MY_OWN_VAR" in str(exc.value)


def test_construction_failure_becomes_config_error(monkeypatch, tmp_path):
    """坏 profile 会让构造期抛 CredentialsError（不是 APIError 子类）。
    它必须收成 ConfigError，并套用 §6.2 的消息契约——不能透传 SDK 英文原文。"""
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(tmp_path / "nope"))
    with pytest.raises(ConfigError) as exc:
        AnthropicBackend(_spec(key=""), role=Role.PLANNER, model_ref="opus")
    message = str(exc.value)
    assert "planner" in message
    assert "opus" in message
    assert "--dry-run" in message


# ---------- 请求形状 ----------


def test_tools_omitted_entirely_when_none():
    """四个角色里三个传 tools=None。不能发 "tools": null。"""
    with patch("anthropic.Anthropic") as MockAnthropic:
        inst = MockAnthropic.return_value
        inst.api_key = "k"
        inst.messages.create.return_value = _resp()
        AnthropicBackend(_spec()).chat(
            role=Role.CRITIC,
            model_ref="sonnet",
            system="sys",
            messages=[],
            tools=None,
            max_tokens=4000,
        )
        assert "tools" not in inst.messages.create.call_args.kwargs


def test_request_kwargs_match_the_real_sdk_signature():
    """防"关键字名拼错但 MagicMock 照样绿"——唯一的防线。"""
    with patch("anthropic.Anthropic") as MockAnthropic:
        inst = MockAnthropic.return_value
        inst.api_key = "k"
        inst.messages.create.return_value = _resp()
        AnthropicBackend(_spec()).chat(
            role=Role.PLANNER,
            model_ref="opus",
            system="sys",
            messages=[{"role": "user", "content": "hi"}],
            tools=[{"name": "t", "description": "d", "input_schema": {}}],
            max_tokens=16000,
        )
        kwargs = inst.messages.create.call_args.kwargs
        inspect.signature(Messages.create).bind(None, **kwargs)
        assert kwargs["max_tokens"] == 16000
        assert kwargs["model"] == "claude-opus-5"


# ---------- 响应归一化（§10.0） ----------


@pytest.mark.parametrize(
    "stop_reason, expected",
    [
        ("end_turn", "end_turn"),
        ("stop_sequence", "end_turn"),
        ("pause_turn", "end_turn"),  # 本项目不用服务端工具，此值不可达
        ("max_tokens", "max_tokens"),
        (None, "end_turn"),  # Message.stop_reason 的标注是 Optional
    ],
)
def test_stop_reason_normalisation(stop_reason, expected):
    with patch("anthropic.Anthropic") as MockAnthropic:
        inst = MockAnthropic.return_value
        inst.api_key = "k"
        inst.messages.create.return_value = _resp(stop_reason=stop_reason)
        out = AnthropicBackend(_spec()).chat(
            role=Role.PLANNER,
            model_ref="opus",
            system="s",
            messages=[],
            tools=None,
            max_tokens=100,
        )
        assert out.stop_reason == expected


def test_tool_use_blocks_make_it_a_tool_round_even_when_truncated():
    """有工具调用就是工具轮，stop_reason 只在没有工具调用时才决定分支。
    这是 OpenAI 那条"判工具轮看 tool_calls 非空"在 Anthropic 侧的对称情形。"""
    with patch("anthropic.Anthropic") as MockAnthropic:
        inst = MockAnthropic.return_value
        inst.api_key = "k"
        inst.messages.create.return_value = _resp(
            text="", tool_uses=[("id1", "search_poi", {"query": "芜湖"})],
            stop_reason="max_tokens",
        )
        out = AnthropicBackend(_spec()).chat(
            role=Role.PLANNER,
            model_ref="opus",
            system="s",
            messages=[],
            tools=None,
            max_tokens=100,
        )
        assert out.stop_reason == "tool_use"
        assert out.tool_calls[0].name == "search_poi"


@pytest.mark.parametrize(
    "stop_reason, needle",
    [("refusal", "拒绝"), ("model_context_window_exceeded", "上下文窗口")],
)
def test_hard_stop_reasons_become_provider_error(stop_reason, needle):
    """不归一的话会去解析空 text → SchemaError → 修复轮（而修复轮把消息
    再加长）→ LimitExceeded("schema 修复 2 次仍失败")，诊断与真因无关，
    且会假触发 §13 的推理预算提示。"""
    with patch("anthropic.Anthropic") as MockAnthropic:
        inst = MockAnthropic.return_value
        inst.api_key = "k"
        inst.messages.create.return_value = _resp(text="", stop_reason=stop_reason)
        with pytest.raises(ProviderError) as exc:
            AnthropicBackend(_spec()).chat(
                role=Role.PLANNER,
                model_ref="opus",
                system="s",
                messages=[],
                tools=None,
                max_tokens=100,
            )
        assert needle in str(exc.value)


# ---------- 请求期错误映射（铁律） ----------


def test_api_error_becomes_provider_error():
    request = httpx.Request("POST", "https://api.anthropic.com")
    with patch("anthropic.Anthropic") as MockAnthropic:
        inst = MockAnthropic.return_value
        inst.api_key = "k"
        inst.messages.create.side_effect = anthropic.APIConnectionError(
            request=request
        )
        with pytest.raises(ProviderError):
            AnthropicBackend(_spec()).chat(
                role=Role.PLANNER,
                model_ref="opus",
                system="s",
                messages=[],
                tools=None,
                max_tokens=100,
            )


def test_non_api_error_vendor_exception_also_becomes_provider_error():
    """铁律"漏捕"一侧的唯一防线。

    CredentialsError / RetryableError / IdentityTokenFileError 都不是
    APIError 子类，而它们会在**请求期**刷新令牌时抛出（AccessTokenAuth
    的 auth_flow 调 TokenCache.get_token）；_base_client.py:1296-1302 明确
    把 AnthropicError 原样穿出、不包装成 APIConnectionError。

    写成 `except APIError` 的实现会让这个异常绕过 ProviderError，落到
    orchestrator._safe_slot，而它把 itinerary 硬编码成 None——长跑 planner
    中途令牌过期，已经生成好的行程当场丢失。
    """
    with patch("anthropic.Anthropic") as MockAnthropic:
        inst = MockAnthropic.return_value
        inst.api_key = "k"
        inst.messages.create.side_effect = anthropic.CredentialsError("token 过期")
        with pytest.raises(ProviderError):
            AnthropicBackend(_spec()).chat(
                role=Role.PLANNER,
                model_ref="opus",
                system="s",
                messages=[],
                tools=None,
                max_tokens=100,
            )


def test_authentication_error_keeps_provider_error_type_but_gains_message():
    """401 改消息**不改类型**。转成 MissingCredential 会让它绕过 run_slot
    的 except（slot.py:76/86 只捕 LimitExceeded 与 ProviderError），落到
    _safe_slot 把已生成的行程丢掉，然后照旧打印"请先修改需求后重试"。"""
    request = httpx.Request("POST", "https://api.anthropic.com")
    response = httpx.Response(401, request=request)
    with patch("anthropic.Anthropic") as MockAnthropic:
        inst = MockAnthropic.return_value
        inst.api_key = "k"
        inst.messages.create.side_effect = anthropic.AuthenticationError(
            "unauthorized", response=response, body=None
        )
        with pytest.raises(ProviderError) as exc:
            AnthropicBackend(_spec()).chat(
                role=Role.CRITIC,
                model_ref="sonnet",
                system="s",
                messages=[],
                tools=None,
                max_tokens=100,
            )
        message = str(exc.value)
        assert "401" in message
        assert "critic" in message
        assert "sonnet" in message
        assert "ANTHROPIC_API_KEY" in message
```

- [ ] **Step 2: 跑测试确认失败**

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/llm/test_anthropic_backend.py -q
```
预期：`ModuleNotFoundError: No module named 'tripplan.llm.backends'`

- [ ] **Step 3: 实现**

创建空的 `src/tripplan/llm/backends/__init__.py`，然后 `src/tripplan/llm/backends/anthropic.py`：

```python
"""Anthropic 适配器。

凭据处理见设计文档 §6：原方案查 client.auth_headers 两个方向都错——空串
key 会让它返回 {'X-Api-Key': ''}（非空，假阴性放行，然后请求期抛裸
TypeError），而 OAuth profile / WIF 的 auth_headers 恒为空（假阳性，把能
正常工作的用户判成缺凭据）。正确判据是「SDK 是否解析出了任何一种凭据」。
"""

import json
import logging

from tripplan.llm.client import LlmResponse, ToolCall, Usage
from tripplan.llm.config import ModelSpec, Role, is_var_reference
from tripplan.llm.errors import ConfigError, MissingCredential
from tripplan.providers.base import ProviderError

logger = logging.getLogger(__name__)

#: 没有可用凭据时，提示用户去 export 哪个变量。
ENV_HINT = "ANTHROPIC_API_KEY"


def _where(role: Role | None, model_ref: str | None) -> str:
    if role is None or model_ref is None:
        return "某个 model"
    return f"角色 {role.value} 使用的 model「{model_ref}」"


def _var_hint(spec: ModelSpec) -> str:
    """只有 key_source 确实是个 ${...} 引用时才报它——用户写字面量 key 时
    key_source 就是明文密钥本身，原样吐出去就是泄漏。"""
    if is_var_reference(spec.key_source):
        return spec.key_source.strip("${}").split(":-")[0]
    return ENV_HINT


class AnthropicBackend:
    def __init__(
        self,
        spec: ModelSpec,
        role: Role | None = None,
        model_ref: str | None = None,
    ) -> None:
        import anthropic

        self.spec = spec
        self._role, self._model_ref = role, model_ref
        try:
            # `or None` 是规则一：空串会被 SDK 当成"显式给了凭据"，
            # 从而跳过整条 API_KEY → AUTH_TOKEN → profile → WIF 的解析链。
            self._client = anthropic.Anthropic(
                api_key=spec.key or None,
                base_url=spec.base_url or None,
            )
        except anthropic.AnthropicError as e:
            # 捕厂商基类而不是 APIError：CredentialsError 等构造期异常都不是
            # APIError 子类。消息套用 §6.2 的契约，不透传 SDK 英文原文。
            raise ConfigError(
                f"{_where(role, model_ref)} 的 Anthropic 客户端构造失败。"
                f"请检查凭据配置（通常是 `export {_var_hint(spec)}=你的key`）；"
                f"只想试跑工具就加 --dry-run。原始错误：{e}"
            ) from e

        if not (
            self._client.api_key
            or self._client.auth_token
            or getattr(self._client, "credentials", None)
        ):
            raise MissingCredential(
                f"缺少凭据：{_where(role, model_ref)}（provider=anthropic）"
                f"没有可用的 API key。"
                f"请先执行 `export {_var_hint(spec)}=你的key` 再运行；"
                "如果只是想在没有凭据的情况下试跑工具，加 --dry-run。"
            )

        if spec.base_url.rstrip("/").endswith("/v1"):
            # 两家 base_url 的后缀语义不同：anthropic SDK 在其后追加
            # /v1/messages，openai 追加 /chat/completions（所以 openai 的
            # base_url 要自带 /v1）。把同一个网关地址原样复制过来会 404，
            # 而请求期 404 的诊断离真因太远。
            logger.debug(
                "base_url 以 /v1 结尾，anthropic 会在其后再追加 /v1/messages，"
                "这多半是从 openai 的配置复制过来的：%s",
                spec.base_url,
            )

    def chat(
        self,
        role: Role,
        model_ref: str,
        system: str,
        messages: list,
        tools: list | None,
        max_tokens: int,
    ) -> LlmResponse:
        import anthropic

        # 不传 temperature/top_p/top_k：当代模型收到采样参数会返回 400。
        kwargs = dict(
            model=self.spec.name,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
        )
        if tools:
            kwargs["tools"] = tools
        try:
            resp = self._client.messages.create(**kwargs)
        except anthropic.AuthenticationError as e:
            # 401 改消息不改类型——见本文件顶部与设计文档 §6.3。
            raise ProviderError(
                f"凭据被拒绝（401）：{_where(role, model_ref)}（provider=anthropic）。"
                f"请检查 `{_var_hint(self.spec)}` 是否正确或已过期。原始错误：{e}"
            ) from e
        except anthropic.AnthropicError as e:
            # 捕基类不捕 APIError：请求期刷新令牌失败会抛 CredentialsError 等，
            # 它们不是 APIError 子类，SDK 也明确不把它们包装成 APIConnectionError。
            raise ProviderError(str(e)) from e

        calls = [
            ToolCall(b.id, b.name, b.input) for b in resp.content if b.type == "tool_use"
        ]
        if not calls:
            if resp.stop_reason == "refusal":
                raise ProviderError("模型拒绝了本次请求（stop_reason=refusal）")
            if resp.stop_reason == "model_context_window_exceeded":
                raise ProviderError("上下文窗口已超出：对话历史太长")

        text = "".join(b.text for b in resp.content if b.type == "text")
        logger.debug(
            "anthropic 响应 model=%s stop_reason=%s requested_max_tokens=%d "
            "output_tokens=%d",
            self.spec.name,
            resp.stop_reason,
            max_tokens,
            resp.usage.output_tokens,
        )
        return LlmResponse(
            stop_reason=_stop_reason(resp.stop_reason, bool(calls)),
            text=text,
            tool_calls=calls,
            usage=Usage(resp.usage.input_tokens, resp.usage.output_tokens),
        )


def _stop_reason(raw: str | None, has_tool_calls: bool) -> str:
    """有工具调用就是工具轮——stop_reason 只在没有工具调用时才决定分支。

    完整枚举有七个值（实测）：end_turn / max_tokens / stop_sequence /
    tool_use / pause_turn / refusal / model_context_window_exceeded，且标注
    是 Optional 所以可能是 None。refusal 与 model_context_window_exceeded
    已在上面转成 ProviderError；pause_turn 是服务端工具的续跑信号，本项目
    工具全在本地实现，此值不可达。
    """
    if has_tool_calls:
        return "tool_use"
    if raw == "max_tokens":
        return "max_tokens"
    return "end_turn"
```

- [ ] **Step 4 / Step 5：已在 Task 3 完成，本任务不做**

从 `client.py` 删掉 `AnthropicClient`、以及从 `tests/llm/test_client.py` 删掉它的那几条测试，**已经在 Task 3 一并做完**。

原因：`client.py` 也消费 `DEFAULT_ROLES`（顶部 import + `AnthropicClient.__init__` 里的 `configs or DEFAULT_ROLES`），而 Task 3 要删掉那个符号。不一起删，Task 3 结束时会留下 9 个测试模块 collection error，交不出一个可独立测试的交付物，后续任务还得在破树上开工。

本任务**不要碰** `src/tripplan/llm/client.py` 与 `tests/llm/test_client.py`。

- [ ] **Step 6: 不动 `tests/test_slot.py`**

该文件已在 Task 3 改写完毕（`DEFAULT_ROLES` 消失时一并处理）。本任务**不要碰它**——「厂商异常转成 ProviderError」这一半由本任务的 `test_api_error_becomes_provider_error` 与 `test_non_api_error_vendor_exception_also_becomes_provider_error` 直接覆盖，那是比端到端串一遍更精确的位置。

- [ ] **Step 7: 跑测试**

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/llm tests/test_slot.py -q
```
预期：全部 passed

- [ ] **Step 8: 格式化并提交**

```bash
.venv/bin/black src tests
git add -A
git commit -m "feat(llm): Anthropic backend 迁移到 llm/backends/

凭据判据改为「SDK 是否解析出任何一种凭据」，并把空 key 归一成 None——
原来查 auth_headers 在空串上假阴性、在 OAuth/WIF 上假阳性。请求期捕
厂商基类而非 APIError：CredentialsError 等不是 APIError 子类，而它们会
在刷新令牌时抛出。"
```

---

## Task 5: OpenAI backend

**Files:**
- Create: `src/tripplan/llm/backends/openai.py`
- Modify: `pyproject.toml`（`openai` 进 optional-dependencies 与 dev extra）
- Test: `tests/llm/test_openai_backend.py`（新建）

**Interfaces:**
- Produces: `OpenAIBackend(spec, role=None, model_ref=None)`，与 `AnthropicBackend` 同签名的 `chat(...)`

**归一化表（§10）：**

| 方向 | 映射 |
|---|---|
| system | 并入新列表的第 0 条 `{"role": "system", "content": system}`。**不得原地修改传入的 `messages`**——`run_agent` 每轮复用同一个 list |
| tools | `{name, description, input_schema}` → `{"type": "function", "function": {name, description, parameters}}`；空则整个字段不放进请求 |
| 输出上限 | `max_completion_tokens` |
| 是否工具轮 | `message.tool_calls` **非空**，不看 `finish_reason` |
| stop_reason | 工具轮 → `tool_use`；否则 `length` → `max_tokens`；其余 → `end_turn` |
| text | `content` 为 `None` → `""` |
| `choices` 为空 | `ProviderError` |
| `usage` 缺失 | `Usage(0, 0)` + debug 日志 |
| `refusal` 非空 | `ProviderError` |
| `content_filter` | `ProviderError` |
| `arguments` | JSON 字符串；空串按 `{}`；解析失败 → `args={}` + `[适配器]` 诊断注入 text |

- [ ] **Step 1: 改 `pyproject.toml`**

```toml
[project.optional-dependencies]
openai = ["openai>=3.13"]  # 下限=实测验证过的版本，见下方说明
dev = ["pytest>=8", "pytest-cov>=5", "black>=24", "openai>=3.13"]
```

`openai` 必须同时进 `dev`，否则本任务的测试文件在默认环境下是 **collect error** 而不是 skip。

```bash
.venv/bin/uv pip install 'openai>=3.13'
```

- [ ] **Step 2: 写失败测试**

创建 `tests/llm/test_openai_backend.py`：

```python
"""OpenAI 适配器。patch("openai.OpenAI")——backend 必须写成
`import openai` + `openai.OpenAI(...)`，用 `from openai import OpenAI`
会让这个 patch 失效。"""

import inspect
from unittest.mock import MagicMock, patch

import httpx
import openai
import pytest

from tripplan.llm.backends.openai import OpenAIBackend
from tripplan.llm.config import ModelSpec, Role
from tripplan.llm.errors import MissingCredential
from tripplan.providers.base import ProviderError


def _spec(**over):
    base = dict(
        provider="openai",
        name="gpt-5",
        base_url="https://gw.example.com/v1",
        key="sk-test",
        name_source="gpt-5",
        key_source="${OPENAI_API_KEY}",
    )
    base.update(over)
    return ModelSpec(**base)


def _resp(*, content="{}", tool_calls=None, finish_reason="stop", refusal=None,
          usage=(10, 5), choices=1):
    r = MagicMock()
    if choices == 0:
        r.choices = []
        return r
    msg = MagicMock()
    msg.content = content
    msg.refusal = refusal
    msg.tool_calls = tool_calls or []
    choice = MagicMock()
    choice.message, choice.finish_reason = msg, finish_reason
    r.choices = [choice]
    if usage is None:
        r.usage = None
    else:
        r.usage.prompt_tokens, r.usage.completion_tokens = usage
    return r


def _call(name, arguments, cid="c1"):
    c = MagicMock()
    c.id = cid
    c.function.name, c.function.arguments = name, arguments
    return c


def _chat(backend, **over):
    kw = dict(
        role=Role.PLANNER,
        model_ref="gpt5",
        system="sys",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        max_tokens=4000,
    )
    kw.update(over)
    return backend.chat(**kw)


@pytest.fixture
def backend():
    with patch("openai.OpenAI") as MockOpenAI:
        MockOpenAI.return_value.api_key = "sk-test"
        b = OpenAIBackend(_spec())
        b._mock = MockOpenAI.return_value
        yield b


# ---------- 凭据 ----------


def test_construction_failure_becomes_missing_credential(monkeypatch):
    """判据就是"构造成不成功"——不做任何环境变量枚举。

    打真实的 openai.OpenAI（构造不发请求）：环境里没有任何凭据时它会抛
    OpenAIError，我们把它转成带 §6.2 消息契约的 MissingCredential。
    """
    for var in ("OPENAI_API_KEY", "OPENAI_ADMIN_KEY"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(MissingCredential) as exc:
        OpenAIBackend(_spec(key=""), role=Role.CRITIC, model_ref="gpt5")
    message = str(exc.value)
    assert "critic" in message
    assert "gpt5" in message
    assert "OPENAI_API_KEY" in message
    assert "--dry-run" in message


def test_admin_key_alone_is_accepted(monkeypatch):
    """不许枚举环境变量：OPENAI_ADMIN_KEY 单独设置时 SDK 构造得起来
    （但 client.api_key == ''），凭据完全正常的用户不能被判成缺凭据。

    这是规则二在 openai 侧的形态——枚举 OPENAI_API_KEY 的实现会在这里红。
    """
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_ADMIN_KEY", "sk-admin-env")
    OpenAIBackend(_spec(key=""))  # 不抛


def test_empty_key_is_handed_to_sdk_as_none(monkeypatch):
    """规则一。空串不会静默带病上路（openai 3.13 对 api_key="" 当场抛），
    但它会让 SDK **拒绝去读环境变量**——于是 ${OPENAI_API_KEY:-} 展开成
    空串时，配了该变量的用户反而起不来。"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    with patch("openai.OpenAI") as MockOpenAI:
        MockOpenAI.return_value.api_key = "sk-env"
        OpenAIBackend(_spec(key=""))
        assert MockOpenAI.call_args.kwargs["api_key"] is None


def test_non_empty_base_url_is_passed_through(monkeypatch):
    """整个多 provider 特性的存在意义就是能指向内网网关；base_url 被静默
    丢弃是无声的产品故障。"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    with patch("openai.OpenAI") as MockOpenAI:
        MockOpenAI.return_value.api_key = "sk-env"
        OpenAIBackend(_spec(base_url="https://gw.internal/v1"))
        assert MockOpenAI.call_args.kwargs["base_url"] == "https://gw.internal/v1"


# ---------- 请求形状 ----------


def test_tools_field_omitted_when_none(backend):
    backend._mock.chat.completions.create.return_value = _resp()
    _chat(backend, tools=None)
    kwargs = backend._mock.chat.completions.create.call_args.kwargs
    assert "tools" not in kwargs  # 不是 "tools": None


def test_tools_are_translated_to_function_shape(backend):
    backend._mock.chat.completions.create.return_value = _resp()
    _chat(
        backend,
        tools=[{"name": "search_poi", "description": "d", "input_schema": {"a": 1}}],
    )
    tools = backend._mock.chat.completions.create.call_args.kwargs["tools"]
    assert tools == [
        {
            "type": "function",
            "function": {
                "name": "search_poi",
                "description": "d",
                "parameters": {"a": 1},
            },
        }
    ]


def test_system_is_prepended_without_mutating_caller_list(backend):
    """run_agent 每轮复用同一个 messages list。原地 insert(0, ...) 会让
    system 消息逐轮累积。"""
    backend._mock.chat.completions.create.return_value = _resp()
    caller_messages = [{"role": "user", "content": "hi"}]
    _chat(backend, messages=caller_messages)
    _chat(backend, messages=caller_messages)
    assert caller_messages == [{"role": "user", "content": "hi"}]
    sent = backend._mock.chat.completions.create.call_args.kwargs["messages"]
    assert sent[0] == {"role": "system", "content": "sys"}


def test_uses_max_completion_tokens(backend):
    backend._mock.chat.completions.create.return_value = _resp()
    _chat(backend, max_tokens=4000)
    kwargs = backend._mock.chat.completions.create.call_args.kwargs
    assert kwargs["max_completion_tokens"] == 4000
    assert "max_tokens" not in kwargs
    inspect.signature(openai.resources.chat.completions.Completions.create).bind(
        None, **kwargs
    )


# ---------- 响应归一化 ----------


def test_tool_calls_decide_the_round_not_finish_reason(backend):
    """多家网关会在带 tool_calls 的响应上给出 finish_reason="stop"/"length"。
    按 finish_reason 判会让这类响应被当成普通文本轮，tool_calls 非空却无人
    执行，模型空转到 max_tool_calls 或 deadline。"""
    backend._mock.chat.completions.create.return_value = _resp(
        content=None,
        tool_calls=[_call("search_poi", '{"query": "芜湖"}')],
        finish_reason="stop",
    )
    out = _chat(backend)
    assert out.stop_reason == "tool_use"
    assert out.tool_calls[0].args == {"query": "芜湖"}


def test_none_content_becomes_empty_string(backend):
    """LlmResponse.text 的类型是 str。不归一则 runner.py:159 会把 None 塞进
    messages，runner.py:154 的 text.strip() 抛 AttributeError——既不是
    ProviderError 也不是 LimitExceeded，只会被 orchestrator 吞成
    「候选线出现未处理异常」。"""
    backend._mock.chat.completions.create.return_value = _resp(content=None)
    assert _chat(backend).text == ""


def test_length_becomes_max_tokens_and_does_not_raise(backend):
    """归一成 max_tokens 让 runner 的修复轮照常工作。抛 ProviderError 会让
    一次普通的输出截断在 OpenAI 侧杀死整条候选线，而 Anthropic 侧只是进
    修复轮——那正是本设计承诺要消灭的 provider 分歧。"""
    backend._mock.chat.completions.create.return_value = _resp(
        content="", finish_reason="length"
    )
    assert _chat(backend).stop_reason == "max_tokens"


@pytest.mark.parametrize(
    "kwargs, needle",
    [
        ({"choices": 0}, "空的 choices"),
        ({"finish_reason": "content_filter"}, "内容过滤"),
        ({"refusal": "我不能帮你做这个"}, "拒绝"),
    ],
)
def test_response_holes_become_provider_error(backend, kwargs, needle):
    backend._mock.chat.completions.create.return_value = _resp(**kwargs)
    with pytest.raises(ProviderError) as exc:
        _chat(backend)
    assert needle in str(exc.value)


def test_missing_usage_becomes_zero(backend):
    """计量失真好过整条候选线以无用诊断挂掉。"""
    backend._mock.chat.completions.create.return_value = _resp(usage=None)
    out = _chat(backend)
    assert (out.usage.input_tokens, out.usage.output_tokens) == (0, 0)


def test_empty_arguments_string_becomes_empty_dict(backend):
    """部分网关对无参调用返回 "" 而非 "{}"。"""
    backend._mock.chat.completions.create.return_value = _resp(
        content=None, tool_calls=[_call("t", "")]
    )
    assert _chat(backend).tool_calls[0].args == {}


def test_broken_arguments_go_through_the_tool_error_channel(backend):
    """模型写坏 JSON 是可自愈的模型失误，不是外部依赖挂了。

    用 ProviderError 的真实代价：slot.py:87-89 把 itin/facts 原样交给一个
    FAILED slot，而 candidates.py:15 只在 itinerary is None 时报警、:23 只在
    EXHAUSTED 时显示 detail——产出的是一个看起来完整、可被用户选中、失败
    原因被静默隐藏的候选。比"清零"更糟。

    改走 args={}：impl(**{}) 会因缺必填参数抛 TypeError，被 runner.py:148
    的 except Exception 捕获并回喂给模型自己改正。
    """
    backend._mock.chat.completions.create.return_value = _resp(
        content=None, tool_calls=[_call("search_poi", '{"query": "京')]
    )
    out = _chat(backend)
    assert out.stop_reason == "tool_use"
    assert out.tool_calls[0].args == {}
    assert "[适配器]" in out.text


def test_adapter_diagnostic_is_appended_not_replacing_model_text(backend):
    backend._mock.chat.completions.create.return_value = _resp(
        content="我先查一下", tool_calls=[_call("search_poi", "{bad")]
    )
    out = _chat(backend)
    assert out.text.startswith("我先查一下")
    assert "[适配器]" in out.text


# ---------- 请求期错误映射（铁律） ----------


def test_api_error_becomes_provider_error(backend):
    backend._mock.chat.completions.create.side_effect = openai.APIConnectionError(
        request=httpx.Request("POST", "https://gw.example.com")
    )
    with pytest.raises(ProviderError):
        _chat(backend)


def test_non_api_error_vendor_exception_also_becomes_provider_error(backend):
    """铁律"漏捕"一侧。写成 `except APIError` 的实现会让这个异常绕过
    ProviderError，落到 _safe_slot 把已生成的行程丢掉。"""
    backend._mock.chat.completions.create.side_effect = openai.OpenAIError(
        "凭据刷新失败"
    )
    with pytest.raises(ProviderError):
        _chat(backend)


def test_authentication_error_keeps_provider_error_type(backend):
    request = httpx.Request("POST", "https://gw.example.com")
    backend._mock.chat.completions.create.side_effect = openai.AuthenticationError(
        "unauthorized", response=httpx.Response(401, request=request), body=None
    )
    with pytest.raises(ProviderError) as exc:
        _chat(backend, role=Role.CRITIC, model_ref="gpt5")
    message = str(exc.value)
    assert "401" in message
    assert "critic" in message
    assert "OPENAI_API_KEY" in message
```

- [ ] **Step 3: 跑测试确认失败**

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/llm/test_openai_backend.py -q
```
预期：`ModuleNotFoundError: No module named 'tripplan.llm.backends.openai'`

- [ ] **Step 4: 实现 `src/tripplan/llm/backends/openai.py`**

```python
"""OpenAI 适配器。

写法约束：内部用 `import openai` + `openai.OpenAI(...)`，**不要**
`from openai import OpenAI`——后者会让测试里的 patch("openai.OpenAI") 失效。
"""

import json
import logging

from tripplan.llm.client import LlmResponse, ToolCall, Usage
from tripplan.llm.config import ModelSpec, Role, is_var_reference
from tripplan.llm.errors import ConfigError, MissingCredential
from tripplan.providers.base import ProviderError

logger = logging.getLogger(__name__)

ENV_HINT = "OPENAI_API_KEY"
_MAX_DIAGNOSTIC = 200


def _where(role: Role | None, model_ref: str | None) -> str:
    if role is None or model_ref is None:
        return "某个 model"
    return f"角色 {role.value} 使用的 model「{model_ref}」"


def _var_hint(spec: ModelSpec) -> str:
    if is_var_reference(spec.key_source):
        return spec.key_source.strip("${}").split(":-")[0]
    return ENV_HINT


class OpenAIBackend:
    def __init__(
        self,
        spec: ModelSpec,
        role: Role | None = None,
        model_ref: str | None = None,
    ) -> None:
        try:
            import openai
        except ImportError as e:
            raise ConfigError(
                f"{_where(role, model_ref)} 的 provider 是 openai，"
                "但 openai 包没有安装。请执行 "
                "`uv pip install 'tripplan[openai]'`。"
            ) from e

        self.spec = spec

        try:
            # `or None` 是规则一。空串不会"静默带病上路"（实测：openai 3.13
            # 对 api_key="" 当场抛），但它会让 SDK **拒绝去读环境变量**——
            # 于是 ${OPENAI_API_KEY:-} 展开成空串时，配了该变量的用户反而起不来。
            self._client = openai.OpenAI(
                api_key=spec.key or None,
                base_url=spec.base_url or None,
            )
        except openai.OpenAIError as e:
            # 构造期 OpenAIError 就是 SDK 在说"我解析不出任何凭据"——实测确认
            # 这是它在构造期的唯一成因（坏 base_url / 空 base_url / 负 timeout
            # 全部构造成功，不抛）。
            #
            # 刻意**不做任何环境变量枚举**：既不在构造前守卫、也不在构造后查
            # client.api_key。两者都会误判——OPENAI_ADMIN_KEY 单独设置时构造
            # 成功但 client.api_key == ''，而 SDK 的凭据通道还有
            # workload_identity 等，枚举会随 SDK 新增通道持续失效。让 SDK 做
            # 权威，我们只读它的结论。
            raise MissingCredential(
                f"缺少凭据：{_where(role, model_ref)}（provider=openai）"
                f"没有可用的 API key。"
                f"请先执行 `export {_var_hint(spec)}=你的key` 再运行；"
                "如果只是想在没有凭据的情况下试跑工具，加 --dry-run。"
                f"（SDK 原文：{e}）"
            ) from e

    def chat(
        self,
        role: Role,
        model_ref: str,
        system: str,
        messages: list,
        tools: list | None,
        max_tokens: int,
    ) -> LlmResponse:
        import openai

        # 新建列表，不原地修改——run_agent 每轮复用同一个 messages list，
        # insert(0, ...) 会让 system 消息逐轮累积。
        payload = [{"role": "system", "content": system}, *messages]
        kwargs = dict(
            model=self.spec.name,
            messages=payload,
            max_completion_tokens=max_tokens,
        )
        if tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t["description"],
                        "parameters": t["input_schema"],
                    },
                }
                for t in tools
            ]
        try:
            resp = self._client.chat.completions.create(**kwargs)
        except openai.AuthenticationError as e:
            raise ProviderError(
                f"凭据被拒绝（401）：{_where(role, model_ref)}（provider=openai）。"
                f"请检查 `{_var_hint(self.spec)}` 是否正确或已过期。原始错误：{e}"
            ) from e
        except openai.OpenAIError as e:
            # 捕基类不捕 APIError——openai 里 OpenAIError 是基类、APIError 是
            # 其子类，凭据刷新一类的错误不会是 APIError。
            raise ProviderError(str(e)) from e

        if not resp.choices:
            raise ProviderError("上游返回了空的 choices")
        choice = resp.choices[0]
        message = choice.message

        if getattr(message, "refusal", None):
            raise ProviderError(f"模型拒绝了本次请求：{message.refusal}")
        raw_calls = list(message.tool_calls or [])
        if not raw_calls and choice.finish_reason == "content_filter":
            raise ProviderError("上游内容过滤拦截了本次生成")

        text = message.content or ""
        calls, notes = [], []
        for c in raw_calls:
            args, note = _parse_arguments(c.function.arguments)
            calls.append(ToolCall(c.id, c.function.name, args))
            if note:
                notes.append(note)
        if notes:
            # 拼接不覆盖：模型可能在发起工具调用的同时也吐了文本。
            # 前缀让这段适配器生成的文字在 assistant 历史里可辨认——
            # 它会经补偿一进入历史，模型否则会以为那是自己说的话。
            text = "\n".join(x for x in (text, *notes) if x)

        usage = resp.usage
        if usage is None:
            logger.debug("上游未返回 usage，计量按 0 记")
            counted = Usage(0, 0)
        else:
            counted = Usage(usage.prompt_tokens, usage.completion_tokens)
        logger.debug(
            "openai 响应 model=%s finish_reason=%s requested_max_completion_tokens=%d "
            "completion_tokens=%d",
            self.spec.name,
            choice.finish_reason,
            max_tokens,
            counted.output_tokens,
        )
        return LlmResponse(
            stop_reason=_stop_reason(choice.finish_reason, bool(calls)),
            text=text,
            tool_calls=calls,
            usage=counted,
        )


def _parse_arguments(raw: str) -> tuple[dict, str | None]:
    """arguments 是 JSON 字符串。空串（部分网关对无参调用的返回）按 {} 处理。

    解析失败不抛 ProviderError——那会杀死整条候选线。产出 args={} 让
    impl(**{}) 因缺必填参数抛 TypeError，走 runner.py:143-149 既有的
    「工具错误回喂给模型」通道；同时把原文注入 text，让模型知道是自己的
    JSON 坏了，而不是只看到"缺少必填参数"。
    """
    if not raw:
        return {}, None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        brief = raw[:_MAX_DIAGNOSTIC]
        logger.debug("工具参数不是合法 JSON：%s（%s）", brief, e)
        return {}, f"[适配器] 上一轮的工具参数不是合法 JSON：{brief}"
    if not isinstance(parsed, dict):
        return {}, f"[适配器] 工具参数必须是 JSON 对象，实际是 {type(parsed).__name__}"
    return parsed, None


def _stop_reason(finish_reason: str | None, has_tool_calls: bool) -> str:
    """判工具轮看 tool_calls 非空，不看 finish_reason。"""
    if has_tool_calls:
        return "tool_use"
    if finish_reason == "length":
        return "max_tokens"
    return "end_turn"
```

- [ ] **Step 5: 跑测试确认通过**

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/llm/test_openai_backend.py -q
```

- [ ] **Step 6: 格式化并提交**

```bash
.venv/bin/black src tests
git add -A
git commit -m "feat(llm): 新增 OpenAI backend（可选依赖，懒加载）"
```

---

## Task 6: `RoutingClient`

**Files:**
- Create: `src/tripplan/llm/router.py`
- Test: `tests/llm/test_router.py`（新建）

**Interfaces:**
- Consumes: `LlmConfig`、两个 backend
- Produces: `RoutingClient(config: LlmConfig)`，实现 `LlmClient` Protocol：`chat(role, system, messages, tools) -> LlmResponse`（**位置参数顺序不能变**，`runner.py:130` 按位置调用）

- [ ] **Step 1: 写失败测试**

创建 `tests/llm/test_router.py`：

```python
from unittest.mock import patch

import pytest

from tripplan.llm.client import LlmResponse, Usage
from tripplan.llm.config import LlmConfig, ModelSpec, Role, RoleConfig
from tripplan.llm.router import RoutingClient


def _spec(provider="anthropic", name="claude-opus-5", base_url="", key="k"):
    return ModelSpec(provider, name, base_url, key, name, "${X}")


class _Recorder:
    """假 backend——记录构造与调用，不碰任何 SDK。"""

    made: list = []

    def __init__(self, spec, role=None, model_ref=None):
        self.spec, self.calls = spec, []
        _Recorder.made.append((spec.provider, spec.base_url, spec.key))

    def chat(self, role, model_ref, system, messages, tools, max_tokens):
        self.calls.append((role, model_ref, max_tokens))
        return LlmResponse("end_turn", "{}", [], Usage(1, 1))


@pytest.fixture(autouse=True)
def _reset():
    _Recorder.made = []


def _config(**over):
    models = {
        "opus": _spec(),
        "sonnet": _spec(name="claude-sonnet-5"),
        "gpt5": _spec(provider="openai", name="gpt-5", base_url="https://gw/v1"),
    }
    roles = {
        Role.PLANNER: RoleConfig("opus", 16000),
        Role.CRITIC: RoleConfig("sonnet", 4000),
        Role.ANGLE: RoleConfig("sonnet", 2000),
        Role.CLASSIFIER: RoleConfig("sonnet", 1000),
    }
    roles.update(over)
    return LlmConfig(models=models, roles=roles)


def _client(cfg):
    return RoutingClient(cfg, factories={"anthropic": _Recorder, "openai": _Recorder})


def test_chat_dispatches_to_the_roles_model():
    cfg = _config(**{Role.CRITIC: RoleConfig("gpt5", 4000)})
    client = _client(cfg)
    client.chat(Role.CRITIC, "sys", [], None)
    backend = client.backend_for(Role.CRITIC)
    assert backend.spec.provider == "openai"
    assert backend.calls[0][:2] == (Role.CRITIC, "gpt5")


def test_max_tokens_comes_from_the_role_not_the_model():
    """同一个 model 被 angle 与 classifier 复用，但预算不同。"""
    client = _client(_config())
    client.chat(Role.ANGLE, "sys", [], None)
    client.chat(Role.CLASSIFIER, "sys", [], None)
    backend = client.backend_for(Role.ANGLE)
    assert {c[2] for c in backend.calls} == {2000, 1000}


def test_backends_are_cached_by_provider_base_url_key():
    """§7 的默认配置里三个 model 同 provider、同端点、同 key——按 model 名
    缓存会开三个客户端、三份从不关闭的 httpx 连接池。name 不进缓存键：
    它是每次请求的参数，不是客户端的属性。"""
    _client(_config())
    assert len(_Recorder.made) == 2  # anthropic 那一份 + openai 那一份


def test_all_referenced_backends_are_built_at_load_time():
    """按需构造时 MissingCredential 会被 orchestrator.py:254 的
    except Exception 吞成「候选线出现未处理异常」，cli.py:380 永远等不到。"""
    boom = []

    class _Boom(_Recorder):
        def __init__(self, spec, role=None, model_ref=None):
            boom.append(role)
            raise RuntimeError("构造失败")

    with pytest.raises(RuntimeError):
        RoutingClient(_config(), factories={"anthropic": _Boom, "openai": _Boom})
    assert boom  # 构造发生在 __init__，不是第一次 chat


def test_chat_signature_is_positional_compatible_with_protocol():
    """runner.py:130 按位置调用 client.chat(role, system, messages, tools)。"""
    client = _client(_config())
    out = client.chat(Role.PLANNER, "sys", [], None)
    assert isinstance(out, LlmResponse)
```

- [ ] **Step 2: 跑测试确认失败**

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/llm/test_router.py -q
```
预期：`ModuleNotFoundError: No module named 'tripplan.llm.router'`

- [ ] **Step 3: 实现 `src/tripplan/llm/router.py`**

```python
"""按角色把请求分派到对应的 backend。

RoutingClient 实现现有的 LlmClient Protocol，所以 runner.py / steps.py /
deps.py 对 LLM 层的调用面完全不变。
"""

from tripplan.llm.client import LlmResponse
from tripplan.llm.config import LlmConfig, Role


def _default_factory(provider: str):
    """按 provider 惰性解析 backend 类——用到哪个才 import 哪个。

    不一次性 import 两个：openai 是可选依赖，只用 anthropic 的用户不该因为
    router 顺手 import 了 openai backend 模块而被牵连。
    """
    if provider == "anthropic":
        from tripplan.llm.backends.anthropic import AnthropicBackend

        return AnthropicBackend
    if provider == "openai":
        from tripplan.llm.backends.openai import OpenAIBackend

        return OpenAIBackend
    raise KeyError(provider)  # load_config 已经挡住了非法 provider


class RoutingClient:
    def __init__(self, config: LlmConfig, factories: dict | None = None) -> None:
        """加载期就把**每一个被角色引用到的** model 的 backend 构造出来。

        不按需构造：那样 critic 的 backend 要等第一次 critic 调用才构造，
        此时 planner 的 16000 token 已经花掉；更要命的是那时抛出的
        MissingCredential 会被 orchestrator.py:254 的 except Exception 吞成
        「候选线出现未处理异常」，cli.py:380 的 except 永远等不到它。

        缓存键是 (provider, base_url, key)，不是 model 引用名——默认配置里
        三个 model 同端点同 key，按名缓存会开三份连接池。
        """
        self._config = config
        self._factories = factories or {}
        self._by_key: dict[tuple, object] = {}
        self._by_role: dict[Role, object] = {}
        # 逐 (role, model) 校验，而不是逐 backend：一个 backend 对应多个角色，
        # 按 backend 校验时凭据错误消息里的「角色名」只能任选一个。
        for role, rc in config.roles.items():
            spec = config.models[rc.model]
            cache_key = (spec.provider, spec.base_url, spec.key)
            backend = self._by_key.get(cache_key)
            if backend is None:
                factory = self._factories.get(spec.provider) or _default_factory(
                    spec.provider
                )
                backend = factory(spec, role=role, model_ref=rc.model)
                self._by_key[cache_key] = backend
            self._by_role[role] = backend

    def backend_for(self, role: Role):
        """测试与诊断用。"""
        return self._by_role[role]

    def chat(self, role: Role, system: str, messages: list, tools: list | None):
        """位置参数顺序必须与 LlmClient Protocol 一致——runner.py:130
        是按位置调用的。"""
        rc = self._config.roles[role]
        return self._by_role[role].chat(
            role=role,
            model_ref=rc.model,
            system=system,
            messages=messages,
            tools=tools,
            max_tokens=rc.max_tokens,
        )
```

- [ ] **Step 4: 跑测试确认通过**

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/llm/test_router.py -q
```

- [ ] **Step 5: 格式化并提交**

```bash
.venv/bin/black src tests
git add -A
git commit -m "feat(llm): RoutingClient 按角色分派，加载期构造全部 backend"
```

---

## Task 7: `runner.py` 的三处改动

**Files:**
- Modify: `src/tripplan/agents/runner.py:136`、`:159`、`:128-162`
- Test: `tests/agents/test_runner.py`、`tests/llm/test_tool_loop_wire_shape.py`（新建）

**Interfaces:**
- Consumes: 无（纯内部）
- Produces: `run_agent` 行为变化——assistant 轮文本含拍平的工具调用；`LimitExceeded` 消息在特定条件下追加推理预算提示

- [ ] **Step 1: 写失败测试（wire shape）**

创建 `tests/llm/test_tool_loop_wire_shape.py`：

```python
"""覆盖 §3 那个地基级取舍：工具结果被拍平成纯文本，messages 恒为
{"role": str, "content": str}，两家 API 都能直接接受。

这是整个设计能成立的前提，必须有端到端测试钉住——否则后来人会把它
"修好"成原生 tool 协议，provider 中立性当场毁掉。
"""

import json

import pytest

from tripplan.agents.limits import SlotContext, SlotLimits
from tripplan.agents.runner import run_agent
from tripplan.llm.client import FakeLlm, LlmResponse, ToolCall, Usage
from tripplan.llm.config import Role

_SCHEMA = {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}


def _ctx():
    return SlotContext(SlotLimits(), emit=lambda *a, **k: None)


def test_tool_round_wire_shape_stays_provider_neutral():
    llm = FakeLlm(
        [
            LlmResponse(
                "tool_use", "", [ToolCall("c1", "probe", {"q": "芜湖"})], Usage(1, 1)
            ),
            LlmResponse("end_turn", '{"ok": true}', [], Usage(1, 1)),
        ]
    )
    run_agent(
        system_prompt="sys",
        user_prompt="hi",
        tools=[{"name": "probe", "description": "d", "input_schema": {}}],
        output_schema=_SCHEMA,
        role=Role.PLANNER,
        ctx=_ctx(),
        client=llm,
        tool_impls={"probe": lambda q: {"hit": q}},
    )
    sent = llm.calls[-1].messages
    assert all(set(m) == {"role", "content"} for m in sent)
    assert all(isinstance(m["content"], str) and m["content"] for m in sent)
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]
    assert not any("tool_calls" in m for m in sent)
    assert not any(m["role"] == "tool" for m in sent)


def test_assistant_turn_shows_what_the_model_actually_called():
    """补偿一。OpenAI 推理模型发起工具调用时 content 恒为 None，历史里那一轮
    只剩字面 "(tool_use)"，模型不知道自己查的是哪个词 → 重复调用 →
    烧穿 max_tool_calls=40（整条候选线的累计额度）。"""
    llm = FakeLlm(
        [
            LlmResponse(
                "tool_use", "", [ToolCall("c1", "probe", {"q": "芜湖"})], Usage(1, 1)
            ),
            LlmResponse("end_turn", '{"ok": true}', [], Usage(1, 1)),
        ]
    )
    run_agent(
        system_prompt="sys",
        user_prompt="hi",
        tools=None,
        output_schema=_SCHEMA,
        role=Role.PLANNER,
        ctx=_ctx(),
        client=llm,
        tool_impls={"probe": lambda q: {"hit": q}},
    )
    assistant = llm.calls[-1].messages[1]["content"]
    assert "probe" in assistant
    assert "芜湖" in assistant


def test_tool_args_are_truncated_in_history():
    """args 完整抄进历史会显著加长 planner 的对话，抬高撞上
    model_context_window_exceeded 的概率。用 §10.1 同一把尺子：200 字符。"""
    long_arg = "x" * 500
    llm = FakeLlm(
        [
            LlmResponse(
                "tool_use", "", [ToolCall("c1", "probe", {"q": long_arg})], Usage(1, 1)
            ),
            LlmResponse("end_turn", '{"ok": true}', [], Usage(1, 1)),
        ]
    )
    run_agent(
        system_prompt="sys",
        user_prompt="hi",
        tools=None,
        output_schema=_SCHEMA,
        role=Role.PLANNER,
        ctx=_ctx(),
        client=llm,
        tool_impls={"probe": lambda q: {"hit": "ok"}},
    )
    assistant = llm.calls[-1].messages[1]["content"]
    assert len(assistant) < 400


@pytest.mark.parametrize("blank", ["", "   ", "\n\t"])
def test_repair_round_never_sends_blank_assistant_content(blank):
    """补偿二。Anthropic 对空 content 返回 400，对纯空白同样。而 `"   "`
    是 truthy，裸 `or` 兜不住——必须 .strip()。

    只用 blank="" 测的话，`resp.text or ...` 与 `resp.text.strip() or ...`
    两种实现都会变绿，而前者已被证伪。参数化是区分它们的唯一手段。
    """
    llm = FakeLlm(
        [
            LlmResponse("end_turn", blank, [], Usage(1, 1)),  # 触发 SchemaError
            LlmResponse("end_turn", '{"ok": true}', [], Usage(1, 1)),
        ]
    )
    run_agent(
        system_prompt="sys",
        user_prompt="hi",
        tools=None,
        output_schema=_SCHEMA,
        role=Role.PLANNER,
        ctx=_ctx(),
        client=llm,
        tool_impls={},
    )
    sent = llm.calls[-1].messages
    assistant = [m for m in sent if m["role"] == "assistant"]
    assert assistant, "修复轮必须往 messages 里写过一条 assistant"
    assert all(m["content"].strip() for m in assistant)
    assert assistant[0]["content"] == "(空回复)"
```

在 `tests/agents/test_runner.py` 末尾追加 §13 谓词测试：

```python
# ---------- §13：推理预算耗尽的诊断谓词 ----------
#
# 谓词必须精确，否则会复制它本要修的那个毛病——诊断与真实原因无关。


def test_hint_added_when_every_non_tool_round_is_blank():
    llm = FakeLlm(
        [
            LlmResponse("end_turn", "", [], Usage(1, 1)),
            LlmResponse("end_turn", "", [], Usage(1, 1)),
            LlmResponse("end_turn", "", [], Usage(1, 1)),
        ]
    )
    ctx = SlotContext(SlotLimits(max_schema_repairs=1), emit=lambda *a, **k: None)
    with pytest.raises(LimitExceeded) as exc:
        run_agent(
            system_prompt="s",
            user_prompt="u",
            tools=None,
            output_schema={"type": "object", "required": []},
            role=Role.CLASSIFIER,
            ctx=ctx,
            client=llm,
            tool_impls={},
        )
    message = str(exc.value)
    assert "classifier" in message
    assert "推理预算" in message
    # 不带具体数值——run_agent 这一层拿不到 per-role max_tokens
    assert "1000" not in message


def test_no_hint_when_exhausted_by_tool_calls():
    """工具轮误报。OpenAI 推理模型发起工具调用时 content 恒为 None，经
    归一化成 ""，于是「每一轮 text 都空」为真——但真因是工具空转烧穿
    max_tool_calls，与 max_tokens 毫无关系。"""
    llm = FakeLlm(
        [
            LlmResponse("tool_use", "", [ToolCall("c", "probe", {})], Usage(1, 1))
            for _ in range(5)
        ]
    )
    ctx = SlotContext(SlotLimits(max_tool_calls=2), emit=lambda *a, **k: None)
    with pytest.raises(LimitExceeded) as exc:
        run_agent(
            system_prompt="s",
            user_prompt="u",
            tools=None,
            output_schema={"type": "object", "required": []},
            role=Role.PLANNER,
            ctx=ctx,
            client=llm,
            tool_impls={"probe": lambda: {}},
        )
    assert "推理预算" not in str(exc.value)


def test_no_hint_when_interrupted_before_any_round():
    """零轮真空为真。runner.py:129 的 ctx.check() 在 client.chat 之前，
    而 ctx 的作用域是整条候选线——第二、三次 run_agent 可能一次 chat 都
    没发出就被 deadline 打断。"""
    llm = FakeLlm([])
    ctx = SlotContext(SlotLimits(max_output_tokens=0), emit=lambda *a, **k: None)
    ctx.charge(Usage(0, 1))  # 预先把额度打满
    with pytest.raises(LimitExceeded) as exc:
        run_agent(
            system_prompt="s",
            user_prompt="u",
            tools=None,
            output_schema={"type": "object", "required": []},
            role=Role.PLANNER,
            ctx=ctx,
            client=llm,
            tool_impls={},
        )
    assert "推理预算" not in str(exc.value)
```

`SlotLimits` 是个带默认值的 frozen dataclass（`limits.py:13-19`），五个字段分别是 `max_rounds=3` / `max_tool_calls=40` / `max_output_tokens=120_000` / `max_schema_repairs=2` / `deadline_s=600`，所以上面 `SlotLimits(max_schema_repairs=1)` 这种单字段覆盖直接可用。

- [ ] **Step 2: 跑测试确认失败**

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/llm/test_tool_loop_wire_shape.py tests/agents/test_runner.py -q
```
预期：wire shape 的 `test_assistant_turn_shows_what_the_model_actually_called` 与三条谓词测试失败

- [ ] **Step 3: 改 `runner.py`**

**改动 1**——把 `runner.py:136` 那一行替换为：

```python
            # 把工具调用本身也拍平进 assistant 文本。不这么做的话，OpenAI 的
            # 推理模型发起工具调用时 content 恒为 None，这一轮在历史里就只剩
            # 字面 "(tool_use)"，模型下一轮不知道自己查的是哪个词，会重复调用
            # ——而 max_tool_calls 是整条候选线的累计额度（limits.py:16）。
            # args 截断 200 字符：完整 JSON 会显著加长 planner 的历史，抬高撞上
            # 上下文窗口上限的概率。
            calls_text = "\n".join(
                f"(调用工具) {c.name}({_brief_args(c.args)})" for c in resp.tool_calls
            )
            content = "\n".join(x for x in (resp.text, calls_text) if x) or "(tool_use)"
            messages.append({"role": "assistant", "content": content})
```

并在模块级加辅助函数（放在 `_repair_prompt` 附近）：

```python
_MAX_ARGS_IN_HISTORY = 200


def _brief_args(args: dict) -> str:
    s = json.dumps(args, ensure_ascii=False, default=str)
    return s if len(s) <= _MAX_ARGS_IN_HISTORY else s[:_MAX_ARGS_IN_HISTORY] + "…"
```

**改动 2**——把 `runner.py:159` 替换为：

```python
            # .strip() 不能省：Anthropic 对空 content 返回 400，对纯空白同样，
            # 而 "   " 是 truthy，裸 or 兜不住。
            messages.append(
                {"role": "assistant", "content": resp.text.strip() or "(空回复)"}
            )
```

**改动 3**（结构性）——把整个 `while True:` 包进 try/except。`LimitExceeded` 有三个抛出点（`:129` 与 `:135` 的 `ctx.check()`，以及 `:158` 的 `raise`），所以必须包住整个循环，不能只在某一处加。

`run_agent` 的完整最终形态（**含改动 1 与 2**，直接替换 `runner.py:115-162`）：

```python
def run_agent(
    system_prompt: str,
    user_prompt: str,
    tools,
    output_schema: dict,
    role: Role,
    ctx: SlotContext,
    client: LlmClient,
    tool_impls: dict,
) -> dict:
    messages: list[dict] = [{"role": "user", "content": user_prompt}]
    repairs = 0
    # §13 的诊断需要知道：本次调用里有没有出现过「非工具轮」，以及它们是不是
    # 全都返回了空文本。作用域是**本次 run_agent 调用**，不是整条候选线——
    # 取 slot 作用域的话，generate 只要出过一次正常文本就永久置假，功能几乎
    # 永不触发。
    non_tool_rounds = 0
    blank_non_tool_rounds = 0

    try:
        while True:
            ctx.check()  # 超 deadline / token / 取消 → 抛
            resp = client.chat(role, system_prompt, messages, tools)
            ctx.charge(resp.usage)

            if resp.stop_reason == "tool_use":
                ctx.charge_tool_calls(len(resp.tool_calls))
                ctx.check()  # 本轮工具调用若把额度打穿，这一轮工具一个都不执行
                # 改动 1：把工具调用本身也拍平进 assistant 文本。不这么做的话，
                # OpenAI 的推理模型发起工具调用时 content 恒为 None，这一轮在
                # 历史里就只剩字面 "(tool_use)"，模型下一轮不知道自己查的是哪
                # 个词，会重复调用——而 max_tool_calls 是整条候选线的累计额度
                # （limits.py:16）。args 截断 200 字符：完整 JSON 会显著加长
                # planner 的历史，抬高撞上上下文窗口上限的概率。
                calls_text = "\n".join(
                    f"(调用工具) {c.name}({_brief_args(c.args)})"
                    for c in resp.tool_calls
                )
                content = (
                    "\n".join(x for x in (resp.text, calls_text) if x) or "(tool_use)"
                )
                messages.append({"role": "assistant", "content": content})
                results = []
                for call in resp.tool_calls:
                    impl = tool_impls.get(call.name)
                    if impl is None:
                        results.append(f"[{call.name}] 错误：没有这个工具")
                        continue
                    try:
                        results.append(
                            f"[{call.name}] "
                            + json.dumps(
                                impl(**call.args), ensure_ascii=False, default=str
                            )
                        )
                    except Exception as e:  # 工具报错交回模型，不炸穿这条线
                        results.append(f"[{call.name}] 错误：{e}")
                messages.append({"role": "user", "content": "\n".join(results)})
                continue

            non_tool_rounds += 1
            if not resp.text.strip():
                blank_non_tool_rounds += 1

            try:
                return _parse_and_validate(resp.text, output_schema)
            except SchemaError as e:
                repairs += 1
                if repairs > ctx.limits.max_schema_repairs:
                    raise LimitExceeded(f"schema 修复 {repairs} 次仍失败：{e}") from e
                # 改动 2：.strip() 不能省——Anthropic 对空 content 返回 400，
                # 对纯空白同样，而 "   " 是 truthy，裸 or 兜不住。
                messages.append(
                    {"role": "assistant", "content": resp.text.strip() or "(空回复)"}
                )
                messages.append(
                    {"role": "user", "content": _repair_prompt(e, output_schema)}
                )
    except LimitExceeded as e:
        # 三个条件全部满足才追加：本次调用、至少一个非工具轮、所有非工具轮都是
        # 空文本。少任何一条都会误报——工具空转烧穿额度时每轮 text 也都是空的
        # （OpenAI 推理模型工具轮 content 恒为 None），而零轮时「每轮都空」
        # 真空成立。
        #
        # 另有三类必须先被 backend 归一成 ProviderError、根本到不了这里：
        # refusal、content_filter、model_context_window_exceeded。三者都会产出
        # 「非工具轮 text 全空」从而满足谓词，但真因与 max_tokens 无关。
        #
        # 不带 max_tokens 的具体数值：run_agent 这一层拿不到 per-role 的
        # max_tokens（ctx.limits 是 SlotLimits，client 是只有 chat 的
        # Protocol），为一条诊断改 Protocol 代价不成比例。报角色名即可。
        if non_tool_rounds > 0 and blank_non_tool_rounds == non_tool_rounds:
            raise LimitExceeded(
                f"{e}（本次调用（角色 {role.value}）的每一轮非工具响应都是空文本，"
                "该角色的 max_tokens 可能被推理预算吃光）"
            ) from e
        raise
```

对照 `runner.py:115-162` 的原文逐行核一遍：除了新增的两个计数、外层 try/except、以及改动 1/2 那两处 `messages.append`，其余每一行都与原文相同。

- [ ] **Step 4: 跑测试确认通过**

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/llm/test_tool_loop_wire_shape.py tests/agents/ -q
```

- [ ] **Step 5: 格式化并提交**

```bash
.venv/bin/black src tests
git add -A
git commit -m "feat(agents): runner 三处改动——拍平工具调用、空回复占位、推理预算诊断"
```

---

## Task 8: `cli.py` 接线

**Files:**
- Modify: `src/tripplan/cli.py`（删 `:250` 那道检查；`build_deps` 改用 `RoutingClient`；`TRIPPLAN_CONFIG` 优先级；`TRIPPLAN_LOG` 开关）
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `RoutingClient`、`load_config`

- [ ] **Step 1: 写失败测试**

改写 `tests/test_cli.py` 里那三条只针对 `ANTHROPIC_API_KEY` 的测试（`:356` / `:368` / `:379`），并扩充 fixture：

```python
@pytest.fixture(autouse=True)
def _no_real_credentials(monkeypatch, tmp_path):
    """本文件全程离线。

    凭据判据已经从「环境变量 ANTHROPIC_API_KEY」扩成「SDK 是否解析出任何
    一种凭据」，所以光 delenv 不够——还要把 profile 的平台默认位置（HOME）
    指向一个空目录。注意 ANTHROPIC_CONFIG_DIR 必须 **delenv** 而不是 setenv：
    设置它会把 profile 解析升级为「显式选择」，构造期直接抛 CredentialsError。
    """
    for var in (
        "AMAP_KEY",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "OPENAI_API_KEY",
        "ANTHROPIC_PROFILE",
        "ANTHROPIC_CONFIG_DIR",
        "ANTHROPIC_IDENTITY_TOKEN",
        "ANTHROPIC_IDENTITY_TOKEN_FILE",
        "ANTHROPIC_FEDERATION_RULE_ID",
        "ANTHROPIC_ORGANIZATION_ID",
        "TRIPPLAN_ROLES",
        "TRIPPLAN_CONFIG",
        "TRIPPLAN_CACHE",
        "TRIPPLAN_LOG",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "sealed-home"))


def test_build_deps_missing_llm_credential_is_readable(tmp_path, monkeypatch):
    monkeypatch.setenv("AMAP_KEY", "test-key-123")
    monkeypatch.setenv("TRIPPLAN_CACHE", str(tmp_path / "cache"))
    with pytest.raises(MissingCredential) as exc:
        build_deps(dry_run=False)
    message = str(exc.value)
    assert "planner" in message  # 角色名
    assert "ANTHROPIC_API_KEY" in message  # 该 export 的变量名
    assert "--dry-run" in message  # 出路


def test_plan_without_llm_credential_reports_readable_error(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("AMAP_KEY", "test-key-123")
    monkeypatch.setenv("TRIPPLAN_CACHE", str(tmp_path / "cache"))
    code = main(["plan", "去芜湖", "--dir", str(tmp_path / "wuhu")])
    err = capsys.readouterr().err
    assert code != 0
    assert "ANTHROPIC_API_KEY" in err
    assert "TypeError" not in err
    assert "Traceback" not in err


def test_auth_token_alone_gets_past_the_credential_check(tmp_path, monkeypatch):
    """ANTHROPIC_API_KEY 不是唯一凭据来源。只配了 AUTH_TOKEN 的用户不能被挡。
    这正是旧实现（只看 ANTHROPIC_API_KEY）引入的回归。"""
    monkeypatch.setenv("AMAP_KEY", "test-key-123")
    monkeypatch.setenv("TRIPPLAN_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "bearer-abc")
    deps = build_deps(dry_run=False)  # 不抛
    assert deps.client is not None


def test_trippan_config_wins_over_trippan_roles(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AMAP_KEY", "test-key-123")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("TRIPPLAN_CACHE", str(tmp_path / "cache"))
    new = tmp_path / "new.toml"
    new.write_text('[roles.planner]\nmax_tokens = 111\n', encoding="utf-8")
    old = tmp_path / "old.toml"
    old.write_text('[roles.planner]\nmax_tokens = 222\n', encoding="utf-8")
    monkeypatch.setenv("TRIPPLAN_CONFIG", str(new))
    monkeypatch.setenv("TRIPPLAN_ROLES", str(old))

    deps = build_deps(dry_run=False)
    assert deps.client._config.roles[Role.PLANNER].max_tokens == 111
    assert "TRIPPLAN_CONFIG" in capsys.readouterr().err
```

- [ ] **Step 2: 跑测试确认失败**

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_cli.py -q
```

- [ ] **Step 3: 改 `build_deps`**

把 `cli.py` 里那道 `if not os.environ.get("ANTHROPIC_API_KEY"):` 检查（`:244-255` 整段，含注释）**整段删除**——它是"只认 ANTHROPIC_API_KEY"这个误判的来源，凭据校验已经移到 backend 构造期。然后：

```python
def _config_path() -> Path | None:
    """TRIPPLAN_CONFIG 优先于 TRIPPLAN_ROLES（旧名，保留兼容）。

    两者同时设置时提示一句，免得用户"改了文件却没生效"还查不出来。
    """
    new = os.environ.get("TRIPPLAN_CONFIG")
    old = os.environ.get("TRIPPLAN_ROLES")
    if new and old:
        print(
            f"提示：TRIPPLAN_CONFIG 与 TRIPPLAN_ROLES 都设置了，"
            f"使用 TRIPPLAN_CONFIG（{new}），忽略 TRIPPLAN_ROLES（{old}）。",
            file=sys.stderr,
        )
    chosen = new or old
    return Path(chosen) if chosen else None


def build_deps(dry_run: bool = False) -> Deps:
    from tripplan.providers.fake import FakeProvider

    if dry_run:
        return Deps(client=None, provider=FakeProvider())

    provider = build_provider(dry_run=False)
    if provider is None:
        raise MissingCredential(
            "缺少环境变量 AMAP_KEY（高德开放平台的 key，用于路线查询与静态地图）。"
            "请先执行 `export AMAP_KEY=你的高德key` 再运行；"
            "如果只是想在没有凭据的情况下试跑工具，加 --dry-run。"
        )

    from tripplan.llm.config import load_config
    from tripplan.llm.router import RoutingClient

    # RoutingClient 在构造时就把每个被引用到的 model 的 backend 建出来并校验
    # 凭据——不能拖到第一次 chat：那时抛出的 MissingCredential 会被
    # orchestrator._safe_slot 吞成「候选线出现未处理异常」，下面 main() 的
    # except MissingCredential 永远等不到。
    return Deps(client=RoutingClient(load_config(_config_path())), provider=provider)
```

- [ ] **Step 4: 加 `TRIPPLAN_LOG` 开关**

在 `main()` 开头（`parser = argparse.ArgumentParser(...)` 之前）：

```python
    # 诊断日志默认完全静默。本设计里 backend 的旁路诊断（请求的 token 预算
    # 与实际用量、工具参数解析失败的原文、base_url 形态提示）全部走 logging
    # 的 debug 级——不加这个开关，那些信息永远没人看得见。
    level = os.environ.get("TRIPPLAN_LOG", "").lower()
    if level in ("debug", "info"):
        logging.basicConfig(level=getattr(logging, level.upper()), stream=sys.stderr)
```

并在文件顶部 `import logging`。

- [ ] **Step 5: 跑全量测试**

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q
```
预期：全部 passed（Task 1 留下的两条 `config_error_from` 此时应变绿）

- [ ] **Step 6: 格式化并提交**

```bash
.venv/bin/black src tests
git add -A
git commit -m "feat(cli): build_deps 改用 RoutingClient；新增 TRIPPLAN_CONFIG 与 TRIPPLAN_LOG

删掉那道只认 ANTHROPIC_API_KEY 的前置检查——它会把用 ANTHROPIC_AUTH_TOKEN、
ant auth login profile 或 WIF 的用户误判成缺凭据。凭据校验移到 backend
构造期，判据是「SDK 是否解析出任何一种凭据」。"
```

---

## Task 9: 工具注册期校验

**Files:**
- Modify: `src/tripplan/agents/tools.py`（`build_planning_tools` 返回前）
- Test: `tests/agents/test_tools.py`

**背景**：`§10.1` 的自愈通道（解析失败 → `args={}` → `impl(**{})` 抛 `TypeError` → `runner.py:148` 捕获回喂）依赖一条隐性契约：**每个工具至少有一个必填参数**。用注册期校验钉住它，而不是写进 docstring 当君子协定。

- [ ] **Step 1: 写失败测试**

在 `tests/agents/test_tools.py` 末尾追加：

```python
# ---------- 注册期契约：impl(**{}) 必抛 ----------
#
# openai backend 在 arguments 解析失败时产出 args={}，靠 impl(**{}) 抛
# TypeError 走 runner.py:143-149 的错误回喂通道。参数全带默认值、零参数、
# 纯 *args / **kwargs 三种工具都会让 impl(**{}) 静默成功——白烧一次
# max_tool_calls 额度，结果被当成正常工具结果回喂，且不留任何痕迹。

import pytest

from tripplan.agents.tools import check_tool_impls


@pytest.mark.parametrize(
    "fn",
    [
        lambda: {},  # 零参数
        lambda **kw: {},  # 纯 **kwargs
        lambda *a: {},  # 纯 *args
    ],
)
def test_tools_without_a_required_parameter_are_rejected(fn):
    with pytest.raises(ValueError) as exc:
        check_tool_impls({"bad": fn})
    assert "bad" in str(exc.value)


def test_tool_with_at_least_one_required_parameter_is_accepted():
    """收紧到"至少一个必填"而不是"每个参数都无默认值"——后者会永久剥夺
    工具作者写可选参数的自由，而 search_poi(query, limit=10) 完全满足
    impl(**{}) 必抛。"""
    check_tool_impls({"ok": lambda query, limit=10: {}})  # 不抛


def test_real_planning_tools_satisfy_the_contract():
    from tripplan.providers.fake import FakeProvider

    _, impls = build_planning_tools(FakeProvider(), "芜湖")
    check_tool_impls(impls)  # 不抛
```

- [ ] **Step 2: 跑测试确认失败**

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/agents/test_tools.py -q
```
预期：`ImportError: cannot import name 'check_tool_impls'`

- [ ] **Step 3: 实现**

在 `src/tripplan/agents/tools.py` 顶部 `import inspect`，并在文件末尾加：

```python
def check_tool_impls(impls: dict) -> None:
    """每个工具实现必须至少有一个必填参数。

    这条契约存在的唯一理由在 llm/backends/openai.py：arguments 解析失败时
    backend 产出 args={}，靠 impl(**{}) 抛 TypeError 走 runner.py:143-149
    的「工具错误回喂给模型」通道。零参数、纯 *args、纯 **kwargs、或参数
    全带默认值的实现都会让 impl(**{}) 静默成功——白烧一次 max_tool_calls
    额度，坏结果被当成正常结果回喂，且不留任何痕迹。

    用显式 raise 而不是 assert：python -O 会把 assert 整条剥掉，那时这道
    校验静默消失。
    """
    for name, fn in impls.items():
        params = inspect.signature(fn).parameters.values()
        has_required = any(
            p.default is inspect.Parameter.empty
            and p.kind
            not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
            for p in params
        )
        if not has_required:
            raise ValueError(
                f"工具 {name} 没有任何必填参数。llm/backends/openai.py 在工具"
                "参数解析失败时依赖 impl(**{}) 抛 TypeError 来把错误回喂给模型；"
                "没有必填参数会让那次调用静默成功。请至少保留一个必填参数。"
            )
```

在 `build_planning_tools` 的 `return specs, {...}` 之前加：

```python
    impls = {"search_poi": search_poi, "route_duration": route_duration}
    check_tool_impls(impls)
    return specs, impls
```

- [ ] **Step 4: 跑测试并提交**

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/agents/test_tools.py -q
.venv/bin/black src tests
git add -A
git commit -m "feat(agents): 工具注册期校验「至少一个必填参数」"
```

---

## Task 10: `candidates.py` 的失败可见性修复

**Files:**
- Modify: `src/tripplan/render/candidates.py:23`
- Test: `tests/render/test_candidates.py`

**背景**：`candidates.py:23` 的条件是 `if slot.status is SlotStatus.EXHAUSTED and slot.detail`——`FAILED` 且 `itinerary` 非空时 `detail` 一个字都不显示，而 `candidates.py:33` 照常把它列进可选项。用户拿到的是一份看起来完整、可直接选中、连一个 ⚠️ 都没有的行程。这与该模块自己的 docstring（「遗留问题是用户挑选方案的重要依据，必须显示」）直接冲突，而本设计新增了四个会在 revise/critic 轮触发 `ProviderError` 的生产者，把这个洞显著撑大了。

- [ ] **Step 1: 写失败测试**

在 `tests/render/test_candidates.py` 末尾追加：

```python
def test_failed_slot_that_still_has_an_itinerary_shows_why_it_failed():
    """revise/critic 轮挂掉时 slot.py:86-89 会保住已生成的 itin，于是
    itinerary 非空、status=FAILED——当前实现（candidates.py:23 只认
    EXHAUSTED）下 detail 完全不显示，而 candidates.py:33 照常把它列进
    可选项。用户会选中一份中途挂掉的行程而毫不知情。

    注意与已有的 test_failed_candidate_is_shown_but_marked_unselectable
    的区别：那条用的是 has_itin=False，走的是 candidates.py:15 那个分支。
    """
    out = render_candidates(
        [
            _slot(
                "D",
                SlotStatus.FAILED,
                detail="外部依赖失败：凭据被拒绝（401）",
                has_itin=True,
            )
        ]
    )
    assert "401" in out
```

用的是这个文件里现成的 `_slot()` helper（`test_candidates.py:7-15`），`has_itin=True` 是它的默认值，这里显式写出来是为了点明与上一条测试的差别。

- [ ] **Step 2: 跑测试确认失败**（断言 `"401" in out` 失败）

- [ ] **Step 3: 改一行**

```python
        # FAILED 也要显示 detail：revise/critic 轮挂掉时 slot.py:86-89 会保住
        # 已生成的 itin，于是 itinerary 非空、走不到上面那个 ⚠️ 分支。不显示
        # detail 的话，用户拿到的是一份看起来完整、可直接选中、失败原因被
        # 静默隐藏的行程——与本模块 docstring 的承诺直接冲突。
        # 只补显示，不改可选性：一份"主体已生成、critic 挂了"的行程，
        # 用户看到 ⚠️ 之后仍然可以选它，比强行剥夺选择更合理。
        if slot.status in (SlotStatus.EXHAUSTED, SlotStatus.FAILED) and slot.detail:
            lines.append(f"⚠️ {slot.detail}")
```

- [ ] **Step 4: 跑测试并提交**

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/render/ -q
.venv/bin/black src tests
git add -A
git commit -m "fix(render): FAILED 的候选也要显示失败原因，不能静默隐藏"
```

---

## Task 11: `limits.py` 的跨 provider 计量注释

**Files:**
- Modify: `src/tripplan/agents/limits.py:17`

无测试——这是纯注释，为它写测试就是 change detector。

- [ ] **Step 1: 加注释**

在 `SlotLimits` 的 `max_output_tokens` 字段上方：

```python
    #: 跨 provider 混用时这只是一道**粗粒度熔断，不是可比的计量**：
    #: OpenAI 推理模型的 completion_tokens 混着 reasoning tokens，同样"干一件
    #: 事"的计数可能是 Anthropic 的数倍，这个阈值不再对应稳定语义，候选线会
    #: 以看不出规律的方式提前 EXHAUSTED。
    #: 另外，对省略 usage 的网关（backends/openai.py 把它归一成 Usage(0,0)），
    #: 这道熔断根本不会触发，那条候选线只剩 deadline 兜底。
    max_output_tokens: int = 120_000
```

- [ ] **Step 2: 跑全量测试并提交**

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q
.venv/bin/black src tests
git add -A
git commit -m "docs(limits): 写明跨 provider 时 max_output_tokens 只是粗粒度熔断"
```

---

## Task 12: `resume --dry-run`

**Files:**
- Modify: `src/tripplan/cli.py:353-355`（argparse 注册）与 `_cmd_resume`（早返回）
- Test: `tests/test_cli.py`

**背景**：`§6.2` 的凭据错误消息里承诺了 `--dry-run` 这条出路，但 `resume` 没有这个 flag。**只加 argparse 一行是不够的**——`_cmd_plan` 的 dry-run 语义来自它自己在 `cli.py:285-287` 的早返回，而 `_cmd_resume` 无条件走到 `_drive_and_report`，`Deps(client=None)` 会在 COLLECT 阶段炸出 `AttributeError: 'NoneType' object has no attribute 'chat'`——一条不在 `main()` except 元组里的裸 traceback，比今天 argparse 干净拒绝（exit 2）更糟。

- [ ] **Step 1: 写失败测试**

```python
def test_resume_dry_run_reports_state_without_calling_llm(tmp_path, mk, capsys):
    repo = FileRepo(tmp_path / "kyoto")
    repo.create(_state())
    code = main(["resume", str(repo.dir), "--dry-run"])
    out = capsys.readouterr().out
    assert code == 0
    assert "已载入" in out
```

`_state()` 是 `tests/test_cli.py:62` 现成的夹具（默认 `stage=Stage.AWAIT_REQ_CONFIRM, rev=1`），直接用。

- [ ] **Step 2: 跑测试确认失败**

预期：`error: unrecognized arguments: --dry-run`（argparse 层）

- [ ] **Step 3: 两处改动**

argparse：

```python
    r = sub.add_parser("resume", help="接着上次的进度继续")
    r.add_argument("dir")
    r.add_argument(
        "--dry-run", action="store_true", help="只载入并报告状态，不调 LLM 与高德"
    )
    r.set_defaults(func=_cmd_resume)
```

`_cmd_resume`，在 `print(f"已载入 rev …")` 之后：

```python
    print(f"已载入 rev {state.revision}，阶段 {state.stage.value}")
    if args.dry_run:
        # 与 _cmd_plan 的早返回对称。没有这一句的话，Deps(client=None) 会被
        # 交给 advance()，COLLECT 阶段的 LLM 步骤炸出 AttributeError，
        # 而它不在 main() 的 except 元组里——一条裸 traceback，比 argparse
        # 干净拒绝更糟。而 §6.2 的凭据错误消息正好把用户指向这条路。
        return 0
    return _drive_and_report(state, repo, args)
```

- [ ] **Step 4: 跑全量测试并提交**

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q
.venv/bin/black src tests
git add -A
git commit -m "feat(cli): resume 支持 --dry-run（argparse 注册 + 早返回两处）"
```

---

## 收尾验证

- [ ] **全量测试**

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q
```

- [ ] **实跑四种凭据场景**（对照设计文档 §6.3 的三档语义）

```bash
# 1. 什么都没配 → 可读的中文 + 变量名 + --dry-run
env -u ANTHROPIC_API_KEY AMAP_KEY=fake .venv/bin/trip plan '去芜湖' --dir /tmp/t1

# 2. 只有 AUTH_TOKEN（不能被挡）
env -u ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN=fake AMAP_KEY=fake \
  .venv/bin/trip plan '去芜湖' --dir /tmp/t2

# 3. --dry-run 逃生口
env -u ANTHROPIC_API_KEY -u AMAP_KEY .venv/bin/trip plan '去芜湖' --dry-run --dir /tmp/t3
env -u ANTHROPIC_API_KEY -u AMAP_KEY .venv/bin/trip resume /tmp/t3 --dry-run

# 4. 坏配置 → ConfigError，不是裸 traceback
printf '[roles.critic]\nmodel = "claude-sonnet-5"\n' > /tmp/bad.toml
env TRIPPLAN_CONFIG=/tmp/bad.toml AMAP_KEY=fake ANTHROPIC_API_KEY=sk-x \
  .venv/bin/trip plan '去芜湖' --dir /tmp/t4
```

- [ ] **实跑一次真正的异厂交叉评审**（需要两家真 key，可选）

```toml
# ~/.config/tripplan/config.toml
[models.gpt5]
provider = "openai"
name     = "gpt-5"
key      = "${OPENAI_API_KEY}"

[roles.critic]
model = "gpt5"
```

```bash
TRIPPLAN_CONFIG=~/.config/tripplan/config.toml TRIPPLAN_LOG=debug \
  .venv/bin/trip plan '我下个周末去芜湖2天，骑车+咖啡。'
```

- [ ] **确认设计文档里标注"未离线实测"的 openai 断言**（实施第一件事就该做，这里再核一遍）

```bash
.venv/bin/python -c "
import openai, inspect
print('OpenAIError 是基类:', issubclass(openai.APIError, openai.OpenAIError))
print('AuthenticationError MRO:', [c.__name__ for c in openai.AuthenticationError.__mro__[:4]])
try:
    openai.OpenAI(api_key=None)
except Exception as e:
    print('api_key=None 构造:', type(e).__name__)
print('api_key=\"\" 构造:', type(openai.OpenAI(api_key='')).__name__)
"
```

若结果与设计文档 §10.2 / §6.1 的描述不符，**停下来先更新设计文档**，再调整 Task 5 的实现。

---

## 自查

**Spec 覆盖**：§3 → Task 7；§4 / §4.1 / §4.2 → Task 3 + Task 8；§5 → Task 2 + Task 3；§6 → Task 4 + Task 5 + Task 8；§7 → Task 3；§8 / §8.1 / §8.2 → Task 3 + Task 6；§9 → Task 3；§10 / §10.0 / §10.1 / §10.2 / §10.3 / §10.4 → Task 4 + Task 5；§10.0.1 → Task 10；§11 → Task 1 + Task 3；§12 → Task 5；§13 → Task 7；§14 → Task 11；§14.1 → 记录在案不动；§15 → 分散在各任务。

**已知缺口**：`§2` 的推理强度配置按设计是非目标，无对应任务。

**类型一致性**：`ModelSpec` 六字段在 Task 3 定义、Task 4/5/6 使用；`RoleConfig(model, max_tokens, allow_same_model)` 同；backend 的 `chat(role, model_ref, system, messages, tools, max_tokens)` 在 Task 4 定下、Task 5 对齐、Task 6 调用；`RoutingClient.chat(role, system, messages, tools)` 与 `LlmClient` Protocol 位置参数一致。
