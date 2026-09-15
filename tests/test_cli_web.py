"""trip web 的启动纪律（spec §6.5 / §7 / §9 回归 6）。"""

import pytest

from tripplan.cli import main


@pytest.fixture(autouse=True)
def _sealed(monkeypatch, tmp_path):
    for var in (
        "AMAP_KEY",
        "ANTHROPIC_API_KEY",
        "TRIPPLAN_WEB_TOKEN",
        "TRIPPLAN_WEB_SECRET",
        "TRIPPLAN_CONFIG",
        "TRIPPLAN_ROLES",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "sealed-home"))


@pytest.fixture
def served(monkeypatch):
    """拦住 waitress.serve，测试只看「有没有走到起服务这一步、参数对不对」。"""
    calls = []
    monkeypatch.setattr("tripplan.cli.build_deps", lambda dry_run=False: object())
    monkeypatch.setattr(
        "tripplan.cli._serve", lambda app, host, port: calls.append((host, port))
    )
    return calls


def test_binding_to_all_interfaces_without_a_token_is_refused(tmp_path, capsys, served):
    """§9 回归 6：不给「裸奔到局域网」留口子（spec §6.5）。"""
    code = main(["web", "--host", "0.0.0.0", "--trips-dir", str(tmp_path)])
    err = capsys.readouterr().err
    assert code != 0
    assert "TRIPPLAN_WEB_TOKEN" in err
    assert "Traceback" not in err
    assert served == []  # 服务压根没起来


def test_binding_to_localhost_without_a_token_is_fine(tmp_path, served):
    assert main(["web", "--trips-dir", str(tmp_path)]) == 0
    assert served == [("127.0.0.1", 8000)]


def test_a_token_unlocks_binding_to_all_interfaces(tmp_path, monkeypatch, served):
    monkeypatch.setenv("TRIPPLAN_WEB_TOKEN", "hunter2")
    assert (
        main(
            ["web", "--host", "0.0.0.0", "--port", "9000", "--trips-dir", str(tmp_path)]
        )
        == 0
    )
    assert served == [("0.0.0.0", 9000)]


def test_startup_sweeps_stale_staging_directories(tmp_path, monkeypatch, served):
    """spec §4.1：.staging/ 下的孤儿子目录由 trip web 启动时扫一次删掉。"""
    import os
    import time

    from tripplan.artifacts import STAGING_DIR

    orphan = tmp_path / "kyoto" / STAGING_DIR / "dead-job"
    orphan.mkdir(parents=True)
    ancient = time.time() - 7200
    os.utime(orphan, (ancient, ancient))

    main(["web", "--trips-dir", str(tmp_path)])
    assert not orphan.exists()


def test_missing_credentials_are_reported_at_startup_not_inside_a_job(
    tmp_path, capsys, monkeypatch
):
    """spec §7：凭据类错误在启动时处理，运行类在 job 里转成 error 字段。"""
    monkeypatch.setattr("tripplan.cli._serve", lambda app, host, port: None)
    code = main(["web", "--trips-dir", str(tmp_path)])
    err = capsys.readouterr().err
    assert code != 0
    assert "AMAP_KEY" in err
    assert "Traceback" not in err


def test_a_missing_web_extra_is_reported_readably_not_as_a_traceback(
    tmp_path, capsys, monkeypatch, served
):
    """flask / waitress 是 **optional-dependency**（Task 9 写进 pyproject +
    uv.lock 的 `[project.optional-dependencies].web`）。只装了基础依赖的人跑
    `trip web`，撞上的是 `ModuleNotFoundError: No module named 'flask'` 一段
    堆栈——它既不说要装什么，也不说怎么装。`trip render` 不需要这两个包，所以
    「没装」是完全正常的状态，不是用户犯错。
    """

    def missing():
        raise ModuleNotFoundError("No module named 'flask'", name="flask")

    monkeypatch.setattr("tripplan.cli._require_web_deps", missing)

    code = main(["web", "--trips-dir", str(tmp_path)])
    err = capsys.readouterr().err
    assert code != 0
    assert "flask" in err
    assert "uv sync --extra web" in err
    assert "Traceback" not in err
    assert served == []  # 没装依赖就别假装起过服务


def test_the_interactive_subcommands_are_gone():
    """spec §7：只剩 web 与 render 两个子命令。"""
    import tripplan.cli as cli

    for name in (
        "terminal_ask",
        "drive",
        "_resolve_candidate_key",
        "write_artifacts",
        "_cmd_plan",
        "_cmd_resume",
        "_drive_and_report",
    ):
        assert not hasattr(cli, name), name

    with pytest.raises(SystemExit):
        main(["plan", "去京都"])
    with pytest.raises(SystemExit):
        main(["resume", "trips/kyoto"])
