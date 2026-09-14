# 多 provider LLM 接入设计

日期：2026-09-13
状态：待评审（第 12 稿，已吸收十一轮评审）

> **贯穿全篇的一条铁律**（第 8 轮评审揪出的那类错误的根源）：
> **异常类型决定数据能不能活下来，异常消息决定用户能不能看懂，两者不要混为一谈。**
> `run_slot` 只捕获 `LimitExceeded`（`slot.py:76`）与 `ProviderError`（`slot.py:86`）；其余一切落到 `orchestrator.py:254` 的 `_safe_slot`，而它把 `itinerary` 与 `facts` **硬编码成 `None`**——已生成的行程当场丢失。
> 因此：**加载期**可以用 `ConfigError` / `MissingCredential`（那时还没进 slot）；**请求期的一切错误一律 `ProviderError`**，想改善诊断就改消息内容，永远不要为了"消息更贴切"去换类型。
>
> 这条铁律有两种违反方式，本设计在评审中各踩过一次：**错捕**（第 8 稿把 401 转成 `MissingCredential`）与**漏捕**（第 8 稿请求期只捕 `APIError`，漏掉请求期刷新令牌时抛出的 `CredentialsError` 等非 `APIError` 的 `AnthropicError`）。两者后果相同——已生成的行程被 `_safe_slot` 丢弃。所以请求期一律捕**厂商基类**（`AnthropicError` / `OpenAIError`），不捕 `APIError`。

## 1. 背景

`tripplan` 的 LLM 访问层目前硬绑 Anthropic：

- `llm/client.py:83-126` 的 `AnthropicClient` 直接构造 `anthropic.Anthropic`，遍历 `resp.content` 的 block，读 `usage.input_tokens/output_tokens`。
- `llm/config.py:24-33` 的 `DEFAULT_ROLES` 把四个角色的模型写死成 Claude 模型 ID，没有 base_url、没有凭据配置。
- `cli.py:250` 有一道只认 `ANTHROPIC_API_KEY` 的前置检查。

需求：允许每个角色使用 OpenAI 或 Anthropic，可配置 base_url / key / 模型名，为后续增加推理强度等参数留出位置。

## 2. 目标与非目标

**目标**

- 支持 `anthropic` 与 `openai` 两种 provider。
- 配置分两层：先定义一组 model（各自带 provider / name / base_url / key），角色再引用 model 名。
- 配置值支持 `${VAR}` 与 `${VAR:-默认值}` 展开，使配置文件本身可以进版本库而密钥留在环境中。
- 不给配置文件时保持现有开箱行为；给了配置文件时保持现有的**合并**语义（§4.1）。

**非目标（本次明确不做）**

- 推理强度（effort / thinking）配置。用户已说明这是后续需求。
- 与之相关的一个隐患同样推迟，但**两个 provider 都有**：
  - Anthropic：`claude-opus-5` / `claude-sonnet-5` 默认开启 adaptive thinking，`max_tokens` 是 thinking 与正文的**共同**上限。
  - OpenAI：`max_completion_tokens` 同样是 reasoning tokens 与可见输出的共同上限；gpt-5 系默认带 reasoning，`usage.completion_tokens` **包含** `completion_tokens_details.reasoning_tokens`。
  - 后果相同：预算被推理吃光 → `content` 为空 + `finish_reason="length"`。§13 给出本次的最小缓解。
  - 上述两条关于两家计费口径的描述来自各自的公开 API 契约，**本次未离线实测**（`openai` 未安装、评审环境无网）。
- 删除死字段 `independent_context` 之外的任何清理。
- 兼容旧的扁平 TOML 格式（`[roles.planner] model = "claude-opus-5"` 这种把真实模型 ID 直接写在角色下的写法）。

## 3. 关键前提：runner 已经是 provider 中立的

`agents/runner.py` 从不使用 Anthropic 原生的 `tool_use` / `tool_result` content block。工具轮里它这样做（`runner.py:136-150`）：

```python
messages.append({"role": "assistant", "content": resp.text or "(tool_use)"})
# ... 执行工具 ...
messages.append({"role": "user", "content": "\n".join(results)})
```

把 assistant 的 tool_calls **丢弃**、只保留文本，再用一条普通的 user message 把工具结果作为纯文本喂回去。因此 `messages` 始终是 `{"role": str, "content": str}`，首条恒为 user（`runner.py:125`），角色严格交替，两家 API 都能直接接受。

**这是刻意的取舍，不是缺陷，不要"修好"它。** 改成原生 tool 协议会立刻引入 provider 分歧（Anthropic 用 `tool_use`/`tool_result` block，OpenAI 要求每个 `tool_call_id` 有对应的 `role:"tool"` 消息），把中立性毁掉。当前写法对 OpenAI 合法的原因是：我们从未在 assistant message 里声明过 `tool_calls`，所以不存在"缺少 tool_call_id 回应"的问题。

### 3.1 代价，以及本次必须做的两处补偿

**代价一：模型看不到自己发出的工具调用。**

`runner.py:136` 把 assistant 轮压成 `resp.text or "(tool_use)"`。Anthropic 通常会在 tool_use 之前吐一段文本前言，历史里还留着"我打算查 X"的痕迹；**OpenAI 的推理模型发起工具调用时 `message.content` 基本恒为 `None`**，那一轮在历史里字面上就变成字符串 `"(tool_use)"`。模型下一轮看到：

```
assistant: (tool_use)
user: [search_poi] {"ambiguous": true, "results": [...]}
```

它不知道自己查的是哪个词，会诱发重复调用。而 `SlotLimits.max_tool_calls = 40`（`limits.py:16`，经 `limits.py:50-51` 累加、`slot.py:41` 确认作用域是整条候选线）是**整条候选线**的累计额度，空转几轮即可烧穿 → `LimitExceeded` → `EXHAUSTED`。

**补偿一（工具轮）**：把工具调用本身也拍平进 assistant 文本，与结果配对。**保留 `or "(tool_use)"` 兜底**——它是现状行为，保留零成本，不需要额外理由。

（说明一处前稿的论证不对称：第 4 稿一边以"现状零成本"保留这个 `or`，一边以"不给不可能发生的情形加防御"拒绝给 `runner.py:150` 的 `"\n".join(results)` 加同构兜底——两者都只是一个 `or`。真实区别只有"一个是现状、一个是新增"。两处分支在生产上都不可达：`stop_reason=="tool_use"` 蕴含至少一个 call（Anthropic 必有 block，OpenAI 按 §10 的判据必非空）。**结论不变**：保留既有的、不新增不可达的防御，但理由是"不改现状"，不是什么原则。）

**args 要截断**，用 §10.1 同一把尺子（200 字符）。模型认出"自己查的是哪个词"不需要完整的坐标浮点数，而完整 JSON 会把 planner 的历史显著加长（`max_tool_calls` 是 40 轮），抬高撞上 §10.0 那条上下文撑爆路径的概率——那条路径正是本设计新引入归一化去接的。

```python
def _brief(args):
    s = json.dumps(args, ensure_ascii=False, default=str)
    return s if len(s) <= 200 else s[:200] + "…"

calls_text = "\n".join(
    f"(调用工具) {c.name}({_brief(c.args)})" for c in resp.tool_calls
)
content = "\n".join(x for x in (resp.text, calls_text) if x) or "(tool_use)"
messages.append({"role": "assistant", "content": content})
```

**代价二：修复轮可能产出空 content，而 Anthropic 不接受空 content。**

`runner.py:159` 原样 `messages.append({"role": "assistant", "content": resp.text})`。经 §10 的 `content=None → ""` 归一化、以及 §12 的"length 不抛异常、走修复轮"之后，`content=""` 会被塞进下一次请求。OpenAI 接受空串；**Anthropic API 返回 400**（空 content 是 invalid_request）→ `APIError` → `ProviderError` → 候选线 `FAILED`。这正是本设计承诺要消灭的 provider 分歧。

**补偿二（修复轮）**：`runner.py:159` 改为 `content=resp.text.strip() or "(空回复)"`。

用 `.strip()` 而不是裸 `or`：`"   "` 是 truthy，`or` 兜不住。Anthropic 对**纯空白** text block 是否同样 400，本次**未离线实测**（需要真实请求），依据是其 API 对空 content 的既有拒绝行为；但即便它恰好接受，`.strip()` 也只是让 `"(空回复)"` 这个占位更准确，没有代价。概率低（模型只吐空白），按最坏情况处理。

### 3.2 `runner.py` 的完整改动清单（三处）

第 3 稿在此与 §13 自相矛盾（一处说"有且只有两处"，一处说"runner.py 之外的第三处"，而 `run_agent` 就在 `runner.py:115-162`）。完整清单：

| # | 位置 | 改动 | 性质 |
|---|---|---|---|
| 1 | `runner.py:136` | 补偿一：拍平工具调用 | 单行替换 |
| 2 | `runner.py:159` | 补偿二：`resp.text.strip() or "(空回复)"`（**`.strip()` 不能省**，理由见 §3.1） | 单行替换 |
| 3 | `runner.py:128-162` | §13 的诊断：把整个 `while` 包进 try/except 并重抛 | **结构性** |

第 3 处不是"记一个标志"那么轻：`LimitExceeded` 有三个抛出点——`runner.py:129` 与 `runner.py:135` 的 `ctx.check()`，以及 `runner.py:158` 的 `raise`。要在消息上追加，必须包住整个循环。这一点必须写进计划，否则实施者会低估它。

（核验过：`tests/agents/test_runner.py` 里没有针对 assistant 轮文本的**正向**断言，但 `test_runner.py:215` 有一条 `assert not any("错误" in str(m) for m in last_messages)`——它覆盖**全部** message，包含 assistant 轮。当前 fixture 的 args 是 `{"x": 1}`，拍平后不含"错误"二字，所以前两处改动仍不会打破它；但这条断言的存在意味着"assistant 轮文本随便改"是不成立的，将来改动拍平格式时要一并看它。）

**改动 1 有一个必须写明的副作用**：拍平后 planner 的历史会加长——每轮工具调用的 args（已按 §3.1 截断到 200 字符）又抄了一遍，而 `max_tool_calls` 是 40。这抬高了撞上 §10.0 那条"上下文窗口撑爆"路径的概率。不因此放弃补偿一（代价二那个"模型不知道自己查了什么"的问题更贵），但 §10.0 对 `model_context_window_exceeded` 的归一化因此从"边角情形"升级为"本设计自己抬高了概率的情形"，两者要一起做。

### 3.3 `cli.py` 的完整改动清单（七处）

`runner.py` 的改动列了表，`cli.py` 的却散在六个小节里，容易被低估（§10.3 那句"这是 §11 之外 `cli.py` 的唯一新增"尤其容易让人以为只剩两处）。汇总：

