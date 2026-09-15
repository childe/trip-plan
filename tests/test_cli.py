import logging
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tripplan.agents.limits import LimitExceeded
from tripplan.cli import (
    MissingCredential,
    build_deps,
    build_provider,
    main,
    slugify,
)
from tripplan.deps import Deps
from tripplan.llm.config import Role
from tripplan.models.common import Field, Origin
from tripplan.models.facts import FactSnapshot
from tripplan.models.itinerary import Angle, Itinerary
from tripplan.models.requirements import DateRange, Party, Requirements
from tripplan.providers.base import ProviderError
from tripplan.providers.fake import FakeProvider
from tripplan.repo import FileRepo
from tripplan.state import (
    CandidateSlot,
    ConfirmRequirements,
    InputKind,
    SlotStatus,
    Stage,
    TripState,
)

D1 = date(2026, 10, 1)
_JST = timezone(timedelta(hours=9))


def _reqs():
    return Requirements(
        destination=Field("京都", Origin.USER),
        dates=Field(DateRange(D1, D1), Origin.USER),
        party=Field(Party(adults=2), Origin.USER),
    )


def _facts() -> FactSnapshot:
    """write_artifacts 跳过 facts is None 的候选，夹具必须挂真快照。

    没有 FactSnapshot 就没有交通段与 gap 信息，也就没有可渲染的行程——
    这是生产行为本身正确，不是 write_artifacts 的 bug（详见 task-23 amendment 2）。
    """
    return FactSnapshot(
        poi_by_activity={},
        constraint_pois={},
        routes=[],
        weather={},
        trip_timezone="Asia/Tokyo",
        resolved_at=datetime(2026, 9, 1, tzinfo=_JST),
        gaps=[],
    )


def _state(stage=Stage.AWAIT_REQ_CONFIRM, rev=1) -> TripState:
    s = TripState.new("去京都", run_id="r1")
    s.stage, s.revision, s.requirements = stage, rev, _reqs()
    if stage is Stage.AWAIT_CHOICE:
        s.candidates = [
            CandidateSlot(
                Angle("A", "古寺", ""),
                Itinerary(angle=Angle("A", "古寺", "")),
                _facts(),
                SlotStatus.OK,
            )
        ]
    return s


def _deps():
    return Deps(client=None, provider=FakeProvider())


@pytest.fixture(autouse=True)
def _no_real_credentials(monkeypatch, tmp_path):
    """本文件全程离线。

    凭据判据已经从「环境变量 ANTHROPIC_API_KEY」扩成「SDK 是否解析出任何
    一种凭据」，所以光 delenv 不够——还要把 profile 的平台默认位置（HOME）
    指向一个空目录。注意 ANTHROPIC_CONFIG_DIR 必须 **delenv** 而不是 setenv：
    设置它会把 profile 解析升级为「显式选择」，构造期直接抛 CredentialsError。
    """
    for var in (
        "AMAP_KEY",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "OPENAI_API_KEY",
        "ANTHROPIC_PROFILE",
        "ANTHROPIC_CONFIG_DIR",
        "ANTHROPIC_IDENTITY_TOKEN",
        "ANTHROPIC_IDENTITY_TOKEN_FILE",
        "ANTHROPIC_FEDERATION_RULE_ID",
        "ANTHROPIC_ORGANIZATION_ID",
        "TRIPPLAN_ROLES",
        "TRIPPLAN_CONFIG",
        "TRIPPLAN_CACHE",
        "TRIPPLAN_LOG",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "sealed-home"))


# ---------- slug ----------


def test_slugify_keeps_cjk_and_strips_punctuation():
    assert slugify("十一想去京都玩5天！") == "十一想去京都玩5天"


def test_slugify_collapses_whitespace():
    assert slugify("go  to   kyoto") == "go-to-kyoto"


def test_slugify_truncates_long_input():
    assert len(slugify("很长的需求" * 30)) <= 40


def test_slugify_never_returns_empty():
    assert slugify("！！！") == "trip"


# ---------- driver 的 CAS 纪律 ----------


# ---------- 产物 ----------


# ---------- 命令行 ----------


