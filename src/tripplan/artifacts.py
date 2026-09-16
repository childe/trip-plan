"""产物的暂存与原子发布。Web 与 `trip render` 共用同一条路径。

为什么不是「CAS 成功后再 write_artifacts」（spec §4.1）：前端一看到 revision
变了 / stage 变成 DONE 就会刷新并亮出成稿链接，而那一刻 itinerary.html 可能
还没开始写、或正在被非原子地覆盖写到一半——用户点进去看到 404 或半截文件。
更糟的是进程在这中间崩掉：state.json 已经是 DONE，产物却永远不存在，之后
每次进详情页都是一个死链。

所以慢且可能失败的那部分（渲染 + 拉高德静态图）挪到 CAS 之前，写进
`<trip>/.staging/<stage_id>/`；CAS 成功后只剩一串 os.replace（同文件系统内
原子改名）+ 写一个 artifacts.json。

暂存目录必须**按写者隔离**，不能是同一 trip 共享的 .staging/：文件名是固定的，
共享一个目录等于让两个写者互相踩，而 Web 的 per-trip 互斥是**进程内**的锁，
管不到另一个进程里的 `trip render`。两种坏结局都是静默的——(a) 发布出去的
artifacts.json revision 与 state.json 完全对得上，但 HTML 讲的是另一份行程；
(b) 败者的 discard() 删掉胜者刚放进去的文件，publish() 搬了个空。

按写者隔离只挡住了**暂存阶段**。publish() 那串「逐个 os.replace + 最后写
artifacts.json」本身还不是一个整体：单个 os.replace 是原子的，一串不是。
两个写者的 publish 交错一下，同样能凑出上面的坏结局 (a)——新写者换完
itinerary.html 还没写 manifest 时，旧写者整轮插进来把 HTML 换回上一版并写下
自己的 manifest，新写者这才写下自己的 → manifest 说 rev 9、HTML 却是 rev 7
那份，而 state.json 正好也是 9，artifact_ready() 一路绿灯。所以 publish()
从第一个 os.replace 到 manifest 落地全程握一把**跨进程**的文件锁（进程内的
per-trip 互斥依然管不到另一个进程里的 trip render）。
"""

import contextlib
import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

try:  # POSIX
    import fcntl
except ModuleNotFoundError:  # pragma: no cover —— Windows
    fcntl = None
    import msvcrt

from tripplan.maps import fetch_day_maps
from tripplan.render.itinerary_html import render_itinerary_html
from tripplan.render.itinerary_md import render_itinerary_md
from tripplan.state import Stage

ARTIFACTS_JSON = "artifacts.json"
STAGING_DIR = ".staging"
FINAL_HTML = "itinerary.html"
PUBLISH_LOCK = ".publish.lock"


@dataclass(frozen=True)
class Staged:
    """一次暂存的结果。names 的顺序就是 publish 时 os.replace 的顺序。"""

    trip_dir: Path
    stage_dir: Path
    revision: int
    names: tuple[str, ...]


def stage_artifacts(
    state, trip_dir, provider, stage_id: str, fmt: str = "both"
) -> Staged:
    """把产物渲染进 `<trip_dir>/.staging/<stage_id>/`，**不碰最终路径**。

    provider=None 表示「跳过地图」，不是「用假地图顶替」——FakeProvider 的
    占位图结构合法但与目的地毫无关系，混进这份最终要转发给同行者的 HTML 里
    比压根没有图更糟。
    """
    trip_dir = Path(trip_dir)
    stage_dir = trip_dir / STAGING_DIR / stage_id
    stage_dir.mkdir(parents=True, exist_ok=True)

    write_md = fmt in ("md", "both")
    write_html = fmt in ("html", "both")
    names: list[str] = []

    if write_md:
        for slot in state.candidates:
            if slot.itinerary is None or slot.facts is None:
                continue
            name = f"plan-{slot.angle.key}.md"
            (stage_dir / name).write_text(
                render_itinerary_md(slot.itinerary, slot.facts, state.requirements),
                encoding="utf-8",
            )
            names.append(name)

    slot = state.chosen() if state.stage is Stage.DONE else None
    if slot is not None and slot.itinerary is not None and slot.facts is not None:
        if write_md:
            (stage_dir / "itinerary.md").write_text(
                render_itinerary_md(slot.itinerary, slot.facts, state.requirements),
                encoding="utf-8",
            )
            names.append("itinerary.md")
        if write_html:
            day_maps = (
                fetch_day_maps(slot.itinerary, slot.facts, provider)
                if provider is not None
                else {}
            )
            (stage_dir / "itinerary.html").write_text(
                render_itinerary_html(
                    slot.itinerary, slot.facts, state.requirements, day_maps
                ),
                encoding="utf-8",
            )
            names.append("itinerary.html")

    return Staged(trip_dir, stage_dir, state.revision, tuple(names))


