from pathlib import Path

import pytest

from tripplan.llm.config import (
    LlmConfig,
    ModelSpec,
    Role,
    RoleConfig,
    expand,
    load_config,
)
from tripplan.llm.errors import ConfigError

# ---------- expand()（Task 2） ----------


def test_expand_uses_env_when_set(monkeypatch):
    monkeypatch.setenv("TRIP_X", "hello")
    assert expand("${TRIP_X}", "models.m.key") == "hello"


def test_expand_falls_back_to_default_when_unset(monkeypatch):
    monkeypatch.delenv("TRIP_X", raising=False)
    assert expand("${TRIP_X:-gpt-5}", "models.m.name") == "gpt-5"


def test_expand_raises_when_unset_and_no_default(monkeypatch):
    monkeypatch.delenv("TRIP_X", raising=False)
    with pytest.raises(ConfigError) as exc:
        expand("${TRIP_X}", "models.gpt5.key")
    message = str(exc.value)
    assert "models.gpt5.key" in message
    assert "TRIP_X" in message


def test_expand_empty_default_is_legal(monkeypatch):
    """${VAR:-} 的语义是「显式留空」，不是错误。内置默认配置靠它表达
    「交给 SDK 自己解析凭据」。"""
    monkeypatch.delenv("TRIP_X", raising=False)
    assert expand("${TRIP_X:-}", "models.m.base_url") == ""


def test_expand_treats_set_but_empty_env_as_defined(monkeypatch):
    """`export X=` 是用户显式表达「我知道它，但留空」，与「拼写错了」
    是两回事。判据是 os.environ.get(name) is None，不是真值判断。"""
    monkeypatch.setenv("TRIP_X", "")
    assert expand("${TRIP_X}", "models.m.key") == ""


def test_expand_leaves_non_matching_text_literal(monkeypatch):
    assert expand("claude-opus-5", "models.m.name") == "claude-opus-5"
    assert expand("$NOT_A_VAR", "models.m.name") == "$NOT_A_VAR"
    assert expand("price is $5", "models.m.name") == "price is $5"


def test_expand_default_stops_at_first_brace(monkeypatch):
    """不支持嵌套，默认值取到第一个 } 为止。写明规则比发明转义便宜，
    但必须写明——否则三个实现者会发明三套规则。"""
    monkeypatch.delenv("TRIP_A", raising=False)
    monkeypatch.setenv("TRIP_B", "bee")
    assert expand("${TRIP_A:-${TRIP_B}}", "models.m.name") == "${TRIP_B}"


@pytest.fixture(autouse=True)
def _sealed_env(monkeypatch):
    """配置解析读环境变量，测试必须不依赖开发机上碰巧 export 过什么。"""
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "TRIP_GPT_MODEL"):
        monkeypatch.delenv(var, raising=False)


def _write(tmp_path, text):
    p = tmp_path / "cfg.toml"
    p.write_text(text, encoding="utf-8")
    return p


# ---------- 开箱默认 ----------


def test_defaults_cover_every_role():
    cfg = load_config(None)
    assert set(cfg.roles) == set(Role)


def test_defaults_match_current_behaviour():
    """不给配置文件时必须与现状一致，否则这是破坏性变更。"""
    cfg = load_config(None)
    expected = {
        Role.PLANNER: ("anthropic", "claude-opus-5", 16000),
        Role.CRITIC: ("anthropic", "claude-sonnet-5", 4000),
        Role.ANGLE: ("anthropic", "claude-sonnet-5", 2000),
        Role.CLASSIFIER: ("anthropic", "claude-haiku-4-5", 1000),
    }
    for role, (provider, name, max_tokens) in expected.items():
        spec = cfg.models[cfg.roles[role].model]
        assert (spec.provider, spec.name, cfg.roles[role].max_tokens) == (
            provider,
            name,
            max_tokens,
        )


def test_default_key_is_empty_so_sdk_can_resolve_credentials():
    """内置默认用 ${ANTHROPIC_API_KEY:-}，展开成空串交给 SDK 自己解析——
    ANTHROPIC_API_KEY 不是唯一凭据来源（还有 AUTH_TOKEN / profile / WIF）。"""
    cfg = load_config(None)
    assert cfg.models[cfg.roles[Role.PLANNER].model].key == ""


# ---------- 合并语义（§4.1） ----------