def test_render_reads_state_and_writes_files(tmp_path):
    repo = FileRepo(tmp_path / "kyoto")
    state = _state(Stage.AWAIT_CHOICE)
    state.stage, state.chosen_key = Stage.DONE, "A"
    repo.create(state)
    assert main(["render", str(repo.dir), "--format", "html"]) == 0
    assert (repo.dir / "itinerary.html").exists()


def test_render_rejects_unknown_format(tmp_path, capsys):
    repo = FileRepo(tmp_path / "kyoto")
    repo.create(_state())
    assert main(["render", str(repo.dir), "--format", "pdf"]) != 0


def test_unknown_command_returns_nonzero(capsys):
    with pytest.raises(SystemExit):
        main(["fly-me-to-the-moon"])


# ---------- render 的纯粹性 ----------


def test_render_twice_produces_identical_files(tmp_path):
    """re-render 同一份 state.json 两次必须字节相同，且不改 state.json 本身。"""
    repo = FileRepo(tmp_path / "kyoto")
    state = _state(Stage.AWAIT_CHOICE)
    state.stage, state.chosen_key = Stage.DONE, "A"
    repo.create(state)
    state_before = (repo.dir / "state.json").read_text()

    assert main(["render", str(repo.dir), "--format", "both"]) == 0
    md1 = (repo.dir / "itinerary.md").read_text()
    html1 = (repo.dir / "itinerary.html").read_text()
    state_after_first = (repo.dir / "state.json").read_text()

    assert main(["render", str(repo.dir), "--format", "both"]) == 0
    md2 = (repo.dir / "itinerary.md").read_text()
    html2 = (repo.dir / "itinerary.html").read_text()
    state_after_second = (repo.dir / "state.json").read_text()

    assert md1 == md2
    assert html1 == html2
    assert state_before == state_after_first == state_after_second


# ---------- amendment 3：ProviderError / LimitExceeded 不能是裸 traceback ----------


# ---------- amendment 4：缺 AMAP_KEY 不能是裸 KeyError ----------
#
# 下面几条不用再自己 monkeypatch.delenv 了——_no_real_credentials 这个 autouse
# 夹具已经在每条测试开始前把 AMAP_KEY 等四个变量清空过一遍。


def test_build_deps_missing_amap_key_is_readable_not_keyerror():
    with pytest.raises(MissingCredential) as exc_info:
        build_deps(dry_run=False)
    message = str(exc_info.value)
    assert "AMAP_KEY" in message
    assert "export AMAP_KEY" in message


def _subcommand_help(cmd, capsys):
    with pytest.raises(SystemExit):
        main([cmd, "--help"])
    return capsys.readouterr().out


def test_missing_amap_key_hint_never_points_at_a_flag_the_cli_rejects(capsys):
    """提示里提到的每个 `--flag`，都得真的能被某个子命令接受。

    回归：交互命令 plan/resume 被删掉之后，入口只剩 `web` 与 `render`，两个
    都没有 `--dry-run`；而这条提示的末尾还写着「加 --dry-run」。用户照着做
    只会撞上 `trip: error: unrecognized arguments: --dry-run` 并 exit 2——
    唯一一条逃生指引把人指进死胡同，比不给指引更坏。

    断言写成「扫出消息里所有 --flag，逐个要求出现在某个子命令的 --help 里」
    而不是「不许出现 --dry-run」：这样以后谁再往凭据提示里塞一个不存在的开
    关，一样会被这条测试拦下。
    """
    with pytest.raises(MissingCredential) as exc_info:
        build_deps(dry_run=False)
    message = str(exc_info.value)

    all_help = _subcommand_help("web", capsys) + _subcommand_help("render", capsys)
    for flag in sorted(set(re.findall(r"--[a-z][a-z0-9-]*", message))):
        assert flag in all_help, f"凭据提示让用户加 {flag}，但没有任何子命令认这个开关"


def test_build_deps_dry_run_works_with_no_env_vars_at_all():
    deps = build_deps(dry_run=True)
    assert deps.client is None
    assert isinstance(deps.provider, FakeProvider)