def publish(staged: Staged | None) -> None:
    """提交点。artifacts.json **最后写**——它在就代表整组文件都到位了。

    整段搬运握 `<trip>/.publish.lock`：单个 os.replace 原子，一串不原子，
    而这个目录下同时可能有第二个写者（另一个进程里的 `trip render`，或对同一
    `trips/` 起的第二个 `trip web`）。锁只覆盖搬运本身——慢的那截（渲染 + 拉
    高德图）早在 stage_artifacts() 里做完了，所以持锁时间是几次改名的量级。

    锁没能让两个写者变成一个赢家，它只保证**不混搭**：最后落地的那一份整组
    一致。落地的若是旧版，manifest 的 revision 与 state.json 对不上，
    artifact_ready() 判不就绪 → 详情页给「产物待重建」，安全且可恢复。
    """
    if staged is None:
        return
    with _publish_lock(staged.trip_dir):
        for name in staged.names:
            os.replace(staged.stage_dir / name, staged.trip_dir / name)
        _atomic_write_json(
            staged.trip_dir / ARTIFACTS_JSON,
            {"revision": staged.revision, "files": list(staged.names)},
        )
    shutil.rmtree(staged.stage_dir, ignore_errors=True)


@contextlib.contextmanager
def _publish_lock(trip_dir: Path):
    """按行程目录的跨进程互斥锁（阻塞式）。

    锁文件本身不含任何状态，留在目录里也无所谓——重要的是它**不参与发布**：
    不进 manifest 的 files，artifact_ready() 也不看它。
    """
    trip_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(trip_dir / PUBLISH_LOCK, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        _flock(fd)
        try:
            yield
        finally:
            _funlock(fd)
    finally:
        os.close(fd)


def _flock(fd: int) -> None:
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_EX)
    else:  # pragma: no cover —— Windows
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)


def _funlock(fd: int) -> None:
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_UN)
    else:  # pragma: no cover —— Windows
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


def discard(staged: Staged | None) -> None:
    """只删自己那个子目录，绝不碰兄弟写者的（spec §4.1）。"""
    if staged is None:
        return
    shutil.rmtree(staged.stage_dir, ignore_errors=True)


def artifact_ready(trip_dir, revision: int) -> bool:
    """详情页不靠 `stage is DONE` 决定要不要给链接，靠这个（spec §4.1）。

    崩溃恢复、旧版本产物残留、手工删文件，三种情况共用这一条判定。

    **它回答的是一个很具体的问题：「`/trips/<tid>/itinerary` 现在点进去，
    拿到的是不是这一版 revision 的成稿？」** 所以 manifest 里必须**明确列出**
    `itinerary.html` 且该文件真的在——`revision` 对得上 + `files` 里的东西都在，
    这两条加起来并不蕴含它。

    spec §4.1 的字面表述是「`artifacts.json` 存在且 revision 相等且文件都在」，
    照字面写成 `all(files 都存在)` 有一个致命的空集陷阱：**`all([])` 是 `True`**。
    于是两种 manifest 会拿到假绿灯，而两种都是现实路径：

    - **空 manifest**：`stage_artifacts()` 在没有可发布候选时返回 `names=()`
      （非 DONE、或 chosen 那条候选 `itinerary`/`facts` 是 None），`publish()`
      照样写下 `{"revision": N, "files": []}`；
    - **只有 Markdown 的 manifest**：`trip render <dir> --format md`（§7 明确
      支持的第二个写者）发布 `{"revision": N, "files": ["plan-A.md",
      "itinerary.md"]}`，压根没碰 HTML。

    两种情况下 `artifact_ready` 若返回 `True`，详情页就会亮出成稿链接（spec §6.1
    那张表的 `DONE + artifact_ready` 行），而点进去只有两种结局：**(a)** 文件不在 →
    成稿页 409「产物需要重建」，详情页刚刚才承诺过它就绪，自相矛盾；**(b)** 更糟，
    上一版的 `itinerary.html` 还躺在目录里没人删 —— `--format md` 不会清理它 ——
    于是**静默给出一份过期成稿**，manifest 的 revision 还和 `state.json` 严丝合缝
    对得上，谁也查不出来。这正是 spec §6.1「**不给死链**」和 §9 回归 15 要堵的洞。

    所以这里比 §4.1 的字面表述**更严**一档：强制要求 `itinerary.html` 在册。
    宁可多显示一次「产物待重建」（点一下重建按钮就好，不碰 LLM），也不要给一个
    404 或一份看不出来的旧成稿。
    """
    trip_dir = Path(trip_dir)
    path = trip_dir / ARTIFACTS_JSON
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict) or data.get("revision") != revision:
        return False
    files = data.get("files")
    if not isinstance(files, list) or FINAL_HTML not in files:
        # ★ 空 manifest 与「只有 md」的 manifest 都到此为止：all([]) 是 True，
        #   少了这一行它们全都是假绿灯。
        return False
    return all(isinstance(n, str) and (trip_dir / n).exists() for n in files)


def sweep_stale_staging(trips_root, max_age_s: float = 3600.0) -> int:
    """`trip web` 启动时扫一次，删掉进程崩在中途留下的孤儿暂存目录。

    只删 mtime 超过 max_age_s 的：启动那一刻本进程没有任何 job，但别的进程
    （一个正在跑的 trip render）可能正往自己的子目录里写。删错的代价本来也
    有限——暂存内容不是任何权威状态，大不了重建一次。
    """
    root = Path(trips_root)
    if not root.is_dir():
        return 0
    cutoff = time.time() - max_age_s
    removed = 0
    for staging in root.glob(f"*/{STAGING_DIR}"):
        if not staging.is_dir():
            continue
        for child in staging.iterdir():
            try:
                if child.is_dir() and child.stat().st_mtime < cutoff:
                    shutil.rmtree(child, ignore_errors=True)
                    removed += 1
            except OSError:
                continue
    return removed


def _atomic_write_json(path: Path, payload: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".artifacts-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
