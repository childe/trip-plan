"""模板只吃结构化对象，绝不吃 Markdown 串（spec §6.1）。"""

from datetime import date

from tripplan.models.common import Field, Origin
from tripplan.models.issue import Issue, Severity, Source
from tripplan.models.itinerary import Angle, Day, Itinerary
from tripplan.models.requirements import DateRange, Party, Requirements
from tripplan.repo import FileRepo
from tripplan.state import CandidateSlot, SlotStatus, Stage, TripState
from tripplan.web.jobs import JobRegistry
from tripplan.web.view import candidate_vms, event_text, req_card_vm, trip_rows

D1 = date(2026, 10, 1)


def test_req_card_vm_splits_fields_into_independent_attributes():
    """把 Requirements 摊成模板能直接遍历的字段：label、值、is_inferred、
    rationale 各自独立，模板自己出 HTML 结构。"""
    reqs = Requirements(
        destination=Field("京都", Origin.USER),
        dates=Field(DateRange(D1, D1), Origin.USER),
        party=Field(Party(adults=2), Origin.USER),
        pace=Field(None, None),
        lodging_area=Field("四条", Origin.MODEL, rationale="按预算推断"),
    )
    vm = req_card_vm(reqs)
    by_name = {f.name: f for f in vm.fields}

    assert by_name["destination"].label == "目的地"
    assert by_name["destination"].value == "京都"
    assert by_name["destination"].is_inferred is False
    assert by_name["lodging_area"].is_inferred is True
    assert by_name["lodging_area"].rationale == "按预算推断"
    assert "pace" not in by_name  # 无取值的字段不出现
    assert vm.missing == []


def test_req_card_vm_lists_missing_required_labels():
    vm = req_card_vm(Requirements(destination=Field("京都", Origin.USER)))
    assert vm.missing == ["日期", "人员"]


def test_req_card_vm_never_returns_markdown():
    """反面断言：这是把 render_requirement_card() 塞进 Jinja 的那条路
    被禁掉的原因——autoescape 开着就显示 `**目的地**` 的星号，用 |safe
    就等于把模型输出与用户输入当可信 HTML 注入（spec §6.1）。"""
    vm = req_card_vm(Requirements(destination=Field("京都", Origin.USER)))
    blob = "".join(f.label + f.value + f.rationale for f in vm.fields)
    assert "**" not in blob
    assert "- " not in blob


def test_candidate_vm_carries_counts_status_and_issues():
    angle = Angle("foodie", "吃遍京都", "从早市到居酒屋")
    itin = Itinerary(
        angle=angle,
        days=[
            Day(id="d1", date=D1, activities=[]),
            Day(id="d2", date=D1, activities=[]),
        ],
        issues=[Issue(Severity.WARNING, Source.CRITIC, "C1", "第二天略赶")],
    )
    [vm] = candidate_vms(
        [
            CandidateSlot(
                angle, itin, None, SlotStatus.EXHAUSTED, "修订 3 次后仍有 1 个硬伤"
            )
        ]
    )

    assert vm.key == "foodie"
    assert vm.title == "吃遍京都"
    assert vm.days == 2
    assert vm.activities == 0
    assert vm.selectable is True
    assert vm.status == "EXHAUSTED"
    assert vm.detail == "修订 3 次后仍有 1 个硬伤"
    assert [i.message for i in vm.issues] == ["第二天略赶"]
    assert vm.issues[0].mark == "🟡"


def test_a_candidate_without_an_itinerary_is_not_selectable():
    angle = Angle("_error", "角度生成失败", "")
    [vm] = candidate_vms(
        [CandidateSlot(angle, None, None, SlotStatus.FAILED, "角度生成失败：限流")]
    )
    assert vm.selectable is False
    assert vm.detail == "角度生成失败：限流"


def test_trip_rows_marks_a_corrupt_directory_instead_of_blowing_up(tmp_path):
    """§9 回归 9 的数据层：一个坏目录不能把整页炸掉（spec §6.1）。"""
    good = FileRepo(tmp_path / "kyoto")
    state = TripState.new("十一想去京都玩5天", run_id="r1")
    state.stage, state.revision = Stage.AWAIT_CHOICE, 3
    good.create(state)

    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "state.json").write_text("not json at all", encoding="utf-8")

    (tmp_path / "not-a-trip").mkdir()  # 连 state.json 都没有：直接跳过

    rows = {r.tid: r for r in trip_rows(tmp_path, JobRegistry())}
    assert rows["kyoto"].corrupt is False
    assert rows["kyoto"].stage == "AWAIT_CHOICE"
    assert rows["kyoto"].revision == 3
    assert "京都" in rows["kyoto"].summary
    assert rows["broken"].corrupt is True
    assert "not-a-trip" not in rows


def test_trip_rows_reports_a_running_job(tmp_path):
    import threading

    from tripplan.web.events import EventLog
    from tripplan.web.jobs import JobOutcome

    FileRepo(tmp_path / "kyoto").create(TripState.new("去京都", run_id="r1"))
    reg = JobRegistry()
    gate = threading.Event()
    job = reg.start(
        "kyoto",
        EventLog(tmp_path / "kyoto" / "events.jsonl"),
        lambda j: (gate.wait(5), JobOutcome.ok(1))[1],
    )
    try:
        assert trip_rows(tmp_path, reg)[0].running is True
    finally:
        gate.set()
        job.thread.join(5)


def test_event_text_renders_every_known_event_type_in_chinese():
    """事件文案在**服务端**渲染（spec §6.3 结尾），前端只负责追加 DOM。"""
    cases = [
        ({"type": "stage_started", "payload": {"args": ["GENERATE"]}}, "生成候选"),
        ({"type": "angles_picked", "payload": {"args": [["A", "B"]]}}, "A"),
        ({"type": "generating", "payload": {"args": ["foodie"]}}, "foodie"),
        ({"type": "revision", "payload": {"args": ["foodie", 0]}}, "foodie"),
        (
            {
                "type": "requirements_patched",
                "payload": {"args": [{"destination": "大阪"}]},
            },
            "需求",
        ),
        ({"type": "angle_generation_failed", "payload": {"args": ["限流"]}}, "限流"),
        ({"type": "diversity_retry", "payload": {"args": ["B", ["B001"]]}}, "B"),
        ({"type": "paused", "payload": {"args": ["AWAIT_CHOICE", 4]}}, "等你"),
        (
            {
                "type": "job_failed",
                "payload": {
                    "job_id": "x",
                    "kind": "ProviderError",
                    "message": "高德限流",
                },
            },
            "高德限流",
        ),
        (
            {
                "type": "job_cancelled",
                "payload": {"job_id": "x", "kind": "Cancelled", "message": "已取消"},
            },
            "取消",
        ),
    ]
    for ev, needle in cases:
        assert needle in event_text(ev), ev["type"]


def test_event_text_degrades_gracefully_on_malformed_payloads():
    """事件是从磁盘回读的，可能是旧版本写的、也可能残缺。渲染函数不许崩。"""
    assert event_text({"type": "generating", "payload": {}}) == "generating"
    assert event_text({"type": "未来的类型", "payload": {"args": [1]}}) == "未来的类型"
    assert event_text({}) == ""
