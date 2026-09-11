"""CLI driver。阻塞发生在这一层，orchestrator 内部不阻塞。

纪律：调 advance 之前记下盘上的 revision，之后拿它作 expected 提交。
advance 在暂停时会自增 revision，拿自增后的值去 CAS 必然失败。
"""

import argparse
import os
import re
import sys
import uuid
from pathlib import Path

from tripplan.agents.limits import LimitExceeded
from tripplan.deps import Deps
from tripplan.orchestrator import advance as _advance
from tripplan.providers.base import ProviderError
from tripplan.render.candidates import render_candidates
from tripplan.render.itinerary_html import render_itinerary_html
from tripplan.render.itinerary_md import render_itinerary_md
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

# fetch_day_maps 触网（调用 provider），所以它住在 tripplan.maps 而不是
# render/ 下——render/ 里的模块一律不许碰网络（详见 tripplan/maps.py 的说明）。
from tripplan.maps import fetch_day_maps

_SLUG_STRIP = re.compile(r"[^\w一-鿿\s-]", re.U)


class MissingCredential(Exception):
    """跑真实流程缺一个必须的环境变量凭据。给用户看得懂的名字和出路，不是 KeyError。"""


def slugify(text: str) -> str:
    cleaned = _SLUG_STRIP.sub("", text).strip()
    cleaned = re.sub(r"\s+", "-", cleaned)
    return cleaned[:40] or "trip"


# ---------- 交互 ----------


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
    key = head.strip().upper()
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
            # 状态未变、revision 未变——advance 的校验分支保证了这一点。
            # 不能再走上面那次 save_if_revision：那是一次没有意义的重写，
            # 只会白白占用一次 CAS 窗口，让本来什么都没做错的这次调用在
            # 撞上并发写者时被误杀。直接拿 outcome.current 重新问即可。
            outcome = advance_fn(state, deps, ask(outcome.current), _print_event)


# ---------- 产物 ----------


def write_artifacts(state, trip_dir: Path, provider, fmt: str = "both") -> None:
    """provider=None 表示「跳过地图」，不是「用假地图顶替」——见 build_provider
    的说明：FakeProvider 的占位图结构合法但与目的地毫无关系，混进这份最终要
    转发给同行者的 HTML 里比压根没有图更糟，所以这里绝不会拿它当默认值。

    fmt 控制到底写哪些文件：md-only 不该连 itinerary.html 也一起写出来，
    反过来也一样——校验在 _cmd_render 里做过，这里只管照办。
    """
    trip_dir = Path(trip_dir)
    write_md = fmt in ("md", "both")
    write_html = fmt in ("html", "both")

    if write_md:
        for slot in state.candidates:
            if slot.itinerary is None or slot.facts is None:
                continue
            (trip_dir / f"plan-{slot.angle.key}.md").write_text(
                render_itinerary_md(slot.itinerary, slot.facts, state.requirements),
                encoding="utf-8",
            )

    if state.stage is not Stage.DONE:
        return
    slot = state.chosen()
    if slot is None or slot.itinerary is None:
        return
    facts = slot.facts
    if facts is None:
        return

    if write_md:
        (trip_dir / "itinerary.md").write_text(
            render_itinerary_md(slot.itinerary, facts, state.requirements),
            encoding="utf-8",
        )
    if write_html:
        day_maps = (
            fetch_day_maps(slot.itinerary, facts, provider)
            if provider is not None
            else {}
        )
        (trip_dir / "itinerary.html").write_text(
            render_itinerary_html(slot.itinerary, facts, state.requirements, day_maps),
            encoding="utf-8",
        )


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

    from tripplan.llm.client import AnthropicClient
    from tripplan.llm.config import load_config

    cfg_path = os.environ.get("TRIPPLAN_ROLES")
    return Deps(
        client=AnthropicClient(load_config(Path(cfg_path) if cfg_path else None)),
        provider=provider,
    )


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
    return _drive_and_report(state, repo, args)


def _drive_and_report(state, repo, args) -> int:
    deps = build_deps(dry_run=getattr(args, "dry_run", False))
    itinerary = drive(state, repo, deps, terminal_ask, print, persisted=state.revision)
    if itinerary is None:
        # drive() 在这里返回 None 只有一种原因：某次 save_if_revision 输掉了
        # CAS，说明盘上的 state 已经被别的进程改动，我们手上这份内存中的
        # state 不再权威。这时候绝不能拿它去写 write_artifacts——那会让
        # itinerary.md/html 描述的是「我们这边以为的结局」，而 state.json
        # 里记的是赢得那场 CAS 的另一个进程的结局，两者对不上。
        return 1
    write_artifacts(state, repo.dir, deps.provider)
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
    # build_provider 而不是 build_deps，连 AnthropicClient 都不必碰。
    write_artifacts(state, repo.dir, build_provider(dry_run=False), fmt=args.format)
    print(f"已写入 {repo.dir}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="trip", description="旅行规划")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan", help="开始一次新的规划")
    p.add_argument("request", help="自然语言需求")
    p.add_argument("--dir", help="行程目录，默认按需求生成")
    p.add_argument("--dry-run", action="store_true", help="只建目录，不调 LLM 与高德")
    p.set_defaults(func=_cmd_plan)

    r = sub.add_parser("resume", help="接着上次的进度继续")
    r.add_argument("dir")
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
    except MissingCredential as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
