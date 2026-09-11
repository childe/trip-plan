"""渲染器不触网：AST 检查三个渲染模块各自的 import 语句。

同一份 state.json 反复渲染必须得到同样的结果——这要求渲染器只读
FactSnapshot，不自己发起任何网络请求。这里不做行为断言（那属于各自的
测试文件），只守住「模块本身有没有引入网络相关依赖」这条静态边界，
三个渲染器一起查，样式常量能共享、这条守卫也不该只盯着其中一个模块。
"""

import ast
import pathlib

import pytest

import tripplan.render.candidates as candidates_mod
import tripplan.render.itinerary_md as itinerary_mod
import tripplan.render.requirement_card as requirement_card_mod

_MODULES = [candidates_mod, itinerary_mod, requirement_card_mod]


def _imported_names(mod) -> set[str]:
    src = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    names = {
        a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
    }
    names |= {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    return names


@pytest.mark.parametrize("mod", _MODULES, ids=lambda m: m.__name__)
def test_renderer_does_not_touch_the_network(mod):
    names = _imported_names(mod)
    assert not (names & {"httpx", "requests", "urllib"})
    assert not any(m.startswith("tripplan.providers") for m in names)
