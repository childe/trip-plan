"""鉴权、CSRF、tid 校验、体积上限（spec §6.0 / §6.5 / §9 回归 5、9、19）。"""

import pytest

from tripplan.repo import FileRepo
from tripplan.state import Stage, TripState


def _seed(trips_root, tid="kyoto", raw="十一想去京都玩5天"):
    state = TripState.new(raw, run_id="r1")
    state.stage, state.revision = Stage.AWAIT_CHOICE, 3
    FileRepo(trips_root / tid).create(state)


# ---------- Basic Auth ----------


def test_without_a_token_the_service_is_open(client):
    assert client.get("/").status_code == 200


def test_with_a_token_an_anonymous_request_gets_401_and_a_challenge(make_app):
    c = make_app(token="hunter2").test_client()
    resp = c.get("/")
    assert resp.status_code == 401
    assert "Basic" in resp.headers["WWW-Authenticate"]


def test_the_right_credentials_get_through(make_app):
    import base64

    c = make_app(token="hunter2").test_client()
    cred = base64.b64encode(b"trip:hunter2").decode()
    assert c.get("/", headers={"Authorization": f"Basic {cred}"}).status_code == 200


def test_a_wrong_password_or_username_is_rejected(make_app):
    import base64

    c = make_app(token="hunter2").test_client()
    for raw in (b"trip:wrong", b"admin:hunter2"):
        cred = base64.b64encode(raw).decode()
        assert c.get("/", headers={"Authorization": f"Basic {cred}"}).status_code == 401


# ---------- CSRF（中间件层；四个真实 POST 路由的覆盖在 Task 12） ----------


def test_every_post_is_guarded_by_default_not_by_remembering_a_decorator(app, client):
    """spec §6.5：before_request 里对所有 POST 统一拦，默认全拦。
    这条用一个临时注册的路由证明「默认就拦」，而不是逐个路由记得加装饰器。"""
    app.add_url_rule("/_probe", "_probe", lambda: "ok", methods=["POST"])
    assert client.post("/_probe").status_code == 403


def test_a_matching_token_passes(app, client, csrf):
    app.add_url_rule("/_probe", "_probe", lambda: "ok", methods=["POST"])
    assert client.post("/_probe", data={"_csrf": csrf()}).status_code == 200


def test_a_wrong_token_is_rejected(app, client, csrf):
    app.add_url_rule("/_probe", "_probe", lambda: "ok", methods=["POST"])
    csrf()
    assert client.post("/_probe", data={"_csrf": "别的值"}).status_code == 403


def test_the_csrf_token_is_stable_within_a_session(client, csrf):
    assert csrf() == csrf()


def test_auth_is_checked_before_csrf(make_app):
    """顺序要对：未鉴权的请求应该拿 401 去登录，而不是一头雾水的 403。"""
    c = make_app(token="hunter2").test_client()
    assert c.post("/trips", data={"request": "去京都"}).status_code == 401


# ---------- tid 校验 ----------


@pytest.mark.parametrize(
    "path",
    [
        "/trips/..%2f..%2fetc%2fpasswd",
        "/trips/%2e%2e",
        "/trips/%2e%2e%2f%2e%2e%2fetc",
    ],
)
def test_path_traversal_is_refused(client, path):
    """§9 回归 5。对局域网暴露的服务这是硬要求（spec §6.0）。"""
    assert client.get(path).status_code == 404


def test_a_chinese_directory_name_works(client, trips_root):
    """中文目录名可正常工作——这一期存在的全部理由就是中文（spec §6.0）。"""
    _seed(trips_root, "十一去京都")
    resp = client.get("/trips/%E5%8D%81%E4%B8%80%E5%8E%BB%E4%BA%AC%E9%83%BD")
    assert resp.status_code == 200


def test_an_unknown_trip_is_404(client):
    assert client.get("/trips/nope").status_code == 404


# ---------- 体积上限 ----------


def test_an_oversized_body_is_refused_by_flask(client, csrf):
    """MAX_CONTENT_LENGTH = 64 KiB（spec §6.5）。局域网暴露的服务不能
    任由请求体撑爆内存。"""
    token = csrf()
    resp = client.post("/trips", data={"_csrf": token, "request": "去" * 40_000})
    assert resp.status_code == 413


# ---------- 列表页 ----------


def test_the_index_lists_trips_with_stage_and_revision(client, trips_root):
    _seed(trips_root)
    body = client.get("/").get_data(as_text=True)
    assert "kyoto" in body
    assert "十一想去京都玩5天" in body
    assert "等你选方案" in body


def test_a_corrupt_directory_does_not_blow_up_the_index(client, trips_root):
    """§9 回归 9：列表页仍 200 且标为「损坏」。"""
    _seed(trips_root)
    (trips_root / "broken").mkdir()
    (trips_root / "broken" / "state.json").write_text(
        "not json at all", encoding="utf-8"
    )

    resp = client.get("/")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "kyoto" in body
    assert "损坏" in body


def test_the_index_escapes_user_text(client, trips_root):
    """autoescape 全程开着（Global Constraint 4）。"""
    _seed(trips_root, raw="<script>alert(1)</script>")
    body = client.get("/").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


def test_no_template_uses_safe_or_markup():
    """§9 回归 16 后半条：全库扫一遍 web/templates/。这条测试是纪律本身——
    将来任何人往模板里写 |safe，它当场红（spec §6.1）。"""
    from pathlib import Path

    import tripplan.web

    root = Path(tripplan.web.__file__).parent / "templates"
    for path in root.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        assert "|safe" not in text, path
        assert "| safe" not in text, path
        assert "Markup" not in text, path
