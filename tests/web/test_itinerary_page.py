"""成稿页（spec §6.1 / §9 回归 15）。"""

from datetime import date, datetime, timedelta, timezone

from tripplan.artifacts import publish, stage_artifacts
from tripplan.models.common import Field, Origin
from tripplan.models.facts import FactSnapshot
from tripplan.models.itinerary import Angle, Day, Itinerary
from tripplan.models.requirements import DateRange, Party, Requirements
from tripplan.providers.fake import FakeProvider
from tripplan.repo import FileRepo
from tripplan.state import CandidateSlot, SlotStatus, Stage, TripState

D1 = date(2026, 10, 1)


def _done(trips_root, tid="kyoto", rev=5):
    state = TripState.new("去京都", run_id="r1")
    state.stage, state.revision = Stage.DONE, rev
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
        CandidateSlot(
            angle,
            Itinerary(angle=angle, days=[Day(id="d1", date=D1, activities=[])]),
            facts,
            SlotStatus.OK,
        )
    ]
    state.chosen_key = "foodie"
    FileRepo(trips_root / tid).create(state)
    return state


def test_a_ready_itinerary_is_served_as_a_standalone_document(client, trips_root):
    """页面秒开，**不会在请求里现算高德静态地图**（spec §6.1）——文件在 job
    跑到 DONE 时就已经暂存并原子发布了。"""
    state = _done(trips_root)
    publish(stage_artifacts(state, trips_root / "kyoto", FakeProvider(), "job-1"))

    resp = client.get("/trips/kyoto/itinerary")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "<!DOCTYPE html>" in body
    assert "text/html" in resp.headers["Content-Type"]


def test_a_missing_artifact_is_409_with_a_rebuild_entry_not_a_bare_404(
    client, trips_root
):
    """§9 回归 15：详情页在这种状态下本来也不会给出这个链接，这里是直接输
    URL 或用旧书签进来的兜底（spec §6.1）。"""
    _done(trips_root)
    resp = client.get("/trips/kyoto/itinerary")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 409
    assert "重建" in body
    assert "/trips/kyoto" in body


def test_a_stale_manifest_is_also_409(client, trips_root):
    import json

    state = _done(trips_root, rev=5)
    publish(stage_artifacts(state, trips_root / "kyoto", FakeProvider(), "job-1"))
    path = trips_root / "kyoto" / "artifacts.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["revision"] = 4
    path.write_text(json.dumps(data), encoding="utf-8")

    assert client.get("/trips/kyoto/itinerary").status_code == 409


def test_deleting_the_file_by_hand_and_rebuilding_restores_the_page(
    client, csrf, trips_root, app
):
    """§9 回归 15 的完整闭环：删文件 → 详情页显示「待重建」→ 409 →
    重建 → 恢复。这一条用真实的 rebuild_artifacts，不注入替身。"""
    from tripplan.web.jobs import rebuild_artifacts

    state = _done(trips_root)
    publish(stage_artifacts(state, trips_root / "kyoto", FakeProvider(), "job-1"))
    (trips_root / "kyoto" / "itinerary.html").unlink()

    assert "待重建" in client.get("/trips/kyoto").get_data(as_text=True)
    assert client.get("/trips/kyoto/itinerary").status_code == 409

    app.extensions["tripplan"]["rebuild_fn"] = rebuild_artifacts
    client.post("/trips/kyoto/artifacts", data={"_csrf": csrf()})
    app.extensions["tripplan"]["registry"].get("kyoto").thread.join(5)

    assert client.get("/trips/kyoto/itinerary").status_code == 200
    assert "待重建" not in client.get("/trips/kyoto").get_data(as_text=True)


def test_the_itinerary_route_refuses_path_traversal(client):
    assert client.get("/trips/..%2f..%2fetc/itinerary").status_code == 404