def test_only_named_roles_are_overridden(tmp_path):
    """承接旧的 test_load_config_overrides_only_named_roles：
    config.py 今天就是 dict(DEFAULT_ROLES) 再按名 replace，改成整份替换
    是无谓的破坏性变更。"""
    path = _write(
        tmp_path,
        '[models.gpt5]\nprovider = "openai"\nname = "gpt-5"\nkey = "k"\n'
        '[roles.critic]\nmodel = "gpt5"\n',
    )
    cfg = load_config(path)
    assert cfg.models[cfg.roles[Role.CRITIC].model].provider == "openai"
    assert cfg.models[cfg.roles[Role.PLANNER].model].name == "claude-opus-5"
    assert cfg.roles[Role.PLANNER].max_tokens == 16000


def test_roles_section_may_be_absent_entirely(tmp_path):
    path = _write(tmp_path, '[models.unused]\nprovider = "openai"\nname = "x"\n')
    cfg = load_config(path)
    assert set(cfg.roles) == set(Role)


def test_max_tokens_is_optional(tmp_path):
    """replace() 只覆盖给出的字段；把 max_tokens 标成必填是破坏性变更。"""
    path = _write(tmp_path, "[roles.planner]\nmax_tokens = 999\n")
    cfg = load_config(path)
    assert cfg.roles[Role.PLANNER].max_tokens == 999
    assert cfg.roles[Role.CRITIC].max_tokens == 4000


def test_same_named_model_is_replaced_wholesale(tmp_path):
    """不做字段级合并——provider="openai" 却继承 name="claude-opus-5"
    只会制造困惑。"""
    path = _write(
        tmp_path,
        '[models.opus]\nprovider = "openai"\nname = "gpt-5"\nkey = "k"\n',
    )
    cfg = load_config(path)
    spec = cfg.models[cfg.roles[Role.PLANNER].model]
    assert (spec.provider, spec.name) == ("openai", "gpt-5")


# ---------- 引用解析先于展开（§5 step 4） ----------


def test_unknown_model_ref_reports_the_literal_the_user_wrote(tmp_path):
    path = _write(tmp_path, '[roles.critic]\nmodel = "claude-sonnet-5"\n')
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert "未知的 model 引用" in str(exc.value)
    assert "claude-sonnet-5" in str(exc.value)


def test_unreferenced_model_does_not_block_startup(tmp_path, monkeypatch):
    """配置里囤着的备用 model 引用了未设的变量、或写了非法 provider，
    都不该阻塞启动——它压根没被任何角色用到。"""
    monkeypatch.delenv("NEVER_SET", raising=False)
    path = _write(
        tmp_path,
        '[models.spare]\nprovider = "openai"\nname = "x"\n' 'key = "${NEVER_SET}"\n',
    )
    cfg = load_config(path)  # 不抛
    assert "spare" not in cfg.models


# ---------- 结构与类型校验（§5 step 3） ----------


@pytest.mark.parametrize(
    "toml_text, needle",
    [
        ('models = "x"\n', "models"),
        ('[models]\ngpt5 = "x"\n[roles.critic]\nmodel = "gpt5"\n', "gpt5"),
        ('[models.m]\nname = "x"\n[roles.critic]\nmodel = "m"\n', "provider"),
        ('[models.m]\nprovider = "openai"\n[roles.critic]\nmodel = "m"\n', "name"),
        (
            '[models.m]\nprovider = "openai"\nname = 5\n'
            '[roles.critic]\nmodel = "m"\n',
            "name",
        ),
        ('[roles.planner]\nmax_tokens = "16000"\n', "max_tokens"),
        ('[roles.planner]\nmodel = ["a"]\n', "model"),
        ('[roles.critic]\nallow_same_model = "false"\n', "allow_same_model"),
        ("[roles.planner]\nallow_same_model = true\n", "allow_same_model"),
        ("[roles.nosuchrole]\nmax_tokens = 1\n", "nosuchrole"),
        ('[roles.planner]\nmdoel = "x"\n', "mdoel"),
        (
            '[models.m]\nprovider = "nope"\nname = "x"\n'
            '[roles.critic]\nmodel = "m"\n',
            "nope",
        ),
    ],
)
def test_structural_errors_become_config_error(tmp_path, toml_text, needle):
    path = _write(tmp_path, toml_text)
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert needle in str(exc.value)


@pytest.mark.parametrize(
    "make_path",
    [
        lambda tmp: tmp / "nope.toml",  # FileNotFoundError
        lambda tmp: tmp,  # IsADirectoryError
    ],
)
def test_unreadable_file_becomes_config_error(tmp_path, make_path):
    with pytest.raises(ConfigError):
        load_config(make_path(tmp_path))


