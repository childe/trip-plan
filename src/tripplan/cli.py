"""CLI。只剩两个非交互子命令：`trip web` 与 `trip render`。

交互全部搬到浏览器：终端 IME 输入错乱（光标位置算错、退格删掉半个字、
长句折行后彻底花掉）在 readline 里绕不过去，而 <textarea> 天然没有这个问题。
Web 层是 orchestrator 的第二个 driver，与本模块平级（spec §1 / §3）。
"""

import argparse
import logging
import os
import sys
import uuid
from pathlib import Path

from tripplan.agents.limits import LimitExceeded
from tripplan.artifacts import publish, stage_artifacts
from tripplan.deps import Deps
from tripplan.naming import (
    slugify,
)  # noqa: F401  （重新导出，保持 trip.cli.slugify 可用）
from tripplan.providers.base import ProviderError
from tripplan.repo import FileRepo, TripCorrupt, TripNotFound

from tripplan.llm.errors import ConfigError, MissingCredential  # noqa: F401

# MissingCredential 从 llm.errors 重新导出：tests/test_cli.py 与下面的
# except 都从 tripplan.cli 拿它，必须是同一个类对象。

# ---------- 依赖装配 ----------


def build_provider(dry_run: bool = False):
    """只构造地理 provider，不碰 LLM 凭据——trip render 用得到这个但从不需要
    ANTHROPIC_API_KEY，不该因为缺一个它压根不用的凭据就失败。

    dry_run 或缺 AMAP_KEY 时返回 None，调用方要把「没有 provider」理解成
    「跳过地图」；绝不能拿 FakeProvider 顶替——那张占位图结构合法但与目的地
    毫无关系，混进最终会被转发给同行者的 HTML 里比没有图更糟。
    """
    if dry_run:
        return None
    amap_key = os.environ.get("AMAP_KEY")
    if not amap_key:
        return None

    from tripplan.providers.amap import AmapProvider
    from tripplan.providers.cache import DiskCache

    cache = DiskCache(
        Path(os.environ.get("TRIPPLAN_CACHE", Path.home() / ".cache" / "tripplan"))
    )
    return AmapProvider(key=amap_key, cache=cache)


def _config_path() -> Path | None:
    """TRIPPLAN_CONFIG 优先于 TRIPPLAN_ROLES（旧名，保留兼容）。

    两者同时设置时提示一句，免得用户"改了文件却没生效"还查不出来。
    """
    new = os.environ.get("TRIPPLAN_CONFIG")
    old = os.environ.get("TRIPPLAN_ROLES")
    if new and old:
        print(
            f"提示：TRIPPLAN_CONFIG 与 TRIPPLAN_ROLES 都设置了，"
            f"使用 TRIPPLAN_CONFIG（{new}），忽略 TRIPPLAN_ROLES（{old}）。",
            file=sys.stderr,
        )
    chosen = new or old
    return Path(chosen) if chosen else None


