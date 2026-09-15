"""产物的暂存与原子发布（spec §4.1 / §9 回归 14、24）。"""

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from tripplan import artifacts as artifacts_mod
from tripplan.artifacts import (
    ARTIFACTS_JSON,
    STAGING_DIR,
    artifact_ready,
    discard,
    publish,
    stage_artifacts,
    sweep_stale_staging,
)
from tripplan.models.common import Field, Origin
from tripplan.models.facts import FactSnapshot
from tripplan.models.itinerary import Angle, Itinerary
from tripplan.models.requirements import DateRange, Party, Requirements
from tripplan.providers.fake import FakeProvider
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


def _done_state(rev=7, title="古寺"):
    s = TripState.new("去京都", run_id="r1")
    s.revision = rev
    s.requirements = Requirements(
        destination=Field("京都", Origin.USER),
        dates=Field(DateRange(D1, D1), Origin.USER),
        party=Field(Party(adults=2), Origin.USER),
    )
    angle = Angle("A", title, "")
    s.candidates = [
        CandidateSlot(angle, Itinerary(angle=angle), _facts(), SlotStatus.OK)
    ]
    s.stage, s.chosen_key = Stage.DONE, "A"
    return s


def test_stage_writes_into_its_own_subdirectory_only(tmp_path):
    staged = stage_artifacts(_done_state(), tmp_path, FakeProvider(), "job-1")
    assert staged.stage_dir == tmp_path / STAGING_DIR / "job-1"
    assert (staged.stage_dir / "itinerary.html").exists()
    # 最终路径此刻必须还是空的——CAS 还没发生
    assert not (tmp_path / "itinerary.html").exists()
    assert not (tmp_path / ARTIFACTS_JSON).exists()


def test_publish_moves_files_and_writes_artifacts_json_last(tmp_path):
    state = _done_state(rev=7)
    staged = stage_artifacts(state, tmp_path, FakeProvider(), "job-1")
    publish(staged)

    assert (tmp_path / "itinerary.html").exists()
    assert (tmp_path / "itinerary.md").exists()
    data = json.loads((tmp_path / ARTIFACTS_JSON).read_text(encoding="utf-8"))
    assert data["revision"] == 7
    assert "itinerary.html" in data["files"]
    assert not staged.stage_dir.exists()  # 发布完删掉自己的暂存目录


def test_discard_removes_only_its_own_subdirectory(tmp_path):
    """§9 回归 24 的后半条。"""
    a = stage_artifacts(
        _done_state(title="A 的成稿"), tmp_path, FakeProvider(), "job-a"
    )
    b = stage_artifacts(
        _done_state(title="B 的成稿"), tmp_path, FakeProvider(), "job-b"
    )
    discard(a)
    assert not a.stage_dir.exists()
    assert b.stage_dir.exists()
    assert (b.stage_dir / "itinerary.html").exists()


def test_two_writers_do_not_clobber_each_other(tmp_path):
    """§9 回归 24：两个写者（Web job 与另一个进程的 trip render）从**不同的
    state** 各自暂存。最终 itinerary.html 的内容必须来自 publish() 那一方，
    而不是「revision 对得上但内容是另一份」。"""
    loser = stage_artifacts(
        _done_state(rev=7, title="输家的成稿"), tmp_path, FakeProvider(), "job-a"
    )
    winner = stage_artifacts(
        _done_state(rev=9, title="赢家的成稿"), tmp_path, FakeProvider(), "job-b"
    )

    discard(loser)
    publish(winner)

    html = (tmp_path / "itinerary.html").read_text(encoding="utf-8")
    assert "赢家的成稿" in html
    assert "输家的成稿" not in html
    assert (
        json.loads((tmp_path / ARTIFACTS_JSON).read_text(encoding="utf-8"))["revision"]
        == 9
    )


