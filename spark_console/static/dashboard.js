(() => {
  const root = document.querySelector("[data-platform-root]");
  if (!root) return;

  const value = (name) => root.querySelector(`[data-platform-value="${name}"]`);
  const health = root.querySelector("[data-platform-health]");
  const healthLabel = root.querySelector("[data-platform-health-label]");
  const updated = root.querySelector("[data-platform-updated]");
  const refresh = root.querySelector('[data-platform-refresh]');
  const descriptions = {
    success: '发送操作已完成，包含已提交和已确认的任务。',
    running: '执行器正在处理的任务，进度会自动更新。',
    pending: '今日尚未执行的任务，将按计划时间依次处理。',
    failed: '包含未完成、部分完成和跳过的任务，可在执行记录查看原因。',
  };
  for (const button of root.querySelectorAll('[data-platform-select]')) {
    button.addEventListener('click', () => {
      for (const other of root.querySelectorAll('[data-platform-select]')) other.setAttribute('aria-pressed', String(other === button));
      root.querySelector('[data-platform-detail]').textContent = descriptions[button.dataset.platformSelect];
    });
  }
  let refreshing = false;

  async function refreshPlatformStatus() {
    if (refreshing) return;
    refreshing = true;
    if (refresh) { refresh.disabled = true; refresh.textContent = '更新中…'; }
    try {
      const response = await fetch("/api/platform-status", {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) throw new Error("status unavailable");
      const status = await response.json();
      for (const name of ["total", "success", "confirmed", "submitted", "running", "pending", "failed"]) {
        const node = value(name);
        if (node) node.textContent = String(status[name]);
      }
      for (const segment of root.querySelectorAll('[data-platform-segment]')) {
        const count = status[segment.dataset.platformSegment];
        segment.style.width = `${status.total ? count / status.total * 100 : 0}%`;
      }
      const percent = status.total ? Math.round(status.success / status.total * 100) : 0;
      root.querySelector('[data-platform-percent]').textContent = String(percent);
      root.querySelector('[data-platform-orbit]').style.setProperty('--progress', `${percent}%`);
      if (health) {
        health.classList.toggle("online", status.worker_online);
        health.classList.toggle("offline", !status.worker_online);
      }
      if (healthLabel) healthLabel.textContent = status.worker_online ? "执行器正常" : "执行器离线";
      if (updated) updated.textContent = "刚刚更新 · 北京时间";
    } catch (_error) {
      if (updated) updated.textContent = "状态更新暂时失败，当前保留上次数据";
    } finally {
      refreshing = false;
      if (refresh) { refresh.disabled = false; refresh.textContent = '立即刷新'; }
    }
  }

  if (refresh) refresh.addEventListener('click', refreshPlatformStatus);
  window.setInterval(refreshPlatformStatus, 15000);
})();
