import pytest

from tripplan.llm.config import DEFAULT_ROLES, Role, load_config, expand
from tripplan.llm.errors import ConfigError


def test_defaults_cover_every_role():
    assert set(DEFAULT_ROLES) == set(Role)


def test_planner_and_critic_use_different_models():
    """同源自审会放过同一个盲点——这条约束是设计的一部分，用测试钉住。"""
    assert DEFAULT_ROLES[Role.PLANNER].model != DEFAULT_ROLES[Role.CRITIC].model


def test_critic_defaults_to_independent_context():
    assert DEFAULT_ROLES[Role.CRITIC].independent_context is True
    assert DEFAULT_ROLES[Role.PLANNER].independent_context is False


def test_load_config_without_file_returns_defaults():
    assert load_config(None) == DEFAULT_ROLES


def test_load_config_overrides_only_named_roles(tmp_path):
    path = tmp_path / "roles.toml"
    path.write_text('[roles.critic]\nmodel = "gpt-5"\n', encoding="utf-8")
    cfg = load_config(path)
    assert cfg[Role.CRITIC].model == "gpt-5"
    assert cfg[Role.PLANNER] == DEFAULT_ROLES[Role.PLANNER]


def test_load_config_rejects_same_model_for_planner_and_critic(tmp_path):
    path = tmp_path / "roles.toml"
    same = DEFAULT_ROLES[Role.PLANNER].model
    path.write_text(f'[roles.critic]\nmodel = "{same}"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="critic"):
        load_config(path)


def test_load_config_rejects_unknown_role(tmp_path):
    path = tmp_path / "roles.toml"
    path.write_text('[roles.wizard]\nmodel = "x"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="wizard"):
        load_config(path)


def test_load_config_rejects_unknown_field(tmp_path):
    """配置中的未知字段应该产生 ValueError（不是 TypeError）。"""
    path = tmp_path / "roles.toml"
    path.write_text('[roles.critic]\nmdoel = "gpt-5"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="无效"):
        load_config(path)


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
