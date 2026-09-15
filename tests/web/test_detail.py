"""详情页按 stage 渲染（spec §6.1 / §9 回归 2、15、16）。"""

from datetime import date, datetime, timedelta, timezone

import pytest

from tripplan.artifacts import publish, stage_artifacts
from tripplan.models.common import Field, Origin
from tripplan.models.facts import FactSnapshot
from tripplan.models.issue import Issue, Severity, Source
from tripplan.models.itinerary import Angle, Day, Itinerary
from tripplan.models.requirements import DateRange, Party, Requirements
from tripplan.providers.fake import FakeProvider
from tripplan.repo import FileRepo
from tripplan.state import CandidateSlot, SlotStatus, Stage, TripState

D1 = date(2026, 10, 1)
_JST = timezone(timedelta(hours=9))


def _facts():
    return FactSnapshot(
        poi_by_activity={},
        constraint_pois={},
        routes=[],
        weather={},
        trip_timezone="Asia/Tokyo",
        resolved_at=datetime(2026, 9, 1, tzinfo=_JST),
        gaps=[],
    )


def _reqs(rationale=""):
    return Requirements(
        destination=Field("京都", Origin.USER),
        dates=Field(DateRange(D1, D1), Origin.USER),
        party=Field(Party(adults=2), Origin.USER),
        lodging_area=Field("四条", Origin.MODEL, rationale=rationale),
    )


def _seed(
    trips_root,
    tid="kyoto",
    stage=Stage.AWAIT_REQ_CONFIRM,
    rev=3,
    raw="去京都",
    detail="",
    rationale="",
):
    state = TripState.new(raw, run_id="r1")
    state.stage, state.revision, state.requirements = stage, rev, _reqs(rationale)
    angle = Angle("foodie", "吃遍京都", "从早市到居酒屋")
    itin = Itinerary(
        angle=angle,
        days=[Day(id="d1", date=D1, activities=[])],
        issues=[Issue(Severity.WARNING, Source.CRITIC, "C1", "第二天略赶")],
    )
    state.candidates = [CandidateSlot(angle, itin, _facts(), SlotStatus.OK, detail)]
    if stage is Stage.DONE:
        state.chosen_key = "foodie"
    FileRepo(trips_root / tid).create(state)
    return state


def test_await_req_confirm_shows_the_requirement_card_and_two_actions(
    client, trips_root
):
    _seed(trips_root, rationale="按预算推断")
    body = client.get("/trips/kyoto").get_data(as_text=True)
    assert "目的地" in body and "京都" in body
    assert "按预算推断" in body
    assert "确认" in body
    assert 'name="kind" value="confirm"' in body
    assert 'name="kind" value="amend"' in body
    assert 'name="expected_revision" value="3"' in body


def test_the_requirement_card_is_not_markdown_source(client, trips_root):
    """§9 回归 16 的一半：autoescape 开着时 render_requirement_card() 的
    Markdown 串会原样带星号显示出来（spec §6.1）。"""
    _seed(trips_root)
    body = client.get("/trips/kyoto").get_data(as_text=True)
    assert "**目的地**" not in body
    assert "## 需求确认" not in body


def test_await_choice_lists_candidates_with_a_button_each(client, trips_root):
    _seed(trips_root, stage=Stage.AWAIT_CHOICE, detail="修订 3 次后仍有 1 个硬伤")
    body = client.get("/trips/kyoto").get_data(as_text=True)
    assert "吃遍京都" in body
    assert 'value="foodie"' in body
    assert "修订 3 次后仍有 1 个硬伤" in body
    assert "第二天略赶" in body
    assert 'name="kind" value="choose"' in body
    assert 'name="kind" value="feedback"' in body


@pytest.mark.parametrize("payload", ["<script>alert(1)</script>"])
def test_model_and_user_text_is_escaped_everywhere(client, trips_root, payload):
    """§9 回归 16 的正题：rationale / slot.detail / raw_request 全是模型输出
    或用户输入的自由文本。标成 safe 等于开一个 XSS 面，而这个服务还要暴露
    在局域网上给别人访问（spec §6.1）。"""
    _seed(
        trips_root,
        stage=Stage.AWAIT_CHOICE,
        raw=payload,
        detail=payload,
        rationale=payload,
    )
    body = client.get("/trips/kyoto").get_data(as_text=True)
    assert payload not in body
    assert "&lt;script&gt;" in body


