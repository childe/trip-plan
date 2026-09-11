from datetime import date

import pytest

from tripplan.agents.limits import SlotLimits
from tripplan.deps import Deps
from tripplan.models.common import Field, Origin
from tripplan.models.issue import Issue, Severity, Source
from tripplan.models.itinerary import Angle
from tripplan.models.requirements import DateRange, Party, Requirements
from tripplan.providers.base import ProviderError
from tripplan.providers.fake import FakeProvider
from tripplan.slot import run_slot
from tripplan.state import SlotStatus

D1 = date(2026, 10, 1)
TZ = "Asia/Tokyo"
ANGLE = Angle("A", "古寺巡礼", "")


def _reqs():
    return Requirements(
        destination=Field("京都", Origin.USER),
        dates=Field(DateRange(D1, D1), Origin.USER),
        party=Field(Party(adults=2), Origin.USER),
    )


class _ScriptedSteps:
    """替换掉 generate/revise/critic，让测试只关心循环控制流。"""

    def __init__(self, itineraries, critiques=None, generate_error=None):
        self.itineraries = list(itineraries)
        self.critiques = list(critiques or [])
        self.generate_error = generate_error
        self.generate_calls = 0
        self.revise_calls = 0
        self.critic_calls = 0
        self.avoid_seen = None

    def generate(self, reqs, angle, deps, ctx, avoid_poi_ids=()):
        self.generate_calls += 1
        self.avoid_seen = avoid_poi_ids
        if self.generate_error:
            raise self.generate_error
        return self.itineraries.pop(0)

    def revise(self, itin, reqs, issues, deps, ctx):
        self.revise_calls += 1
        return self.itineraries.pop(0) if self.itineraries else itin

    def critic(self, itin, reqs, deps, ctx):
        self.critic_calls += 1
        return self.critiques.pop(0) if self.critiques else []


def _clean(mk):
    """一份能通过全部规则的行程。"""
    from tripplan.models.itinerary import Category

    return mk.itin(
        [
            mk.day(
                "d1",
                D1,
                [
                    mk.act("d1a1", "d1", "10:00", "11:30", query="清水寺"),
                    mk.act(
                        "d1a2",
                        "d1",
                        "12:30",
                        "13:30",
                        query="某食堂",
                        category=Category.MEAL,
                    ),
                    mk.act(
                        "d1a3",
                        "d1",
                        "18:30",
                        "20:00",
                        query="某居酒屋",
                        category=Category.MEAL,
                    ),
                ],
            )
        ]
    )


def _broken(mk):
    """第二项与第一项时间重叠 —— 必然触发 R1 BLOCKING。"""
    return mk.itin(
        [
            mk.day(
                "d1",
                D1,
                [
                    mk.act("d1a1", "d1", "09:00", "12:00"),
                    mk.act("d1a2", "d1", "11:00", "13:00"),
                ],
            )
        ]
    )


def _deps():
    return Deps(
        client=None,
        provider=FakeProvider(
            pois={
                "清水寺": [("B001", 34.9949, 135.785)],
                "某食堂": [("B002", 34.9950, 135.786)],
                "某居酒屋": [("B003", 34.9951, 135.787)],
            }
        ),
    )


def _run(mk, steps, monkeypatch, **kw):
    import tripplan.slot as slot_mod

    monkeypatch.setattr(slot_mod, "generate", steps.generate)
    monkeypatch.setattr(slot_mod, "revise", steps.revise)
    monkeypatch.setattr(slot_mod, "run_llm_critic", steps.critic)
    return run_slot(
        angle=ANGLE, seed=kw.pop("seed", None), reqs=_reqs(), tz=TZ, deps=_deps(), **kw
    )


def test_returns_ok_when_first_draft_is_clean(mk, monkeypatch):
    steps = _ScriptedSteps([_clean(mk)])
    slot = _run(mk, steps, monkeypatch)
    assert slot.status is SlotStatus.OK
    assert slot.itinerary is not None
    assert slot.facts is not None  # 快照跟着候选走，供回放
    assert steps.revise_calls == 0


def test_revises_until_blocking_issues_clear(mk, monkeypatch):
    steps = _ScriptedSteps([_broken(mk), _clean(mk)])
    slot = _run(mk, steps, monkeypatch)
    assert slot.status is SlotStatus.OK
    assert steps.revise_calls == 1


def test_critic_is_not_called_while_hard_errors_remain(mk, monkeypatch):
    """让 critic 点评一份时间都对不上的行程没有意义，也白烧钱。"""
    steps = _ScriptedSteps([_broken(mk), _clean(mk)])
    _run(mk, steps, monkeypatch)
    assert steps.critic_calls == 1  # 只在最后那份干净的上跑过


def test_exhausted_when_rounds_run_out(mk, monkeypatch):
    steps = _ScriptedSteps([_broken(mk)] * 5)
    slot = _run(mk, steps, monkeypatch, limits=SlotLimits(max_rounds=2))
    assert slot.status is SlotStatus.EXHAUSTED
    assert slot.itinerary is not None  # 带着残缺行程回来
    assert str(steps.revise_calls) in slot.detail