def test_concurrent_publishes_never_mix_a_manifest_with_another_publishs_html(
    tmp_path, monkeypatch
):
    """§9 回归 24 的另一半：按写者隔离暂存目录只挡住了**暂存阶段**互相踩，
    `publish()` 自己那串「逐个 os.replace + 最后写 artifacts.json」在跨进程
    并发下仍然不是一个整体。

    强制交错出最坏的那一种（两个写者：Web job 刚 CAS 到 rev 9，另一个进程里
    早先读到 rev 7 的 `trip render`）：

    1. 新写者把 itinerary.html 换成 rev 9 的内容；
    2. 新写者**还没写 manifest**；
    3. 旧写者整轮跑完 —— itinerary.html 被换回 rev 7 的内容，manifest 写成 rev 7；
    4. 新写者这才写下 manifest rev 9。

    盘上于是是「manifest 说 rev 9、HTML 讲的是 rev 7 那份行程」。state.json
    正好也是 9，`artifact_ready()` 一路绿灯，详情页亮出成稿链接，用户点进去
    看到的是上一版——没有任何报错，谁也查不出来。这正是 spec §4.1 点名要
    杜绝的坏结局 (a)，只是发生在 publish 阶段而不是 staging 阶段。

    不变式：**manifest 里的 revision 与盘上 HTML 的来源必须是同一次 publish。**
    谁最后落地不重要（落地的是旧版就只是 artifact_ready 判不就绪 → 「产物待
    重建」，安全且可恢复），混搭才是致命的。
    """
    import threading

    old = stage_artifacts(
        _done_state(rev=7, title="旧版成稿"), tmp_path, FakeProvider(), "job-old"
    )
    new = stage_artifacts(
        _done_state(rev=9, title="新版成稿"), tmp_path, FakeProvider(), "job-new"
    )

    at_manifest = threading.Event()  # 新写者：replace 做完了，manifest 还没写
    old_done = threading.Event()  # 旧写者：整轮跑完了
    real_write = artifacts_mod._atomic_write_json

    def hooked(path, payload):
        if payload.get("revision") == 9:
            at_manifest.set()
            old_done.wait(0.3)  # 加了锁之后旧写者被挡住，这里必然超时——正常
        real_write(path, payload)

    monkeypatch.setattr(artifacts_mod, "_atomic_write_json", hooked)

    def run_old():
        at_manifest.wait(5)
        try:
            publish(old)
        finally:
            old_done.set()

    t = threading.Thread(target=run_old, name="old-writer")
    t.start()
    publish(new)
    t.join(10)
    assert not t.is_alive()

    data = json.loads((tmp_path / ARTIFACTS_JSON).read_text(encoding="utf-8"))
    html = (tmp_path / "itinerary.html").read_text(encoding="utf-8")
    expected = {7: "旧版成稿", 9: "新版成稿"}[data["revision"]]
    assert expected in html, f"manifest 说 rev {data['revision']}，HTML 却不是那一份"
    assert artifact_ready(tmp_path, data["revision"])


def test_artifact_ready_requires_matching_revision_and_existing_files(tmp_path):
    state = _done_state(rev=7)
    publish(stage_artifacts(state, tmp_path, FakeProvider(), "job-1"))

    assert artifact_ready(tmp_path, 7)
    assert not artifact_ready(tmp_path, 8)  # 旧版本产物残留
    (tmp_path / "itinerary.html").unlink()
    assert not artifact_ready(tmp_path, 7)  # 手工删了文件


def test_artifact_ready_is_false_without_any_manifest(tmp_path):
    assert not artifact_ready(tmp_path, 1)


def test_artifact_ready_survives_a_corrupt_manifest(tmp_path):
    (tmp_path / ARTIFACTS_JSON).write_text("not json", encoding="utf-8")
    assert not artifact_ready(tmp_path, 1)


def test_an_empty_manifest_is_not_ready(tmp_path):
    """`all([])` 是 True —— 照 spec §4.1 的字面写法，一个 files 为空的
    manifest 会拿到假绿灯，详情页于是亮出一个指向不存在文件的成稿链接
    （spec §6.1「不给死链」）。"""
    s = TripState.new("去京都", run_id="r1")
    s.revision = 3
    publish(stage_artifacts(s, tmp_path, FakeProvider(), "job-1"))

    assert (
        json.loads((tmp_path / ARTIFACTS_JSON).read_text(encoding="utf-8"))["files"]
        == []
    )
    assert not (tmp_path / "itinerary.html").exists()
    assert not artifact_ready(tmp_path, 3)


