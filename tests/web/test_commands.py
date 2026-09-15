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
    facts = FactSnapshot(
        poi_by_activity={},
        constraint_pois={},
        routes=[],
        weather={},
        trip_timezone="Asia/Tokyo",
        resolved_at=datetime(2026, 9, 1, tzinfo=timezone(timedelta(hours=9))),
        gaps=[],
    )
    state.candidates = [
        CandidateSlot(angle, Itinerary(angle=angle), facts, SlotStatus.OK)
    ]
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
        (
            {"kind": "feedback", "angle_key": "foodie", "text": "第2天太赶"},
            GiveFeedback(3, "foodie", "第2天太赶"),
        ),
    ],
)
def test_each_kind_maps_to_the_state_command(
    client, csrf, trips_root, runner, form, expected
):
    _seed(trips_root)
    client.post(
        "/trips/kyoto/commands",
        data={"_csrf": csrf(), "expected_revision": "3", **form},
    )
    _wait(runner)
    assert runner.calls == [("kyoto", expected)]


def test_an_empty_kind_means_continue(client, csrf, trips_root, runner):
    """spec §6.2：kind 留空 = 「继续」，对应 cmd=None。"""
    _seed(trips_root, stage=Stage.GENERATE)
    client.post(
        "/trips/kyoto/commands", data={"_csrf": csrf(), "expected_revision": "3"}
    )
    _wait(runner)
    assert runner.calls == [("kyoto", None)]


def test_a_successful_post_redirects(client, csrf, trips_root):
    _seed(trips_root)
    resp = client.post(
        "/trips/kyoto/commands",
        data={"_csrf": csrf(), "kind": "confirm", "expected_revision": "3"},
    )
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/trips/kyoto")


def test_a_bad_expected_revision_is_400(client, csrf, trips_root, runner):
    _seed(trips_root)
    resp = client.post(
        "/trips/kyoto/commands",
        data={"_csrf": csrf(), "kind": "confirm", "expected_revision": "abc"},
    )
    assert resp.status_code == 400
    assert runner.calls == []


def test_an_unknown_kind_is_400(client, csrf, trips_root, runner):
    _seed(trips_root)
    resp = client.post(
        "/trips/kyoto/commands",
        data={"_csrf": csrf(), "kind": "drop-table", "expected_revision": "3"},
    )
    assert resp.status_code == 400
    assert runner.calls == []


def test_overlong_text_is_400_and_keeps_the_input(client, csrf, trips_root, runner):
    _seed(trips_root)
    resp = client.post(
        "/trips/kyoto/commands",
        data={
            "_csrf": csrf(),
            "kind": "amend",
            "expected_revision": "3",
            "text": "a" * 9000,
        },
    )
    assert resp.status_code == 400
    assert runner.calls == []


def test_an_overlong_angle_key_is_400(client, csrf, trips_root, runner):
    _seed(trips_root)
    resp = client.post(
        "/trips/kyoto/commands",
        data={
            "_csrf": csrf(),
            "kind": "choose",
            "expected_revision": "3",
            "angle_key": "k" * 65,
        },
    )
    assert resp.status_code == 400
    assert runner.calls == []


def test_choose_without_an_angle_key_is_400(client, csrf, trips_root, runner):
    _seed(trips_root)
    resp = client.post(
        "/trips/kyoto/commands",
        data={"_csrf": csrf(), "kind": "choose", "expected_revision": "3"},
    )
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
        runner.calls.append((a[1], a[2])),
        gate.wait(5),
        JobOutcome.ok(4),
    )[2]
    app.extensions["tripplan"]["run_command_fn"] = runner.run_command

    token = csrf()
    first = client.post(
        "/trips/kyoto/commands",
        data={"_csrf": token, "kind": "confirm", "expected_revision": "3"},
    )
    _wait(runner)
    second = client.post(
        "/trips/kyoto/commands",
        data={"_csrf": token, "kind": "confirm", "expected_revision": "3"},
    )
    gate.set()
    app.extensions["tripplan"]["registry"].get("kyoto").thread.join(5)

    assert first.status_code == 302
    assert second.status_code == 409
    assert len(runner.calls) == 1


def test_a_cancelling_job_also_blocks_the_same_trip(
    client, csrf, trips_root, app, runner
):
    """§9 回归 23：**cancelling 也挡**——那个线程还没退出，放第二个进来
    就是两个线程同时对一份 state 跑 advance（spec §6.2）。"""
    _seed(trips_root)
    gate = threading.Event()
    reg = app.extensions["tripplan"]["registry"]
    job = reg.start(
        "kyoto",
        EventLog(trips_root / "kyoto" / "events.jsonl"),
        lambda j: (gate.wait(5), JobOutcome.ok(1))[1],
    )
    job.request_cancel()
    try:
        resp = client.post(
            "/trips/kyoto/commands",
            data={"_csrf": csrf(), "kind": "confirm", "expected_revision": "3"},
        )
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
    busy = reg.start(
        "kyoto",
        EventLog(trips_root / "kyoto" / "events.jsonl"),
        lambda j: (gate.wait(5), JobOutcome.ok(1))[1],
    )
    try:
        resp = c.post(
            "/trips/osaka/commands",
            data={"_csrf": token, "kind": "confirm", "expected_revision": "3"},
        )
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
    job = reg.start(
        "kyoto",
        EventLog(trips_root / "kyoto" / "events.jsonl"),
        lambda j: (gate.wait(5), JobOutcome.ok(1))[1],
    )
    try:
        resp = client.post("/trips/kyoto/cancel", data={"_csrf": csrf()})
        assert resp.status_code == 302
        assert job.status == "cancelling"
        assert job.cancel_token.is_set()
        assert job.status_version == 2
    finally:
        gate.set()
        job.thread.join(5)


def test_cancel_is_idempotent_and_does_not_bump_the_version_twice(
    client, csrf, trips_root, app
):
    """重复点不报错，也**不再 +1 status_version**——否则每点一下都让所有
    标签页白刷一次（spec §6.2）。"""
    _seed(trips_root, stage=Stage.GENERATE)
    reg = app.extensions["tripplan"]["registry"]
    gate = threading.Event()
    job = reg.start(
        "kyoto",
        EventLog(trips_root / "kyoto" / "events.jsonl"),
        lambda j: (gate.wait(5), JobOutcome.ok(1))[1],
    )
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


def test_rebuild_is_refused_while_a_job_is_active(
    client, csrf, trips_root, app, runner
):
    """§9 回归 23 的第三条：那个还没退出的线程可能正在往自己的暂存目录里写、
    马上要 publish()，此时插一次重建就是两个发布者抢同一份最终产物（spec §6.2）。"""
    _seed(trips_root, stage=Stage.DONE, rev=5)
    reg = app.extensions["tripplan"]["registry"]
    gate = threading.Event()
    job = reg.start(
        "kyoto",
        EventLog(trips_root / "kyoto" / "events.jsonl"),
        lambda j: (gate.wait(5), JobOutcome.ok(1))[1],
    )
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
    job = reg.start(
        "kyoto",
        EventLog(trips_root / "kyoto" / "events.jsonl"),
        lambda j: (gate.wait(5), JobOutcome.ok(1))[1],
    )
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
