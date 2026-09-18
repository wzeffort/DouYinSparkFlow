(() => {
  const root = document.querySelector("[data-platform-root]");
  if (!root) return;

  const value = (name) => root.querySelector(`[data-platform-value="${name}"]`);
  const health = root.querySelector("[data-platform-health]");
  const healthLabel = root.querySelector("[data-platform-health-label]");
  const progress = root.querySelector("[data-platform-progress]");
  const updated = root.querySelector("[data-platform-updated]");

  async function refreshPlatformStatus() {
    try {
      const response = await fetch("/api/platform-status", {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) throw new Error("status unavailable");
      const status = await response.json();
      for (const name of ["total", "success", "running", "pending", "failed"]) {
        const node = value(name);
        if (node) node.textContent = String(status[name]);
      }
      const percent = status.total ? Math.min(100, status.success / status.total * 100) : 0;
      if (progress) progress.style.width = `${percent.toFixed(1)}%`;
      if (health) {
        health.classList.toggle("online", status.worker_online);
        health.classList.toggle("offline", !status.worker_online);
      }
      if (healthLabel) healthLabel.textContent = status.worker_online ? "正常运行" : "暂时离线";
      if (updated) updated.textContent = "刚刚更新 · 汇总数据不包含用户、好友或消息内容";
    } catch (_error) {
      if (updated) updated.textContent = "状态更新暂时失败，当前保留上次数据";
    }
  }

  window.setInterval(refreshPlatformStatus, 15000);
})();