| # | 出处 | 改动 |
|---|---|---|
| 1 | §6.4 | 删除 `cli.py:250` 那道只认 `ANTHROPIC_API_KEY` 的检查 |
| 2 | §4.2 | `TRIPPLAN_CONFIG` / `TRIPPLAN_ROLES` 优先级 + 双设时的 stderr 提示 |
| 3 | §8 | 重新导出 `MissingCredential`（保 `tests/test_cli.py:8` 与 `cli.py:380`） |
| 4 | §8.1 | `build_deps` 里 `AnthropicClient(...)` → `RoutingClient(...)` + 加载期构造 |
| 5 | §11 | `main()` 的 except 元组加 `ConfigError` |
| 6 | §10.3 | `main()` 开头按 `TRIPPLAN_LOG` 调 `logging.basicConfig` |
| 7 | §6.2 | 给 `resume` 补 dry-run：argparse 注册 **+** `_cmd_resume` 的早返回，**两处**。只加 argparse 会产出 `AttributeError` 裸 traceback（§6.2 有实跑记录） |

只有 `PLANNER` 传工具（`steps.py:282`），其余三个角色都是 `tools=None`（`steps.py:194/220/320/370`）。但角色可自由指向任何 model，所以 OpenAI 适配器仍必须完整支持工具。

## 4. 配置格式

`[roles.X]` 的外形不变，变的是 `model` 的语义：从模型 ID 字面量变成 model 引用名。

```toml
[models.gpt5]
provider = "openai"
name     = "${TRIP_GPT_MODEL:-gpt-5}"
base_url = "https://gateway.example.com/v1"
key      = "${OPENAI_API_KEY}"

[roles.critic]
model = "gpt5"        # 只改 critic，其余三个角色继承内置默认
```

| 段 | 字段 | 必填 | 说明 |
|---|---|---|---|
| `models.*` | `provider` | 是 | `anthropic` 或 `openai`，其它值报错。**不展开**（§5） |
| `models.*` | `name` | 是 | 真实模型 ID，发给 API 的那个 |
| `models.*` | `base_url` | 否 | 空 = 用 SDK 默认端点。**两家的后缀语义不同**：anthropic SDK 在其后追加 `/v1/messages`，openai SDK 追加 `/chat/completions`（所以 openai 的 base_url 要自带 `/v1`，示例里那个 `…/v1` 不是笔误）。把同一个网关地址原样复制进 `provider = "anthropic"` 的块会得到 404 → `ProviderError`。不做归一化（那要维护端点知识），但**加载期给一条形态提示**：`provider="anthropic"` 而 `base_url` 以 `/v1` 结尾时，经 §10.3 的 logger 记一条 debug。这与本设计通篇"把诊断提前到加载期"的方向一致，成本一行；请求期 404 的诊断离真因太远 |
| `models.*` | `key` | 否 | 空 = 交给 SDK 自行解析凭据（§6） |
| `roles.*` | `model` | 否 | `models` 段里的键名。缺省则沿用该角色的默认 |
| `roles.*` | `max_tokens` | 否 | 属于角色而非 model：同一个 model 会被多个角色复用，planner 要 16000、angle 只要 2000。缺省沿用默认 |
| `roles.critic` | `allow_same_model` | 否 | 默认 `false`，见 §8.2。**仅 `critic` 段接受**，写在其它角色下按未知字段报错 |

未列出的字段一律报错。**但两段的实现手法不同**：`roles.*` 大体可沿用 `config.py:48` 由 `replace(**overrides)` 施加的既有行为（实测：写 `mdoel` 会得到「角色 critic 的配置字段无效」）——注意 `replace()` 只能挡住**不存在的**字段，挡不住 `[roles.planner] allow_same_model = true` 这种"字段存在但不该出现在这个角色下"的情形，那一条由 §5 step 3 的显式校验负责；`models.*` **必须走显式白名单**，不能用同一手法——`ModelSpec` 带 `name_source` / `key_source` 两个内部字段（§8），用户不该能写它们，`**overrides` 会让他们写得进去。这一条顺带裁决了 `independent_context`：**新格式不接受它**，旧配置带这一行会得到「roles.critic 含未知字段：independent_context」。见 §9。

### 4.1 合并语义（裁决第 2 轮评审的阻断项）

第 2 稿在这一点上自相矛盾：§4 的示例只写两个角色（读起来是合并），§5 却要求校验"roles 非空"、§7 说"不提供配置文件时才用默认"（读起来是整份替换）。

**裁决为合并**，与今天的行为一致：

- `config.py:37` 就是 `cfg = dict(DEFAULT_ROLES)` 再按名 `replace()`；`tests/llm/test_config.py:24-29` 明确钉死了「只覆盖被点名的角色，其余保持默认」。改成替换语义是无谓的破坏性变更。
- `roles` 段可以为空或整段缺失，等价于全用默认。§5 的校验步骤里**没有**"roles 非空"这一条。
- `roles.*.model` 与 `roles.*.max_tokens` 因此都是**可选**的（`replace()` 只覆盖给出的字段）。第 2 稿把 `max_tokens` 标为必填是一次未被承认的破坏性变更，已撤销。
- `models` 段**同样合并**：内置默认提供 `opus` / `sonnet` / `haiku` 三个条目（§7），用户定义的同名条目**整条替换**内置条目（不做字段级合并——`[models.opus] provider="openai"` 却继承内置的 `name="claude-opus-5"` 只会制造困惑）。
- 因此，只写 `[roles.critic] model = "gpt5"` 而不定义 `[models.gpt5]`，会得到「未知的 model 引用：gpt5」。

若整份替换才是想要的语义，那是一个独立的需求（例如加一个 `[options] inherit_defaults = false`），本次不做。

### 4.2 配置入口

优先级自高至低：

1. `TRIPPLAN_CONFIG` 指向的 TOML 路径；
2. `TRIPPLAN_ROLES` 指向的 TOML 路径（旧名，保留兼容）；
3. 纯内置默认（§7）。

两者同时设置时 `TRIPPLAN_CONFIG` 胜出，并向 stderr 输出一行提示，避免"我改了文件却没生效"。`tests/test_cli.py:100` 的环境隔离 fixture 必须把 `TRIPPLAN_CONFIG` 加进清理清单（当前清单是 `AMAP_KEY/ANTHROPIC_API_KEY/TRIPPLAN_ROLES/TRIPPLAN_CACHE`），否则开发机上 export 过它就会污染整个 `test_cli.py`。

旧配置写 `model = "claude-sonnet-5"` 会得到「未知的 model 引用：claude-sonnet-5」，错误信息自身说明了迁移方式。

## 5. 变量展开

只识别两个形状：`${NAME}` 与 `${NAME:-默认值}`。其它含 `$` 的文本一律按字面量处理，不提供转义机制。

**作用域：仅 `models.*` 段的 `name` / `base_url` / `key` 三个字段。**

分界线是「**这个值是否会原样出现在对外请求里**」：`name`、`base_url`、`key` 会，展开天经地义；`provider` 与 `roles.model` 是纯内部枚举/引用，展开只会让错误信息里的值与用户在文件里看到的 `${X}` 对不上。`roles.max_tokens` 是 TOML 整数，无从展开。

`${VAR:-}`（空默认值）合法，语义是"显式留空"。

**默认值的解析规则**：从 `:-` 起取到**第一个** `}` 为止，**不支持嵌套**。因此 `${A:-${B}}` 与 `${A:-{"x":1}}` 都不按预期工作——前者取到 `${B` 为止，后者取到 `{"x":1` 为止。这两种写法在 key/url/模型名里不存在，不为它们引入转义或递归展开；但必须写明，否则实现者会各自发明不同的规则。

**加载顺序钉死如下**，避免实现者自由发挥导致错误信息不一致：

1. 读取并解析 TOML。**捕获 `OSError` 与 `ValueError` 两族**转成 `ConfigError`：前者覆盖 `FileNotFoundError`（路径不存在）、`IsADirectoryError`（指向目录）、`PermissionError`（无读权限）；后者覆盖 `TOMLDecodeError`（是 `ValueError` 子类）与 `UnicodeDecodeError`（同样是 `ValueError` 子类，文件非 UTF-8）。只点名 `FileNotFoundError` 与 `TOMLDecodeError` 会漏掉其余四种，全部逃成裸 traceback。
2. 合并：`models` 与 `roles` 各自按 §4.1 与内置默认合并。
3. 校验结构：未知角色名、未知字段、`allow_same_model` 的位置；**`models` 与 `roles` 两个顶层段本身必须是 table**（`models = "x"` 会让 step 2 的 `.items()` 抛 `AttributeError`）；**每个 `models.*` 必须是 table、其字段值必须是字符串**；**`roles.*.max_tokens` 必须是整数**（写成 `"16000"` 会一路送到 API 变成 400 → `ProviderError`，那时诊断已经离真因很远）；**`allow_same_model` 必须是布尔**（TOML 里 `allow_same_model = "false"` 是**字符串**、真值为 `True`，会把 §8.2 那道安全阀静默关掉——正是 §8.2 自己立论要防的"无声关闭"）（`[models] gpt5 = "x"` 会让 step 2 的 `.items()` 抛 `AttributeError`；`name = 5` 会让 step 6 的展开在字符串操作上抛 `TypeError`——两者都不在 §11 的来源清单里，都会裸 traceback）；**`models.*` 的必填字段 `provider` 与 `name` 存在**——缺了会在构造 `ModelSpec` 时得到裸 `TypeError: missing required positional arguments`，而复制粘贴一个 `[models.*]` 块时漏掉 `name` 正是最常见的失手方式。
4. **解析引用**：每个 `roles.*.model` 必须在合并后的 `models` 中存在（失败 → `ConfigError: 未知的 model 引用：<原文>`）。此步**在展开之前**，报错里出现的永远是用户写的原文。
5. 计算被引用到的 model 集合。
6. 对该集合内的 model：校验 `provider` 取值合法（未展开，原样比较）→ 展开 `name`/`base_url`/`key`（`${VAR}` 未定义 → `ConfigError`）。
7. 校验 critic 约束（§8.2）。

**这一步有一处未被承认的行为变化**：今天 `config.py:38-39` 的 `if path is None: return cfg` 早返回，使得 critic≠planner 检查**只在给了配置文件时**才跑；本设计把它变成无条件。对出厂默认无影响（opus vs sonnet 本来就不同），但它是一处真实的语义扩大，记录在此。

**只对被引用到的 model 做第 6 步。** 配置中囤积的备用 model 若引用了未设置的变量或写了非法 provider，不应阻塞启动。

## 6. 凭据：判据与转换

第 1 轮评审推翻重写的一节。原方案（检查 `client.auth_headers` 是否为空）**两个方向都错**，实测记录（anthropic 1.5.0）：

```
情形                    auth_headers                原判据 not auth_headers   真相
api_key=None            {}                          判成缺凭据                 确实缺
api_key=""（空串）       {'X-Api-Key': ''}           放行  ← 假阴性             请求期抛 TypeError
WIF 凭据                 {}                          判成缺凭据 ← 假阳性        SDK 判为有凭据，可正常工作
```

根因：`anthropic/_client.py:213` 用 `api_key is not None` 判断"是否给了显式凭据"，`"" is not None` 为真，于是空串被当成显式凭据、**`_client.py:220` 的整条环境解析链被跳过**；而 `_api_key_auth`（`_client.py:354-358`）对 `""` 返回 `{"X-Api-Key": ""}` 这个非空 dict。另一头，OAuth profile 与 Workload Identity Federation 走 `credentials` + `_token_cache` 在请求时注入，`auth_headers` 恒为空。

