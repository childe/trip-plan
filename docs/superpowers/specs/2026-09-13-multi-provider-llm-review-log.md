# 多 provider LLM 接入设计 —— 十轮交叉评审记录

配套文档：`2026-09-13-multi-provider-llm-design.md`（第 1 稿 → 第 12 稿）
日期：2026-09-13 ~ 09-14
方式：每轮新开一个独立 reviewer，不继承上一轮立场，也不告知上一轮结论（避免它只去确认"修好了没"而不找新问题）
判定规则：存在任何**阻断**或**应修** → FAIL；只剩**可议** → PASS
结果：**10 轮跑满，从未 PASS**。第 10 轮只剩 1 条应修，已落地（见 §7）。

---

## 1. 概览

| 轮次 | 阻断 | 应修 | 判定 | 本轮最重的发现 |
|---|---|---|---|---|
| 1 | 2 | 6 | —（循环前，无判定格式） | 凭据判据两个方向都错；§6 与 §8 构造时机自相矛盾 |
| 2 | 1 | 7 | FAIL | 配置是"合并"还是"整份替换"未裁决 |
| 3 | 0 | 5 | FAIL | §13 诊断谓词会在最主流的新路径上误报 |
| 4 | 0 | 8 | FAIL | `tools=None` 会发成 `"tools": null`；logging 通道默认不可观测 |
| 5 | 0 | 4 | FAIL | Anthropic `stop_reason` 枚举还剩两个值没归一 |
| 6 | 0 | 1 | FAIL | 凭据密封方案方向反了（推翻第 5 轮的建议） |
| 7 | 0 | 2 | FAIL | `resume --dry-run` 实测走不通（推翻我第 7 稿的裁决） |
| 8 | **1** | 0 | FAIL | 401 转 `MissingCredential` 会丢掉已生成的行程 |
| 9 | 0 | 2 | FAIL | 请求期只捕 `APIError`，漏捕刷新令牌时的 `CredentialsError` |
| 10 | 0 | 1 | FAIL | `key_source` 在字面量写法下就是明文密钥，却被列入日志白名单 |
| 11 | 0 | 2 | FAIL | 两条**我自己写的测试规格**会假绿：§11 收口测试撞不到 `load_config`；铁律"漏捕"一侧零覆盖 |

subagent 花费约 132 万 token。第 3 轮中途被 API 错误打断，用同一输入重派一次后完成，未计入轮次。

**趋势**：从第 6 轮起每轮只剩 1-2 条，且全部是"一句话能改"的量级，没有一条动结构。第 10 轮 reviewer 的原话是「事实断言我一条都没能证伪」。

---

## 2. 我驳回或改判的意见（没有照单全收的地方）

这一节比其余部分都重要——它记录了我在哪些地方没听 reviewer 的，以及为什么。

### 2.1 第 5 轮：凭据密封手段 —— 采纳方向，但实测后给出第三种答案

- **第 5 轮说**：`delenv ANTHROPIC_CONFIG_DIR` 是错的，会让 SDK 回落到真实的 `~/.config/anthropic/`；应改成 `setenv` 到空目录。
- **第 6 轮说**：`setenv` 到空目录会让 profile 解析升级为"显式选择"，构造期直接抛 `CredentialsError`——**每台机器都炸**，比 delenv 的机器依赖更糟。
- **我的裁定**（实测四种组合后）：两边都不全对。正解是**同时做**——delenv 全部 `ANTHROPIC_*` 变量（含 `CONFIG_DIR`）**并且** `setenv HOME` 到空目录。

```
setenv CONFIG_DIR=空目录      → CredentialsError（引爆）
setenv CONFIG_DIR=不存在路径   → CredentialsError（引爆）
全 delenv + HOME→空目录       → 构造成功，credentials=None，规则二正确判缺凭据  ✅
全 delenv + 真实 HOME         → 本机通过，但依赖这台机器没有 ~/.config/anthropic/
```

