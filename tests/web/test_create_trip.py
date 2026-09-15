"""POST /trips（spec §6.2）。"""

import pytest

from tripplan.repo import FileRepo
from tripplan.state import Stage, TripState


def test_a_new_trip_is_created_and_redirects_to_its_detail_page(
    client, csrf, trips_root, runner
):
    resp = client.post("/trips", data={"_csrf": csrf(), "request": "十一想去京都玩5天"})
    assert resp.status_code == 302
    assert "/trips/" in resp.headers["Location"]
    assert (trips_root / "十一想去京都玩5天").joinpath("state.json").exists()


def test_the_directory_name_defaults_to_the_existing_slugify(client, csrf, trips_root):
    client.post("/trips", data={"_csrf": csrf(), "request": "十一想去京都玩5天！"})
    assert (trips_root / "十一想去京都玩5天").is_dir()


def test_an_explicit_dir_is_honoured(client, csrf, trips_root):
    client.post(
        "/trips", data={"_csrf": csrf(), "request": "去京都", "dir": "kyoto-2026"}
    )
    assert (trips_root / "kyoto-2026" / "state.json").exists()


def test_the_first_command_is_dispatched_with_cmd_none(client, csrf, runner):
    """新建即开跑：后台线程跑 run_command(..., cmd=None, ...)（spec §6.2）。"""
    client.post("/trips", data={"_csrf": csrf(), "request": "去京都"})
    _wait(runner)
    assert runner.calls and runner.calls[0][1] is None


def _wait(runner, n=1, timeout=5):
    import time

    deadline = time.time() + timeout
    while len(runner.calls) + len(runner.rebuilds) < n and time.time() < deadline:
        time.sleep(0.01)


def test_an_empty_request_is_400_and_keeps_what_was_typed(client, csrf):
    resp = client.post(
        "/trips", data={"_csrf": csrf(), "request": "   ", "dir": "kyoto"}
    )
    body = resp.get_data(as_text=True)
    assert resp.status_code == 400
    assert "kyoto" in body  # 已输入内容被保留


def test_an_overlong_request_is_400_and_keeps_what_was_typed(client, csrf):
    """守的是**字符数**上限（8000 字 → 400 + 一句读得懂的话），不是
    MAX_CONTENT_LENGTH 那道字节上限（→ 413，由 Flask 在路由之前就拦掉）。

    所以这里刻意用 ASCII：表单体是百分号编码的，一个中文字 3 字节编成 9 个
    字符，9000 个中文字的请求体约 81 KB，早就撞上 64 KiB 那道线，拿到的是
    413 而不是这条分支的 400 —— 那样测的就是另一个机制了。ASCII 一字一字节，
    9000 字的请求体约 9 KB，稳稳落在字节上限之内，只会被字符数这道线拦下。
    """
    long_text = "a" * 9000  # 9000 字 > 8000 上限；请求体 ~9 KB，远小于 64 KiB
    resp = client.post("/trips", data={"_csrf": csrf(), "request": long_text})
    assert resp.status_code == 400
    assert "8000" in resp.get_data(as_text=True)


def test_an_overlong_dir_is_400(client, csrf):
    resp = client.post(
        "/trips", data={"_csrf": csrf(), "request": "去京都", "dir": "x" * 81}
    )
    assert resp.status_code == 400


@pytest.mark.parametrize("bad", ["../escape", "a/b", "..", "."])
def test_a_dir_that_is_not_a_single_path_segment_is_400(client, csrf, bad, trips_root):
    resp = client.post(
        "/trips", data={"_csrf": csrf(), "request": "去京都", "dir": bad}
    )
    assert resp.status_code == 400
    assert not (trips_root.parent / "escape").exists()


def test_an_existing_directory_is_409_with_a_direct_link(client, csrf, trips_root):
    FileRepo(trips_root / "kyoto").create(TripState.new("去京都", run_id="r1"))
    resp = client.post(
        "/trips", data={"_csrf": csrf(), "request": "去京都", "dir": "kyoto"}
    )
    body = resp.get_data(as_text=True)
    assert resp.status_code == 409
    assert "已存在" in body
    assert "/trips/kyoto" in body


def test_a_full_server_still_keeps_the_trip_and_says_so(make_app, trips_root):
    """spec §6.2：此时**行程目录已经建好**，提示「已创建，但服务器正忙，
    稍后进去点『继续』」并给直达链接——不静默丢掉用户刚敲的那段需求。"""
    import threading

    from tripplan.web.events import EventLog
    from tripplan.web.jobs import JobOutcome, JobRegistry

    reg = JobRegistry(max_jobs=1)
    gate = threading.Event()
    app = make_app(registry=reg)
    c = app.test_client()
    c.get("/")
    with c.session_transaction() as sess:
        token = sess["_csrf"]

    busy = reg.start(
        "占位",
        EventLog(trips_root / "占位" / "events.jsonl"),
        lambda j: (gate.wait(5), JobOutcome.ok(1))[1],
    )
    try:
        resp = c.post(
            "/trips", data={"_csrf": token, "request": "去京都", "dir": "kyoto"}
        )
        body = resp.get_data(as_text=True)
        assert resp.status_code == 503
        assert (trips_root / "kyoto" / "state.json").exists()  # 需求没被丢掉
        assert "正忙" in body
        assert "/trips/kyoto" in body
    finally:
        gate.set()
        busy.thread.join(5)