### 6.1 两条规则

**规则一：空 key 必须转成 `None` 再交给 SDK。**

```python
client = anthropic.Anthropic(api_key=spec.key or None, base_url=spec.base_url or None)
```

不做这一步，`${ANTHROPIC_API_KEY:-}` 展开出的空串会直接屏蔽 SDK 的整条解析链（`_client.py:185-199` 的 docstring 逐条列出了那 5 档）。

**规则一对 openai 侧同样必需，而且失败得更隐蔽。** openai v1 的构造期判据是 `if api_key is None: raise OpenAIError(...)`，所以 `api_key=""` **不抛**——它会带着一个空 Bearer token 一路走到请求期撞 401，而 401 → `ProviderError` → 候选线 FAILED → `candidates.py:37` 那句「请先修改需求后重试」。anthropic 侧至少在构造期就有信号，openai 侧连信号都没有。（openai 未安装，此条据 v1 公开契约判断，实施时需实测确认。）

**规则二：判据是"SDK 是否解析出了任何一种凭据"，不是 `auth_headers`。**

```python
# anthropic backend
if not (client.api_key or client.auth_token or getattr(client, "credentials", None)):
    raise MissingCredential(...)
```

这与 SDK 自身 `_validate_headers` 的凭据判断等价（`_client.py:396` 在 `_token_cache is not None` 且请求头里没有 X-Api-Key/Authorization 时早返回放行；默认构造路径下 `_token_cache` 非空 ⟺ `credentials` 非空），因此不会误判 OAuth/WIF 用户；空串已在规则一归一成 `None`，也不会假阴性。

**openai 侧的对称判据近乎死代码，真正生效的是构造期捕获。** `openai.OpenAI(api_key=None)` 在 `OPENAI_API_KEY` 未设置时**构造即抛** `OpenAIError`，能活着走到 `if not client.api_key` 的 client 必然已有 key。所以 §6.1 要求的人话消息必须挂在 §10.2 的构造期捕获通道上，不能指望那条 `if`。

### 6.2 错误消息的契约

现有三条测试钉死了两件事（`tests/test_cli.py:364-365` 断言消息里含 `"ANTHROPIC_API_KEY"` 与 `"--dry-run"`；`cli.py:238-242` 与 `cli.py:251-255` 是同一套规矩）。新消息**必须保住这两件事**，否则这个项目一路坚持的「给一条读得懂的话 + 一条出路」会在最容易撞见的错误上悄悄退化。

消息模板：

```
缺少凭据：角色 critic 使用的 model「gpt5」（provider=openai）没有可用的 API key。
请先执行 `export OPENAI_API_KEY=你的key` 再运行；
如果只是想在没有凭据的情况下试跑工具，加 --dry-run。
```

即：**角色名 + model 名 + provider + 该 export 的具体变量名 + `--dry-run` 出路**。变量名按 provider 固定映射（anthropic → `ANTHROPIC_API_KEY`，openai → `OPENAI_API_KEY`）；若用户在配置里写的是 `${SOME_OTHER_VAR:-}`，则报 `key_source` 里的那个变量名（无 `:-` 的写法走 §6.3 第一行的 `ConfigError`，到不了这里）。

**`--dry-run` 这条出路今天只对 `trip plan` 成立**：`resume` 子命令没有这个 flag（`cli.py:353-355` 只注册了 `dir`）。

**裁决：给 `resume` 补上 dry-run，消息统一附上这半句。但这是两处改动，不是一行。**

第 7 稿写成"一行 `r.add_argument(...)`，语义与 plan 现成一致（`cli.py:230` 的早返回已覆盖 resume 路径）"——**这个理由是错的，实测已证伪**。`cli.py:230` 覆盖的只是 `Deps` 的构造；`plan` 的 dry-run 语义真正来自 `_cmd_plan` 自己在 `cli.py:285-287` 的 `if args.dry_run: return 0`，而 `_cmd_resume`（`cli.py:290-301`）**没有对应的早返回**，它无条件走到 `_drive_and_report`。只加 argparse 一行的实跑结果：

```
plan   --dry-run -> 0     （只建目录）
resume --dry-run -> AttributeError: 'NoneType' object has no attribute 'chat'
```

`Deps(client=None)` 被交给 `advance`，COLLECT 阶段的 LLM 步骤不在 `run_slot` 的兜底内，`AttributeError` 一路裸奔，且不在 `main()` 的 except 元组里。那正好把本设计通篇要消灭的东西——裸 traceback——种在了一条由 §6.2 的错误消息亲自指过去的路径上，比今天 argparse 干净拒绝（exit 2）更糟。

**正确改法是两处**：

1. `r.add_argument("--dry-run", action="store_true", help="只载入并报告状态，不推进")`；
2. `_cmd_resume` 在 `print(f"已载入 rev …")` 之后加同构早返回 `if args.dry_run: return 0`。

resume 的 dry-run 语义是"只载入并报告当前状态，不调 LLM 与高德"，与 plan 的"只建目录"对称。计入 §3.3 的 `cli.py` 清单第 7 项。

### 6.3 三档语义

| 写法 | 环境变量状态 | 结果 |
|---|---|---|
| `${OPENAI_API_KEY}` | 未设置 | **`ConfigError`**，加载时抛出。用户显式引用了不存在的变量，多半是拼写错误 |
| `${OPENAI_API_KEY}` | 设置了但为空串 | 判据是 `os.environ.get(name) is None`，**不是真值判断**——所以这算"已定义"，展开为 `""`，走下一行。理由：`export X=` 是用户显式表达"我知道它，但留空"，与"拼写错了"是两回事 |
| `${ANTHROPIC_API_KEY:-}` | 未设置 | 展开为 `""` → 经规则一变成 `None` → **合法**，含义是"交给 SDK 自行解析凭据" |
| 上述之后 | SDK 也解析不出 | **`MissingCredential`**（规则二），消息按 §6.2 |

**加载期只校验凭据的"存在性"，不校验"有效性"。** key 写错（401）在加载期查不出来——校验有效性意味着在启动时对每个 backend 发一次真实请求，那是另一种代价。这一点必须写明，免得"加载期检查过了"给人虚假的安心。

**但 401 的诊断本次要修。** 运行期它是 `AuthenticationError` → 被归进 `ProviderError` → 三条候选全 `FAILED` → `candidates.py:37` 打印「候选全部生成失败，没有可选的方案——**请先修改需求后重试**」：一句与真实原因毫无关系、还会把用户引向错误动作（去改需求）的诊断。

第 7 稿以"这是既有行为"为由不修，**但这个理由在本稿里已经不再一致地成立**：§10.0.1 刚刚以"改动中途接触到的真实缺陷"为由把 `candidates.py:23` 纳入了范围，而 401 这条链同样被本设计撑大了——现在有两家 SDK、两套 key、两个网关能产生它。

**修法：改消息内容，绝不改异常类型。**

两个 backend 都把 `AuthenticationError` 单拎出来（它是 `APIStatusError` → `APIError` → 厂商基类的子类，必须排在 `except anthropic.AnthropicError` / `except openai.OpenAIError` **之前**，否则永远走不到），但**仍然转成 `ProviderError`**，只是把消息换成 §6.2 的契约——角色名 + model 名 + provider + 该检查的变量名，措辞用「凭据被拒绝（401）」而非「缺少凭据」。

**为什么不能转成 `MissingCredential`**（第 8 稿写成了那样，是错的，实测证伪）：

`run_slot` 只捕获 `LimitExceeded`（`slot.py:76`）与 `ProviderError`（`slot.py:86`）。请求期抛出的 `MissingCredential` 两个都不是，会落到 `orchestrator.py:254` 的 `_safe_slot` 兜底，而那里的构造是：

```python
except Exception as e:  # noqa: BLE001
    return CandidateSlot(angle, None, None, SlotStatus.FAILED,
                         f"候选线出现未处理异常：{type(e).__name__}: {e}")
```

第 2、3 个位置参数**硬编码为 `None`**——已经生成好的 `itin` 与 `facts` 被直接丢弃。于是 critic 轮 401（正是本设计造出来的头号场景：planner 走 anthropic、critic 走 openai、openai key 过期）会：

1. 丢掉三份**已经生成好**的行程（走 `ProviderError` 时 `slot.py:86-89` 会把 `itin`/`facts` 原样带出）；
2. 三条线 `itinerary is None` → `candidates.py:37` **照旧**打印「候选全部生成失败……请先修改需求后重试」——正是本节开头声称要消灭的那句；
3. 好不容易写对的 §6.2 消息被包进「候选线出现未处理异常：」这个壳里，而这个字符串正是 §8.1、§10、§10.0.1 三处反复点名的"无用诊断"；
4. 顺带打掉 §10.0.1 的立论前提（"主体已生成的行程，用户看到 ⚠️ 之后仍可选它"）——主体已经被 `_safe_slot` 扔了。

§8.1 早就论证过同一条链（「那时抛出的 `MissingCredential` 会被 `orchestrator.py:254` 的 `except Exception` 兜底吞掉……`cli.py:380` 的 `except MissingCredential` 永远等不到它」）。第 8 稿在 §6.3 里制造了完全相同的情形而没有察觉。

**规律**：`MissingCredential` 只在**加载期**（§8.1）安全，那时还没进 slot；**请求期的一切**——包括 401——必须走 `ProviderError`，因为只有这个类型能让 `slot.py:86-89` 保住已生成的行程。类型决定数据能不能活下来，消息决定用户能不能看懂，两者不要混为一谈。

### 6.4 为什么必须在加载/构造期失败

`anthropic.Anthropic(api_key=None)` 构造时不校验，直到 `messages.create` 才在 `_validate_headers` 抛 `TypeError`；该异常既不是 `anthropic.APIError`（`client.py:110` 的 except 接不住），也不是 `ProviderError`（`cli.py` 的 except 同样接不住），最终变成一条 SDK 内部 traceback 糊到用户脸上。`cli.py:250` 那道只认 `ANTHROPIC_API_KEY` 的检查随之删除——它正是 §6 开头那张表里"假阳性"一行的来源。

## 7. 内置默认配置

```toml
[models.opus]
provider = "anthropic"
name     = "claude-opus-5"
key      = "${ANTHROPIC_API_KEY:-}"

[models.sonnet]
provider = "anthropic"
name     = "claude-sonnet-5"
key      = "${ANTHROPIC_API_KEY:-}"

[models.haiku]
provider = "anthropic"
name     = "claude-haiku-4-5"
key      = "${ANTHROPIC_API_KEY:-}"

[roles.planner]
model = "opus"
max_tokens = 16000
[roles.critic]
model = "sonnet"
max_tokens = 4000
[roles.angle]
model = "sonnet"
max_tokens = 2000
[roles.classifier]
model = "haiku"
max_tokens = 1000
```

`${ANTHROPIC_API_KEY:-}` 配合 §6.1 规则一，在新机器上展开为 `""` → `None` → SDK 尝试 `ANTHROPIC_AUTH_TOKEN` / profile / WIF → 都没有则由规则二给出 §6.2 的可读错误。与现状行为一致，不构成破坏性变更。

## 8. 组件