根因：`anthropic/lib/credentials/_chain.py:119-129`，源码注释原文 *"a user who explicitly names a profile expects a broken config to surface, not to fall through"*。

### 2.2 第 7 轮：401 单拎成 `MissingCredential` —— 采纳后被第 8 轮推翻，最终驳回

- **第 7 轮（可议）说**：既然已经把 `candidates.py:23` 纳入范围，"401 是既有行为所以不修"的理由就不再一致，建议把 `AuthenticationError` 转成 `MissingCredential` 给一条人话。
- **我照做了**（第 8 稿）。
- **第 8 轮（阻断）说**：`run_slot` 只捕 `LimitExceeded` 与 `ProviderError`，请求期抛出的 `MissingCredential` 会落到 `orchestrator.py:254` 的 `_safe_slot`，而它把 `itinerary` 与 `facts` **硬编码成 `None`**——已生成的三份行程当场丢失，然后照旧打印"请先修改需求后重试"，正是这条改动声称要消灭的那句。
- **最终裁定**：**改消息，不改类型。** 并由此在文档开头立了一条贯穿全篇的铁律。

### 2.3 第 2 / 3 轮：critic≠planner 的判据 —— 采纳 reviewer，改判了用户的原始选择

用户最初选了"只比 model 引用名"。第 2 轮 reviewer 反对，理由是：用户复制一个 `[models.*]` 块通常是为了换 base_url / 换 key，不会意识到自己顺手关掉了这道安全阀；**能被复制粘贴无声关闭的安全阀只提供虚假的保障感**。

我采纳了 reviewer 的 `(provider, name)` 判据，但**加了一个显式 `allow_same_model = true` 逃生口**——这样既堵住误关，又让用户原本要的"显式选择可以绕过"名副其实，是用户那个选项的超集。**已在对话中向用户标出，用户未反对。**

### 2.4 第 7 轮：注册期校验剥夺工具作者写默认参数的自由 —— 采纳批评，但用更小的改法

- **第 7 轮说**：谓词"每个参数都无默认值"过严，会让未来的 `search_poi(query, limit=10)` 在注册期直接报错；建议改成让 backend 显式标记解析失败、由 runner 生成回喂文本。
- **我的裁定**：批评成立，但替代方案要改 runner。收紧谓词即可——真正要保证的命题只有 `impl(**{})` 必抛，**"至少一个必填参数"**就充分，且不越界。

### 2.5 第 6 轮：`§10.1` 把诊断塞进 `LlmResponse.text` —— 采纳批评，加了标记

reviewer 指出这让 `text` 的契约从"模型的可见输出"悄悄扩成"模型输出 + 适配器生成的诊断"，而这段文字会进入 assistant 历史、模型会以为是自己说的话。我保留了做法（零额外机制），但强制加固定前缀 `[适配器] ` 并规定与非空 `content` 是**拼接不是覆盖**。

### 2.6 第 8 轮：`test_limits.py:67` 不读 `.spent` —— 驳回

reviewer 说我的引用虚指。实测 `grep '\.spent'` 命中两处，`test_limits.py:67` 确实是 `assert ctx.spent == Usage(11, 22)`。**原文没错，未改。**

### 2.7 第 6 轮：`§6.2` 按子命令决定是否附 `--dry-run` —— 两次改判

- 第 6 轮指出 `--dry-run` 对 `resume` 不存在，照提示做会吃 argparse 报错。
- 我第 7 稿裁定"给 resume 补上 flag，一行"，理由是 `cli.py:230` 的早返回已覆盖 resume 路径。
- **第 7 轮实跑证伪**：`cli.py:230` 只覆盖 `Deps` 构造；`_cmd_plan` 的 dry-run 语义来自它自己在 `cli.py:285-287` 的早返回，而 `_cmd_resume` 没有。只加 argparse 的结果是 `AttributeError: 'NoneType' object has no attribute 'chat'`——把裸 traceback 种在一条由错误消息亲自指过去的路径上。
- **最终裁定**：补 dry-run，但是**两处**改动（argparse 注册 + `_cmd_resume` 早返回），已写进 §3.3 清单第 7 项。