def test_broken_toml_becomes_config_error(tmp_path):
    path = _write(tmp_path, "[models.x\n")
    with pytest.raises(ConfigError):
        load_config(path)


def test_non_utf8_file_becomes_config_error(tmp_path):
    path = tmp_path / "cfg.toml"
    path.write_bytes(b'[models.m]\nname = "\xff\xfe"\n')
    with pytest.raises(ConfigError):
        load_config(path)


# ---------- critic ≠ planner（§8.2） ----------


def test_critic_and_planner_may_not_share_provider_and_name(tmp_path):
    """判据是展开后的 (provider, name)，不是 model 引用名——用户复制一个
    [models.*] 块通常是为了换 base_url/key，不会意识到自己关掉了这道阀。"""
    path = _write(
        tmp_path,
        '[models.a]\nprovider = "openai"\nname = "gpt-5"\nkey = "k"\n'
        '[models.b]\nprovider = "openai"\nname = "gpt-5"\n'
        'base_url = "https://other/v1"\nkey = "k"\n'
        '[roles.planner]\nmodel = "a"\n[roles.critic]\nmodel = "b"\n',
    )
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert "gpt-5" in str(exc.value)


def test_allow_same_model_opens_the_gate(tmp_path):
    path = _write(
        tmp_path,
        '[models.a]\nprovider = "openai"\nname = "gpt-5"\nkey = "k"\n'
        '[roles.planner]\nmodel = "a"\n'
        '[roles.critic]\nmodel = "a"\nallow_same_model = true\n',
    )
    cfg = load_config(path)  # 不抛
    assert cfg.roles[Role.CRITIC].allow_same_model is True


def test_same_model_error_shows_both_source_and_expanded(tmp_path, monkeypatch):
    """name 可以是 ${TRIP_GPT_MODEL:-gpt-5}，同一份配置在不同机器上会一会儿
    触发一会儿不触发。报错必须同时给原文与展开值，否则用户对不上。"""
    monkeypatch.delenv("TRIP_GPT_MODEL", raising=False)
    path = _write(
        tmp_path,
        '[models.a]\nprovider = "openai"\nname = "gpt-5"\nkey = "k"\n'
        '[models.b]\nprovider = "openai"\n'
        'name = "${TRIP_GPT_MODEL:-gpt-5}"\nkey = "k"\n'
        '[roles.planner]\nmodel = "a"\n[roles.critic]\nmodel = "b"\n',
    )
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    message = str(exc.value)
    assert "${TRIP_GPT_MODEL:-gpt-5}" in message
    assert "gpt-5" in message


# ---------- 展开作用域（§5） ----------


def test_expansion_applies_to_models_string_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIP_GPT_MODEL", "gpt-5-mini")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real")
    path = _write(
        tmp_path,
        '[models.gpt5]\nprovider = "openai"\n'
        'name = "${TRIP_GPT_MODEL:-gpt-5}"\n'
        'base_url = "${TRIP_GW:-https://api.openai.com/v1}"\n'
        'key = "${OPENAI_API_KEY}"\n'
        '[roles.critic]\nmodel = "gpt5"\n',
    )
    spec = load_config(path).models["gpt5"]
    assert spec.name == "gpt-5-mini"
    assert spec.base_url == "https://api.openai.com/v1"
    assert spec.key == "sk-real"


def test_sources_keep_the_literal_text(tmp_path, monkeypatch):
    """name_source / key_source 不是冗余：加载期 backend 构造时抛出的凭据
    错误要报出用户写的那个变量名，那时 load_config 已经返回了。"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real")
    path = _write(
        tmp_path,
        '[models.gpt5]\nprovider = "openai"\nname = "gpt-5"\n'
        'key = "${OPENAI_API_KEY}"\n[roles.critic]\nmodel = "gpt5"\n',
    )
    spec = load_config(path).models["gpt5"]
    assert spec.key_source == "${OPENAI_API_KEY}"
    assert spec.key == "sk-real"


def test_provider_is_not_expanded(tmp_path, monkeypatch):
    """provider 是纯内部枚举，不出现在对外请求里——展开只会让错误信息
    里的值与用户在文件里看到的 ${X} 对不上。"""
    monkeypatch.setenv("TRIP_P", "openai")
    path = _write(
        tmp_path,
        '[models.m]\nprovider = "${TRIP_P}"\nname = "x"\n'
        '[roles.critic]\nmodel = "m"\n',
    )
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert "${TRIP_P}" in str(exc.value)