def build_deps(dry_run: bool = False) -> Deps:
    from tripplan.providers.fake import FakeProvider

    if dry_run:
        return Deps(client=None, provider=FakeProvider())

    # 先查地理 provider 的凭据，再构造任何东西：新用户第一次跑 `trip web` 最容易
    # 撞见这个——裸 KeyError: 'AMAP_KEY' 什么都没告诉他，得给读得懂的中文提示 +
    # 一条真能走的出路。规划过程真的要用 provider 去解析 POI/路线，缺了就是硬错
    # 误，不像 render 那样可以体面地跳过地图。
    #
    # 出路这句话只能指向**现在还存在**的东西：交互命令 plan/resume 连同它们的
    # `--dry-run` 已经删掉了，入口只剩 web 与 render，谁都不认这个开关。照着旧
    # 提示敲 `trip web --dry-run` 收获的是 `unrecognized arguments` + exit 2，
    # 把人指进死胡同比不给指引更坏。tests/test_cli.py 里有条测试扫这条消息里的
    # 每个 --flag，逼它和子命令的 --help 对得上。
    provider = build_provider(dry_run=False)
    if provider is None:
        raise MissingCredential(
            "缺少环境变量 AMAP_KEY（高德开放平台的 key，用于路线查询与静态地图）。"
            "请先执行 `export AMAP_KEY=你的高德key` 再运行。"
            "网页界面绕不开它：行程里的路线时长与地图都得现查。"
            "（如果只是想把已有的 state.json 重新渲染一份，用 `trip render`，"
            "它不需要这个 key，只是产出的 HTML 里不会有地图。）"
        )

    from tripplan.llm.config import load_config
    from tripplan.llm.router import RoutingClient

    # RoutingClient 在构造时就把每个被引用到的 model 的 backend 建出来并校验
    # 凭据——不能拖到第一次 chat：那时抛出的 MissingCredential 会被
    # orchestrator._safe_slot 吞成「候选线出现未处理异常」，下面 main() 的
    # except MissingCredential 永远等不到。
    return Deps(client=RoutingClient(load_config(_config_path())), provider=provider)


# ---------- 子命令 ----------


def _cmd_render(args) -> int:
    if args.format not in ("md", "html", "both"):
        print(f"错误：不支持的格式 {args.format}", file=sys.stderr)
        return 1
    repo = FileRepo(Path(args.dir))
    try:
        state = repo.load()
    except TripNotFound:
        print(f"错误：找不到 {args.dir}/state.json", file=sys.stderr)
        return 1
    except TripCorrupt as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1
    # render 必须纯粹：只读 state.json，不碰 advance，不写回 state。
    # 有 AMAP_KEY 就用真实 provider 补地图；没有就跳过地图——绝不能拿
    # FakeProvider 的占位图顶替。render 从不需要 LLM 凭据，所以走
    # build_provider 而不是 build_deps，连 LLM 客户端都不必构造。
    # 与 Web job 共用同一条原子发布路径（spec §7）：自己现生成一个 stage_id，
    # 所以与正在跑的 Web job 共存也不会互相踩暂存文件。
    publish(
        stage_artifacts(
            state,
            repo.dir,
            build_provider(dry_run=False),
            uuid.uuid4().hex,
            fmt=args.format,
        )
    )
    print(f"已写入 {repo.dir}")
    return 0


# ---------- 网页界面 ----------


def _serve(app, host: str, port: int) -> None:
    """单独抽出来只为可测：测试要拦住它，不能真的起一个服务器。

    **不用 Flask 自带的开发服务器**：它明确不适合对外提供服务，而本设计
    要绑 0.0.0.0。waitress 是纯 Python、跨平台、无需编译工具链的生产级
    WSGI 服务器，而且**单进程多线程**——JobRegistry 天然是一份（spec §3.3 / §6.4）。
    """
    from waitress import serve

    serve(app, host=host, port=port, threads=8)


def _require_web_deps() -> None:
    """提前把 flask / waitress 的缺席撞出来，好在 `_cmd_web` 里翻译成人话。

    单独一个函数只为两件事：**(a)** 在真正 import `tripplan.web.app`（它会连带
    拉起整个 Flask 应用模块）之前就失败；**(b)** 测试能 monkeypatch 它来模拟
    「这台机器没装 web extra」——总不能为了测一句提示语真去卸载 flask。
    """
    import waitress  # noqa: F401 —— 真正用它在 _serve 里，这里只探测存在性

    from tripplan.web.app import create_app  # noqa: F401


_WEB_EXTRA_HINT = (
    "错误：网页界面需要额外依赖，但没装上（缺 {missing}）。\n"
    "请执行 `uv sync --extra web` 装上 flask 与 waitress（开发环境用 `uv sync --extra dev`）；\n"
    "它们是可选依赖，`trip render` 用不到，所以默认不装。"
)

