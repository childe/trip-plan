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

    assert state.revision == 2  # 没有递增
    assert state.candidates == []  # 没有一串 FAILED 占位
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

        return CandidateSlot(
            kw["angle"], Itinerary(angle=kw["angle"]), None, SlotStatus.OK
        )

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
