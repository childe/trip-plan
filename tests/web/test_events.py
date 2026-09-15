"""EventLog：磁盘历史 vs 内存 live ring（spec §5.4 / §5.5 / §9 回归 2、3、4、20、25）。"""

import json

from tripplan.web.events import Event, EventLog, EventLogStore


def _log(tmp_path, **kw):
    return EventLog(tmp_path / "events.jsonl", **kw)


def test_seq_starts_at_one_and_is_monotonic(tmp_path):
    log = _log(tmp_path)
    assert log.append("generating", {"args": ["A"]}).seq == 1
    assert log.append("generating", {"args": ["B"]}).seq == 2


def test_durable_events_land_on_disk_after_flush(tmp_path):
    log = _log(tmp_path)
    log.append("generating", {"args": ["A"]})
    log.flush()
    lines = (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["type"] == "generating"


def test_transient_events_are_readable_but_never_written(tmp_path):
    """§9 回归 3：下期的 token 事件量级是每秒几十上百条，落盘会直接撑爆
    events.jsonl（spec §5.3）。"""
    log = _log(tmp_path)
    log.append("token", {"text": "京"}, stream_id="s1", durable=False)
    log.flush()

    assert [e.type for e in log.since(0).events] == ["token"]
    assert (
        not (tmp_path / "events.jsonl").exists()
        or (tmp_path / "events.jsonl").read_text(encoding="utf-8") == ""
    )


def test_restart_continues_the_sequence_from_the_file(tmp_path):
    """§9 回归 4：seq 从文件里的最大值往后接，不从 1 重来（spec §5.4）。"""
    first = _log(tmp_path)
    first.append("generating", {"args": ["A"]})
    first.append("generating", {"args": ["B"]})
    first.flush()

    second = _log(tmp_path)
    assert second.append("generating", {"args": ["C"]}).seq == 3


def test_a_reloaded_log_replays_history_in_its_snapshot(tmp_path):
    first = _log(tmp_path)
    first.append("generating", {"args": ["A"]})
    first.flush()

    snap = _log(tmp_path).snapshot()
    assert [e.type for e in snap.events] == ["generating"]
    assert snap.cursor == 1


def test_a_corrupt_line_is_skipped_not_fatal(tmp_path):
    (tmp_path / "events.jsonl").write_text(
        '{"seq":1,"ts":1.0,"type":"a","payload":{}}\nnot json\n', encoding="utf-8"
    )
    log = _log(tmp_path)
    assert [e.seq for e in log.snapshot().events] == [1]
    assert log.append("b", {}).seq == 2


def test_since_returns_only_the_increment_and_keeps_seq_contiguous(tmp_path):
    """§9 回归 2 的后半条。"""
    log = _log(tmp_path)
    for key in "ABCD":
        log.append("generating", {"args": [key]})
    result = log.since(2)
    assert [e.seq for e in result.events] == [3, 4]
    assert result.last_seq == 4
    assert result.reset_required is False


def test_snapshot_merges_disk_history_and_the_live_ring_without_duplicates(tmp_path):
    """spec §5.5：快照 = 磁盘 durable 历史 + ring 当前内容，按 seq 归并去重。
    没有去重的话，已 flush 的事件会在页面上出现两遍。"""
    log = _log(tmp_path)
    log.append("a", {})
    log.flush()  # 1 号同时在磁盘和 ring 里
    log.append("t", {}, durable=False)  # 2 号只在 ring 里
    snap = log.snapshot()
    assert [e.seq for e in snap.events] == [1, 2]
    assert snap.cursor == 2


def test_snapshot_cursor_is_the_high_water_mark_not_the_disk_max(tmp_path):
    """spec §5.5 的收敛性前提：cursor 是**此刻已分配出去的最大 seq**。
    取「磁盘上的最大 seq」会让 reset→reload→再 reset 变成死循环。"""
    log = _log(tmp_path)
    log.append("a", {})
    log.flush()
    log.append("t", {}, durable=False)
    log.append("t", {}, durable=False)
    assert log.snapshot().cursor == 3


def test_a_cursor_behind_the_ring_demands_a_reset(tmp_path):
    """§9 回归 20 前半条：中间那段已经被挤出内存，再返回 first_seq 之后的
    事件就是默默吞掉一段。"""
    log = _log(tmp_path, ring_size=3)
    for _ in range(10):
        log.append("t", {}, durable=False)
    result = log.since(1)
    assert result.reset_required is True
    assert result.resume_seq == 10
    assert result.events == []


def test_a_stale_epoch_demands_a_reset(tmp_path):
    """§9 回归 20 后半条：重启后新事件会复用客户端已经见过的号段，
    单看 seq 分不出「这是新事件」还是「这是我早就有的那条」。"""
    log = _log(tmp_path)
    log.append("a", {})
    assert log.since(0, epoch="别的进程").reset_required is True
    assert log.since(0, epoch=log.stream_epoch).reset_required is False


def test_reset_converges_after_exactly_one_refresh(tmp_path):
    """§9 回归 25：守的是收敛性，不是单次行为。

    ring 被一批 durable=False 的事件挤爆、这些事件不在 events.jsonl 里时，
    「快照 = 磁盘历史」会让 reload 后的新游标又落在 first_seq 之前——一个
    每秒 reload 一次、永远读不完内容的死循环（spec §5.5）。
    """
    log = _log(tmp_path, ring_size=3)
    log.append("a", {})
    log.flush()
    for _ in range(20):
        log.append("t", {}, durable=False)

    assert log.since(1).reset_required is True  # 旧游标失效
    snap = log.snapshot()
    assert log.since(snap.cursor, epoch=snap.stream_epoch).reset_required is False


def test_a_fresh_log_does_not_demand_a_reset_for_since_zero(tmp_path):
    """空 ring 上的边界：新建行程第一次进详情页，游标是 0。若这里判成
    reset_required，页面会 reload、再拿到 0、再 reset——同一个死循环。"""
    log = _log(tmp_path)
    assert log.since(0).reset_required is False
    assert log.since(0).events == []


def test_a_cursor_exactly_one_behind_the_ring_is_still_servable(tmp_path):
    """off-by-one：ring 首元素 seq = first_seq，游标 first_seq - 1 的客户端
    要的是 first_seq 起的事件，ring 完全服务得了，不该触发 reset。"""
    log = _log(tmp_path, ring_size=3)
    for _ in range(5):
        log.append("t", {}, durable=False)
    result = log.since(2)  # ring 里是 3、4、5，first_seq = 3
    assert result.first_seq == 3
    assert result.reset_required is False
    assert [e.seq for e in result.events] == [3, 4, 5]


def test_event_to_json_has_the_wire_shape_the_frontend_expects(tmp_path):
    ev = Event(seq=12, ts=1757900000.1, type="generating", payload={"args": ["foodie"]})
    assert ev.to_json() == {
        "seq": 12,
        "ts": 1757900000.1,
        "type": "generating",
        "payload": {"args": ["foodie"]},
        "stream_id": None,
    }


def test_store_hands_out_one_log_per_trip_sharing_one_epoch(tmp_path):
    store = EventLogStore(tmp_path)
    (tmp_path / "kyoto").mkdir()
    (tmp_path / "osaka").mkdir()
    a, b = store.get("kyoto"), store.get("osaka")
    assert a is store.get("kyoto")  # 同一 tid 复用同一份
    assert a is not b
    assert a.stream_epoch == b.stream_epoch == store.stream_epoch


def test_store_creates_the_trip_directory_lazily_on_first_write(tmp_path):
    store = EventLogStore(tmp_path)
    log = store.get("kyoto")  # 目录还不存在也不许崩
    log.append("a", {})
    log.flush()
    assert (tmp_path / "kyoto" / "events.jsonl").exists()
