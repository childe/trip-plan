"""结构化 view model。模板遍历字段、自己出 HTML 结构。

**绝不复用 CLI 的 Markdown 渲染器**（spec §6.1）：
render_requirement_card() / render_candidates() 返回的是 Markdown 串
（"## 需求确认"、"- **目的地**：…"）。塞进 Jinja 只有两种结局，都不能要——
autoescape 开着就在页面上显示 Markdown 源文；用 |safe 当 HTML 就等于把
field.rationale / slot.detail / angle.title / Issue.message / raw_request
这些**模型输出或用户输入的自由文本**当可信 HTML 注入，直接开一个 XSS 面，
而这个服务还要暴露在局域网上给别人访问。
"""

from dataclasses import dataclass
from pathlib import Path

from tripplan.artifacts import (
    artifact_ready,
)  # noqa: F401  （详情页要用，这里一并导出）
from tripplan.models.common import Origin
from tripplan.models.requirements import describe_value, missing_required
from tripplan.render import SEVERITY_MARK
from tripplan.render.requirement_card import FIELD_LABELS
from tripplan.repo import FileRepo, TripCorrupt, TripNotFound
from tripplan.wire import UnsupportedVersion

_SUMMARY_CHARS = 60

_STAGE_TEXT = {
    "COLLECT": "收集需求",
    "AWAIT_REQ_CONFIRM": "等你确认需求",
    "GENERATE": "生成候选",
    "AWAIT_CHOICE": "等你选方案",
    "REFINE": "按意见打磨",
    "DONE": "已定稿",
}


@dataclass(frozen=True)
class FieldVM:
    name: str
    label: str
    value: str
    is_inferred: bool
    rationale: str


@dataclass(frozen=True)
class ReqCardVM:
    fields: list[FieldVM]
    missing: list[str]


@dataclass(frozen=True)
class IssueVM:
    mark: str
    severity: str
    message: str


@dataclass(frozen=True)
class CandidateVM:
    key: str
    title: str
    description: str
    selectable: bool
    days: int
    activities: int
    status: str
    detail: str
    issues: list[IssueVM]


@dataclass(frozen=True)
class TripRowVM:
    tid: str
    summary: str
    stage: str
    stage_text: str
    revision: int
    mtime: float
    running: bool
    corrupt: bool
    error: str


def req_card_vm(reqs) -> ReqCardVM:
    fields = []
    for name, label in FIELD_LABELS.items():
        field = getattr(reqs, name)
        if field.value is None:
            continue
        fields.append(
            FieldVM(
                name=name,
                label=label,
                value=describe_value(field.value),
                is_inferred=field.origin is Origin.MODEL,
                rationale=field.rationale or "",
            )
        )
    return ReqCardVM(fields, [FIELD_LABELS[n] for n in missing_required(reqs)])


def candidate_vms(slots) -> list[CandidateVM]:
    out = []
    for slot in slots or []:
        itin = slot.itinerary
        out.append(
            CandidateVM(
                key=slot.angle.key,
                title=slot.angle.title,
                description=slot.angle.description or "",
                # 一份「主体已生成、critic 挂了」的行程仍然可选，比强行剥夺
                # 选择更合理——与 render/candidates.py 的判据保持一致。
                selectable=itin is not None,
                days=len(itin.days) if itin else 0,
                activities=sum(len(d.activities) for d in itin.days) if itin else 0,
                status=slot.status.value,
                detail=slot.detail or "",
                issues=[
                    IssueVM(SEVERITY_MARK[i.severity], i.severity.value, i.message)
                    for i in (itin.issues if itin else [])
                ],
            )
        )
    return out


def trip_rows(trips_root, registry) -> list[TripRowVM]:
    """单个目录的 TripCorrupt / TripNotFound 要单独标记为「损坏」并继续，
    不能让一个坏目录把整页炸掉（spec §6.1）。"""
    root = Path(trips_root)
    rows: list[TripRowVM] = []
    if not root.is_dir():
        return rows
    for child in sorted(root.iterdir()):
        state_path = child / "state.json"
        if not child.is_dir() or not state_path.exists():
            continue
        job = registry.get(child.name)
        running = job is not None and job.active
        try:
            state = FileRepo(child).load()
        except (TripCorrupt, TripNotFound, UnsupportedVersion) as e:
            rows.append(
                TripRowVM(
                    child.name,
                    "",
                    "",
                    "",
                    0,
                    state_path.stat().st_mtime,
                    running,
                    True,
                    str(e),
                )
            )
            continue
        rows.append(
            TripRowVM(
                tid=child.name,
                summary=_summary(state.raw_request),
                stage=state.stage.value,
                stage_text=_STAGE_TEXT.get(state.stage.value, state.stage.value),
                revision=state.revision,
                mtime=state_path.stat().st_mtime,
                running=running,
                corrupt=False,
                error="",
            )
        )
    rows.sort(key=lambda r: r.mtime, reverse=True)
    return rows


def _summary(raw: str) -> str:
    text = " ".join((raw or "").split())
    return text if len(text) <= _SUMMARY_CHARS else text[:_SUMMARY_CHARS] + "…"


# ---------- 事件文案（服务端渲染，前端只负责追加 DOM，spec §6.3） ----------

_EVENT_TEXT = {
    "stage_started": lambda a: f"开始{_STAGE_TEXT.get(a[0], a[0])}",
    "angles_picked": lambda a: "已确定切入角度：" + "、".join(str(k) for k in a[0]),
    "generating": lambda a: f"正在生成候选 {a[0]}",
    "revision": lambda a: f"候选 {a[0]} 第 {int(a[1]) + 1} 轮修订",
    "requirements_patched": lambda a: "需求已更新",
    "angle_generation_failed": lambda a: f"角度生成失败：{a[0]}",
    "diversity_retry": lambda a: f"候选 {a[0]} 与其它候选重合，重跑一次",
    "paused": lambda a: f"暂停：{_STAGE_TEXT.get(a[0], a[0])}",
}

_JOB_TEXT = {
    "job_succeeded": "这一步完成",
    "job_rejected": "命令被拒绝",
    "job_failed": "这一步失败",
    "job_cancelled": "已取消",
}


def event_text(ev: dict) -> str:
    """事件是从磁盘回读的，可能是旧版本写的、也可能残缺——渲染函数不许崩。"""
    etype = (ev or {}).get("type", "")
    payload = (ev or {}).get("payload") or {}
    render = _EVENT_TEXT.get(etype)
    if render is not None:
        try:
            return render(payload.get("args") or [])
        except (IndexError, KeyError, TypeError, ValueError):
            return etype
    if etype in _JOB_TEXT:
        message = payload.get("message")
        return _JOB_TEXT[etype] + (f"：{message}" if message else "")
    return etype
