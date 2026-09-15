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
        poi_by_activity={},
        constraint_pois={},
        routes=[],
        weather={},
        trip_timezone="Asia/Tokyo",
        resolved_at=datetime(2026, 9, 1, tzinfo=timezone(timedelta(hours=9))),
        gaps=[],
    )
    state.candidates = [
        CandidateSlot(
            angle,
            Itinerary(angle=angle, days=[Day(id="d1", date=d1, activities=[])]),
            facts,
            SlotStatus.OK,
        )
    ]
    if stage is Stage.DONE:
        state.chosen_key = "foodie"
    FileRepo(trips_root / tid).create(state)
    return state


def test_the_envelope_has_every_field_the_frontend_reads(client, trips_root, app):
    _seed(trips_root)
    app.extensions["tripplan"]["store"].get("kyoto").append(
        "generating", {"args": ["foodie"]}
    )

    body = client.get("/trips/kyoto/events?since=0").get_json()
    assert set(body) == {
        "events",
        "first_seq",
        "last_seq",
        "stream_epoch",
        "reset_required",
        "resume_seq",
        "job",
        "stage",
        "revision",
        "artifact_ready",
    }
    assert body["stage"] == "GENERATE"
    assert body["revision"] == 3
    assert body["artifact_ready"] is False
    assert body["job"] == {
        "id": None,
        "status": "none",
        "status_version": 0,
        "kind": None,
        "message": None,
    }


def test_events_carry_server_rendered_text(client, trips_root, app):
    """前端不需要知道任何业务状态怎么渲染，它只判断「要不要刷新」（spec §6.3）。"""
    _seed(trips_root)
    app.extensions["tripplan"]["store"].get("kyoto").append(
        "generating", {"args": ["foodie"]}
    )
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


def test_a_stale_cursor_or_epoch_demands_a_reset_with_a_resume_point(
    client, trips_root, make_app
):
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
    again = c.get(
        f"/trips/kyoto/events?since={cursor}&epoch={log.stream_epoch}"
    ).get_json()
    assert again["reset_required"] is False


def test_a_terminal_job_keeps_a_stable_status_version(client, trips_root, app):
    """§9 回归 17：error 是**留在 job 上的终态字段**，不是一次性事件。
    照原稿「error 非空就 reload」会每秒刷一次，用户连错误信息都读不完
    （spec §6.3）。"""
    _seed(trips_root)
    reg = app.extensions["tripplan"]["registry"]
    job = reg.start(
        "kyoto",
        EventLog(trips_root / "kyoto" / "events.jsonl"),
        lambda j: JobOutcome.failed("ProviderError", "高德限流"),
    )
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
        assert (
            second["job"]["status_version"] == first["job"]["status_version"]
        )  # 相等！
        assert second["job"]["id"] != first["job"]["id"]  # 靠它才分得出
        assert second["revision"] == first["revision"]  # revision 兜不住
    finally:
        gate2.set()
        job_b.thread.join(5)


def test_artifact_ready_flips_when_the_manifest_lands(client, trips_root):
    """前端把 artifact_ready 当作四个刷新判据之一（spec §6.3），所以它必须
    真的会翻。"""
    state = _seed(trips_root, stage=Stage.DONE, rev=4)
    assert (
        client.get("/trips/kyoto/events?since=0").get_json()["artifact_ready"] is False
    )
    publish(stage_artifacts(state, trips_root / "kyoto", FakeProvider(), "job-1"))
    assert (trips_root / "kyoto" / "itinerary.html").exists()  # 确实发布了东西
    assert (
        client.get("/trips/kyoto/events?since=0").get_json()["artifact_ready"] is True
    )


def test_a_bad_since_value_is_treated_as_zero(client, trips_root):
    _seed(trips_root)
    assert client.get("/trips/kyoto/events?since=abc").status_code == 200


def test_the_events_endpoint_refuses_path_traversal(client):
    assert client.get("/trips/..%2f..%2fetc/events").status_code == 404
