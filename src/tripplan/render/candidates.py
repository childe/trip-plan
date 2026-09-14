"""三份候选并排。遗留问题是用户挑选方案的重要依据，必须显示。"""

from tripplan.render import SEVERITY_MARK as _MARK
from tripplan.state import SlotStatus


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

        # FAILED 也要显示 detail：revise/critic 轮挂掉时 slot.py:86-89 会保住
        # 已生成的 itin，于是 itinerary 非空、走不到上面那个 ⚠️ 分支。不显示
        # detail 的话，用户拿到的是一份看起来完整、可直接选中、失败原因被
        # 静默隐藏的行程——与本模块 docstring 的承诺直接冲突。
        # 只补显示，不改可选性：一份"主体已生成、critic 挂了"的行程，
        # 用户看到 ⚠️ 之后仍然可以选它，比强行剥夺选择更合理。
        if slot.status in (SlotStatus.EXHAUSTED, SlotStatus.FAILED) and slot.detail:
            lines.append(f"⚠️ {slot.detail}")

        if slot.itinerary.issues:
            lines += ["", "遗留问题："]
            lines += [
                f"- {_MARK[i.severity]} {i.message}" for i in slot.itinerary.issues
            ]
        lines.append("")

    keys = "/".join(s.angle.key for s in slots if s.itinerary is not None)
    if keys:
        lines += [f"选一份（{keys}），或对某一份提修改意见，或直接说需求要改。"]
    else:
        lines += ["候选全部生成失败，没有可选的方案——请先修改需求后重试。"]
    return "\n".join(lines)
