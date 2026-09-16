"""行程目录名。放在这里而不是 cli.py，是为了让 web/ 不必反向依赖 cli/。"""

import re

_SLUG_STRIP = re.compile(r"[^\w一-鿿\s-]", re.U)


def slugify(text: str) -> str:
    cleaned = _SLUG_STRIP.sub("", text).strip()
    cleaned = re.sub(r"\s+", "-", cleaned)
    return cleaned[:40] or "trip"
