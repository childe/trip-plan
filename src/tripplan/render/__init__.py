"""Markdown 渲染器包。

跨渲染器共用的样式常量放在这里——`candidates.py` 与 `itinerary_md.py`
都要把 `Issue.severity` 画成同一套记号，放在两处各写一份会让样式慢慢分叉。
"""

from tripplan.models.issue import Severity

SEVERITY_MARK = {
    Severity.BLOCKING: "🔴",
    Severity.WARNING: "🟡",
    Severity.SUGGESTION: "⚪",
}
