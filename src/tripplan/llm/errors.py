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
