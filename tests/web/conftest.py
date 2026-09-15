import pytest

from tripplan.deps import Deps
from tripplan.providers.fake import FakeProvider
from tripplan.web.jobs import JobOutcome


@pytest.fixture
def trips_root(tmp_path):
    root = tmp_path / "trips"
    root.mkdir()
    return root


class RecordingRunner:
    """记录 run_command / rebuild_artifacts 有没有被调用、被调了几次。

    §9 的多条回归要断言「advance 没被碰过」，而 advance 藏在 run_command
    里面——在这一层拦住即可，路由测试不需要真的跑状态机。
    """

    def __init__(self, outcome=None):
        self.calls = []
        self.rebuilds = []
        self.outcome = outcome or JobOutcome.ok(2)

    def run_command(self, trips_root, tid, cmd, deps, job, **kw):
        self.calls.append((tid, cmd))
        return self.outcome

    def rebuild(self, trips_root, tid, deps, job, **kw):
        self.rebuilds.append(tid)
        return self.outcome


@pytest.fixture
def runner():
    return RecordingRunner()


@pytest.fixture
def make_app(trips_root, runner):
    from tripplan.web.app import create_app

    def _make(**kw):
        kw.setdefault("deps", Deps(client=None, provider=FakeProvider()))
        kw.setdefault("run_command_fn", runner.run_command)
        kw.setdefault("rebuild_fn", runner.rebuild)
        kw.setdefault("secret", "test-secret")
        app = create_app(trips_root, **kw)
        app.config["TESTING"] = True
        return app

    return _make


@pytest.fixture
def app(make_app):
    return make_app()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def csrf(client):
    """先 GET 一次拿到 session 里的 token —— 与真实浏览器同一条路。"""

    def _get():
        client.get("/")
        with client.session_transaction() as sess:
            return sess["_csrf"]

    return _get
