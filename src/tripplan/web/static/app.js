/* 前端逻辑只有四条，刻意做得极薄（spec §6.3）：
   1. 每秒拉一次，带上本地游标（初值是快照的 cursor）与 epoch
   2. 新事件追加到进度区（认 stream_id：同 id 拼进同一个块）
   3. 四者之一变化就 location.reload()：job.id、job.status_version、
      revision、artifact_ready。刷新后把新值记为本地基线
   4. job.status 是终态时停止轮询；reset_required 为 true 时 reload 一次

   第 3 条必须是「版本变化」而不是「error 非空」：error 是留在 job 上的终态
   字段，不是一次性事件——reload 之后它还在那儿，于是又 reload，每秒一次，
   用户连错误信息都读不完。
   而光有 status_version 也不够：它是 per-job 的，新 job 从 1 重来，
   「老 job 的 running(1)」和「新 job 的 running(1)」看起来完全一样。
   revision 也补不上这个洞——failed/rejected/cancelled 三种终态不改 revision。
*/
(function () {
  var root = document.getElementById("progress");
  if (!root) return;
  var feed = document.getElementById("event-feed");
  var url = root.dataset.eventsUrl;
  var TERMINAL = ["succeeded", "rejected", "failed", "cancelled", "none"];

  var cursor = Number(root.dataset.cursor || 0);
  var epoch = root.dataset.epoch || "";
  var jobId = root.dataset.jobId || "";
  var statusVersion = Number(root.dataset.statusVersion || 0);
  var revision = Number(root.dataset.revision || 0);
  var artifactReady = root.dataset.artifactReady === "1";
  var timer = null;

  function stop() { if (timer) { clearInterval(timer); timer = null; } }

  function append(ev) {
    if (ev.stream_id) {
      var existing = feed.querySelector('[data-stream-id="' + ev.stream_id.replace(/"/g, "") + '"]');
      if (existing) { existing.textContent += ev.text; return; }
    }
    var li = document.createElement("li");
    if (ev.stream_id) li.setAttribute("data-stream-id", ev.stream_id);
    li.textContent = ev.text;          // textContent，不是 innerHTML
    feed.appendChild(li);
    feed.scrollTop = feed.scrollHeight;
  }

  function tick() {
    fetch(url + "?since=" + cursor + "&epoch=" + encodeURIComponent(epoch),
          { headers: { "Accept": "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data) return;                      // 网络抖动：下一秒再试
        if (data.reset_required) { stop(); window.location.reload(); return; }

        (data.events || []).forEach(append);
        cursor = data.last_seq;

        var job = data.job || {};
        var id = job.id || "";
        var version = Number(job.status_version || 0);
        if (id !== jobId || version !== statusVersion ||
            data.revision !== revision || data.artifact_ready !== artifactReady) {
          stop();
          window.location.reload();
          return;
        }
        if (TERMINAL.indexOf(job.status) >= 0) stop();
      })
      .catch(function () { /* 下一秒再试 */ });
  }

  timer = setInterval(tick, 1000);
})();