---

## 3. 三次"照上一轮意见改，被下一轮推翻"

| # | 上一轮建议 | 我改成 | 下一轮证伪 | 最终 |
|---|---|---|---|---|
| 1 | R5：密封改 setenv | setenv 空目录 | R6：每台机器都抛 `CredentialsError` | delenv **+** `HOME`→空目录（实测第三种答案） |
| 2 | R7：401 单拎人话消息 | 转 `MissingCredential` | R8：`_safe_slot` 丢掉已生成行程 | 转 `ProviderError`，只换消息 |
| 3 | —（我自己的疏漏） | 请求期捕 `APIError` | R9：漏捕请求期刷新令牌的 `CredentialsError` | 捕厂商基类 `AnthropicError` / `OpenAIError` |

第 2、3 条是同一条铁律的两种违反方式——**错捕**与**漏捕**。它们逼出了文档开头那条纲领：

> **异常类型决定数据能不能活下来，异常消息决定用户能不能看懂，两者不要混为一谈。**
> 加载期可以用 `ConfigError` / `MissingCredential`（那时还没进 slot）；**请求期的一切错误一律 `ProviderError`**，想改善诊断就改消息内容。

这是整轮循环最大的产出，而它是被两次翻车逼出来的，不是一开始就想到的。

---

## 4. 我自己实测复现过的结论

reviewer 的发现我没有照单全收，凡是承重的都自己跑过一遍。以下每条都有复现记录：

| 结论 | 复现方式 |
|---|---|
| `anthropic.Anthropic(api_key="")` → `auth_headers={'X-Api-Key': ''}`（非空），但请求期仍抛 `TypeError` | 构造 + `_validate_headers` 探针 |
| WIF 凭据 → `auth_headers={}`（空），但 SDK 判为有凭据 | 设四个 WIF 环境变量后构造 |
| `TypeError` 不是 `anthropic.APIError` 子类 | `issubclass` |
| `anthropic` 也有 `AnthropicError` 基类，`APIError` 是其子类（两家**同构**，不是"相反"） | `issubclass` |
| `CredentialsError` / `RetryableError` / `IdentityTokenFileError` 都不是 `APIError` 子类 | `issubclass` 三连 |
| `AuthenticationError` MRO = `APIStatusError → APIError → AnthropicError`（必须排在 `except` 最前） | `__mro__` |
| `tomllib.TOMLDecodeError` **是** `ValueError` 子类（我原文说反了） | `__mro__` |
| `StopReason` 实际有 **7** 个取值，不是 4 个 | `typing.get_args` |
| `_base_client.py:1296-1302` 把 `AnthropicError` 原样穿出、不包装成 `APIConnectionError` | 读源码 |
| `AccessTokenAuth.auth_flow` 在**请求期**调 `get_token()`，刷新失败抛非 `APIError` | 读源码 |
| 凭据密封四种组合的实际行为 | 四个子进程分别构造 |
| `resume --dry-run`（只加 argparse）→ `AttributeError` | 实跑 |
| `_safe_slot` 把 itinerary / facts 硬编码成 `None` | 读 `orchestrator.py:254-261` |
| `candidates.py:23` 只认 `EXHAUSTED`，`FAILED` + 非空 itinerary 什么都不显示 | 读源码 |
| `_cmd_resume` 没有 dry_run 早返回 | 读源码 |
| `src/` 零处 logging | `grep` |
| `config.py:37-39` 的合并语义 + `test_config.py:24-29` 钉死它 | 读源码 |
| `test_limits.py:67` **确实**读 `.spent`（驳回 reviewer） | `grep` |
| 四处行号错位（`steps.py` 221/321/371 → 220/320/370 等） | `grep -n` |

---

## 5. 设计因此发生的实质变化

按影响大小排：

