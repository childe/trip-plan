import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tripplan.agents.limits import LimitExceeded
from tripplan.cli import (
    MissingCredential,
    build_deps,
    build_provider,
    drive,
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


class _Ask:
    """脚本化的「问人」。"""

    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []

    def __call__(self, need_input):
        self.prompts.append(need_input)
        assert self.answers, "问的次数比脚本多"
        return self.answers.pop(0)


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


def test_driver_saves_with_the_persisted_revision_not_the_new_one(tmp_path):
    """advance 暂停时自增 revision，拿自增后的值去 CAS 必然失败。"""
    repo = FileRepo(tmp_path / "kyoto")
    state = _state(rev=0)
    repo.create(state)

    def fake_advance(s, deps, cmd=None, emit=None):
        from tripplan.state import Done, NeedInput

        if cmd is None:
            s.revision += 1
            return NeedInput(InputKind.CONFIRM_REQUIREMENTS, s.requirements, s.revision)
        s.revision += 1
        s.stage = Stage.DONE
        return Done(Itinerary(angle=Angle("A", "古寺", "")))

    ask = _Ask([ConfirmRequirements(1)])
    out = drive(
        state,
        repo,
        _deps(),
        ask,
        lambda _t: None,
        persisted=0,
        advance_fn=fake_advance,
    )
    assert out is not None
    assert repo.load().revision == 2  # 两次 CAS 都成功了


def test_driver_reprompts_with_the_current_question_after_reject(tmp_path):
    repo = FileRepo(tmp_path / "kyoto")
    state = _state(rev=1)
    repo.create(state)
    calls = []

    def fake_advance(s, deps, cmd=None, emit=None):
        from tripplan.state import Done, NeedInput, Rejected, RejectReason

        pending = NeedInput(InputKind.CONFIRM_REQUIREMENTS, s.requirements, s.revision)
        calls.append(cmd)
        if cmd is None:
            return pending
        if len(calls) == 2:
            return Rejected(RejectReason.UNKNOWN_CANDIDATE, pending)
        s.revision += 1
        return Done(Itinerary(angle=Angle("A", "古寺", "")))

    ask = _Ask([ConfirmRequirements(1), ConfirmRequirements(1)])
    drive(
        state,
        repo,
        _deps(),
        ask,
        lambda _t: None,
        persisted=1,
        advance_fn=fake_advance,
    )
    assert len(ask.prompts) == 2  # 被拒后重新问了一次


# ---------- 产物 ----------


# ---------- 命令行 ----------


def test_plan_refuses_to_reuse_an_existing_directory(tmp_path, capsys):
    (tmp_path / "kyoto").mkdir(parents=True)
    (tmp_path / "kyoto" / "state.json").write_text("{}")
    code = main(["plan", "去京都", "--dir", str(tmp_path / "kyoto"), "--dry-run"])
    assert code != 0
    assert "已存在" in capsys.readouterr().err


def test_resume_reports_missing_trip(tmp_path, capsys):
    code = main(["resume", str(tmp_path / "nope")])
    assert code != 0
    assert "找不到" in capsys.readouterr().err


def test_resume_dry_run_reports_state_without_calling_llm(tmp_path, mk, capsys):
    repo = FileRepo(tmp_path / "kyoto")
    repo.create(_state())
    code = main(["resume", str(repo.dir), "--dry-run"])
    out = capsys.readouterr().out
    assert code == 0
    assert "已载入" in out


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


def test_plan_reports_provider_error_without_traceback(tmp_path, capsys, monkeypatch):
    """advance 抛 ProviderError（高德限流 / LLM 传输层故障）时，main() 必须把它
    转成可读的中文提示 + 非零退出码，而不是让原始 traceback 逃到用户面前。"""

    def boom(state, deps, cmd=None, emit=None):
        raise ProviderError("高德限流")

    monkeypatch.setattr("tripplan.cli._advance", boom)
    monkeypatch.setattr("tripplan.cli.build_deps", lambda dry_run=False: _deps())

    trip_dir = tmp_path / "kyoto"
    code = main(["plan", "去京都玩5天", "--dir", str(trip_dir)])

    err = capsys.readouterr().err
    assert code != 0
    assert "高德限流" in err
    assert "Traceback" not in err
    # 中断前 repo.create 已经把初始状态落了盘——目录没坏，能 resume。
    assert (trip_dir / "state.json").exists()
    assert "resume" in err or "trip resume" in err


def test_resume_reports_limit_exceeded_without_traceback(tmp_path, capsys, monkeypatch):
    """LimitExceeded（候选线撞轮数/token/deadline）同样不能变成裸 traceback。"""
    repo = FileRepo(tmp_path / "kyoto")
    repo.create(_state(rev=0))

    def boom(state, deps, cmd=None, emit=None):
        raise LimitExceeded("超时（700s > 600s）")

    monkeypatch.setattr("tripplan.cli._advance", boom)
    monkeypatch.setattr("tripplan.cli.build_deps", lambda dry_run=False: _deps())

    code = main(["resume", str(repo.dir)])

    err = capsys.readouterr().err
    assert code != 0
    assert "超时" in err
    assert "Traceback" not in err
    assert (repo.dir / "state.json").exists()


# ---------- amendment 4：缺 AMAP_KEY 不能是裸 KeyError ----------
#
# 下面几条不用再自己 monkeypatch.delenv 了——_no_real_credentials 这个 autouse
# 夹具已经在每条测试开始前把 AMAP_KEY 等四个变量清空过一遍。


def test_build_deps_missing_amap_key_is_readable_not_keyerror():
    with pytest.raises(MissingCredential) as exc_info:
        build_deps(dry_run=False)
    message = str(exc_info.value)
    assert "AMAP_KEY" in message
    assert "--dry-run" in message


def test_build_deps_dry_run_works_with_no_env_vars_at_all():
    deps = build_deps(dry_run=True)
    assert deps.client is None
    assert isinstance(deps.provider, FakeProvider)


def test_plan_without_amap_key_reports_readable_error(tmp_path, capsys):
    code = main(["plan", "去京都", "--dir", str(tmp_path / "kyoto")])
    err = capsys.readouterr().err
    assert code != 0
    assert "AMAP_KEY" in err
    assert "KeyError" not in err
    assert "Traceback" not in err


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


def test_plan_without_llm_credential_reports_readable_error(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("AMAP_KEY", "test-key-123")
    monkeypatch.setenv("TRIPPLAN_CACHE", str(tmp_path / "cache"))
    code = main(["plan", "去芜湖", "--dir", str(tmp_path / "wuhu")])
    err = capsys.readouterr().err
    assert code != 0
    assert "ANTHROPIC_API_KEY" in err
    assert "TypeError" not in err
    assert "Traceback" not in err


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


def test_driver_does_not_save_on_rejected_round(tmp_path):
    """重写自 review round 1 里那条名不副实的测试：原来的断言是
    `... != before or True`，`or True` 让它对任何实现都无脑通过。真正的不变量
    是"被拒的那一轮不产生 save_if_revision 调用"，内容比较看不出这个——
    一次没有意义的重写完全可能写出字节相同的内容。这里直接数
    save_if_revision 被调用的次数与参数。"""
    repo = FileRepo(tmp_path / "kyoto")
    state = _state(rev=1)
    repo.create(state)

    save_calls = []
    original_save = repo.save_if_revision

    def counting_save(s, expected):
        save_calls.append(expected)
        return original_save(s, expected)

    repo.save_if_revision = counting_save
    seen = []

    def fake_advance(s, deps, cmd=None, emit=None):
        from tripplan.state import Done, NeedInput, Rejected, RejectReason

        pending = NeedInput(InputKind.CONFIRM_REQUIREMENTS, s.requirements, s.revision)
        if cmd is None:
            return pending
        if not seen:
            seen.append(cmd)
            return Rejected(RejectReason.STALE_REVISION, pending)
        s.revision += 1
        return Done(Itinerary(angle=Angle("A", "古寺", "")))

    ask = _Ask([ConfirmRequirements(0), ConfirmRequirements(1)])
    drive(
        state,
        repo,
        _deps(),
        ask,
        lambda _t: None,
        persisted=1,
        advance_fn=fake_advance,
    )
    # 恰好两次落盘：最初那次 NeedInput 一次，最后 Done 一次。被拒的那一轮
    # 夹在中间，一次 save_if_revision 都不应该触发——如果 Rejected 分支又
    # 悄悄绕回了保存逻辑，这里会变成 [1, 1, 1] 而不是 [1, 1]。
    assert save_calls == [1, 1]
    assert repo.load().revision == 2


# ---------- review round 2 —— item 5：损坏的 state.json 不能是裸 traceback ----------


def test_resume_reports_corrupt_state_readably(tmp_path, capsys):
    repo = FileRepo(tmp_path / "kyoto")
    repo.create(_state(rev=0))
    (repo.dir / "state.json").write_text("not json at all", encoding="utf-8")

    code = main(["resume", str(repo.dir)])

    err = capsys.readouterr().err
    assert code != 0
    assert "损坏" in err
    assert "Traceback" not in err


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


# ---------- 最终评审 C2：terminal_ask 的第一批测试 ----------
#
# 这一层此前零覆盖（`grep -rn terminal_ask tests/` 一条都搜不到），而树里
# 所有夹具用的都是 Angle("A")/("B")/("C") ——本来就是大写，`.upper()` 对它们
# 是恒等变换，所以那个 bug 在测试里根本没有现身的机会。


def _choice_need(*keys, revision=5):
    slots = [
        CandidateSlot(
            Angle(k, f"方案{k}", ""),
            Itinerary(angle=Angle(k, f"方案{k}", "")),
            _facts(),
            SlotStatus.OK,
        )
        for k in keys
    ]
    from tripplan.state import NeedInput

    return NeedInput(InputKind.CHOOSE_OR_FEEDBACK, slots, revision)


def _typed(monkeypatch, text):
    monkeypatch.setattr("builtins.input", lambda _prompt="": text)


@pytest.mark.parametrize(
    "candidate_key,typed",
    [
        ("foodie", "foodie"),  # 照抄界面打印的 key —— 曾经必被拒
        ("foodie", "FOODIE"),
        ("A", "a"),
        ("A", "A"),
        ("寺社巡礼", "寺社巡礼"),
    ],
)
def test_terminal_ask_matches_candidate_keys_case_insensitively(
    monkeypatch, capsys, candidate_key, typed
):
    """界面显示什么 key，用户敲什么就该认——不分大小写，且回填的一定是
    候选自己的那个 key（不是用户敲的那个大小写），这样 advance 的
    _check_candidate 一定能对上。"""
    from tripplan.cli import terminal_ask
    from tripplan.state import ChooseCandidate as CC

    _typed(monkeypatch, typed)
    cmd = terminal_ask(_choice_need(candidate_key))
    capsys.readouterr()

    assert isinstance(cmd, CC)
    assert cmd.angle_key == candidate_key
    assert cmd.expected_revision == 5


def test_terminal_ask_key_and_feedback_are_split_and_the_key_still_matches(
    monkeypatch, capsys
):
    from tripplan.cli import terminal_ask
    from tripplan.state import GiveFeedback as GF

    _typed(monkeypatch, "foodie 第2天太赶了")
    cmd = terminal_ask(_choice_need("foodie", "寺社巡礼"))
    capsys.readouterr()

    assert isinstance(cmd, GF)
    assert cmd.angle_key == "foodie"
    assert cmd.text == "第2天太赶了"


def test_terminal_ask_passes_an_unmatched_key_through_unchanged(monkeypatch, capsys):
    """对不上任何候选时不要自作聪明地改写输入：原样传下去，
    UNKNOWN_CANDIDATE 才是一句诚实的话，而不是 CLI 自己制造出来的谎。"""
    from tripplan.cli import terminal_ask
    from tripplan.state import ChooseCandidate as CC

    _typed(monkeypatch, "Z")
    cmd = terminal_ask(_choice_need("foodie"))
    capsys.readouterr()

    assert isinstance(cmd, CC)
    assert cmd.angle_key == "Z"


def test_terminal_ask_empty_choice_input_asks_again_via_a_rejectable_command(
    monkeypatch, capsys
):
    from tripplan.cli import terminal_ask

    _typed(monkeypatch, "   ")
    cmd = terminal_ask(_choice_need("A"))
    capsys.readouterr()
    assert isinstance(cmd, ConfirmRequirements)  # AWAIT_CHOICE 下会被拒，于是重新问


def test_terminal_ask_confirm_stage_empty_confirms_and_text_amends(monkeypatch, capsys):
    from tripplan.cli import terminal_ask
    from tripplan.state import AmendRequirements, NeedInput

    need = NeedInput(InputKind.CONFIRM_REQUIREMENTS, _reqs(), 3)

    _typed(monkeypatch, "")
    assert terminal_ask(need) == ConfirmRequirements(3)

    _typed(monkeypatch, "改成四天")
    assert terminal_ask(need) == AmendRequirements(3, "改成四天")
    capsys.readouterr()


def test_a_lowercase_angle_key_no_longer_livelocks_the_choice_prompt(
    monkeypatch, tmp_path, capsys
):
    """C2 的端到端回归：LLM 给候选取名 foodie（prompts/angle.md 从没约束过
    key），界面打印「选一份（foodie/…）」，用户照抄。修复前每一次重试都是
    UNKNOWN_CANDIDATE，这个提示下没有任何输入能成功，只能 Ctrl-C；修复后
    第一次就定稿，reject 循环一次都不该转。"""
    from tripplan.orchestrator import advance
    from tripplan.cli import terminal_ask

    repo = FileRepo(tmp_path / "kyoto")
    state = _state(Stage.AWAIT_CHOICE, rev=1)
    state.candidates = [
        CandidateSlot(
            Angle("foodie", "吃遍京都", ""),
            Itinerary(angle=Angle("foodie", "吃遍京都", "")),
            _facts(),
            SlotStatus.OK,
        )
    ]
    state.trip_timezone = "Asia/Tokyo"
    repo.create(state)

    asked = []

    def ask(need):
        asked.append(need)
        assert len(asked) < 4, "又是那个死循环：同一个提示反复问"
        _typed(monkeypatch, "foodie")
        return terminal_ask(need)

    itinerary = drive(state, repo, _deps(), ask, lambda _t: None, persisted=1)
    capsys.readouterr()

    assert itinerary is not None
    assert len(asked) == 1  # 一次就过，没有被拒后的重问
    assert state.stage is Stage.DONE
    assert state.chosen_key == "foodie"


# ---------- 最终评审 C3：TripCorrupt / TripNotFound 不能是裸 traceback ----------
#
# 已有的两条测试（test_resume_reports_corrupt_state_readably /
# test_render_...）覆盖的是「命令刚启动、repo.load() 就发现文件坏了」，那条
# 路径由 _cmd_resume / _cmd_render 自己的 except 兜住。但 save_if_revision
# 内部同样会 _decode() 盘上的文件，所以会话**中途**被别的进程改坏/删掉时，
# 异常是从 drive() 的 CAS 循环里抛出来的 —— 那里此前无人接管。


def _run_resume_with_a_hostile_ask(tmp_path, monkeypatch, sabotage):
    """跑一次 resume，在「问人」的那一刻对盘上的 state.json 动手脚。

    时序正是评审描述的那个：drive 先落盘（CAS 成功），再去问人；用户还在
    盯着提示的时候，另一个进程把文件改坏或删掉；答完之后的那次
    save_if_revision 就会撞上。
    """
    from tripplan.state import NeedInput

    repo = FileRepo(tmp_path / "kyoto")
    repo.create(_state(rev=1))

    def fake_advance(s, deps, cmd=None, emit=None):
        return NeedInput(InputKind.CONFIRM_REQUIREMENTS, s.requirements, s.revision)

    def hostile_ask(need):
        sabotage(repo.dir / "state.json")
        return ConfirmRequirements(need.revision)

    monkeypatch.setattr("tripplan.cli._advance", fake_advance)
    monkeypatch.setattr("tripplan.cli.terminal_ask", hostile_ask)
    monkeypatch.setattr("tripplan.cli.build_deps", lambda dry_run=False: _deps())
    return main(["resume", str(repo.dir)])


def test_state_corrupted_mid_session_is_reported_readably(
    tmp_path, capsys, monkeypatch
):
    code = _run_resume_with_a_hostile_ask(
        tmp_path,
        monkeypatch,
        lambda p: p.write_text("not json at all", encoding="utf-8"),
    )
    err = capsys.readouterr().err
    assert code != 0
    assert "损坏" in err
    assert "Traceback" not in err
    assert "TripCorrupt" not in err


def test_state_deleted_mid_session_is_reported_readably(tmp_path, capsys, monkeypatch):
    code = _run_resume_with_a_hostile_ask(tmp_path, monkeypatch, lambda p: p.unlink())
    err = capsys.readouterr().err
    assert code != 0
    assert "找不到" in err
    assert "Traceback" not in err
    assert "TripNotFound" not in err


def test_driver_gives_up_readably_when_a_reject_carries_no_question(tmp_path, capsys):
    """Rejected.current 为 None（工作态被塞了命令）时，重新问是问不出来的。
    必须说清楚并退出，不能对着 None 再调一次 ask——那就是 AttributeError；
    也不能退回去渲染一个编出来的问题。"""
    from tripplan.state import NeedInput, Rejected, RejectReason

    repo = FileRepo(tmp_path / "kyoto")
    state = _state(rev=1)
    repo.create(state)
    asked = []

    def fake_advance(s, deps, cmd=None, emit=None):
        if cmd is None:
            return NeedInput(InputKind.CONFIRM_REQUIREMENTS, s.requirements, s.revision)
        return Rejected(RejectReason.WRONG_COMMAND_FOR_STAGE, None)

    def ask(need):
        asked.append(need)
        return ConfirmRequirements(1)

    lines = []
    out = drive(
        state, repo, _deps(), ask, lines.append, persisted=1, advance_fn=fake_advance
    )

    assert out is None  # 没有结局可写，_drive_and_report 会据此返回非零
    assert len(asked) == 1  # 没有对着 None 再问一次
    assert any("不在等待输入" in line for line in lines)


# ---------- 最终评审 M1 / M3：两条小的承诺落空 ----------


def test_eof_on_stdin_is_reported_readably_not_as_a_traceback(
    tmp_path, capsys, monkeypatch
):
    """`trip resume dir < /dev/null`、管道输入、提示上按 Ctrl-D —— input()
    抛 EOFError，此前一路裸奔成 traceback。没有人可问不是程序出错。"""
    from tripplan.state import NeedInput

    repo = FileRepo(tmp_path / "kyoto")
    repo.create(_state(rev=1))

    def fake_advance(s, deps, cmd=None, emit=None):
        return NeedInput(InputKind.CONFIRM_REQUIREMENTS, s.requirements, s.revision)

    def eof(_prompt=""):
        raise EOFError

    monkeypatch.setattr("tripplan.cli._advance", fake_advance)
    monkeypatch.setattr("tripplan.cli.build_deps", lambda dry_run=False: _deps())
    monkeypatch.setattr("builtins.input", eof)

    code = main(["resume", str(repo.dir)])

    err = capsys.readouterr().err
    assert code != 0
    assert "Traceback" not in err
    assert "标准输入" in err
    assert (repo.dir / "state.json").exists()  # 进度还在，目录没坏


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
    monkeypatch.setenv("AMAP_KEY", "test-key-123")
    monkeypatch.setenv("TRIPPLAN_CACHE", str(tmp_path / "cache"))
    bad = tmp_path / "bad.toml"
    bad.write_text("[models.x\n", encoding="utf-8")  # 缺右方括号
    monkeypatch.setenv("TRIPPLAN_CONFIG", str(bad))

    code = main(["plan", "去芜湖", "--dir", str(tmp_path / "t")])
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

    code = main(["plan", "去芜湖", "--dir", str(tmp_path / "t")])
    err = capsys.readouterr().err
    assert code != 0
    assert "Traceback" not in err
    assert "未知的 model 引用" in err
    assert "claude-sonnet-5" in err  # 报的是用户写的原文
