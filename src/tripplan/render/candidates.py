"""三份候选并排。遗留问题是用户挑选方案的重要依据，必须显示。"""

from tripplan.models.issue import Severity
from tripplan.state import SlotStatus

_MARK = {Severity.BLOCKING: "🔴", Severity.WARNING: "🟡", Severity.SUGGESTION: "⚪"}


def render_candidates(slots) -> str:
    lines = ["## 候选方案", ""]
    for slot in slots:
        lines.append(f"### [{slot.angle.key}] {slot.angle.title}")
        if slot.angle.description:
            lines.append(f"_{slot.angle.description}_")
        lines.append("")

        if slot.itinerary is None:
            lines += [f"⚠️ 未能生成（{slot.detail}）——**无法选择**", ""]
            continue

        days = len(slot.itinerary.days)
        acts = sum(len(d.activities) for d in slot.itinerary.days)
        lines.append(f"{days} 天 / {acts} 项安排")

        if slot.status is SlotStatus.EXHAUSTED and slot.detail:
            lines.append(f"⚠️ {slot.detail}")

        if slot.itinerary.issues:
            lines += ["", "遗留问题："]
            lines += [
                f"- {_MARK[i.severity]} {i.message}" for i in slot.itinerary.issues
            ]
        lines.append("")

    keys = "/".join(s.angle.key for s in slots if s.itinerary is not None)
    lines += [f"选一份（{keys}），或对某一份提修改意见，或直接说需求要改。"]
    return "\n".join(lines)