1. **凭据判据整节重写。** 原方案查 `auth_headers`，假阴性（空串放行→请求期炸）与假阳性（OAuth/WIF 误判为缺凭据）两个方向都错；且内置默认配置 `${ANTHROPIC_API_KEY:-}` 正好走在假阴性那条路上——**开箱默认路径就是它声称要修的那个 bug**。改成 `key or None` + 判 `api_key or auth_token or credentials`。
2. **backend 构造时机裁决为加载期。** 按需构造时 `MissingCredential` 会被 `orchestrator.py:254` 吞成"候选线出现未处理异常"，`cli.py:380` 永远等不到。
3. **配置语义裁决为合并**（沿用 `config.py:37` 与 `test_config.py:24-29` 的既有契约），`max_tokens` 随之改回可选——第 2 稿把它标成必填是一次未被承认的破坏性变更。
4. **新增 `ConfigError` 并进 `main()` 的 except 元组。** 配置错误今天就逃成裸 traceback，而本设计把产生面放大了一个数量级（最终清单 10 条）。
5. **`runner.py` 的改动从"零处"变成三处**，其中第三处是结构性的（`LimitExceeded` 有三个抛出点，必须包住整个 while）。
6. **补偿一：把工具调用拍平进 assistant 文本。** OpenAI 推理模型工具轮 `content` 恒为 `None`，历史里只剩字面 `"(tool_use)"`，模型不知道自己查了哪个词 → 重复调用 → 烧穿 `max_tool_calls=40`。
7. **§13 诊断谓词三次收紧**，排除工具轮误报、零轮真空、以及 refusal / content_filter / `model_context_window_exceeded` 三类假触发。
8. **`candidates.py:23` 的一行修复纳入范围**：`FAILED` + 非空 itinerary 时 `detail` 一个字都不显示，与该模块自己的 docstring 直接冲突，而本设计新增了四个会在 revise/critic 轮触发 `ProviderError` 的生产者，把这个洞撑大了。
9. **`ModelSpec` 加 `name_source` / `key_source`**，否则 §6.2 与 §8.2 两处错误消息契约不可实现。
10. **`TRIPPLAN_LOG` 开关必须真做出来**，否则 §12 从"判别"降级为"记录"换来的价值全部归零（`src/` 原本零处 logging）。

---

## 6. 明确记录"不做"的事项

这些是评审提出、我判断为范围外或代价不值的，全部写进了设计文档而不是悄悄略过：

- 推理强度（effort / thinking）配置，以及它与 `max_tokens` 共同上限的完整解决方案。
- 删除 `ToolCall.id`（与 `independent_context` 区别对待：一个在用户可见配置表面上，一个不在）。
- 改计量模型以区分 reasoning tokens（`max_output_tokens=120_000` 跨 provider 不可比，只加注释）。
- `candidates.py:33` 的**可选性**（只补显示，不剥夺用户选择一份"critic 轮挂了但主体已生成"的行程）。
- 加载期校验凭据**有效性**（需要启动时发真实请求）。
- 两家 `base_url` 后缀语义差异的归一化（anthropic 追加 `/v1/messages`，openai 追加 `/chat/completions`）。
- o1 类模型不接受 `system` role。
- 无鉴权网关的显式表达（目前只能填 `key = "unused"` 绕过）。
- 兼容旧的扁平 TOML 格式。

---

## 6.5 第 11 轮：两条假绿的测试规格（讽刺的是都是我自己写的）

第 10 轮那处未评审的 `key_source` 修改**通过了**第 11 轮（reviewer 原话：「这条我找不到反例」）。但它找出两条新的，而且都踩在本文档自己反复使用的那把尺子上——**会变绿的假测试**：

### 6.5.1 §11 的收口测试撞不到 `load_config`

我原本写着「这是防止回归到今天那条裸 traceback 的**唯一**测试」。实跑证明它是最没用的一条：