```
src/tripplan/llm/
├── client.py       # Protocol / Usage / ToolCall / LlmResponse / FakeLlm
├── config.py       # ModelSpec / RoleConfig / LlmConfig / expand() / load_config()
├── errors.py       # ConfigError / MissingCredential
├── router.py       # RoutingClient：按角色分派
└── backends/
    ├── anthropic.py
    └── openai.py
```

`MissingCredential` 从 `cli.py:43` 下沉到 `llm/errors.py`——让 `llm/backends/*` 去 import `cli` 是层次倒置。`cli.py` 重新导出它，`tests/test_cli.py:8` 的导入路径与 `cli.py:380` 的 `except` 都仍指向同一个类对象。

```python
@dataclass(frozen=True)
class ModelSpec:
    provider: str        # "anthropic" | "openai"
    name: str            # 展开后的真实模型 ID
    base_url: str        # 展开后；"" = SDK 默认
    key: str             # 展开后；"" = 交给 SDK 解析
    # 用户在 TOML 里写的原文，仅用于错误消息。展开前后相同时与上面一致
    # ——注意 key_source 因此可能就是明文密钥本身（用户写字面量时），
    # 输出前必须按 §10.3 的规则判形态，不可无条件取用。
    name_source: str
    key_source: str

@dataclass(frozen=True)
class RoleConfig:
    model: str           # ModelSpec 的键名
    max_tokens: int
    allow_same_model: bool = False   # 仅 critic 有意义

@dataclass(frozen=True)
class LlmConfig:
    models: dict[str, ModelSpec]
    roles: dict[Role, RoleConfig]
```

`name_source` / `key_source` 不是冗余：§6.2 要求凭据错误里报出用户写的那个变量名，而这条错误在 §8.1 的**加载期 backend 构造**时抛出，那时 `load_config` 已经返回，原文只存在于 `ModelSpec` 里。§8.2 的「原文与展开值都要打出来」同理。不留这两个字段，两条契约都只能悄悄降级成"只报展开值"。`base_url` 不需要原文——它不出现在任何错误消息里。

### 8.1 构造时机：加载期，全部构造

`load_config()` 成功后，立即为**每一个被角色引用到的 model** 构造 backend 并执行 §6.1 规则二的凭据校验。

理由：按需构造时，critic 那个 backend 要等第一次 critic 调用才构造，此时 planner 的 16000 token 已经花掉；更要命的是那时抛出的 `MissingCredential` 会被 `orchestrator.py:254` 的 `except Exception` 兜底吞掉，变成 `CandidateSlot(..., FAILED, "候选线出现未处理异常：...")`（`orchestrator.py:260`），而 `cli.py:380` 的 `except MissingCredential` 永远等不到它。

**每个 backend 的构造流程钉死为三步**，让"缺凭据"与"其它构造失败"各有归宿，不靠从 SDK 异常里猜：

1. **import**。`ImportError` → `ConfigError`，消息指明 `uv pip install 'tripplan[openai]'`。（第 4 稿把这条写在正文里却没进 §11 的清单、也没定类型——"provider 写了 openai 但没装包"是新用户用这个特性最可能撞到的一条路径，不能靠运气收口。）
2. **凭据**。openai 侧**先判空再构造**：`spec.key` 与 `OPENAI_API_KEY` 都空 → 直接 `MissingCredential`（因为 `openai.OpenAI(api_key=None)` 构造即抛，抢在它之前判才能给出 §6.2 的人话消息）。anthropic 侧构造后按 §6.1 规则二判 → `MissingCredential`。
3. **其余构造期异常**（`AnthropicError` / `OpenAIError`）→ `ConfigError`。

**step 3 抛出的 `ConfigError` 必须套用 §6.2 的同一套消息契约**，不能把 SDK 的英文原文直接抛给用户。这一档不是理论情形：用户只要 export 过 `ANTHROPIC_CONFIG_DIR`（或家目录里有个指向坏 profile 的 `active_config`）却没有 key，构造期就会抛 `CredentialsError`（实测），而它本质上**就是缺凭据**——却因为发生在构造期而被 step 3 扫进 `ConfigError`。若消息直接透传，§6.2 承诺必须保住的"变量名 + `--dry-run`"两件事在这一档全部丢失。

所以：step 2 与 step 3 的异常类型可以不同（一个 `MissingCredential`、一个 `ConfigError`，各自有明确归宿），但**用户看到的消息格式必须一致**——角色名 + model 名 + provider + 该 export 的变量名 + `--dry-run`，SDK 原文附在末尾作为技术细节。类型的区分是给 `main()` 收口用的，不是给用户看的。

（措辞说明：anthropic 侧规则二必须构造之后才能判，所以"构造"这个动作横跨 step 2 与 step 3——step 2 是"凭据判定"，step 3 是"构造本身失败"的兜底，两者在时间上交织，不是严格串行。）

**凭据校验按 `(role, model)` 逐对做，不是按 backend 做。** §8.1 的缓存键是 `(provider, base_url, key)`，§7 的四个默认角色共享同一个 backend，而 §6.2 承诺报出"角色名 + model 名"——若按 backend 校验，一个 backend 对应四个 (role, model) 对，报哪个都是任选。遍历 `roles` 逐对校验、报当前这一对，消息才准确；backend 本身仍然复用。

**backend 缓存键是 `(provider, base_url, key)`，不是 model 引用名。** §7 的默认配置里 opus/sonnet/haiku 三个 model 同 provider、同端点、同 key，按 model 名缓存会开三个 `anthropic.Anthropic`、三份 httpx 连接池且从不关闭。`name` 不进缓存键——它是每次请求的参数，不是客户端的属性。

`build_deps(dry_run=True)` 走 `cli.py:230` 的早返回，不触发加载期构造（`tests/test_cli.py:332-335` 已钉住该行为）。

`RoutingClient` 实现现有的 `LlmClient` Protocol，`chat` 保持**位置参数**顺序 `(role, system, messages, tools)`。它按 `chat(role, ...)` 查出 `RoleConfig` → `ModelSpec` → 分派到对应 backend，并把角色的 `max_tokens` **与 `role`、model 引用名**一起传下去。

后两个不能省：§6.2 要求请求期 401 的消息含"角色名 + model 名"，而 backend 按 `(provider, base_url, key)` 缓存、跨角色共享，自己无从得知这次是替谁发的请求。这不是 Protocol 变更——`RoutingClient` → backend 是内部接口，`max_tokens` 已经走的同一条通道。（对照 §13：那里 `max_tokens` 的**数值**拿不到是因为要跨 `LlmClient` Protocol，性质不同。）

### 8.2 critic ≠ planner

现有检查（`config.py:52-56`）比的是**真实模型 ID**，注释写明理由："同源自审会放过同一个盲点"。这是行为约束，不是命名约束。

**判据：展开后的 `(provider, name)` 二元组。** `base_url` 不参与——同一个模型放在不同网关后面，盲点不变。

只比 model 引用名是不够的：用户复制一个 `[models.*]` 块通常是为了换 base_url / 换 key（多网关、灰度、区域），不会意识到自己顺手关掉了这道安全阀。能被复制粘贴无声关闭的安全阀只提供虚假的保障感。

两个细节：

- 判据落在**展开后**的 `name` 上，而 `name` 可以是 `${TRIP_GPT_MODEL:-gpt-5}`，同一份配置在不同机器上会一会儿触发一会儿不触发。这与 §5 step 4「报错里出现的永远是原文」有张力，所以**报错时原文与展开值都要打出来**：「critic 与 planner 指向同一个模型：openai/gpt-5（critic 的 name 原文为 `${TRIP_GPT_MODEL:-gpt-5}`）」。
- `allow_same_model` **只在 `[roles.critic]` 段接受**（§4 表格）。放进通用字段会让写在 `[roles.planner]` 下的那份被静默忽略，与同一节"未知字段一律报错"的严格程度不对称。

真要绕过，走 `[roles.critic] allow_same_model = true`，让"用户的显式选择"名副其实。

## 9. `independent_context` 的处置

该字段在 `config.py:21` 定义、`config.py:29` 为 critic 设为 `True`，但**没有任何生产代码读取它**（全仓命中恰为三处：定义、赋值、`tests/llm/test_config.py:15-17` 断言常量值——典型的 change detector，没有任何行为依赖它）。

处置：从 `RoleConfig` 中**移除**该字段，连同那条 change-detector 测试一并删除。在 §4「未知字段一律报错」的前提下，保留一个既不可配置、也无人读取的字段只会误导后来人。若将来 critic 真需要"只喂最终产物"的语义，那是一次带实现、带测试的独立改动。

## 10. OpenAI 侧归一化

| 方向 | 映射 |
|---|---|
| system | 并入 `messages[0]` 的 `{"role": "system", "content": system}`。**不得原地修改传入的 `messages`**——`run_agent` 每轮复用同一个 list（`runner.py:125` 建、`:136/150/159/161` 追加），`messages.insert(0, ...)` 会逐轮累积 system 消息。必须新建列表 |
| tools | `{name, description, input_schema}` → `{"type": "function", "function": {name, description, parameters: input_schema}}`。**`tools` 为空时整个字段不放进请求**，不能发 `"tools": null`。四个角色里三个传 `tools=None`（`steps.py:194/220/320/370`），现有 anthropic 实现靠 `client.py:106` 的 `if tools:` 守住，openai 侧必须对称。§4 示例里的 critic 正好是 `tools=None` 的角色，这条漏了就砸在主路径上 |
| 输出上限 | 发送 `max_completion_tokens`（新模型拒收 `max_tokens`）。见 §12 |
| **是否工具轮** | 判据是 `message.tool_calls` **非空**，不是 `finish_reason`。多家网关（以及 OpenAI 在工具调用中途被 `length` 截断时）会在带 tool_calls 的响应上给出 `finish_reason="stop"`/`"length"`；按 finish_reason 判会让这类响应被当成普通文本轮，`tool_calls` 非空却无人执行，模型空转至 `max_tool_calls` 或 deadline |
| stop_reason | `tool_calls` 非空 → `"tool_use"`；否则 `finish_reason=="length"` → `"max_tokens"`；其余一律 `"end_turn"`。`runner.py:133` 只比 `"tool_use"`，无需改动 |
| **text** | `message.content` 为 `None` 时归一成 `""`。OpenAI 推理模型在工具调用轮、以及 refusal 时 `content` 就是 `None`，而 `LlmResponse.text` 的类型是 `str`（`client.py:33`）。不归一则 `runner.py:159` 会把 `None` 塞进 messages，`runner.py:154` → `_load_json` → `text.strip()` 抛 `AttributeError`——既不是 `ProviderError` 也不是 `LimitExceeded`，只会被 `orchestrator.py:254` 吞成"候选线出现未处理异常" |
| **choices 为空列表** | 部分网关在内容过滤/上游错误时返回 `choices: []`，直接下标会 `IndexError`。归一成 `ProviderError("上游返回了空的 choices")` |
| **usage 缺失** | 部分网关省略 `usage`，`resp.usage.prompt_tokens` 会 `AttributeError`。缺失时归一成 `Usage(0, 0)` 并记一条 warning——计量失真好过整条候选线以无用诊断挂掉 |
| **`finish_reason == "content_filter"`** | 归一成 `ProviderError("上游内容过滤拦截了本次生成")`。若按兜底当成 `"end_turn"`，`content` 多半是 `None` → `""` → `_load_json("")` → SchemaError → 修复轮 → `LimitExceeded("schema 修复 2 次仍失败")`，又是一条与真因无关的诊断，且它工具轮/非工具轮都可能出现，不在 §13 的补救范围内 |
| **`message.refusal` 非空** | 归一成 `ProviderError(f"模型拒绝了本次请求：{refusal}")`。这是两家都有、而前几稿都漏掉的一处语义差异（Anthropic 侧对应 `stop_reason="refusal"`，同样归一）。漏掉它的后果比 `content_filter` 更隐蔽：refusal 时 `content` 是 `None` → `""`，走完修复轮后抛 `LimitExceeded`，而这条链**恰好满足 §13 修正后的全部三个条件**（本次调用、至少一轮、所有非工具轮 text 为空），于是用户会看到"推理预算可能被吃光"——而真实原因就明摆在没人读的 `message.refusal` 里 |
| 工具参数 | `function.arguments` 是 JSON **字符串**，需 `json.loads`。空串（部分网关对无参调用返回 `""` 而非 `"{}"`）按 `{}` 处理。解析失败见 §10.1 |
| usage | `prompt_tokens` / `completion_tokens` → `Usage(input_tokens, output_tokens)`。见 §14 |
| 错误 | 见 §10.2 |