# ---------- 缺 LLM 凭据不能是裸 TypeError，也不能只认 ANTHROPIC_API_KEY ----------
#
# build_deps 现在把 load_config 的结果交给 RoutingClient，构造期就会为每个
# 被角色引用到的 model 建 backend——缺凭据在这一步就抛 MissingCredential，
# 不会拖到第一次 chat（那时会被 orchestrator._safe_slot 吞掉）。判据也从
# 「环境变量 ANTHROPIC_API_KEY 是否非空」扩成了「SDK 是否解析出任何一种
# 凭据」（API_KEY / AUTH_TOKEN / profile / WIF），所以下面还专门测了一条
# 「只有 AUTH_TOKEN 也能通过」——旧实现在这里会误判。


def test_build_deps_missing_llm_credential_is_readable(tmp_path, monkeypatch):
    monkeypatch.setenv("AMAP_KEY", "test-key-123")
    monkeypatch.setenv("TRIPPLAN_CACHE", str(tmp_path / "cache"))
    with pytest.raises(MissingCredential) as exc:
        build_deps(dry_run=False)
    message = str(exc.value)
    assert "planner" in message  # 角色名
    assert "ANTHROPIC_API_KEY" in message  # 该 export 的变量名
    assert "--dry-run" in message  # 出路