_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _cmd_web(args) -> int:
    from tripplan.artifacts import sweep_stale_staging

    token = os.environ.get("TRIPPLAN_WEB_TOKEN")
    if args.host not in _LOCAL_HOSTS and not token:
        # 不给「裸奔到局域网」留口子（spec §6.5）。
        print(
            f"错误：--host {args.host} 会把服务暴露到局域网，但没有设置访问口令。\n"
            "请先执行 `export TRIPPLAN_WEB_TOKEN=你自己想的口令` 再启动"
            "（浏览器会弹出登录框，用户名固定是 trip）；\n"
            "或者去掉 --host，只在本机 127.0.0.1 上访问。",
            file=sys.stderr,
        )
        return 1

    try:
        _require_web_deps()
    except ModuleNotFoundError as e:
        print(
            _WEB_EXTRA_HINT.format(missing=e.name or "flask / waitress"),
            file=sys.stderr,
        )
        return 1

    from tripplan.web.app import create_app

    trips_root = Path(args.trips_dir)
    trips_root.mkdir(parents=True, exist_ok=True)
    # 启动时扫一次 .staging/ 的孤儿子目录（spec §4.1）。只删够老的：别的
    # 进程（一个正在跑的 trip render）可能正往自己的子目录里写。
    sweep_stale_staging(trips_root)

    deps = build_deps(dry_run=False)  # 凭据问题在这里就炸，不拖到 job 里
    app = create_app(trips_root, deps, token=token)

    where = "本机" if args.host in _LOCAL_HOSTS else "本机与局域网"
    print(f"行程目录：{trips_root.resolve()}")
    print(f"服务已启动（{where}）：http://{args.host}:{args.port}/")
    if token:
        print("访问需要口令：用户名 trip，密码取自 TRIPPLAN_WEB_TOKEN。")
    _serve(app, args.host, args.port)
    return 0


def main(argv=None) -> int:
    # 诊断日志默认完全静默。本设计里 backend 的旁路诊断（请求的 token 预算
    # 与实际用量、工具参数解析失败的原文、base_url 形态提示）全部走 logging
    # 的 debug 级——不加这个开关，那些信息永远没人看得见。
    level = os.environ.get("TRIPPLAN_LOG", "").lower()
    if level in ("debug", "info"):
        logging.basicConfig(level=getattr(logging, level.upper()), stream=sys.stderr)

    parser = argparse.ArgumentParser(prog="trip", description="旅行规划")
    sub = parser.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("web", help="启动网页界面")
    w.add_argument(
        "--host",
        default="127.0.0.1",
        help="绑定地址；0.0.0.0 需要设 TRIPPLAN_WEB_TOKEN",
    )
    w.add_argument("--port", type=int, default=8000)
    w.add_argument("--trips-dir", default="trips", help="行程目录的父目录")
    w.set_defaults(func=_cmd_web)

    d = sub.add_parser("render", help="从 state.json 重新生成产物")
    d.add_argument("dir")
    d.add_argument("--format", default="both", help="md | html | both")
    d.set_defaults(func=_cmd_render)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ProviderError, LimitExceeded) as e:
        # 现在只有 `trip render` 会走到这里（Web 侧的运行期错误由 job 转成
        # JobOutcome，不冒到这一层）。
        kind = "高德或大模型服务" if isinstance(e, ProviderError) else "资源额度"
        print(f"错误：{kind}出了问题，本次操作已中断：{e}", file=sys.stderr)
        return 1
    except ConfigError as e:
        # 配置读不懂是用户的输入问题，不是程序 bug——给一句人话，
        # 不要把 TOMLDecodeError / ValueError 的 traceback 糊到脸上。
        print(f"错误：{e}", file=sys.stderr)
        return 1
    except MissingCredential as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1
    except TripNotFound as e:
        # repo.py 的文档明说这两个类型存在的意义就是「给一句读得懂的话，
        # 而不是一截 JSON 栈回溯」；这里把那两句话复用过来即可。
        print(f"错误：找不到 {e}", file=sys.stderr)
        return 1
    except TripCorrupt as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
