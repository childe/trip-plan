"""CLI driver。阻塞发生在这一层，orchestrator 内部不阻塞。

纪律：调 advance 之前记下盘上的 revision，之后拿它作 expected 提交。
advance 在暂停时会自增 revision，拿自增后的值去 CAS 必然失败。
"""

import argparse
import logging
import os
import re
import sys
import uuid
from pathlib import Path

from tripplan.agents.limits import LimitExceeded
from tripplan.deps import Deps
from tripplan.orchestrator import advance as _advance
from tripplan.providers.base import ProviderError
from tripplan.artifacts import publish, stage_artifacts
from tripplan.naming import (
    slugify,
)  # noqa: F401  （重新导出，保持 trip.cli.slugify 可用）
from tripplan.render.candidates import render_candidates
from tripplan.render.requirement_card import render_requirement_card
from tripplan.repo import FileRepo, TripCorrupt, TripExists, TripNotFound
from tripplan.state import (
    AmendRequirements,
    ChooseCandidate,
    ConfirmRequirements,
    Done,
    GiveFeedback,
    InputKind,
    NeedInput,
    Rejected,
    Stage,
    TripState,
)

from tripplan.llm.errors import ConfigError, MissingCredential  # noqa: F401

# MissingCredential 从 llm.errors 重新导出：tests/test_cli.py 与下面的
# except 都从 tripplan.cli 拿它，必须是同一个类对象。

# ---------- 交互 ----------


def _resolve_candidate_key(typed: str, slots) -> str:
    """把用户敲的东西对回**真实存在的**候选 key，大小写不敏感。

    原来这里是无条件 `.upper()`。角度 key 由 LLM 自己取名（Angle 的
    docstring：「由 LLM 自己想，不写死枚举」，prompts/angle.md 里对 key
    一个字都没提），所以它完全可能是 `foodie`。界面照原样打印
    `选一份（foodie/…）`，用户照抄 `foodie`，CLI 却送出 `FOODIE` ——
    `UNKNOWN_CANDIDATE`，而且这个提示下**没有任何输入能成功**：重试多少
    次都一样，用户已经为一整轮三候选的规划付过钱，唯一出路是 Ctrl-C。
    （CJK key 侥幸没事，因为 upper() 对它是恒等。）

    修法取「大小写不敏感地匹配真实 key」而不是「在 prompt 里约束 key」：
    后者依赖模型守规矩，这个项目一路上反复被这个假设打脸；前者不依赖
    任何人守规矩。对不上就原样传下去——那时 UNKNOWN_CANDIDATE 是一句
    诚实的话，而不是一个由 CLI 自己制造出来的谎。
    """
    folded = typed.casefold()
    for slot in slots or []:
        key = getattr(getattr(slot, "angle", None), "key", None)
        if isinstance(key, str) and key.casefold() == folded:
            return key  # 回填真实 key，大小写以候选为准
    return typed


def terminal_ask(need: NeedInput):
    if need.kind is InputKind.CONFIRM_REQUIREMENTS:
        print(render_requirement_card(need.payload))
        answer = input("\n回车确认，或直接说要改什么> ").strip()
        return (
            ConfirmRequirements(need.revision)
            if not answer
            else AmendRequirements(need.revision, answer)
        )

    print(render_candidates(need.payload))
    raw = input("\n输入方案号定稿（如 A），或「A 第2天太赶了」提意见> ").strip()
    if not raw:
        return ConfirmRequirements(need.revision)  # 会被拒，重新问
    head, _, rest = raw.partition(" ")
    key = _resolve_candidate_key(head.strip(), need.payload)
    return (
        ChooseCandidate(need.revision, key)
        if not rest.strip()
        else GiveFeedback(need.revision, key, rest.strip())
    )


def _print_event(event) -> None:
    print(f"  · {event}", file=sys.stderr)


# ---------- driver ----------


