import json
import multiprocessing as mp
from pathlib import Path

import pytest

from tripplan.repo import FileRepo, TripExists, TripNotFound
from tripplan.state import Stage, TripState
from tripplan.wire import dumps


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


def test_write_is_atomic_no_partial_file(tmp_path: Path):
    """临时文件 + os.replace：目录里不该留下半截文件。"""
    repo = FileRepo(tmp_path / "kyoto")
    repo.create(_state(0))
    s = repo.load()
    s.revision = 1
    repo.save_if_revision(s, expected=0)
    names = {p.name for p in repo.dir.iterdir()}
    assert names == {"state.json", ".lock"}
    json.loads((repo.dir / "state.json").read_text())  # 合法 JSON
