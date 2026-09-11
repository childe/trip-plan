"""纯函数层不触网：AST 检查各模块的 import 语句，并跟进一层本地依赖。

同一份 state.json 反复渲染必须得到同样的结果，同一份 FactSnapshot 反复
校验也必须得到同样的结果——这要求这两层只读快照，不自己发起任何网络请求。
这里不做行为断言（那属于各自的测试文件），只守住「模块引没引入网络相关
依赖」这条静态边界。

覆盖范围是 Global Constraints 点名的两处：`render/` 的四个渲染器，以及
`validation/rules.py`（连同它唯一的本地依赖 `validation/budget.py`）。
规则层此前完全没有这条守卫，尽管约束里和 `render/` 并列写着"测试会断言
这一点"——承诺兑现了一半。

`itinerary_html` 触网的那一半（`fetch_day_maps`）已经搬到 `tripplan.maps`
里，`render/itinerary_html.py` 现在和其余三个渲染器一样干净。

跟进一层的理由：只看直接 import 的话，一个渲染器只要改成 import
`tripplan.maps` 或 `tripplan.validation.resolver` 就能绕过这条守卫，而那两个
模块转手就摸到 httpx——守卫会通过，文档说的那句话却不再成立。今天所有被
覆盖的模块传递闭包都是干净的，所以这层加深是免费的。
"""

import ast
import importlib
import pathlib

import pytest

import tripplan.render.candidates as candidates_mod
import tripplan.render.itinerary_html as itinerary_html_mod
import tripplan.render.itinerary_md as itinerary_mod
import tripplan.render.requirement_card as requirement_card_mod
import tripplan.validation.budget as budget_mod
import tripplan.validation.rules as rules_mod

_MODULES = [
    candidates_mod,
    itinerary_mod,
    requirement_card_mod,
    itinerary_html_mod,
    rules_mod,
    budget_mod,
]

_FORBIDDEN = {"httpx", "requests", "urllib", "http", "socket", "aiohttp"}


def _imported_names(mod) -> set[str]:
    src = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    names = {
        a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
    }
    names |= {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    return names


def _names_within(mod, depth: int) -> set[str]:
    """本模块的 import，外加每个 tripplan 本地依赖再往下 depth 层的 import。"""
    names = _imported_names(mod)
    if depth <= 0:
        return names
    for name in sorted(n for n in names if n.startswith("tripplan")):
        try:
            sub = importlib.import_module(name)
        except ImportError:  # 名字指的是包里的某个对象，不是模块
            continue
        if getattr(sub, "__file__", None):
            names |= _names_within(sub, depth - 1)
    return names


@pytest.mark.parametrize("mod", _MODULES, ids=lambda m: m.__name__)
def test_module_does_not_touch_the_network(mod):
    names = _names_within(mod, depth=1)
    leaked = {n for n in names if n.split(".")[0] in _FORBIDDEN}
    assert not leaked, f"{mod.__name__} 引入了网络依赖：{sorted(leaked)}"
    assert not any(m.startswith("tripplan.providers") for m in names)
