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
from urllib.parse import quote

from flask import Flask, abort, redirect, render_template, request, session

from tripplan.naming import slugify
from tripplan.repo import FileRepo, TripExists
from tripplan.state import TripState
from tripplan.web.events import EventLogStore
from tripplan.web.jobs import ServerBusy, TripBusy
from tripplan.web.jobs import JobRegistry, rebuild_artifacts, run_command
from tripplan.web.view import trip_rows

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
        return (
            render_template(
                "notice.html",
                title="行程正忙",
                message="这个行程正在跑上一步，等它结束再操作。",
                link_tid=tid,
            ),
            409,
        )
    except ServerBusy:
        if busy_page is not None:
            return busy_page()
        return (
            render_template(
                "notice.html",
                title="服务器正忙",
                message="同时进行的规划已达上限，稍后再试。",
                link_tid=tid,
            ),
            503,
        )
    return redirect(f"/trips/{quote(tid)}", code=302)


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