### 10.0 Anthropic 的 `stop_reason` 必须按完整枚举处理

前几稿把 Anthropic 的 `stop_reason` 当成只有四个值。实测完整枚举有**七个**：

```
('end_turn', 'max_tokens', 'stop_sequence', 'tool_use', 'pause_turn', 'refusal', 'model_context_window_exceeded')
```

`refusal` 已在上表处理。剩下两个：

- **`model_context_window_exceeded` → `ProviderError("上下文窗口已超出：对话历史太长")`。** 这是一处**未被调和的 provider 语义差异**：同一件事，OpenAI 侧是 `BadRequestError(context_length_exceeded)` → `APIError` → `ProviderError`；Anthropic 侧却是 **HTTP 200 + 一个特殊 stop_reason**。不归一的后果是：`runner.py:133` 判它不是 `tool_use` → 去解析截断或空的 `resp.text` → `SchemaError` → 进修复轮，而**修复轮会把消息历史再加长**，第二次必然再撞 → `LimitExceeded("schema 修复 2 次仍失败")`。且这条链同样满足 §13 的三个条件，用户会读到"推理预算可能被吃光"——真因是上下文撑爆，而 planner 的历史正是 §3.1 补偿一刚刚加长过的那一份。§13 的谓词必须排除它。
- **Anthropic 侧 `stop_reason=="max_tokens"` 但 `content` 里带 `tool_use` block** → 仍按工具轮处理。这是 OpenAI 那条"判工具轮看 `tool_calls` 非空、不看 `finish_reason`"在 Anthropic 侧的对称情形，前几稿只写了 OpenAI 一半。两侧统一为：**有工具调用就是工具轮，`stop_reason` 只在没有工具调用时才决定后续分支。**
- **`stop_reason` 可以是 `None`**（`Message.stop_reason` 的标注是 `Optional[Literal[...]]`）。非流式下不可达，落到"其余 → `end_turn`"的兜底即可，不单独分支。
- **`pause_turn` → 当作 `end_turn` 处理。** 它是 Anthropic 为长时服务端工具设计的续跑信号；本项目不使用服务端工具（工具全在 `tools.py` 本地实现），这个值在当前架构下不可达。显式列出并说明，免得后来人以为是漏掉的。

### 10.0.1 `ProviderError` 的落点：一处本设计会撑大的既有漏洞

§10 的标准是「任何会让响应解析抛出非 `ProviderError` 异常的空洞，都必须显式归一」，于是 `choices=[]`、`content_filter`、`refusal`、`model_context_window_exceeded` 四种情形都归到了 `ProviderError`。但 §10.1 已经把 `ProviderError` 的落点查清楚了，两节用的是同一套证据：

`slot.py:86-89` 返回 `CandidateSlot(angle, itin, facts, FAILED, ...)` 且不回写 `itin.issues`；而 `candidates.py:15` 只在 `itinerary is None` 时报警，`candidates.py:23` 的条件是 `if slot.status is SlotStatus.EXHAUSTED and slot.detail`——**`FAILED` 且 `itinerary` 非空时，`detail` 一个字都不显示**，`candidates.py:33` 还把它照常列进可选项。

于是：失败发生在首次 `generate`（`itin is None`）时诊断是诚实的；发生在 **revise / critic 轮**时，用户拿到的是一份看起来完整、可以直接选中、连一个 ⚠️ 都没有的行程。而 refusal 与 content_filter 本质是模型行为，最可能出现的正是 critic 轮（`steps.py:311` 起，`tools=None` 的纯文本点评）——恰好落在被隐藏的那一格。

这是既有漏洞，但**本设计新增了四个会在 revise/critic 轮触发 `ProviderError` 的生产者**，把它显著撑大。不能一边用这个落点论证"参数解析失败不该用 `ProviderError`"（§10.1），一边把四个新情形径直塞进同一个落点还宣称收口闭合。

**纳入本次范围，一行修复**：`candidates.py:23` 的条件改为 `if slot.status in (SlotStatus.EXHAUSTED, SlotStatus.FAILED) and slot.detail:`。

依据是这个模块自己的 docstring（`candidates.py:1`）：「遗留问题是用户挑选方案的重要依据，必须显示」。一个 `FAILED` slot 的 `detail` 就是遗留问题，当前实现与这句话直接冲突。这是改动中途接触到的真实缺陷，不是顺手扩范围。

**只补显示，不改可选性。** `candidates.py:33` 仍会把这类 slot 列进可选项，`orchestrator.py:108` 的 `_check_candidate` 也照样放行——这是**有意保留**的：一份"critic 轮挂了但主体已生成"的行程，用户看到 ⚠️ 之后仍然可以选它，比强行剥夺选择更合理。本次只保证"失败不再无声"，不改"失败是否还能选"。
`choices` / `usage` 这两条与 `content=None` 是同一条标准：任何会让响应解析抛出非 `ProviderError` 异常的空洞，都必须显式归一，否则终点都是 `orchestrator.py:254` 那句无用诊断。

### 10.1 `arguments` 解析失败不抛 `ProviderError`

模型把 `arguments` 生成截断是一次**可自愈的模型失误**，不是外部依赖挂了。

用 `ProviderError` 标记它的真实代价（第 2 稿这里写错了，说是"行程清零"）：`slot.py:87-89` 把 `itin`、`facts` **原样交给** `CandidateSlot`，只是 `status=FAILED` 且不回写 `itin.issues`。而下游 `render/candidates.py:15` 只在 `itinerary is None` 时标「无法选择」、`candidates.py:23` 只在 `EXHAUSTED` 时显示 `detail`（`orchestrator.py:108` 的 `_check_candidate` 同样只看 `itinerary is None`）。所以产出的是一个**看起来完全正常、可被用户选中、而失败原因被静默隐藏**的候选——比"清零"更糟。

`runner.py:143-149` 对工具**执行**失败已有成熟通道：捕获异常 → `results.append(f"[{name}] 错误：{e}")` → 回喂给模型自己改正。参数解析失败走同一条路：backend 在解析失败时产出 `ToolCall(id, name, args={})`，`impl(**{})` 会因缺必填参数抛 `TypeError`，被既有的 `except Exception` 捕获并回喂。

**这条通道依赖一个必须写明的前提：工具的参数必须全部必填。** 现有两个工具满足（`tools.py:15` 的 `search_poi(query)`、`tools.py:34-36` 的 `route_duration(...)` 五个参数全必填）。若将来新增的工具参数全带默认值，`impl(**{})` 会**静默执行一次无意义调用**、把结果当正常结果回喂、白烧一次 `max_tool_calls` 额度、且不留任何痕迹。

**这个前提用注册期校验钉住，不用 docstring 君子协定**：`build_planning_tools` 返回前遍历 `tool_impls`，用 `inspect.signature` 校验。

**谓词是「至少存在一个必填参数（无默认值、且不是 `*args`/`**kwargs`）」。**

要保证的命题只有一条：`impl(**{})` 必抛。一个必填参数就足够了。零参数、纯 `*args`、纯 `**kwargs` 三种情形都让 `impl(**{})` 静默成功——那正是本节要防的真空，必须排除。

第 6 稿写的是"每个参数都无默认值"，**过严了**：它会永久剥夺工具作者写可选参数的自由，未来一个 `search_poi(query, limit=10)` 会在注册期直接报错，而它其实完全满足 `impl(**{})` 必抛。收紧到"至少一个必填"既充分又不越界。 只写后半句会在零参数工具上真空成立——一个未来的 `def list_cities() -> dict` 顺利通过校验，然后 `impl(**{})` 静默执行成功、结果被当正常结果回喂、白烧一次 `max_tool_calls`，**恰好是这一节要防的那个故障**。真正要保证的命题是「`impl(**{})` 必抛」，零参数直接证伪它。

用显式 `raise`，不用 `assert`——`python -O` 会把 assert 整条剥掉，那时校验静默消失。

