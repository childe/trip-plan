import pytest

from tripplan.llm.config import DEFAULT_ROLES, Role, load_config


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
