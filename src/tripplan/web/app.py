"""Flask 应用工厂 + 路由。只做「鉴权 → 校验参数 → 调 registry → 渲染」。

选 Flask + waitress、服务端渲染、前端零构建零框架的理由见 spec §3.3：
advance() 是同步阻塞的，同步 WSGI 框架与之直接对上；项目已有 models/ +
wire.py，引入 pydantic 等于并存两套模型体系；render/itinerary_html.py 已经
产出 HTML 串，服务端渲染能直接复用。

**必须单进程**（spec §6.4）：JobRegistry 是进程内单例，多 worker 会让轮询
请求被路由到没有该 job 的进程，进度页随机失灵、取消按钮随机失效。
"""

import hmac
import os
import secrets
import uuid
from pathlib import Path

from flask import (
    Flask,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

from tripplan.naming import slugify
from tripplan.repo import FileRepo, TripCorrupt, TripExists, TripNotFound
from tripplan.state import (
    AmendRequirements,
    ChooseCandidate,
    ConfirmRequirements,
    GiveFeedback,
    Stage,
    TripState,
)
from tripplan.web.events import EventLogStore
from tripplan.web.jobs import (
    JobRegistry,
    ServerBusy,
    TripBusy,
    rebuild_artifacts,
    run_command,
)
from tripplan.web.view import (
    artifact_ready,
    candidate_vms,
    event_text,
    req_card_vm,
    trip_rows,
)
from tripplan.wire import UnsupportedVersion

#: 用户名固定，只有口令是秘密（spec §6.5）。
BASIC_AUTH_USER = "trip"
MAX_CONTENT_LENGTH = 64 * 1024
MAX_REQUEST_CHARS = 8000
MAX_DIR_CHARS = 80
MAX_ANGLE_KEY_CHARS = 64


def create_app(
    trips_root,
    deps,
    *,
    token: str | None = None,
    secret: str | None = None,
    registry=None,
    store=None,
    max_jobs: int = 3,
    run_command_fn=run_command,
    rebuild_fn=rebuild_artifacts,
) -> Flask:
    trips_root = Path(trips_root)
    app = Flask(__name__)

    # secret_key：未设置就现生成。重启即所有旧表单失效，对单进程本机服务
    # 可接受——**比硬编码一个默认值安全得多**，那种默认值一定会被原样带到
    # 局域网上（spec §6.5）。
    app.secret_key = (
        secret or os.environ.get("TRIPPLAN_WEB_SECRET") or secrets.token_urlsafe(32)
    )
    app.config.update(
        MAX_CONTENT_LENGTH=MAX_CONTENT_LENGTH,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        # 明文 HTTP，见 spec §6.5 的「如实记录代价」一节。
        SESSION_COOKIE_SECURE=False,
        SESSION_COOKIE_PATH="/",
    )

    app.extensions["tripplan"] = {
        "trips_root": trips_root,
        "deps": deps,
        "registry": registry or JobRegistry(max_jobs=max_jobs),
        "store": store or EventLogStore(trips_root),
        "run_command_fn": run_command_fn,
        "rebuild_fn": rebuild_fn,
        "token": token,
    }

    app.jinja_env.globals["csrf_token"] = _csrf_token

    @app.before_request
    def _guard():
        # 顺序刻意：未鉴权的请求应该拿 401 去登录，而不是一头雾水的 403。
        if not _auth_ok(request.authorization, token):
            return (
                render_template(
                    "notice.html", title="需要口令", message="请输入访问口令。"
                ),
                401,
                {"WWW-Authenticate": 'Basic realm="tripplan"'},
            )
        if request.method == "POST":
            # **默认全拦**，不是逐个路由自己记得加装饰器——将来新增的任何
            # POST 自动被覆盖（spec §6.5）。
            expected = session.get("_csrf", "")
            supplied = request.form.get("_csrf", "")
            # 两个 not 不能省：compare_digest("", "") 是 True，少了这一道，
            # 「session 里还没有 token 且表单也没带」会被判成通过——正是
            # 攻击者的跨站表单最容易构造出来的那种请求。
            if not expected or not supplied or not _same_secret(expected, supplied):
                return (
                    render_template(
                        "notice.html",
                        title="表单已过期",
                        message="请刷新页面后重试（服务重启会让旧表单失效）。",
                    ),
                    403,
                )
        return None

    @app.get("/")
    def index():
        cfg = app.extensions["tripplan"]
        return render_template(
            "index.html",
            rows=trip_rows(cfg["trips_root"], cfg["registry"]),
            form={"request": "", "dir": ""},
            error=None,
        )

    @app.post("/trips")
    def create_trip():
        cfg = app.extensions["tripplan"]
        raw = (request.form.get("request") or "").strip()
        wanted_dir = (request.form.get("dir") or "").strip()

        if not raw:
            return _index_error(
                cfg, "请先写点什么——想去哪、什么时候、几个人。", raw, wanted_dir
            )
        if len(raw) > MAX_REQUEST_CHARS:
            return _index_error(
                cfg,
                f"需求太长了（{len(raw)} 字，上限 {MAX_REQUEST_CHARS} 字）。",
                raw,
                wanted_dir,
            )
        if len(wanted_dir) > MAX_DIR_CHARS:
            return _index_error(
                cfg, f"目录名太长了（上限 {MAX_DIR_CHARS} 字）。", raw, wanted_dir
            )

        tid = wanted_dir or slugify(raw)
        if not _is_single_segment(tid):
            return _index_error(
                cfg, "目录名必须是单个名字，不能带 / 或 ..。", raw, wanted_dir
            )

        repo = FileRepo(cfg["trips_root"] / tid)
        try:
            repo.create(TripState.new(raw, run_id=uuid.uuid4().hex[:12]))
        except TripExists:
            return (
                render_template(
                    "notice.html",
                    title="行程已存在",
                    message=f"{tid} 已存在。换个目录名，或者直接进去接着上次的进度。",
                    link_tid=tid,
                ),
                409,
            )

        # 目录已经建好了。下面这一步就算被拒，用户敲的那段需求也没丢。
        return _start_command(
            cfg,
            tid,
            None,
            busy_page=lambda: (
                render_template(
                    "notice.html",
                    title="服务器正忙",
                    message="同时进行的规划已达上限。行程已经创建好了，稍后进去点「继续」即可。",
                    link_tid=tid,
                ),
                503,
            ),
        )

    @app.get("/trips/<tid>")
    def detail(tid):
        cfg = app.extensions["tripplan"]
        return render_template("detail.html", **_detail_context(cfg, tid))

    @app.post("/trips/<tid>/commands")
    def commands(tid):
        cfg = app.extensions["tripplan"]
        resolve_trip_dir(cfg["trips_root"], tid)  # 越界一律 404
        cmd, error = _build_command(request.form)
        if error is not None:
            return _detail_with_notice(cfg, tid, error, 400)
        return _start_command(cfg, tid, cmd)

    @app.post("/trips/<tid>/cancel")
    def cancel(tid):
        cfg = app.extensions["tripplan"]
        resolve_trip_dir(cfg["trips_root"], tid)
        job = cfg["registry"].get(tid)
        if job is not None:
            # 幂等：没有 job 在跑、或已经是 cancelling 时都是 no-op，
            # 重复点不报错也不 +1 status_version（spec §6.2）。
            job.request_cancel()
        return redirect(url_for("detail", tid=tid), code=302)

    @app.post("/trips/<tid>/artifacts")
    def rebuild(tid):
        cfg = app.extensions["tripplan"]
        trip_dir = resolve_trip_dir(cfg["trips_root"], tid)
        try:
            state = FileRepo(trip_dir).load()
        except (TripCorrupt, TripNotFound, UnsupportedVersion) as e:
            return _detail_with_notice(cfg, tid, str(e), 409)
        if state.stage is not Stage.DONE:
            return _detail_with_notice(
                cfg, tid, "行程还没定稿，没有可重建的成稿产物。", 409
            )

        job_holder = cfg["registry"]
        rebuild_target = cfg["rebuild_fn"]
        try:
            job_holder.start(
                tid,
                cfg["store"].get(tid),
                lambda job: rebuild_target(cfg["trips_root"], tid, cfg["deps"], job),
            )
        except TripBusy:
            # 那个还没退出的线程可能正要 publish()，插一次重建就是两个
            # 发布者抢同一份最终产物（spec §6.2）。
            return _detail_with_notice(
                cfg, tid, "这个行程正在跑上一步，等它结束再重建。", 409
            )
        except ServerBusy:
            return _detail_with_notice(cfg, tid, "服务器正忙，稍后再试。", 503)
        return redirect(url_for("detail", tid=tid), code=302)

    @app.get("/trips/<tid>/events")
    def events(tid):
        cfg = app.extensions["tripplan"]
        trip_dir = resolve_trip_dir(cfg["trips_root"], tid)

        try:
            since = int(request.args.get("since", 0))
        except ValueError:
            since = 0
        epoch = request.args.get("epoch") or None

        result = cfg["store"].get(tid).since(since, epoch)
        job = cfg["registry"].get(tid)

        stage, revision, ready = "", 0, False
        try:
            state = FileRepo(trip_dir).load()
            stage, revision = state.stage.value, state.revision
            ready = artifact_ready(trip_dir, revision)
        except (TripCorrupt, TripNotFound, UnsupportedVersion):
            pass  # 轮询不该因为文件坏了就 500；详情页会把话说清楚

        return jsonify(
            events=[
                {**e.to_json(), "text": event_text(e.to_json())} for e in result.events
            ],
            first_seq=result.first_seq,
            last_seq=result.last_seq,
            stream_epoch=result.stream_epoch,
            reset_required=result.reset_required,
            resume_seq=result.resume_seq,
            job=(
                job.snapshot()
                if job is not None
                else {
                    "id": None,
                    "status": "none",
                    "status_version": 0,
                    "kind": None,
                    "message": None,
                }
            ),
            stage=stage,
            revision=revision,
            artifact_ready=ready,
        )

    @app.get("/trips/<tid>/itinerary")
    def itinerary(tid):
        cfg = app.extensions["tripplan"]
        trip_dir = resolve_trip_dir(cfg["trips_root"], tid)
        try:
            state = FileRepo(trip_dir).load()
        except (TripCorrupt, TripNotFound, UnsupportedVersion) as e:
            return (
                render_template("notice.html", title="读不出这个行程", message=str(e)),
                409,
            )

        path = trip_dir / "itinerary.html"
        if not artifact_ready(trip_dir, state.revision) or not path.exists():
            # **不是裸 404**：详情页在这种状态下本来也不会给出这个链接，
            # 这里是直接输 URL 或用旧书签进来的兜底（spec §6.1）。
            return (
                render_template(
                    "notice.html",
                    title="产物需要重建",
                    message="成稿文件缺失，或与当前 rev 对不上。回到行程页点「重建产物」即可。",
                    link_tid=tid,
                ),
                409,
            )
        # send_file 一份**完整的独立 HTML 文档**，根本不过模板——转义责任在
        # render/itinerary_html.py（既有行为，本期不改，spec §6.1）。
        return send_file(path, mimetype="text/html")

    return app


# ---------- 共用工具（后续任务的路由都调它们） ----------


def _is_single_segment(tid: str) -> bool:
    return (
        bool(tid)
        and tid not in (".", "..")
        and not any(c in tid for c in ("/", "\\", "\0"))
    )


def _index_error(cfg, message: str, raw: str, wanted_dir: str):
    return (
        render_template(
            "index.html",
            rows=trip_rows(cfg["trips_root"], cfg["registry"]),
            form={"request": raw, "dir": wanted_dir},
            error=message,
        ),
        400,
    )


def _start_command(cfg, tid: str, cmd, *, busy_page=None):
    """起一个后台 job 跑一次 advance。TripBusy → 409，ServerBusy → 503。

    请求线程只负责登记后立刻返回，不占 waitress 线程池（spec §4.2）。
    """
    log = cfg["store"].get(tid)
    run = cfg["run_command_fn"]
    try:
        cfg["registry"].start(
            tid, log, lambda job: run(cfg["trips_root"], tid, cmd, cfg["deps"], job)
        )
    except TripBusy:
        return _detail_with_notice(
            cfg, tid, "这个行程正在跑上一步，等它结束再操作。", 409
        )
    except ServerBusy:
        if busy_page is not None:
            return busy_page()
        return _detail_with_notice(cfg, tid, "服务器正忙，稍后再试。", 503)
    return redirect(url_for("detail", tid=tid), code=302)


_KINDS = {"", "confirm", "amend", "choose", "feedback"}


def _build_command(form):
    """把表单摊成 state.py 现成的命令类型。

    顺带消除的一个 bug 面（spec §6.2）：angle_key 来自页面上渲染的按钮 value，
    是真实 key。CLI 里 _resolve_candidate_key() 那整块「大小写兜底」逻辑
    （连同它注释里描述的「用户被困死只能 Ctrl-C」的场景）在 Web 下从根上
    不存在——用户不再手敲 key。
    """
    kind = (form.get("kind") or "").strip()
    if kind not in _KINDS:
        return None, f"不认识的操作：{kind}"

    raw_rev = (form.get("expected_revision") or "").strip()
    try:
        expected = int(raw_rev)
    except ValueError:
        return None, "表单已过期，请刷新页面后重试。"

    text = (form.get("text") or "").strip()
    angle_key = (form.get("angle_key") or "").strip()
    if len(text) > MAX_REQUEST_CHARS:
        return None, f"内容太长了（{len(text)} 字，上限 {MAX_REQUEST_CHARS} 字）。"
    if len(angle_key) > MAX_ANGLE_KEY_CHARS:
        return None, "候选标识不合法。"

    if kind == "":
        return None, None  # 「继续」
    if kind == "confirm":
        return ConfirmRequirements(expected), None
    if kind == "amend":
        if not text:
            return None, "要改什么？写一句再提交。"
        return AmendRequirements(expected, text), None
    if not angle_key:
        return None, "没有指定是哪一份候选。"
    if kind == "choose":
        return ChooseCandidate(expected, angle_key), None
    if not text:
        return None, "意见写一句再提交，或者直接点「选它」。"
    return GiveFeedback(expected, angle_key, text), None


def _detail_context(cfg, tid: str, notice: str | None = None) -> dict:
    trip_dir = resolve_trip_dir(cfg["trips_root"], tid)
    job = cfg["registry"].get(tid)
    snap = cfg["store"].get(tid).snapshot()
    base = {
        "tid": tid,
        "job": job.snapshot() if job is not None else None,
        "active": job is not None and job.active,
        "notice": notice,
        "events": [
            {"seq": e.seq, "stream_id": e.stream_id, "text": event_text(e.to_json())}
            for e in snap.events
        ],
        "cursor": snap.cursor,
        "epoch": snap.stream_epoch,
    }

    try:
        state = FileRepo(trip_dir).load()
    except (TripCorrupt, TripNotFound, UnsupportedVersion) as e:
        # 一个坏目录不该是 500。给一句读得懂的话（repo.py 的既定承诺）。
        return {
            **base,
            "corrupt": str(e),
            "state": None,
            "stage": "",
            "revision": 0,
            "req_card": None,
            "candidates": [],
            "artifact_ready": False,
        }

    return {
        **base,
        "corrupt": None,
        "state": state,
        "stage": state.stage.value,
        "revision": state.revision,
        "req_card": req_card_vm(state.requirements) if state.requirements else None,
        "candidates": candidate_vms(state.candidates),
        # 详情页不靠 `stage is DONE` 决定要不要给链接（spec §4.1）。
        "artifact_ready": artifact_ready(trip_dir, state.revision),
    }


def _detail_with_notice(cfg, tid: str, message: str, status: int):
    return (
        render_template("detail.html", **_detail_context(cfg, tid, notice=message)),
        status,
    )


def _csrf_token() -> str:
    tok = session.get("_csrf")
    if not tok:
        tok = secrets.token_urlsafe(32)
        session["_csrf"] = tok
    return tok


def _same_secret(a: str, b: str) -> bool:
    """定时安全比较，**先编码成 bytes**。

    `hmac.compare_digest` 对 str 只接受 ASCII，喂进一个带中文的串会抛
    `TypeError: comparing strings with non-ASCII characters is not supported`。
    这不是理论问题：CSRF token 直接来自表单（攻击者想放什么放什么），口令
    来自 TRIPPLAN_WEB_TOKEN（用户完全可能设一句中文）。少了这一层，那种
    输入换来的是 500 + 一截堆栈，而不是本该给出的 403 / 401 —— 一个拒绝
    路径被异常顶掉，比拒绝本身更糟。
    """
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _auth_ok(auth, token: str | None) -> bool:
    """用 hmac.compare_digest 而不是 ==：口令比较不留计时侧信道，
    反正一行的事（spec §6.5）。"""
    if token is None:
        return True
    if auth is None or (auth.type or "").lower() != "basic":
        return False
    ok_user = _same_secret(auth.username or "", BASIC_AUTH_USER)
    ok_pass = _same_secret(auth.password or "", token)
    return ok_user and ok_pass


def resolve_trip_dir(trips_root, tid: str) -> Path:
    """tid 必须是单个路径段，且解析后必须是 trips_root 的**直接子目录**。

    对局域网暴露的服务这是硬要求（spec §6.0）。werkzeug 不会把 %2F 合并进
    路径段，所以 `/trips/..%2f..%2fetc%2fpasswd` 到这里 tid 就是
    `../../etc/passwd`，正好被下面这两道挡住。
    """
    if not tid or tid in (".", "..") or any(c in tid for c in ("/", "\\", "\0")):
        abort(404)
    root = Path(trips_root).resolve()
    target = (root / tid).resolve()
    if target.parent != root or not target.is_dir():
        abort(404)
    return target