def test_exhausted_detail_reports_actual_revision_count_not_round_cap(mk, monkeypatch):
    """轮 0 只校验首稿、不修订，所以「修订次数」永远 <= max_rounds - 1。
    detail 里的数字必须是真正调用过 revise 的次数，不能拿轮数上限充数——
    否则读的人会以为模型改了 3 次，其实只改了 2 次。"""
    steps = _ScriptedSteps([_broken(mk)] * 5)
    slot = _run(mk, steps, monkeypatch, limits=SlotLimits(max_rounds=3))
    assert slot.status is SlotStatus.EXHAUSTED
    assert steps.revise_calls == 2  # 轮0只校验首稿，轮1、轮2各修订一次
    assert "修订 2 次" in slot.detail
    assert "修订 3" not in slot.detail  # 不能拿轮数上限（3）冒充修订次数


def test_exhausted_slot_carries_unresolved_issues(mk, monkeypatch):
    steps = _ScriptedSteps([_broken(mk)] * 5)
    slot = _run(mk, steps, monkeypatch, limits=SlotLimits(max_rounds=1))
    assert any(i.severity is Severity.BLOCKING for i in slot.itinerary.issues)


def test_critic_blocking_also_drives_revision(mk, monkeypatch):
    critique = [Issue(Severity.BLOCKING, Source.CRITIC, "CRITIC", "太流水账")]
    steps = _ScriptedSteps([_clean(mk), _clean(mk)], critiques=[critique, []])
    slot = _run(mk, steps, monkeypatch)
    assert steps.revise_calls == 1
    assert slot.status is SlotStatus.OK


def test_failed_when_generate_raises_provider_error(mk, monkeypatch):
    steps = _ScriptedSteps([], generate_error=ProviderError("高德限流"))
    slot = _run(mk, steps, monkeypatch)
    assert slot.status is SlotStatus.FAILED
    assert slot.itinerary is None
    assert "高德限流" in slot.detail


def test_limit_exceeded_with_a_draft_is_exhausted_not_failed(mk, monkeypatch):
    from tripplan.agents.limits import LimitExceeded

    class Steps(_ScriptedSteps):
        def revise(self, itin, reqs, issues, deps, ctx):
            raise LimitExceeded("输出 token 超限")

    steps = Steps([_broken(mk)])
    slot = _run(mk, steps, monkeypatch)
    assert slot.status is SlotStatus.EXHAUSTED
    assert slot.itinerary is not None
    assert "资源超限" in slot.detail


def test_limit_exceeded_before_any_draft_is_failed(mk, monkeypatch):
    from tripplan.agents.limits import LimitExceeded

    steps = _ScriptedSteps([], generate_error=LimitExceeded("超时"))
    slot = _run(mk, steps, monkeypatch)
    assert slot.status is SlotStatus.FAILED
    assert slot.itinerary is None


def test_seed_skips_generation(mk, monkeypatch):
    steps = _ScriptedSteps([])
    slot = _run(mk, steps, monkeypatch, seed=_clean(mk))
    assert steps.generate_calls == 0
    assert slot.status is SlotStatus.OK


def test_incoming_issues_are_addressed_before_validation(mk, monkeypatch):
    """REFINE 入口：带着人工意见进来，先改再校验。"""
    steps = _ScriptedSteps([_clean(mk)])
    human = [Issue.from_human("第2天太赶了")]
    slot = _run(mk, steps, monkeypatch, seed=_broken(mk), issues=human)
    assert steps.revise_calls == 1
    assert slot.status is SlotStatus.OK


def test_avoid_poi_ids_are_forwarded_to_generate(mk, monkeypatch):
    steps = _ScriptedSteps([_clean(mk)])
    _run(mk, steps, monkeypatch, avoid_poi_ids=frozenset({"B001"}))
    assert steps.avoid_seen == frozenset({"B001"})


def test_emits_progress_events(mk, monkeypatch):
    events = []
    steps = _ScriptedSteps([_broken(mk), _clean(mk)])
    _run(mk, steps, monkeypatch, emit=events.append)
    assert any("revision" in str(e) for e in events)


def test_raising_emit_does_not_break_the_loop(mk, monkeypatch):
    """Task 20 会让三条候选线共享同一个 emit（含多样性重试路径）——一个
    进度回调里的 bug 不该拖垮正在跑的候选。"""

    def bad_emit(_event):
        raise RuntimeError("callback 里的 bug，不该管到 run_slot")

    steps = _ScriptedSteps([_broken(mk), _clean(mk)])
    slot = _run(mk, steps, monkeypatch, emit=bad_emit)
    assert slot.status is SlotStatus.OK


def test_transport_error_surfaces_as_failed_not_propagating(mk):
    """anthropic.APIError（连接失败/429/5xx）必须在 llm/client.py 里就转成
    ProviderError——不然它既不是 ProviderError 也不是 LimitExceeded，会原样
    穿过 run_slot，带崩这一条候选线（以及已经跑完的另外两条）。这里不
    monkeypatch generate/revise/critic，走的是真实 AnthropicClient。"""
    import anthropic
    import httpx

    from tripplan.llm.client import AnthropicClient
    from tripplan.llm.config import DEFAULT_ROLES

    class _RaisingMessages:
        def create(self, **kwargs):
            raise anthropic.APIConnectionError(
                request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
            )

    client = AnthropicClient(DEFAULT_ROLES, api_key="test-key")
    client._client = type("FakeAnthropic", (), {"messages": _RaisingMessages()})()

    deps = Deps(client=client, provider=FakeProvider(pois={}))
    slot = run_slot(angle=ANGLE, seed=None, reqs=_reqs(), tz=TZ, deps=deps)

    assert slot.status is SlotStatus.FAILED
    assert slot.itinerary is None
    assert "外部依赖失败" in slot.detail