```
$ TRIPPLAN_ROLES=<含 mdoel 的坏 toml>  trip plan 去芜湖     # AMAP_KEY 被 fixture 清掉
exit code            = 1
stderr 含 Traceback  = False
stderr 实际内容      = 错误：缺少环境变量 AMAP_KEY（高德开放平台的 key…）
```

`build_deps` 里 AMAP 前置检查（`cli.py:236-242`）排在 `load_config`（`cli.py:258`）之前，而 `_no_real_credentials` fixture 会 delenv `AMAP_KEY`。两条断言全绿，**TOML 一个字节都没读过**，§11 那份十条清单得到零有效覆盖。

**已落地**：测试必须先 `setenv("AMAP_KEY", ...)`（现有三条 ANTHROPIC 测试正是这么做的），断言从"无 Traceback"加强为"stderr 含该条 `ConfigError` 的正文关键字"。

### 6.5.2 铁律"漏捕"一侧零覆盖

开篇铁律把"漏捕"列为两种违规之一，§10.2 用一整节论证它是本设计**新开**的路径。但 §15 里所有请求期错误测试用的都是 `APIError` 子类（迁移过来的 `test_slot.py:259-284` 用 `APIConnectionError`）——**一个写成 `except APIError` 的错误实现能让每一条测试都变绿**，而那条真实故障链（刷新令牌失败 → `CredentialsError` → `_safe_slot` 丢行程）没有任何断言拦得住。

**已落地**：两个 backend 各加一条"请求期抛非 `APIError` 的厂商异常 → 必须是 `ProviderError`"。

### 6.5.3 顺带采纳的一条反对意见

reviewer 明确**反对**我"两家 base_url 后缀语义差异只写明、不做任何检查"的取舍，理由是这与全篇"把诊断提前到加载期"的方向相反，而请求期 404 的诊断离真因太远。已采纳：加载期发现 `provider="anthropic"` 而 `base_url` 以 `/v1` 结尾时记一条 debug。

---

## 7. 第 10 轮之后、尚未经评审的一处改动

（第 11 轮已验证这处修改，reviewer 结论：「这条我找不到反例」。以下保留原始记录。）

第 10 轮唯一的应修：

> `key_source` 的定义是"用户在 TOML 里写的原文，展开前后相同时与上面一致"——所以用户写字面量 key 时，`key_source` **就是明文密钥本身**。而 §10.3 的日志白名单写着"只允许出现 `provider`、`name`、`key_source`（变量名）"，那个"（变量名）"只在用户用了 `${...}` 时才成立。同时 §14.1 又主动把字面量写法背书为一等配置（无鉴权网关靠 `key = "unused"` 绕过）。一句 `logger.debug(..., spec.key_source)` 就会把密钥打进 stderr。

**落地的规则**：`key_source` 只有在能被识别为 `${NAME}` / `${NAME:-…}` 形态时才可进入日志与错误消息；否则一律回落到该 provider 的固定映射变量名。同时在 `ModelSpec` 的字段注释上标明这个陷阱。

---

## 8. 我对终止条件的判断

判定规则是"有任何应修即 FAIL"。一个对抗性 reviewer 面对一份 700 行的规格，总能再找出一条——从第 6 轮起每轮确实只剩 1-2 条，且都不动结构，但从没到零。**这个循环的终止条件可能本身就不可达。**

第 11 轮提供了一个有意思的旁证：它验证了第 10 轮的修改「找不到反例」，然后转头在**测试规格**里找到两条新的。也就是说，评审的焦点正在从"设计对不对"移向"测试能不能真的抓住设计要求的东西"——这本身是收敛的信号，但只要还有一条，判定就是 FAIL。

我的判断：**设计已经可以进实施计划。** 依据是第 10 轮 reviewer 自己的结论——事实断言一条都没能证伪、铁律全篇一致、两家 API 的语义差异覆盖面超出预期、测试策略自己枪毙了四条会变绿的假测试。

但这是需要人来拍板的判断，不是 reviewer 能给的。
