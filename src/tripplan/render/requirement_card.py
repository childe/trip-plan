"""需求卡。推断项标出来，用户一眼知道该盯哪几行。"""

from tripplan.models.common import Origin
from tripplan.models.requirements import (
    Requirements,
    describe_value,
    missing_required,
)

_LABELS = {
    "destination": "目的地",
    "dates": "日期",
    "party": "人员",
    "arrival": "抵达",
    "departure": "离开",
    "budget": "预算",
    "styles": "风格",
    "pace": "节奏",
    "must_visit": "必去",
    "avoid": "避开",
    "lodging_area": "住宿区域",
    "constraints": "其他约束",
}


def render_requirement_card(reqs: Requirements) -> str:
    lines = ["## 需求确认", ""]
    for name, label in _LABELS.items():
        field = getattr(reqs, name)
        if field.value is None:
            continue
        text = f"- **{label}**：{describe_value(field.value)}"
        if field.origin is Origin.MODEL:
            text += f"  _? 推断_"
            if field.rationale:
                text += f"（{field.rationale}）"
        lines.append(text)

    missing = missing_required(reqs)
    if missing:
        lines += ["", "### 待补充（必答）", ""]
        lines += [f"- **{_LABELS[n]}**：？" for n in missing]
        lines += ["", "这几项无法推断——猜出来会让整个规划建立在假约束上。"]
    else:
        lines += ["", "_标 `?` 的是推断值，不对请直接说。_"]
    return "\n".join(lines)