def drive(state, repo, deps, ask, out, persisted: int, advance_fn=None):
    """plan 与 resume 共用。persisted 跟踪的是「盘上是什么」。"""
    advance_fn = advance_fn or _advance
    outcome = advance_fn(state, deps, None, _print_event)

    while True:
        if isinstance(outcome, Done):
            if not repo.save_if_revision(state, persisted):
                out("⚠️ 另一个进程改动了这个行程，已放弃写入。")
                return None
            return outcome.itinerary

        # 到这里 outcome 必然是一个 NeedInput，且相对 persisted 有实打实的
        # 新内容——要么是最开头那次调用，要么是上一轮成功推进后落到的暂停点。
        # 必须先落盘再去问人：万一问完就崩，刚花掉的 LLM/网络成本不会白费。
        if not repo.save_if_revision(state, persisted):
            out("⚠️ 另一个进程改动了这个行程，请重新 resume。")
            return None
        persisted = state.revision  # ★ 提交后才更新

        outcome = advance_fn(state, deps, ask(outcome), _print_event)
        while isinstance(outcome, Rejected):
            out(f"⚠️ {outcome.reason.value}")
            if outcome.current is None:
                # 这个阶段压根不在等人（工作态被塞了一条命令）。走不到这里
                # 才是常态——drive 只在收到 NeedInput 之后才发命令——但真到了
                # 这一步，重新问是问不出来的：没有问题可问。说清楚并退出，
                # 强过对着一个编出来的问题空转。
                out(
                    "⚠️ 当前阶段并不在等待输入，无法继续交互；"
                    "请用 `trip resume <行程目录>` 重新接上。"
                )
                return None
            # 状态未变、revision 未变——advance 的校验分支保证了这一点。
            # 不能再走上面那次 save_if_revision：那是一次没有意义的重写，
            # 只会白白占用一次 CAS 窗口，让本来什么都没做错的这次调用在
            # 撞上并发写者时被误杀。直接拿 outcome.current 重新问即可。
            outcome = advance_fn(state, deps, ask(outcome.current), _print_event)


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

    # 先查地理 provider 的凭据，再构造任何东西：新用户第一次跑最容易撞见这个——
    # 裸 KeyError: 'AMAP_KEY' 什么都没告诉他，读得懂的中文提示 + --dry-run
    # 出路才有用。plan/resume 真的要用 provider 去解析 POI/路线，缺了就是
    # 硬错误，不像 render 那样可以体面地跳过地图。
    provider = build_provider(dry_run=False)
    if provider is None:
        raise MissingCredential(
            "缺少环境变量 AMAP_KEY（高德开放平台的 key，用于路线查询与静态地图）。"
            "请先执行 `export AMAP_KEY=你的高德key` 再运行；"
            "如果只是想在没有凭据的情况下试跑工具，加 --dry-run。"
        )

    from tripplan.llm.config import load_config
    from tripplan.llm.router import RoutingClient

    # RoutingClient 在构造时就把每个被引用到的 model 的 backend 建出来并校验
    # 凭据——不能拖到第一次 chat：那时抛出的 MissingCredential 会被
    # orchestrator._safe_slot 吞成「候选线出现未处理异常」，下面 main() 的
    # except MissingCredential 永远等不到。
    return Deps(client=RoutingClient(load_config(_config_path())), provider=provider)


# ---------- 子命令 ----------


def _cmd_plan(args) -> int:
    trip_dir = Path(args.dir or Path("trips") / slugify(args.request))
    repo = FileRepo(trip_dir)
    state = TripState.new(args.request, run_id=uuid.uuid4().hex[:12])
    try:
        repo.create(state)
    except TripExists:
        print(
            f"错误：{trip_dir} 已存在。换个 --dir，或用 "
            f"`trip resume {trip_dir}` 继续。",
            file=sys.stderr,
        )
        return 1

    print(f"行程目录：{trip_dir}")
    if args.dry_run:
        return 0
    return _drive_and_report(state, repo, args)


def _cmd_resume(args) -> int:
    repo = FileRepo(Path(args.dir))
    try:
        state = repo.load()
    except TripNotFound:
        print(f"错误：找不到 {args.dir}/state.json", file=sys.stderr)
        return 1
    except TripCorrupt as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1
    print(f"已载入 rev {state.revision}，阶段 {state.stage.value}")
    if args.dry_run:
        # 与 _cmd_plan 的早返回对称。没有这一句的话，Deps(client=None) 会被
        # 交给 advance()。已经推进到 COLLECT 及之后的行程会在 LLM 步骤炸出
        # AttributeError: 'NoneType' object has no attribute 'chat'——一条
        # 不在 main() except 元组里的裸 traceback，比 argparse 干净拒绝更糟。
        # （停在 AWAIT_REQ_CONFIRM 的行程走的是另一条路：advance 在①校验
        # 阶段就短路返回 _pending(state)，根本碰不到 deps.client；drive 转去
        # terminal_ask()，非交互下 input() 撞 EOF，被 main() 的 EOFError 分支
        # 接住返回 1。本文件的回归测试用的正是这种状态，它守住的是「退出码
        # 不为 0」，不是这条 AttributeError——AttributeError 那条链要到
        # COLLECT 阶段才成立，值得留着但不能算在这条测试头上。）
        # 而 §6.2 的凭据错误消息正好把用户指向这条 --dry-run 逃生口。
        return 0
    return _drive_and_report(state, repo, args)


