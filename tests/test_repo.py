import json
import multiprocessing as mp
from decimal import Decimal
from pathlib import Path

import pytest

from tripplan.models.common import Field
from tripplan.models.requirements import Basis, BudgetSpec, Requirements
from tripplan.repo import FileRepo, TripCorrupt, TripExists, TripNotFound
from tripplan.state import Stage, TripState
from tripplan.wire import UnsupportedVersion


def _state(rev: int = 0) -> TripState:
    s = TripState.new("去京都", run_id="r1")
    s.revision = rev
    return s


def test_create_then_load_roundtrips(tmp_path: Path):
    repo = FileRepo(tmp_path / "kyoto")
    repo.create(_state())
    assert repo.load().raw_request == "去京都"


def test_create_refuses_to_overwrite(tmp_path: Path):
    repo = FileRepo(tmp_path / "kyoto")
    repo.create(_state())
    with pytest.raises(TripExists):
        repo.create(_state())


def test_load_missing_trip_raises(tmp_path: Path):
    with pytest.raises(TripNotFound):
        FileRepo(tmp_path / "nope").load()


def test_load_corrupt_state_raises_clear_error(tmp_path: Path):
    """损坏的 state.json 应该报一个说得清楚的错误，而不是原始 JSON 栈回溯。"""
    repo = FileRepo(tmp_path / "kyoto")
    repo.create(_state(0))
    (repo.dir / "state.json").write_text("not json at all", encoding="utf-8")

    with pytest.raises(TripCorrupt) as exc_info:
        repo.load()

    message = str(exc_info.value)
    assert str(repo.dir / "state.json") in message
    assert "损坏" in message


def test_load_null_json_raises_trip_corrupt(tmp_path: Path):
    """合法 JSON（null）但不是对象：decode_state 对它 .get(...) 会炸
    AttributeError——这条不在原来枚举的异常类型里，必须也归为 TripCorrupt。"""
    repo = FileRepo(tmp_path / "kyoto")
    repo.create(_state(0))
    (repo.dir / "state.json").write_text("null", encoding="utf-8")

    with pytest.raises(TripCorrupt):
        repo.load()


def test_load_non_numeric_money_amount_raises_trip_corrupt(tmp_path: Path):
    """结构合法，但 BudgetSpec.amount 不是数字：Decimal(...) 抛
    decimal.InvalidOperation（ArithmeticError 的子类，不是 ValueError）——
    同样必须落到 TripCorrupt，而不是裸的 decimal 异常。"""
    repo = FileRepo(tmp_path / "kyoto")
    s = _state(0)
    s.requirements = Requirements(
        budget=Field(value=BudgetSpec(Decimal("100"), "JPY", Basis.TOTAL, frozenset()))
    )
    repo.create(s)

    raw = json.loads((repo.dir / "state.json").read_text(encoding="utf-8"))
    raw["requirements"]["budget"]["value"]["amount"] = "NOT-A-NUMBER"
    (repo.dir / "state.json").write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(TripCorrupt):
        repo.load()


def test_load_too_new_format_version_is_unsupported_not_corrupt(tmp_path: Path):
    """UnsupportedVersion 是那条例外：文件是更新版本的工具写的，用户该升级
    工具而不是删目录。回归哨兵——防止以后重构把这条也吞成 TripCorrupt。"""
    repo = FileRepo(tmp_path / "kyoto")
    repo.create(_state(0))

    raw = json.loads((repo.dir / "state.json").read_text(encoding="utf-8"))
    raw["format_version"] = raw["format_version"] + 1000
    (repo.dir / "state.json").write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(UnsupportedVersion):
        repo.load()


def test_save_succeeds_when_expected_matches_disk(tmp_path: Path):
    repo = FileRepo(tmp_path / "kyoto")
    repo.create(_state(0))
    s = repo.load()
    s.revision = 1
    s.stage = Stage.AWAIT_REQ_CONFIRM
    assert repo.save_if_revision(s, expected=0) is True
    assert repo.load().revision == 1


def test_save_fails_when_disk_moved_on(tmp_path: Path):
    """lost update 的核心用例：两个写者都基于 rev=0，第二个必须失败。"""
    repo = FileRepo(tmp_path / "kyoto")
    repo.create(_state(0))

    a = repo.load()
    b = repo.load()  # 两个写者读到同一个 revision

    a.revision = 1
    assert repo.save_if_revision(a, expected=0) is True

    b.revision = 1
    assert repo.save_if_revision(b, expected=0) is False  # ★ 被挡住
    assert repo.load().revision == 1


def test_failed_save_does_not_touch_disk(tmp_path: Path):
    repo = FileRepo(tmp_path / "kyoto")
    repo.create(_state(0))
    good = repo.load()
    good.revision = 1
    repo.save_if_revision(good, expected=0)
    before = (repo.dir / "state.json").read_text()

    stale = _state(9)
    assert repo.save_if_revision(stale, expected=0) is False
    assert (repo.dir / "state.json").read_text() == before


def _child(trip_dir: str, queue) -> None:
    repo = FileRepo(Path(trip_dir))
    s = repo.load()
    s.revision = 1
    queue.put(repo.save_if_revision(s, expected=0))


def test_concurrent_writers_exactly_one_wins(tmp_path: Path):
    repo = FileRepo(tmp_path / "kyoto")
    repo.create(_state(0))

    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    procs = [ctx.Process(target=_child, args=(str(repo.dir), q)) for _ in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)

    results = [q.get() for _ in range(4)]
    assert sum(results) == 1  # 恰好一个成功
    assert repo.load().revision == 1


def test_replace_failure_leaves_old_state_and_no_tmp_file(tmp_path: Path, monkeypatch):
    """临时文件 + os.replace 才是真原子：os.replace 中途失败时，旧
    state.json 必须原封不动，目录里也不该留下 .tmp 半截文件。一个直接写
    state.json（不经过临时文件）的实现在这里会失败。"""
    repo = FileRepo(tmp_path / "kyoto")
    repo.create(_state(0))
    before = (repo.dir / "state.json").read_text()

    def _boom(*args, **kwargs):
        raise OSError("simulated os.replace failure")

    monkeypatch.setattr("tripplan.repo.os.replace", _boom)

    s = repo.load()
    s.revision = 1
    with pytest.raises(OSError):
        repo.save_if_revision(s, expected=0)

    assert (repo.dir / "state.json").read_text() == before
    names = {p.name for p in repo.dir.iterdir()}
    assert names == {"state.json", ".lock"}