**诊断精度不必有损。** 把解析失败的原文（截断 200 字符）塞进 `LlmResponse.text`，经 §3.1 补偿一它会自然出现在下一轮的 assistant 文本里，模型于是看到"我上一轮的 arguments 是 `{"query": "京` ——坏了"，而不是只看到"缺少必填参数 query"。零额外机制。原文同时经 §10.3 的日志通道记录一份。

**代价要写明**：这让 `LlmResponse.text` 的契约从"模型的可见输出"扩成"模型输出 + 适配器生成的诊断"，而这段文字会原样进入 assistant 历史——模型会以为那是自己说的话。因此这段注入**必须带固定前缀** `[适配器] `，让它在历史里可辨认，也便于日后需要时过滤掉。

**与非空 `content` 的关系：拼接，不覆盖。** 模型在发起工具调用的同时可能也吐了文本；覆盖会把它弄丢。顺序是先模型文本、后 `[适配器] ...` 一行。

### 10.2 两家的异常层级是**同构**的（修正第 2 稿的错误论断）

第 2 稿称"openai 的异常层级与 Anthropic 相反"，**错误**。实测：

```
anthropic.AnthropicError 存在                              -> True
issubclass(anthropic.APIError, anthropic.AnthropicError)   -> True
CredentialsError        是APIError子类=False  是AnthropicError子类=True
RetryableError          是APIError子类=False  是AnthropicError子类=True
IdentityTokenFileError  是APIError子类=False  是AnthropicError子类=True
```

两家都是「`<Vendor>Error` 是基类、`APIError` 是其子类」。真正的推论是**对称的**，而且比第 2 稿写的更重要：

| 时机 | anthropic backend | openai backend |
|---|---|---|
| 请求期 | `except anthropic.AnthropicError` → `ProviderError` | `except openai.OpenAIError` → `ProviderError` |
| 构造期 | `except anthropic.AnthropicError` → `ConfigError` | `except openai.OpenAIError` → `ConfigError` |

**请求期捕的是厂商基类，不是 `APIError`。** 第 8 稿写的是 `APIError`（沿袭 `client.py:110` 的现状），**漏捕**——而这恰好是本设计新开的那条路径：

1. 凭据在**每次请求**时刷新：`lib/credentials/_auth.py:65` 的 `AccessTokenAuth(httpx2.Auth)` 是 httpx auth flow，`auth_flow` 里调 `TokenCache.get_token()`（`_auth.py:109`），刷新失败抛 `CredentialsError` / `RetryableError` / `IdentityTokenFileError`——实测三者**都不是** `APIError` 子类。
2. SDK **明确拒绝包装**它们，`_base_client.py:1296-1302` 原文：
   ```python
   except Exception as err:
       if isinstance(err, AnthropicError):
           # SDK-originated errors already carry their own type; don't wrap.
           raise
       raise APIConnectionError(request=request) from err
   ```
   即 httpx send 期间抛出的 `AnthropicError` 原样穿出，**不会**变成 `APIConnectionError`（那才是 `APIError`）。
3. 这条路径是**本设计新开的**：今天 `cli.py:250` 那道检查让 WIF/OAuth 用户根本走不到 `messages.create`；§6.4 + §3.3 第 1 项要删掉它，§6 规则二又专门论证了"不能误判 OAuth/WIF 用户"。把这批用户放进来，就必须给他们请求期的收口。

不改的后果正是开篇铁律要防的那件事：长跑的 planner（deadline 600s）中途令牌过期刷新失败 → `CredentialsError` 绕过 `ProviderError` → `orchestrator.py:254` 的 `_safe_slot` → 「候选线出现未处理异常」+ **已生成的行程被硬编码成 `None` 丢弃**。触发者从 §6.3 那个 `MissingCredential` 换成了 SDK 自己，后果一模一样。

（文档在下一段其实已经写出了正确结论——"`RetryableError`、`IdentityTokenFileError` 同样不是 `APIError` 子类，`client.py:110` 一个都接不住，catch 基类才是对的"——但前几稿只把"catch 基类"派给了构造期。两个时期都要。）

构造期**一律** `ConfigError`，不再是第 4 稿那个未裁决的「`MissingCredential` / `ConfigError`」二选一。缺凭据这一类由 §8.1 的三步流程在构造**之前/之后**单独判定并抛 `MissingCredential`，不依赖从 SDK 异常里猜。`main()` 是按类型收口的（`cli.py:363-405`），留一个二选一给实施者，落地就是两种类型里挑一种，另一种逃成裸 traceback。

第 2 稿只点名了 `CredentialsError`，但 `RetryableError`、`IdentityTokenFileError` 同样不是 `APIError` 子类，`client.py:110` 一个都接不住。catch 基类才是对的。

（openai 侧未离线核验——包未安装、无网。依据是 openai-python v1 的 `_exceptions.py`：`OpenAIError(Exception)` / `APIError(OpenAIError)`，以及 api_key 缺失时构造期 `raise OpenAIError("The api_key client option must be set...")`。实施时需实测确认。）

### 10.3 诊断输出通道：用 `logging`，不用 `emit`

第 2 稿要求把 `arguments` 原文与 §12 的判别结论「经 `emit` 旁路输出」，但 **backend 拿不到 `emit`**：它只活在 `SlotContext`（`limits.py:30`）上，由 `run_slot(emit=...)`（`slot.py:41`）与 `orchestrator._step_ctx(emit)`（`orchestrator.py:144`）构造，从未传给 client；而 `LlmClient.chat` 的签名是 `(role, system, messages, tools)`（`client.py:39-41`），`Deps` 只有 `client` 与 `provider`（`deps.py:9-12`）。

**裁决：backend 用标准库 `logging`**（模块级 `logger = logging.getLogger(__name__)`），不改 Protocol、不改 `Deps`、不给 backend 注入 emit。改 Protocol 只为了两条诊断信息，代价不成比例。

**但必须真的把开关做出来。** 全仓 `src/` 目前**零处** `logging` 使用、无 `basicConfig`、无任何日志开关（已核验）。不加开关，§12「每次响应都记录 requested 与实际 completion_tokens」在默认 CLI 下永远没人看得见，§12 从"判别"降级为"记录"换来的价值全部归零；§10.1 那份原文记录同理。

因此本次一并落地：`main()` 开头读环境变量 `TRIPPLAN_LOG`（取值 `debug` / `info`，缺省不配置任何 handler），调用 `logging.basicConfig(level=..., stream=sys.stderr)`。这是 §11 之外 `cli.py` 的唯一新增。

**禁止把 `ModelSpec` 整个交给 logger 或 repr。** `ModelSpec.key` 存的是展开后的明文密钥，一句 `logger.debug("spec=%s", spec)` 就会把它打到 stderr。本设计新引入 logging，这条纪律必须同时立：日志里只允许出现 `provider`、`name`，**永不出现 `key`**。

**`key_source` 同样不能无条件放行。** §8 对它的定义是"用户在 TOML 里写的原文，**展开前后相同时与上面一致**"——也就是说用户写字面量 key 时，`key_source` **就是明文密钥本身**。而 §14.1 明确把字面量写法背书为一等配置（无鉴权网关靠 `key = "unused"` 绕过）。所以"`key_source` 是变量名"这个前提只在用户用了 `${...}` 时成立，把它无条件列进白名单，等于给字面量用户开了一条泄漏路径。

**规则**：`key_source` 只有在**能被识别为 `${NAME}` / `${NAME:-…}` 形态**时才可进入日志与错误消息（此时它确实是变量名）；否则一律回落到该 provider 的固定映射变量名（anthropic → `ANTHROPIC_API_KEY`，openai → `OPENAI_API_KEY`）。§6.2 的正文其实已经隐含了这个条件判断，这里把它写死，免得实现成无条件取用。

**级别纪律**：§10 的"usage 缺失"一条第 4 稿写的是 `warning`——在无 handler 时它会经 logging 的 lastResort 打到 stderr，与 `_print_event`（`cli.py:103-104`）的 `  · ` 事件流混排成一段没人规定过格式的输出。**统一降为 `debug`**：本设计引入的全部日志都是排查用的旁路信息，没有一条需要在默认运行时打扰用户。真正需要用户看见的东西一律走异常（`ConfigError` / `MissingCredential` / `ProviderError`）或 `emit`。

### 10.4 懒加载的写法约束

backend 内部写 `import openai` 后使用 `openai.OpenAI(...)`，**不要** `from openai import OpenAI`——后者使 `patch("openai.OpenAI")` 失效。这与现有 `client.py:87` + `tests/llm/test_client.py:124` 的 `patch("anthropic.Anthropic")` 同构。

`openai` 作为可选依赖：`[project.optional-dependencies] openai = ["openai>=1.0"]`，**同时加入 `dev` extra**（当前 `pyproject.toml:8` 只有 pytest/pytest-cov/black），否则 §15 的新测试文件在默认环境下是 collect error 而非 skip。

## 11. 配置错误必须可读

`main()` 的 except 元组（`cli.py:363-405`）只认 `ProviderError` / `LimitExceeded`(365) / `MissingCredential`(380) / `TripNotFound`(383) / `TripCorrupt`(392) / `EOFError`(395)。`load_config` 今天抛的是 `ValueError`，**不在其中**，实测逃逸成裸 traceback：

```
ESCAPED main(): ValueError -> 角色 critic 的配置字段无效：
    RoleConfig.__init__() got an unexpected keyword argument 'mdoel'