def test_a_working_stage_with_an_active_job_shows_a_cancel_button(
    client, trips_root, app, runner
):
    import threading

    from tripplan.web.events import EventLog
    from tripplan.web.jobs import JobOutcome

    _seed(trips_root, stage=Stage.GENERATE)
    cfg = app.extensions["tripplan"]
    gate = threading.Event()
    job = cfg["registry"].start(
        "kyoto",
        EventLog(trips_root / "kyoto" / "events.jsonl"),
        lambda j: (gate.wait(5), JobOutcome.ok(1))[1],
    )
    try:
        body = client.get("/trips/kyoto").get_data(as_text=True)
        assert "正在工作" in body
        assert "取消" in body
    finally:
        gate.set()
        job.thread.join(5)


def test_a_cancelling_job_greys_out_the_cancel_button(client, trips_root, app):
    import threading

    from tripplan.web.events import EventLog
    from tripplan.web.jobs import JobOutcome

    _seed(trips_root, stage=Stage.GENERATE)
    cfg = app.extensions["tripplan"]
    gate = threading.Event()
    job = cfg["registry"].start(
        "kyoto",
        EventLog(trips_root / "kyoto" / "events.jsonl"),
        lambda j: (gate.wait(5), JobOutcome.ok(1))[1],
    )
    job.request_cancel()
    try:
        body = client.get("/trips/kyoto").get_data(as_text=True)
        assert "已请求停止" in body
        assert "秒" not in body  # 不给秒数，不假装已停（spec §4.3）
    finally:
        gate.set()
        job.thread.join(5)


def test_a_working_stage_without_a_job_offers_a_way_out(client, trips_root):
    """spec §6.1：服务重启、503 被拒、线程异常挂掉，三种情况都落在这里——
    必须有出路，不能是一个永远转圈的假进度条。"""
    _seed(trips_root, stage=Stage.GENERATE)
    body = client.get("/trips/kyoto").get_data(as_text=True)
    assert "没有跑完" in body
    assert "继续" in body


def test_done_with_ready_artifacts_links_to_the_itinerary(client, trips_root):
    state = _seed(trips_root, stage=Stage.DONE, rev=5)
    publish(stage_artifacts(state, trips_root / "kyoto", FakeProvider(), "job-1"))
    body = client.get("/trips/kyoto").get_data(as_text=True)
    assert "/trips/kyoto/itinerary" in body
    assert "待重建" not in body


def test_done_without_artifacts_offers_a_rebuild_instead_of_a_dead_link(
    client, trips_root
):
    """§9 回归 15 的一半：崩溃恢复、旧版本残留、手工删文件，共用这条出路
    （spec §4.1）。"""
    _seed(trips_root, stage=Stage.DONE, rev=5)
    body = client.get("/trips/kyoto").get_data(as_text=True)
    assert "待重建" in body
    assert "/trips/kyoto/itinerary" not in body


def test_a_stale_manifest_also_counts_as_not_ready(client, trips_root):
    import json

    state = _seed(trips_root, stage=Stage.DONE, rev=5)
    publish(stage_artifacts(state, trips_root / "kyoto", FakeProvider(), "job-1"))
    path = trips_root / "kyoto" / "artifacts.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["revision"] = 4
    path.write_text(json.dumps(data), encoding="utf-8")

    assert "待重建" in client.get("/trips/kyoto").get_data(as_text=True)


def test_the_page_carries_the_whole_event_history_and_a_cursor(client, trips_root, app):
    """§9 回归 2 的前半条：「刷新后接着上次」不靠前端缓存，靠服务端有日志
    （spec §5.5）。"""
    _seed(trips_root)
    log = app.extensions["tripplan"]["store"].get("kyoto")
    log.append("generating", {"args": ["foodie"]})
    log.append("revision", {"args": ["foodie", 0]})

    body = client.get("/trips/kyoto").get_data(as_text=True)
    assert "正在生成候选 foodie" in body
    assert "第 1 轮修订" in body
    assert 'data-cursor="2"' in body
    assert f'data-epoch="{log.stream_epoch}"' in body


def test_the_page_exposes_the_polling_baseline(client, trips_root):
    """前端比的是 (job.id, status_version, revision, artifact_ready) 四者
    （spec §6.3 第 3 条），所以四个基线都要写进 HTML。"""
    _seed(trips_root, rev=3)
    body = client.get("/trips/kyoto").get_data(as_text=True)
    assert 'data-job-id=""' in body
    assert 'data-status-version="0"' in body
    assert 'data-revision="3"' in body
    assert 'data-artifact-ready="0"' in body


def test_a_corrupt_trip_shows_a_readable_page_not_a_500(client, trips_root):
    (trips_root / "broken").mkdir()
    (trips_root / "broken" / "state.json").write_text("not json", encoding="utf-8")
    resp = client.get("/trips/broken")
    assert resp.status_code == 200
    assert "损坏" in resp.get_data(as_text=True)