def _drive_and_report(state, repo, args) -> int:
    deps = build_deps(dry_run=getattr(args, "dry_run", False))
    itinerary = drive(state, repo, deps, terminal_ask, print, persisted=state.revision)
    if itinerary is None:
        # drive() 返回 None 的主因是某次 save_if_revision 输掉了 CAS：盘上的
        # state 已经被别的进程改动，我们手上这份内存中的 state 不再权威。
        # （另一个来源是拒绝循环拿到 current=None——那种情况同样没有可用的
        # 结局可写。）这时候绝不能拿它去写 write_artifacts——那会让
        # itinerary.md/html 描述的是「我们这边以为的结局」，而 state.json
        # 里记的是赢得那场 CAS 的另一个进程的结局，两者对不上。
        return 1
    publish(stage_artifacts(state, repo.dir, deps.provider, uuid.uuid4().hex))
    print(f"\n✅ 已定稿：{repo.dir / 'itinerary.md'}")
    print(f"   网页版：{repo.dir / 'itinerary.html'}")
    return 0


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


def main(argv=None) -> int:
    # 诊断日志默认完全静默。本设计里 backend 的旁路诊断（请求的 token 预算
    # 与实际用量、工具参数解析失败的原文、base_url 形态提示）全部走 logging
    # 的 debug 级——不加这个开关，那些信息永远没人看得见。
    level = os.environ.get("TRIPPLAN_LOG", "").lower()
    if level in ("debug", "info"):
        logging.basicConfig(level=getattr(logging, level.upper()), stream=sys.stderr)

    parser = argparse.ArgumentParser(prog="trip", description="旅行规划")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan", help="开始一次新的规划")
    p.add_argument("request", help="自然语言需求")
    p.add_argument("--dir", help="行程目录，默认按需求生成")
    p.add_argument("--dry-run", action="store_true", help="只建目录，不调 LLM 与高德")
    p.set_defaults(func=_cmd_plan)

    r = sub.add_parser("resume", help="接着上次的进度继续")
    r.add_argument("dir")
    r.add_argument(
        "--dry-run", action="store_true", help="只载入并报告状态，不调 LLM 与高德"
    )
    r.set_defaults(func=_cmd_resume)

    d = sub.add_parser("render", help="从 state.json 重新生成产物")
    d.add_argument("dir")
    d.add_argument("--format", default="both", help="md | html | both")
    d.set_defaults(func=_cmd_render)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ProviderError, LimitExceeded) as e:
        # advance() 在这两种情形下会原样往外抛（见 orchestrator.py 顶部注释）：
        # 高德/LLM 传输层故障，或某条候选线撞了资源额度。两者都是环境/资源
        # 问题，不是程序 bug——不能让原始 traceback 糊到用户脸上。
        # state 只在 advance() 正常返回之后才落盘（drive() 的 CAS 纪律），
        # 所以中断前最后一次成功的进度还在磁盘上，可以直接 resume；正在跑的
        # 这一步没有存下来，说没存下来就够了，不要承诺更多。
        kind = "高德或大模型服务" if isinstance(e, ProviderError) else "资源额度"
        print(
            f"错误：{kind}出了问题，本次操作已中断：{e}\n"
            "上一步已经保存到磁盘的进度还在，行程目录没有损坏；"
            "可以用 `trip resume <行程目录>` 接着跑（正在进行的这一步需要重来）。",
            file=sys.stderr,
        )
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
        # _cmd_resume / _cmd_render 只在启动时的 repo.load() 外侧兜住了这两个
        # 类型，可是 save_if_revision 内部同样会检查存在性、同样会 _decode()
        # 盘上的文件 —— 会话**中途**被另一个进程删掉/改坏时，异常是从
        # drive() 的 CAS 循环里抛出来的，此前一路裸奔到用户脸上。repo.py 的
        # 文档明说这两个类型存在的意义就是「给一句读得懂的话，而不是一截
        # JSON 栈回溯」；这里把那两句话复用过来即可。
        print(f"错误：找不到 {e}", file=sys.stderr)
        return 1
    except TripCorrupt as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1
    except EOFError:
        # `trip resume dir < /dev/null`、管道输入、或者在提示上按 Ctrl-D：
        # input() 抛 EOFError，此前一路裸奔成 traceback。这不是程序出错，
        # 是没有人可问了；进度的处境和 ProviderError 那条一样，说清楚即可。
        print(
            "错误：标准输入已结束（Ctrl-D 或非交互式输入），本次操作已中断。\n"
            "上一步已经保存到磁盘的进度还在，行程目录没有损坏；"
            "可以在交互式终端里用 `trip resume <行程目录>` 接着跑。",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
