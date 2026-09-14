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