ESCAPED main(): TOMLDecodeError -> Expected ']' ...
```

（修正第 2 稿的一处错误：`TOMLDecodeError` **是** `ValueError` 的子类——`MRO: TOMLDecodeError → ValueError → Exception`。它逃逸是因为 `main()` 压根没 catch `ValueError`，不是因为它不是 `ValueError`。`FileNotFoundError` 确实不是。）

本设计把配置错误的产生面放大了一个数量级。**必须全部转成 `ConfigError` 的完整清单**：

1. 未知 model 引用（§5 step 4）
2. 未知 provider（§5 step 6）
3. `${VAR}` 未定义且无默认值（§5 step 6）
4. critic 与 planner 同 `(provider, name)` 且未开 `allow_same_model`（§5 step 7）
5. 未知角色名、未知字段、`allow_same_model` 写在非 critic 段（§5 step 3）
6. **类型与必填**：`models`/`roles` 顶层段不是 table、`models.*` 不是 table、`models.*` 缺 `provider`/`name`、`models.*` 字段值不是字符串、`roles.*.max_tokens` 不是整数（§5 step 3）
7. **读文件的 `OSError` 族**：`FileNotFoundError` / `IsADirectoryError` / `PermissionError`（§5 step 1）
8. **解析的 `ValueError` 族**：`TOMLDecodeError` / `UnicodeDecodeError`（§5 step 1）
9. **`openai` 未安装的 `ImportError`**（§8.1 构造 step 1）
10. **其余构造期异常**：`AnthropicError` / `OpenAIError`（§8.1 构造 step 3）

第 6–8 条是第 3 稿遗漏的，第 9–10 条是第 4 稿遗漏的（当时只写在 §8.1 正文里、没进清单也没定类型）。每一条都能独立逃成裸 traceback。`main()` 按类型收口，清单之外无归宿。

因此：`llm/errors.py` 定义 `ConfigError`，`load_config` 把上述全部情形转成它；`main()` 的 except 元组加上 `ConfigError`，按既有风格打印中文错误并返回非零。这与代码库一路坚持的标准一致（`cli.py:44`、`cli.py:233-242`、`cli.py:366-371`、`cli.py:384-389`）。

## 12. `max_completion_tokens` 的风险

部分网关只接受旧的 `max_tokens` 字段。

**归一化（保留）**：`finish_reason == "length"` 归一成 `stop_reason = "max_tokens"`，与 Anthropic 对齐，**不抛异常**，让 `runner.py:153-162` 的修复轮照常工作。第 1 稿"在 `ProviderError` 消息里附提示"的方案已废弃——它会让一次普通的输出截断在 OpenAI 侧杀死整条候选线，而 Anthropic 侧只是进修复轮，为挂一句提示语引入本设计承诺要消灭的 provider 分歧。

**判别（降级）**：第 2 稿的"减法判别"（比较 `completion_tokens` 与请求上限）有三个问题——"明显小于"没有阈值定义、只在 `finish_reason=="length"` 时才有机会跑（而"网关忽略字段且模型自身上限更高"这一支根本不产生 length，它会安静地按模型默认上限生成然后去撞 `SlotLimits.max_output_tokens = 120_000`，正是 §14 描述的最难诊断的失败）、且输出通道当时并不存在。

**改为：每一次响应都经 §10.3 的 logger 以 debug 级记录 requested 与实际 `completion_tokens` 两个数字**，不做阈值判断、不下结论。

注意不能只在 `finish_reason == "length"` 时记——第 3 稿就是这么写的，而它继承了自己刚刚点名的那个缺陷：「网关忽略字段、模型按自身默认上限生成」这一支**根本不产生 `length`**，它会安静地生成下去，然后去撞 `SlotLimits.max_output_tokens = 120_000`（`limits.py:17`），以"看不出规律的提前 EXHAUSTED"收场。只在 length 时记录恰好看不见它。每次都记，覆盖完整，成本相同。

上界同样无人校验：`max_tokens=16000` 发给一个输出上限 8192 的模型会得到 API 400 → `ProviderError` → FAILED。本地校验需要维护模型能力表，不做，记录在此。

## 13. 推理预算耗尽的诊断（重写）

第 2 稿提出"加载期 `max_tokens < 2000` 告警"，**已废弃**：§7 的出厂默认里 `classifier = 1000` 就在阈值之下，意味着每一次运行、每一个用户、纯 Anthropic 默认路径下都会多出一行告警——正是 §12 亲口判定为"有害"的那类警告，出厂即响，第一天就开始训练用户忽略 stderr（`angle = 2000` 还卡在边界上）。

真正要解决的问题仍然成立：预算被推理吃光时，故障链是「空 text → `_load_json("")` 抛 JSONDecodeError → SchemaError → 修复轮 → 再空 → `LimitExceeded("schema 修复 2 次仍失败")`」，**诊断与真实原因毫无关系**。

**改为在真的发生时给出正确诊断。** 谓词必须精确，否则会复制它本要修的那个毛病——第 3 稿的谓词（"每一轮 `resp.text` 都为空"）就有两个洞：

- **工具轮误报。** §3.1 代价一自己写了：OpenAI 推理模型发起工具调用时 `content` 恒为 `None`，经 §10 归一成 `""`。于是一条 planner 候选线因工具空转烧穿 `max_tool_calls`（`runner.py:135` 的 `ctx.check()` 抛 `LimitExceeded`）时，每一轮 `text` 都是空的，谓词为真，用户看到「`max_tokens`（当前 16000）可能被推理预算吃光」——真因是工具空转，与 `max_tokens` 毫无关系。而这正是 OpenAI + PLANNER 这条最主要的新路径。
- **零轮真空为真。** `runner.py:129` 的 `ctx.check()` 在 `client.chat` **之前**，而 `ctx` 的作用域是整条候选线（`slot.py:41`）。第二、三次 `run_agent`（revise / critic）可能一次 `chat` 都没发出就被 deadline 打断，此时"每一轮都空"真空成立，那句提示会被贴到「超时（612s > 600s）」后面。

**修正后的谓词**，三个条件全部满足才追加：

1. 作用域是**本次 `run_agent` 调用**，不是整条 slot（文字与实现必须一致——取 slot 作用域时 generate 只要出过一次正常文本就永久置假，功能几乎永不触发）；
2. 本次调用**至少完成过一轮** `client.chat`；
3. 所有**非工具轮**（`stop_reason != "tool_use"`）的 `resp.text` 均为空，且至少存在一个非工具轮。

（条件 2 被条件 3 蕴含——"至少存在一个非工具轮"已经保证至少完成过一轮 `chat`。保留它只为把"零轮真空为真"这个洞写在明面上；实现时可以只写条件 1 与 3。）

**另外三类必须先被 §10 / §10.0 拦下，不能走到这里**：`refusal`、`content_filter`、`model_context_window_exceeded`。三者都会产出"非工具轮 text 全空"从而满足谓词，但真因分别是模型拒答、内容过滤、上下文撑爆，与 `max_tokens` 无关。它们在 §10 表格与 §10.0 里已归一成 `ProviderError`，正常情况下到不了 `LimitExceeded`；此处列出是为了让实施者知道谓词的正确性**依赖**那几条归一化，两处不能分开实现。

追加的消息：「本次调用（角色 `<role>`）的每一轮非工具响应都是空文本，该角色的 `max_tokens` 可能被推理预算吃光」。

**消息里不能带 `max_tokens` 的具体数值。** 第 4 稿写了"（当前 N）"，但 N 在 `run_agent` 这一层拿不到：它的参数是 `(system_prompt, user_prompt, tools, output_schema, role, ctx, client, tool_impls)`，`ctx.limits` 是 `SlotLimits`（没有 per-role `max_tokens`），`client` 是只有 `chat` 的 Protocol（`client.py:39-41`），`role` 只是枚举。为了一条诊断去改 Protocol 与 §10.3 的裁决直接冲突。报角色名即可——用户照着角色名去配置里查那个数字，成本几乎为零。

（这条故障链只在 OpenAI 侧完整成立。Anthropic 侧因 §3.1 补偿二把空 content 替换成了 `"(空回复)"`，不会撞 400，但同样会走完修复轮耗尽。）

## 14. 跨 provider 的 token 口径

`limits.py:56-70` 的 `check()` 实际只比 `self._usage.output_tokens`（`limits.py:62` 是唯一读 `_usage` 处），`input_tokens` 不参与任何判定；`ctx.spent` 生产侧零消费（全仓只有 `tests/agents/test_limits.py:67`、`tests/agents/test_runner.py:111` 读）。因此两家 input 口径的差异目前无害。

有害的是 output 侧：`SlotLimits.max_output_tokens = 120_000`（`limits.py:17`）。OpenAI 推理模型的 `completion_tokens` 混着 reasoning tokens，同样"干一件事"的计数可能是 Anthropic 的数倍，混用后 120k 这个阈值不再对应稳定语义，候选线会以看不出规律的方式提前 `EXHAUSTED`。

本次不改计量模型，但必须在 `limits.py:17` 处留注释写明：**跨 provider 混用时 `max_output_tokens` 只是粗粒度熔断，不是可比的计量**；并补一句：**对省略 `usage` 的网关（§10 归一成 `Usage(0,0)`），这道熔断根本不会触发**，那条候选线只剩 deadline 兜底。两条合起来才是这个阈值的真实效力范围。

## 14.1 两处记录在案、本次不动的小事

- **`ToolCall.id`（`client.py:24-27`）在走 §3 的拍平路线后无人读取**（`runner.py:138-149` 只用 `name` / `args`）。它与 §9 处死的 `independent_context` 同属死字段，但处置不同：`independent_context` 之所以必须删，是因为 §4 要把配置表面重新定义一遍，留着它就得回答"这个键还能不能写"；`ToolCall.id` 不出现在任何用户可见表面上，保留它零成本，且它是两家 wire format 都有的原生标识，删了反而要在 backend 里多写一行丢弃。保留，记录在此。
- **`system` 参数对 o1 类推理模型**：§10 只写"并入 `messages[0]`"。Chat Completions 的 o1-mini 等模型不接受 `system` role。§4 的示例用 `gpt-5`，不受影响；用户把 model 指向 o1 类时会得到 API 400 → `ProviderError`。风险低，不做兼容处理，记录在此。
- **无鉴权网关无法表达**：§8.1 构造 step 2 规定 `spec.key` 与 `OPENAI_API_KEY` 皆空即 `MissingCredential`，于是内网免鉴权网关只能靠填一个假 key（`key = "unused"`）绕过。要正经支持得加一个"本 model 不需要凭据"的显式开关，本次不做，记录在此。
- **§14 的措辞收紧**：`limits.py:62` 是 `check()` 内唯一读 `_usage` 处；模块内 `limits.py:41` 的 `spent` property 与 `:48` 的 `charge` 自加同样读它。结论（两家 input 口径差异当前无害，因为只有 `check()` 参与判定、而它只比 output）不受影响。

## 15. 测试策略

TDD。

**改写**

- `tests/llm/test_config.py`：展开语法四情形（有值 / 取默认 / 无值无默认报 `ConfigError` / 空默认值合法）、展开作用域仅限三字段、`provider` 与 `roles.model` 不展开、只展开被引用的 model、加载顺序（未知引用先于展开报错）、未知 provider、未知字段、`allow_same_model` 写在非 critic 段报错、critic 与 planner 同 `(provider, name)` 报错且消息含原文与展开值、`allow_same_model` 放行。**§4.1 合并语义**：只写 `[roles.critic]` 时其余三角色保持默认（承接今天的 `test_load_config_overrides_only_named_roles`）、`[models.opus]` 同名整条替换、`roles` 整段缺失等价全默认。删除 `test_config.py:15-17` 的 change detector。
- `tests/llm/test_client.py`：现有 Anthropic 测试迁移为 backend 形态。保留 `test_client.py:231` 那手 `inspect.signature(Messages.create).bind(None, **call_kwargs)` ——它是防"关键字名拼错但 MagicMock 照样绿"的唯一防线。
- **`tests/llm/test_config.py:10-12`** `test_planner_and_critic_use_different_models`：新语义下它比较的是 model **引用名**（`"opus" != "sonnet"`），**会继续变绿但不再测 §8.2 声明的约束**——一条绿色的假测试，正是 §9 用来处死 `independent_context` 的同一个标准。必须改成断言展开后的 `(provider, name)`。
- **`tests/llm/test_config.py:6-7`** `test_defaults_cover_every_role`：依赖 `DEFAULT_ROLES` 这个符号，随数据结构改变必然改写。
- `tests/test_cli.py`：替换当前三条只针对 `ANTHROPIC_API_KEY` 的测试（`test_cli.py:356/368/379`），**新契约按 §6.2**：消息必须含角色名、model 名、该 export 的变量名、`--dry-run`。

  **这三条必须重新密封，否则会按机器随机变红。** 它们走真实的 `build_deps` → §8.1 的真实加载期构造，而 §6 规则二的判据已经从"环境变量 `ANTHROPIC_API_KEY`"扩成"SDK 是否解析出任何一种凭据"。开发机上只要存在 `ANTHROPIC_AUTH_TOKEN`、`~/.config/anthropic/` 的 profile 或 WIF 环境变量，`MissingCredential` 就不会抛。`_no_real_credentials` 必须同时做两件事：

1. **`delenv`**：`AMAP_KEY` / `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` / `OPENAI_API_KEY` / `ANTHROPIC_PROFILE` / **`ANTHROPIC_CONFIG_DIR`** / WIF 那四个（`ANTHROPIC_IDENTITY_TOKEN` / `ANTHROPIC_IDENTITY_TOKEN_FILE` / `ANTHROPIC_FEDERATION_RULE_ID` / `ANTHROPIC_ORGANIZATION_ID`）/ `TRIPPLAN_ROLES` / `TRIPPLAN_CONFIG` / `TRIPPLAN_CACHE`。
2. **`monkeypatch.setenv("HOME", str(tmp_path))`**，把 profile 的平台默认位置指向一个空家目录。

**只做第 1 条不够，做成"把 `ANTHROPIC_CONFIG_DIR` setenv 到空目录"更糟。** 四种写法的实测（anthropic 1.5.0）：

| 做法 | `anthropic.Anthropic(api_key=None)` 的结果 |
|---|---|
| `setenv ANTHROPIC_CONFIG_DIR` = 空目录 | **抛 `CredentialsError`**（`Config file not found at .../configs/default.json`） |
| `setenv ANTHROPIC_CONFIG_DIR` = 不存在的路径 | **同样抛 `CredentialsError`** |
| 全 delenv + `HOME` → 空目录 | 构造成功，`credentials=None`，规则二正确判缺凭据 ✅ |
| 全 delenv + 真实 `HOME` | 本机通过，但**依赖这台机器恰好没有 `~/.config/anthropic/`**——配过 profile 的机器上会红 |

根因在 `anthropic/lib/credentials/_chain.py:119-129`：只要 `ANTHROPIC_CONFIG_DIR` 或 `ANTHROPIC_PROFILE` 被设置，profile 解析就升级为"显式选择"，失败不再 fall-through 而是直接抛（源码注释：a user who explicitly names a profile expects a broken config to surface）。所以 setenv 不是密封，是引爆——而且是**每台机器**都炸，比 delenv 的机器依赖更糟。

（第 5 稿写成了 setenv，是照上一轮评审意见改的；第 6 轮评审指出它反了，实测证实。记录在此，免得下次又改回去。）

理由与当初为 `AMAP_KEY` 写下的那段 docstring 一字不差：测试必须不依赖、也不触碰真实凭据。
- **`tests/test_slot.py:259-284`**：`test_transport_error_surfaces_as_failed_not_propagating` 同时依赖 `from tripplan.llm.client import AnthropicClient` 的导入路径、`AnthropicClient(DEFAULT_ROLES, api_key=...)` 的构造签名、`_client` 私有属性名——三者本次全变。

**新增**

- `tests/llm/test_openai_backend.py`：`patch("openai.OpenAI")`。请求形状（system 合并、tools 翻译、`max_completion_tokens`，并用 `inspect.signature` 对真 SDK 校验关键字名）；响应归一化（`tool_calls` 非空判工具轮、`content=None → ""`、`choices=[]` → `ProviderError`、`usage` 缺失 → `Usage(0,0)`、`finish_reason="length" → stop_reason="max_tokens"` 且不抛异常、`arguments=""` 按 `{}`、坏 JSON 走 `args={}` 通道、`APIError` → `ProviderError`、**`AuthenticationError` → `ProviderError` 但带 §6.2 的消息**（必须在 `except AnthropicError` / `except OpenAIError` 之前捕获——实测 anthropic 侧 MRO 是 `AuthenticationError → APIStatusError → APIError → AnthropicError`，openai 侧同构但基类是 `OpenAIError`，顺序写反就永远走不到；**类型不能换成 `MissingCredential`**，理由见 §6.3）**、**构造期 `OpenAIError` → `ConfigError`**（不是 `MissingCredential`——缺凭据由 §8.1 构造 step 2 抢在构造之前判定；第 5 稿这里写成 `MissingCredential`，与 §10.2 的裁决和 §11 的第 10 条直接冲突，而文档全程 TDD，照这句写出的测试会把被裁决掉的二选一以"测试已经绿了"的形式复活）。
- `tests/llm/test_router.py`：角色分派正确、**backend 按 `(provider, base_url, key)` 复用**（§7 默认配置只应构造一个 anthropic client）、`max_tokens` 取自角色、加载期即完成全部构造与凭据校验。
- `tests/llm/test_tool_loop_wire_shape.py`：**覆盖 §3 那个地基级取舍**。**两个脚本，缺一不可。**

  **脚本 A（工具轮）**：FakeLlm 第一轮返回 tool_calls、第二轮返回合法 JSON。断言第二次请求的 `messages` 中：没有 `tool_calls` 键、没有 `role:"tool"` 消息、角色严格交替、assistant 轮包含 §3.1 拍平后的工具调用文本。

  **脚本 B（修复轮，覆盖补偿二）**：FakeLlm 第一轮返回 `stop_reason="end_turn"` 的**非工具轮**，触发 `SchemaError` 进修复轮；第二轮返回合法 JSON。断言修复轮那条 assistant message 的 `content` **非空且等于 `"(空回复)"`**。

  **第一轮的 `text` 必须参数化成 `["", "   ", "\n\t"]` 三种。** 只测 `text=""` 的话，`resp.text or ...` 与 `resp.text.strip() or ...` **两种实现都会变绿**——而前者已被 §3.1 证伪（纯空白是 truthy，裸 `or` 兜不住，Anthropic 同样 400）。这把尺子本文档已经用过三次，不能在自己这条补偿上踩空。

  **脚本 B 不能省，而且不能靠在脚本 A 上加一条"每条 content 非空"的断言来代替。** 脚本 A 的消息序列恒为 `[user, assistant(拍平的工具文本), user(工具结果)]`，三条按构造都不可能为空，`runner.py:159` 那条路径**根本不会被走到**——那样一条断言恒绿，补偿二在不在都察觉不到。这正是本文档在 §9 与 §15 两次用来枪毙假测试的同一把尺子，不能自己踩。
- `tests/llm/test_credentials.py`：§6.3 三档各一条，外加 §6.2 的消息契约（含角色名、model 名、该 export 的变量名、`--dry-run`）。

  **`key_source` 的用例必须写成 `${SOME_OTHER_VAR:-}`，不能写 `${SOME_OTHER_VAR}`。** 第 4 稿这里与 §6.3 自相矛盾：无 `:-` 且变量未设置时，§6.3 第一行判的是加载期 `ConfigError`，根本走不到 `MissingCredential`，更谈不上 §6.2 的消息模板。照那句字面写出的测试会去断言 `ConfigError`——测错了东西，却是绿的。能触发 `key_source` 的唯一写法是带空默认值的那个。

  **第三档（"SDK 也解析不出"）必须打真实的 `anthropic.Anthropic`，不能 patch。** `MagicMock().api_key` 是 truthy，规则二 `not (api_key or auth_token or credentials)` 恒为 False，`MissingCredential` 永不抛；手工把三个属性设成 `None` 之后，断言的就是自己刚写的 mock，而不是 §6 花一整节论证的 SDK 真实解析语义——一条名义上的覆盖。有了上面那套 `HOME` 密封，真实构造是可靠且不触网的（`anthropic.Anthropic(api_key=None)` 不发任何请求），直接用它。

  前两档（`${VAR}` 未定义 → `ConfigError`、`${VAR:-}` → 合法）不涉及 SDK，纯 `load_config` 层面。§6.2 的消息契约两种方式都能测。只有涉及 backend 构造形状的断言（请求参数等）才用 `patch("anthropic.Anthropic")` / `patch("openai.OpenAI")`（与 §10.4 的写法约束、现有 `tests/llm/test_client.py:124` 的既有手法一致）。前几稿"假 client factory"那个说法作废——§8 的组件切分里没有这个接缝。
- `tests/agents/test_runner.py` 增补：**§13 的诊断谓词**——三条分别钉住：工具轮全空但因 `max_tool_calls` 耗尽时**不**追加提示、零轮就被 deadline 打断时**不**追加、非工具轮全空时**追加**且消息含角色名、不含具体数字。前两条各对应一类已知误报。

  （第 7 稿还列了第四条"refusal 导致的全空不走到这里"——**删掉**：`FakeLlm` 产出的是 `LlmResponse`，产不出 `message.refusal`，归一化整个发生在 backend 里，这条在 runner 层写不实，要么恒绿要么无意义。它属于下面 backend 测试的职责。）
- **两个 backend 各加一条"漏捕"测试**：请求期抛一个**非 `APIError`** 的厂商异常（anthropic 侧用 `CredentialsError`，openai 侧同理）→ 断言得到 `ProviderError`。

  **这条是开篇铁律"漏捕"一侧的唯一防线，目前零覆盖。** §15 其余所有请求期错误测试用的都是 `APIError` 的子类——`test_slot.py:259-284` 迁移过来那条用的是 `anthropic.APIConnectionError`，就是 `APIError` 子类。也就是说：**一个写成 `except APIError` 的错误实现，能让清单里每一条测试都变绿**，而 §10.2 论证的那条真实故障链（长跑 planner 中途刷新令牌失败 → `CredentialsError` 绕过 `ProviderError` → `_safe_slot` 把已生成的行程硬编码成 `None` 丢弃）没有任何断言拦得住。文档在别处三次用"会变绿的假测试"这把尺子枪毙别人，这里不能自己踩空。

- `tests/llm/test_openai_backend.py` 与 Anthropic backend 测试都要覆盖 **§10 / §10.0 的全部归一化条目**，不能只挑好写的。OpenAI 侧还差 `message.refusal` 与 `finish_reason=="content_filter"`；Anthropic 侧四条新归一化（`model_context_window_exceeded`、`refusal`、`pause_turn`、"`max_tokens` + 带 `tool_use` block 仍按工具轮"）目前一条测试都没有。§13 明说"谓词的正确性**依赖**那几条归一化，两处不能分开实现"——只测谓词不测归一化，等于把依赖的那一半悬空。
- `tests/render/test_candidates.py` 增补：**§10.0.1 的一行修复**——`status=FAILED` 且 `itinerary` 非空的 slot，其 `detail` 必须出现在渲染结果里。当前实现（`candidates.py:23` 只认 `EXHAUSTED`）下这条会红，修完变绿；没有它，一个"失败却看起来正常、可被选中"的候选会继续悄无声息。
- `tests/llm/test_openai_backend.py` 增补两条易漏的请求形状：**`tools=None` 时请求里没有 `tools` 字段**（不是 `"tools": null`；四个角色里三个走这条路）、**`messages` 未被原地修改**（连续两轮 chat 后调用方持有的 list 里没有累积的 system 消息）。
- `tests/test_cli.py` 增补：**§11 的 `ConfigError` 收口**（坏 TOML、未知 model 引用、`${VAR}` 未定义、`models.*` 缺 `name`、`roles.*.max_tokens` 不是整数，各一条）；**§4.2 的入口优先级**（两个变量同设时 `TRIPPLAN_CONFIG` 胜出且 stderr 有提示）。

  **这几条必须先 `monkeypatch.setenv("AMAP_KEY", ...)`，否则从第一天起就是假绿。** `build_deps` 里 AMAP 的前置检查（`cli.py:236-242`）排在 `load_config`（`cli.py:258`）**之前**，而 `_no_real_credentials` fixture 会 delenv 掉 `AMAP_KEY`。实跑确认：

  ```
  $ TRIPPLAN_ROLES=<含 mdoel 的坏 toml>  trip plan 去芜湖     # AMAP_KEY 未设
  exit code            = 1
  stderr 含 Traceback  = False
  stderr 实际内容      = 错误：缺少环境变量 AMAP_KEY（高德开放平台的 key…）
  ```

  「退出码非零」「stderr 无 Traceback」两条断言**全部满足，而 TOML 一个字节都没被读过**——这条测试永远不会变红，§11 那份十条清单将得到零有效覆盖。现有三条 ANTHROPIC 凭据测试正是靠 `setenv("AMAP_KEY", "test-key-123")` 才走到 LLM 那一段的（`test_cli.py:357-358`），新测试必须照做。

  **断言也要加强**：不能只断言"无 Traceback"（缺 AMAP_KEY 同样无 Traceback），必须断言 stderr **含该条 `ConfigError` 的正文关键字**（如 `未知的 model 引用`、`mdoel`、变量名），把"确实走到了配置解析"钉死。
- `tests/llm/test_config.py` 增补：**§7 开箱行为**——不给配置文件时四个角色的 `(provider, name, max_tokens)` 与现状一致（承接今天的 `test_load_config_without_file_returns_defaults`，该测试会随数据结构改变失效）。

全程不触网。现有 505 条测试中，除上述有意改写者外应保持全绿。
