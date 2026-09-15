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
