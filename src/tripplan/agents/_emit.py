"""进度回调的安全外壳。

emit 点散布在业务主干上，而回调实现（Web 的 EventLog）要做 JSON 序列化、
追加写、flush，每一步都可能抛：payload 里混进不可序列化的对象、磁盘满、
events.jsonl 被删或被改成只读。事件日志是给人看的进度历史，不是权威状态
（spec §5.4），它没有资格决定一次规划算不算数。

两道防线是刻意的：Web 侧的 job.emit() 自己就是不抛异常的边界（spec §5.1.1），
这里再兜一层——即使将来有人换上一个会抛的 emit 实现，主干也不被拖下水。
"""

from tripplan.agents.limits import Cancelled


def safe_emit(emit, event) -> None:
    try:
        emit(event)
    except Cancelled:
        raise  # ★ 宽泛捕获前先放行取消（Global Constraint 1）
    except Exception:
        pass