def test_a_markdown_only_manifest_is_not_ready(tmp_path):
    """`trip render <dir> --format md`（§7 支持的第二个写者）发布的 manifest
    里压根没有 HTML。revision 对得上、列出的文件也都在，但成稿页给不出东西。"""
    state = _done_state(rev=7)
    publish(stage_artifacts(state, tmp_path, FakeProvider(), "job-1", fmt="md"))

    assert (tmp_path / "itinerary.md").exists()
    assert not artifact_ready(tmp_path, 7)


def test_a_markdown_only_manifest_does_not_bless_a_leftover_html(tmp_path):
    """这条是上一条里真正危险的那一半，必须单独钉住：`--format md` **不会删掉**
    上一版留下的 itinerary.html。若就绪判定只看「files 里的都在」，它会给出
    一份 rev 7 的旧成稿，却宣称这是 rev 9 —— manifest 的 revision 与 state.json
    严丝合缝对得上，没有任何报错，谁也查不出来。"""
    publish(
        stage_artifacts(
            _done_state(rev=7, title="上一版的成稿"), tmp_path, FakeProvider(), "job-a"
        )
    )
    assert "上一版的成稿" in (tmp_path / "itinerary.html").read_text(encoding="utf-8")

    publish(
        stage_artifacts(
            _done_state(rev=9, title="新版成稿"),
            tmp_path,
            FakeProvider(),
            "job-b",
            fmt="md",
        )
    )

    assert (tmp_path / "itinerary.html").exists()  # 旧文件还在
    assert "上一版的成稿" in (tmp_path / "itinerary.html").read_text(encoding="utf-8")
    assert (
        json.loads((tmp_path / ARTIFACTS_JSON).read_text(encoding="utf-8"))["revision"]
        == 9
    )
    assert not artifact_ready(tmp_path, 9)  # ★ 绝不能放行


def test_publish_and_discard_tolerate_none(tmp_path):
    publish(None)
    discard(None)


def test_stage_is_safe_before_any_candidates(tmp_path):
    s = TripState.new("去京都", run_id="r1")
    staged = stage_artifacts(s, tmp_path, FakeProvider(), "job-1")
    assert staged.names == ()
    publish(staged)
    assert (
        json.loads((tmp_path / ARTIFACTS_JSON).read_text(encoding="utf-8"))["files"]
        == []
    )


def test_stage_never_embeds_a_fake_placeholder_map(tmp_path):
    """provider=None 表示「跳过地图」，不是「用假地图顶替」。"""
    staged = stage_artifacts(_done_state(), tmp_path, None, "job-1")
    assert "<img" not in (staged.stage_dir / "itinerary.html").read_text(
        encoding="utf-8"
    )


def test_format_md_only_does_not_stage_html(tmp_path):
    staged = stage_artifacts(_done_state(), tmp_path, FakeProvider(), "job-1", fmt="md")
    assert "itinerary.md" in staged.names
    assert "itinerary.html" not in staged.names


def test_format_html_only_does_not_stage_markdown(tmp_path):
    staged = stage_artifacts(
        _done_state(), tmp_path, FakeProvider(), "job-1", fmt="html"
    )
    assert "itinerary.html" in staged.names
    assert "itinerary.md" not in staged.names
    assert not any(n.startswith("plan-") for n in staged.names)


def test_sweep_removes_only_old_orphans(tmp_path):
    """启动时扫一次 .staging/ 的孤儿子目录，但只删 mtime 超过 1 小时的——
    别的进程（一个正在跑的 trip render）可能正往自己的子目录里写（spec §4.1）。"""
    import os
    import time

    trip = tmp_path / "kyoto"
    old = trip / STAGING_DIR / "old-job"
    fresh = trip / STAGING_DIR / "fresh-job"
    old.mkdir(parents=True)
    fresh.mkdir(parents=True)
    ancient = time.time() - 7200
    os.utime(old, (ancient, ancient))

    assert sweep_stale_staging(tmp_path, max_age_s=3600) == 1
    assert not old.exists()
    assert fresh.exists()


def test_sweep_tolerates_a_missing_trips_root(tmp_path):
    assert sweep_stale_staging(tmp_path / "nope") == 0
