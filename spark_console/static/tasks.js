(() => {
  const account = document.querySelector("#task-account");
  const target = document.querySelector("#task-target");
  const targetSecUid = document.querySelector("#task-target-sec-uid");
  const options = document.querySelector("#task-target-options");
  const refresh = document.querySelector("#refresh-targets");
  const status = document.querySelector("#target-status");
  if (!account || !target || !targetSecUid || !options || !refresh || !status) return;

  function bindSelectedIdentity() {
    const selected = Array.from(options.options).find((item) => item.value === target.value);
    targetSecUid.value = selected?.dataset.secUid || "";
  }

  async function loadTargets() {
    refresh.disabled = true;
    status.textContent = "正在读取该账号的聊天列表…";
    options.replaceChildren();
    try {
      const prefix = account.dataset.conversationPrefix || "/accounts";
      const response = await fetch(`${prefix}/${encodeURIComponent(account.value)}/conversations`, {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.message || "好友列表读取失败");
      for (const item of body.items || []) {
        const option = document.createElement("option");
        option.value = item.name;
        option.dataset.secUid = item.sec_uid || "";
        options.appendChild(option);
      }
      status.textContent = body.items?.length
        ? `已读取 ${body.items.length} 个最近会话，可直接选择或继续手动输入`
        : "暂未读取到最近会话，仍可手动输入准确昵称";
    } catch (error) {
      status.textContent = error.message || "好友列表读取失败，仍可手动输入";
    } finally {
      refresh.disabled = false;
    }
  }

  account.addEventListener("change", () => {
    target.value = "";
    targetSecUid.value = "";
    loadTargets();
  });
  target.addEventListener("input", bindSelectedIdentity);
  refresh.addEventListener("click", loadTargets);
  loadTargets();
})();

(() => {
  const time = document.querySelector("#task-send-time");
  const status = document.querySelector("#task-slot-status");
  const suggestions = document.querySelector("#task-slot-suggestions");
  if (!time || !status || !suggestions) return;

  async function checkSlot() {
    suggestions.replaceChildren();
    if (!time.value) {
      status.textContent = "选择时间后检查全平台剩余名额";
      return;
    }
    status.textContent = "正在检查执行时段…";
    try {
      const params = new URLSearchParams({ send_time: time.value });
      if (time.dataset.excludeTaskId) params.set("exclude_task_id", time.dataset.excludeTaskId);
      const response = await fetch(`/tasks/availability?${params.toString()}`, {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || "时段检查失败");
      status.textContent = body.available
        ? "该时间满足四分钟安全间隔，可创建任务"
        : "该时间不满足四分钟安全间隔，请选择附近空闲时间";
      for (const item of body.suggestions || []) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "slot-suggestion";
        button.textContent = item;
        button.addEventListener("click", () => {
          time.value = item;
          checkSlot();
        });
        suggestions.appendChild(button);
      }
    } catch (error) {
      status.textContent = error.message || "时段检查失败，提交时会再次校验";
    }
  }

  time.addEventListener("change", checkSlot);
  if (time.value) checkSlot();
})();