def test_auth_token_alone_gets_past_the_credential_check(tmp_path, monkeypatch):
    """ANTHROPIC_API_KEY 不是唯一凭据来源。只配了 AUTH_TOKEN 的用户不能被挡。
    这正是旧实现（只看 ANTHROPIC_API_KEY）引入的回归。"""
    monkeypatch.setenv("AMAP_KEY", "test-key-123")
    monkeypatch.setenv("TRIPPLAN_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "bearer-abc")
    deps = build_deps(dry_run=False)  # 不抛
    assert deps.client is not None


def test_trippan_config_wins_over_trippan_roles(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AMAP_KEY", "test-key-123")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("TRIPPLAN_CACHE", str(tmp_path / "cache"))
    new = tmp_path / "new.toml"
    new.write_text("[roles.planner]\nmax_tokens = 111\n", encoding="utf-8")
    old = tmp_path / "old.toml"
    old.write_text("[roles.planner]\nmax_tokens = 222\n", encoding="utf-8")
    monkeypatch.setenv("TRIPPLAN_CONFIG", str(new))
    monkeypatch.setenv("TRIPPLAN_ROLES", str(old))

    deps = build_deps(dry_run=False)
    assert deps.client._config.roles[Role.PLANNER].max_tokens == 111
    assert "TRIPPLAN_CONFIG" in capsys.readouterr().err


def test_trippan_roles_alone_is_still_honored(tmp_path, monkeypatch, capsys):
    """TRIPPLAN_ROLES 是旧名，Task 8 之前唯一的入口，设计 §4.2 把「保留兼容」
    列为优先级第 2 档的正式承诺。只测「两者同设」（上一条测试）不够：把
    `_config_path` 里的 `chosen = new or old` 改成 `chosen = new`，上一条测试
    需要的只是 TRIPPLAN_CONFIG 生效，TRIPPLAN_ROLES 从头到尾没被读过也照样
    通过——这条测试才是唯一钉住"单独设置 TRIPPLAN_ROLES 时它必须被读"的地方。
    断言用具体的 max_tokens 数值（可证伪），不能只断言提示文案有没有出现。
    """
    monkeypatch.setenv("AMAP_KEY", "test-key-123")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("TRIPPLAN_CACHE", str(tmp_path / "cache"))
    old = tmp_path / "old.toml"
    old.write_text("[roles.planner]\nmax_tokens = 333\n", encoding="utf-8")
    monkeypatch.setenv("TRIPPLAN_ROLES", str(old))

    deps = build_deps(dry_run=False)
    assert deps.client._config.roles[Role.PLANNER].max_tokens == 333
    assert capsys.readouterr().err == ""  # 只设一个时不该有"两者同设"提示


def test_trippan_log_default_leaves_root_logger_untouched(tmp_path):
    """TRIPPLAN_LOG 缺省时 main() 不该往 root logger 上挂任何 handler——
    设计 §10.3 点名要避免 debug 诊断与 `  · ` 事件流混排。不加这条测试的话，
    把 main() 里的 `if level in ("debug", "info")` 改成无条件调用
    `logging.basicConfig(...)`，595 条既有测试一条都不会红。

    root logger 是进程全局状态，会被同一进程里跑过的其它测试/pytest 自己的
    日志插件弄脏，所以先存档再清空，测试体里断言的是"main() 跑完之后"这个
    受控起点有没有被碰过，跑完无论断言是否通过都要还原，不能把污染带给
    后面的测试。
    """
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    root.handlers = []
    try:
        main(["render", str(tmp_path / "nope")])  # 立刻在 repo.load() 处失败，不碰网络
        assert root.handlers == []
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)


# ---------- review round 2 —— item 1：CAS 输了不能拿输掉的 state 写产物 ----------


# ---------- review round 2 —— item 2：render 绝不能用假地图顶替真地图 ----------


def _done_state_with_a_resolvable_map_point(mk):
    itin = mk.itin(
        [mk.day("d1", D1, [mk.act("d1a1", "d1", "09:00", "11:00", query="清水寺")])]
    )
    facts = mk.facts(poi_by_activity={"d1a1": mk.resolved("B001")})
    state = _state(Stage.AWAIT_CHOICE)
    state.candidates = [CandidateSlot(itin.angle, itin, facts, SlotStatus.OK)]
    state.stage = Stage.DONE
    state.chosen_key = itin.angle.key
    return state


def test_build_provider_returns_none_without_amap_key():
    assert build_provider(dry_run=False) is None


def test_build_provider_returns_none_when_dry_run_even_with_a_key(monkeypatch):
    monkeypatch.setenv("AMAP_KEY", "test-key-should-not-matter")
    assert build_provider(dry_run=True) is None


def test_build_provider_returns_a_real_amap_provider_when_key_present(
    tmp_path, monkeypatch
):
    """只验证「有 key 时构造的是真 provider，而不是 Fake」这条接线——不触网：
    AmapProvider.__init__ 本身只是存字段、开一个 httpx.Client，不发请求。"""
    from tripplan.providers.amap import AmapProvider

    monkeypatch.setenv("AMAP_KEY", "test-key-123")
    monkeypatch.setenv("TRIPPLAN_CACHE", str(tmp_path / "cache"))
    provider = build_provider(dry_run=False)
    assert isinstance(provider, AmapProvider)
    assert provider.key == "test-key-123"


def test_render_uses_no_provider_and_skips_maps_when_amap_key_is_absent(tmp_path, mk):
    """端到端过一遍 _cmd_render：没配 AMAP_KEY 时安静跳过地图，而不是拿假图
    顶替，也不因为缺一个它压根用不上的 LLM 凭据就失败。用有实际可解析坐标点
    的行程（而不是空 days 的 _state() 默认夹具）——否则 fetch_day_maps 根本
    不会去碰 provider，这条断言在新旧实现下都会通过，测不出区别。"""
    repo = FileRepo(tmp_path / "kyoto")
    state = _done_state_with_a_resolvable_map_point(mk)
    repo.create(state)

    assert main(["render", str(repo.dir), "--format", "html"]) == 0
    html = (repo.dir / "itinerary.html").read_text()
    assert "<img" not in html


# ---------- review round 2 —— item 3/4：Rejected 那一轮真的不写盘 ----------


# ---------- review round 2 —— item 5：损坏的 state.json 不能是裸 traceback ----------


def test_render_reports_corrupt_state_readably(tmp_path, capsys):
    repo = FileRepo(tmp_path / "kyoto")
    repo.create(_state(rev=0))
    (repo.dir / "state.json").write_text("not json at all", encoding="utf-8")

    code = main(["render", str(repo.dir), "--format", "both"])

    err = capsys.readouterr().err
    assert code != 0
    assert "损坏" in err
    assert "Traceback" not in err


# ---------- review round 2 —— item 6：--format 必须真的管用 ----------


def test_render_format_md_only_does_not_write_html(tmp_path):
    repo = FileRepo(tmp_path / "kyoto")
    state = _state(Stage.AWAIT_CHOICE)
    state.stage, state.chosen_key = Stage.DONE, "A"
    repo.create(state)

    assert main(["render", str(repo.dir), "--format", "md"]) == 0
    assert (repo.dir / "itinerary.md").exists()
    assert (repo.dir / "plan-A.md").exists()
    assert not (repo.dir / "itinerary.html").exists()


def test_render_format_html_only_does_not_write_markdown(tmp_path):
    repo = FileRepo(tmp_path / "kyoto")
    state = _state(Stage.AWAIT_CHOICE)
    state.stage, state.chosen_key = Stage.DONE, "A"
    repo.create(state)

    assert main(["render", str(repo.dir), "--format", "html"]) == 0
    assert (repo.dir / "itinerary.html").exists()
    assert not (repo.dir / "itinerary.md").exists()
    assert not (repo.dir / "plan-A.md").exists()


# ---------- 最终评审 M1 / M3：两条小的承诺落空 ----------


def test_string_format_version_is_unsupported_not_corrupt(tmp_path, capsys):
    """`"format_version": "2"` 之前落到 `version > FORMAT_VERSION`，str 与 int
    比较抛 TypeError，被 repo._decode 归为 TripCorrupt ——给用户的出路成了
    「删掉整个行程目录」，而真相很可能是「这文件是更新版本的工具写的」。
    wire format 的迁移承诺要求这两条出路必须分得清。"""
    import json

    from tripplan.repo import TripCorrupt
    from tripplan.wire import UnsupportedVersion

    repo = FileRepo(tmp_path / "kyoto")
    repo.create(_state(rev=0))
    raw = json.loads((repo.dir / "state.json").read_text(encoding="utf-8"))
    raw["format_version"] = "2"
    (repo.dir / "state.json").write_text(
        json.dumps(raw, ensure_ascii=False), encoding="utf-8"
    )

    with pytest.raises(UnsupportedVersion) as exc_info:
        repo.load()
    assert not isinstance(exc_info.value, TripCorrupt)
    assert "升级" in str(exc_info.value)
    assert "删除" not in str(exc_info.value)


# ---------- ConfigError 必须被 main() 收口成可读中文 ----------
#
# 这几条必须先 setenv AMAP_KEY。build_deps 里 AMAP 的前置检查（构造
# MissingCredential 那一段）排在 load_config 之前，而 _no_real_credentials
# 会把 AMAP_KEY 清掉——不 setenv 的话请求根本走不到配置解析，测试拿到的是
# 「缺少环境变量 AMAP_KEY」，
# 退出码非零、也没有 Traceback，两条断言全绿而 TOML 一个字节都没读过。
# 这是设计文档 §15 点名的假绿陷阱。


def test_config_error_from_bad_toml_is_readable(tmp_path, monkeypatch, capsys):
    """ConfigError 现在只可能从 `trip web` 的启动期 build_deps 里出来。"""
    monkeypatch.setenv("AMAP_KEY", "test-key-123")
    monkeypatch.setenv("TRIPPLAN_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr("tripplan.cli._serve", lambda app, host, port: None)
    bad = tmp_path / "bad.toml"
    bad.write_text("[models.x\n", encoding="utf-8")  # 缺右方括号
    monkeypatch.setenv("TRIPPLAN_CONFIG", str(bad))

    code = main(["web", "--trips-dir", str(tmp_path / "trips")])
    err = capsys.readouterr().err
    assert code != 0
    assert "Traceback" not in err
    assert str(bad) in err  # 断言确实读到了这个文件，而不是被 AMAP 拦下


def test_config_error_from_unknown_model_ref_is_readable(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AMAP_KEY", "test-key-123")
    monkeypatch.setenv("TRIPPLAN_CACHE", str(tmp_path / "cache"))
    cfg = tmp_path / "roles.toml"
    cfg.write_text('[roles.critic]\nmodel = "claude-sonnet-5"\n', encoding="utf-8")
    monkeypatch.setenv("TRIPPLAN_CONFIG", str(cfg))
    monkeypatch.setattr("tripplan.cli._serve", lambda app, host, port: None)

    code = main(["web", "--trips-dir", str(tmp_path / "trips")])
    err = capsys.readouterr().err
    assert code != 0
    assert "Traceback" not in err
    assert "未知的 model 引用" in err
    assert "claude-sonnet-5" in err  # 报的是用户写的原文
