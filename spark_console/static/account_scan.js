(() => {
  "use strict";

  const root = document.querySelector("[data-account-scan]");
  if (!root) return;

  const TERMINAL = new Set(["succeeded", "failed", "expired", "cancelled"]);
  const csrfToken = root.dataset.csrfToken;
  const startButton = document.querySelector("#scan-start");
  const dialog = document.querySelector("#scan-dialog");
  const closeButton = document.querySelector("#scan-close");
  const cancelButton = document.querySelector("#scan-cancel");
  const stageNode = document.querySelector("#scan-stage");
  const statusNode = document.querySelector("#scan-status");
  const countdownNode = document.querySelector("#scan-countdown");
  const qrNode = document.querySelector("#scan-qr");
  const qrCropNode = document.querySelector("#scan-qr-crop");
  const qrSaveNode = document.querySelector("#scan-qr-save");
  const mobileActionsNode = document.querySelector("#scan-mobile-actions");
  const placeholderNode = document.querySelector("#scan-placeholder");
  const textNode = document.querySelector("#scan-text");
  const typeButton = document.querySelector("#scan-type");
  const modeToggle = document.querySelector("#scan-mode-toggle");
  const phoneInput = document.querySelector("#scan-phone");
  const phoneButton = document.querySelector("#scan-phone-send");
  const phoneRow = document.querySelector("#scan-phone-row");
  const smsHint = document.querySelector(".scan-sms-hint");

  let scanId = null;
  let currentStatus = null;
  let timer = null;
  let startPending = false;
  let closeRequested = false;
  let cancelPending = false;
  let preparedState = null;
  let preloadPromise = null;
  let smsMode = mobileView;
  let qrLoadedFor = null;
  let lastQrLoadAt = 0;
  const QR_REFRESH_MS = 15000;

  function pollDelay() {
    // Poll fast while the cloud browser is still booting; once the QR is on
    // screen the status rarely changes and 1.2s keeps the page responsive.
    // Both floors stay under the server's per-minute status limit.
    return currentStatus === "awaiting_scan" || currentStatus === "confirming" ? 1200 : 700;
  }
  const mobileView = Boolean(window.matchMedia && window.matchMedia("(max-width: 760px)").matches);

  function clearTimer() {
    if (timer !== null) window.clearTimeout(timer);
    timer = null;
  }

  function setQrVisible(visible) {
    const cropMode = visible && mobileView && currentStatus === "awaiting_scan";
    qrNode.hidden = !visible || cropMode;
    qrCropNode.hidden = !cropMode;
    mobileActionsNode.hidden = !cropMode;
    placeholderNode.hidden = visible;
    if (!visible) {
      qrNode.removeAttribute("src");
      qrCropNode.removeAttribute("src");
      qrLoadedFor = null;
    }
  }

  function loadQrOnce(id, force) {
    if (!id) return;
    // The QR payload is a full cloud-page screenshot; re-downloading it on
    // every status tick wastes bandwidth. Refresh at most every 15s, or
    // immediately after an interaction that changed the cloud page.
    const now = Date.now();
    if (!force && qrLoadedFor === id && now - lastQrLoadAt < QR_REFRESH_MS) return;
    lastQrLoadAt = now;
    qrNode.src = `/accounts/scan/${encodeURIComponent(id)}/qr?v=${now}`;
    const cropUrl = `/accounts/scan/${encodeURIComponent(id)}/qr-crop?v=${now}`;
    if (mobileView) qrCropNode.src = cropUrl;
    qrSaveNode.href = cropUrl;
    qrLoadedFor = id;
  }

  function setBusy(busy) {
    startButton.disabled = busy;
    startButton.textContent = busy ? "正在发起绑定…" : "扫码绑定抖音账号";
    stageNode.setAttribute("aria-busy", busy ? "true" : "false");
  }

  function showMessage(message, seconds) {
    statusNode.textContent = message || "扫码状态暂不可用";
    countdownNode.textContent = Number.isFinite(seconds) && seconds > 0
      ? `剩余 ${seconds} 秒`
      : "";
  }

  function renderState(state) {
    currentStatus = state.status;
    dialog.dataset.status = state.status;
    showMessage(state.message, state.remaining_seconds);
    const browserActive = state.status === "awaiting_scan" || state.status === "confirming";
    setQrVisible(browserActive);
    if (browserActive) {
      loadQrOnce(scanId);
    }
    if (state.login_mode === "sms") {
      applySmsSteps(state.status);
    }
    const phoneStage = state.status === "awaiting_phone";
    phoneRow.hidden = !phoneStage;
    smsHint.hidden = phoneStage;
    const smsStage = state.status === "awaiting_sms";
    const terminal = TERMINAL.has(state.status);
    textNode.disabled = terminal || !smsStage;
    typeButton.disabled = terminal || !smsStage;
    setBusy(terminal ? false : !phoneStage && !smsStage && state.status !== "awaiting_scan");
    cancelButton.disabled = terminal;
    if (state.status === "succeeded") {
      showMessage("登录成功，正在刷新账号列表", 0);
      setQrVisible(false);
      cancelButton.disabled = true;
      timer = window.setTimeout(() => {
        if (dialog.open) dialog.close();
        window.location.reload();
      }, 900);
    }
    return terminal;
  }

  function applySmsSteps(status) {
    const labels = ["创建绑定会话", "打开抖音登录页", "输入手机号", "输入验证码确认"];
    const activeByStatus = {
      queued: "queued",
      loading_qr: "loading_qr",
      awaiting_phone: "awaiting_scan",
      awaiting_sms: "confirming",
      confirming: "confirming",
      succeeded: "succeeded",
    };
    document.querySelectorAll(".scan-progress li").forEach((li, index) => {
      const span = li.querySelector("span");
      if (span) span.textContent = labels[index];
    });
    if (status in activeByStatus) {
      dialog.dataset.status = activeByStatus[status];
    }
  }

  async function sendPhone() {
    if (!scanId || TERMINAL.has(currentStatus)) return;
    const value = phoneInput.value.trim();
    if (!/^1\d{10}$/.test(value)) {
      showMessage("请输入 11 位手机号码", 0);
      phoneInput.focus();
      return;
    }
    phoneButton.disabled = true;
    try {
      const response = await fetch(`/accounts/scan/${encodeURIComponent(scanId)}/interact`, {
        method: "POST",
        credentials: "same-origin",
        headers: {"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
        body: new URLSearchParams({csrf_token: csrfToken, kind: "phone", text: value}),
      });
      const body = await readJson(response);
      showMessage(body.message || "验证码发送中，请留意短信", 0);
      phoneButton.disabled = false;
      timer = window.setTimeout(pollScan, 800);
    } catch (error) {
      phoneButton.disabled = false;
      showMessage(error.message || "发送失败，请重试", 0);
    }
  }

  async function pollPrepared() {
    if (!scanId || startPending || closeRequested || cancelPending) return;
    try {
      const response = await fetch(`/accounts/scan/${encodeURIComponent(scanId)}`, {
        cache: "no-store",
        credentials: "same-origin",
      });
      const state = await readJson(response);
      preparedState = state;
      currentStatus = state.status;
      if (state.status === "awaiting_scan") {
        loadQrOnce(scanId);
        setQrVisible(true);
        setBusy(false);
        return;
      }
      if (TERMINAL.has(state.status)) {
        scanId = null;
        preparedState = null;
        setBusy(false);
        return;
      }
      timer = window.setTimeout(pollPrepared, pollDelay());
    } catch (_error) {
      scanId = null;
      preparedState = null;
      setBusy(false);
    }
  }

  async function preloadScan() {
    // Background session creation (hover intent): never disable the button or
    // the user's own click during preload would be swallowed.
    startPending = true;
    try {
      const response = await fetch("/accounts/scan", {
        method: "POST",
        credentials: "same-origin",
        headers: {"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
        body: new URLSearchParams({csrf_token: csrfToken, mode: smsMode ? "sms" : "qr"}),
      });
      const state = await readJson(response);
      scanId = state.id;
      currentStatus = state.status;
      preparedState = state;
      startPending = false;
      setBusy(false);
      if (state.status === "awaiting_scan") {
        loadQrOnce(scanId);
        setQrVisible(true);
      } else if (!TERMINAL.has(state.status)) {
        timer = window.setTimeout(pollPrepared, pollDelay());
      }
    } catch (_error) {
      startPending = false;
      scanId = null;
      currentStatus = null;
      preparedState = null;
      setBusy(false);
    }
  }

  async function readJson(response) {
    if (response.redirected) {
      window.location.assign("/login");
      throw new Error("登录状态已失效");
    }
    let body = {};
    try {
      body = await response.json();
    } catch (_error) {
      body = {};
    }
    if (!response.ok) {
      throw new Error(body.message || "请求未完成，请稍后重试");
    }
    return body;
  }

  async function pollScan() {
    if (!scanId || closeRequested || cancelPending) return;
    try {
      const response = await fetch(`/accounts/scan/${encodeURIComponent(scanId)}`, {
        cache: "no-store",
        credentials: "same-origin",
      });
      const state = await readJson(response);
      if (closeRequested || cancelPending) return;
      if (!renderState(state)) {
        timer = window.setTimeout(pollScan, pollDelay());
      }
    } catch (error) {
      setBusy(false);
      setQrVisible(false);
      showMessage(error.message, 0);
    }
  }

  async function startScan() {
    if (preloadPromise) {
      await preloadPromise;
      preloadPromise = null;
    }
    if (scanId && preparedState && !TERMINAL.has(preparedState.status)) {
      clearTimer();
      closeRequested = false;
      cancelPending = false;
      if (!dialog.open) dialog.showModal();
      const terminal = renderState(preparedState);
      if (!terminal) timer = window.setTimeout(pollScan, 1000);
      return;
    }
    clearTimer();
    scanId = null;
    currentStatus = null;
    startPending = true;
    closeRequested = false;
    cancelPending = false;
    setQrVisible(false);
    showMessage("正在创建扫码会话", 0);
    setBusy(true);
    cancelButton.disabled = false;
    if (!dialog.open) dialog.showModal();
    try {
      const response = await fetch("/accounts/scan", {
        method: "POST",
        credentials: "same-origin",
        headers: {"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
        body: new URLSearchParams({csrf_token: csrfToken, mode: smsMode ? "sms" : "qr"}),
      });
      const state = await readJson(response);
      scanId = state.id;
      startPending = false;
      const terminal = renderState(state);
      if (closeRequested) {
        if (TERMINAL.has(currentStatus) || await cancelScan()) {
          closeRequested = false;
          dialog.close();
        } else {
          closeRequested = false;
        }
        return;
      }
      if (!terminal) {
        timer = window.setTimeout(pollScan, pollDelay());
      }
    } catch (error) {
      startPending = false;
      if (closeRequested && !scanId) {
        closeRequested = false;
        dialog.close();
        return;
      }
      setBusy(false);
      cancelButton.disabled = true;
      showMessage(error.message, 0);
    }
  }

  async function cancelScan() {
    clearTimer();
    if (!scanId || TERMINAL.has(currentStatus)) return true;
    if (cancelPending) return false;
    cancelPending = true;
    closeButton.disabled = true;
    cancelButton.disabled = true;
    showMessage("正在取消绑定", 0);
    try {
      const response = await fetch(
        `/accounts/scan/${encodeURIComponent(scanId)}/cancel`,
        {
          method: "POST",
          credentials: "same-origin",
          headers: {"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
          body: new URLSearchParams({csrf_token: csrfToken, mode: smsMode ? "sms" : "qr"}),
        },
      );
      const state = await readJson(response);
      cancelPending = false;
      closeButton.disabled = false;
      renderState(state);
      return true;
    } catch (error) {
      cancelPending = false;
      closeButton.disabled = false;
      cancelButton.disabled = false;
      stageNode.setAttribute("aria-busy", "false");
      showMessage("取消失败，请重试", 0);
      return false;
    }
  }

  async function requestClose(confirmClose) {
    const active = startPending || (scanId && !TERMINAL.has(currentStatus));
    if (!active) {
      dialog.close();
      return;
    }
    if (confirmClose && !window.confirm("关闭窗口将取消本次绑定，确定继续吗？")) return;
    if (closeRequested || cancelPending) return;
    closeRequested = true;
    clearTimer();
    closeButton.disabled = true;
    cancelButton.disabled = true;
    showMessage("正在取消绑定", 0);
    if (startPending) return;
    if (await cancelScan()) {
      closeRequested = false;
      dialog.close();
    } else {
      closeRequested = false;
    }
  }

  async function forwardBrowserClick(event) {
    if (!scanId || TERMINAL.has(currentStatus)) return;
    const bounds = qrNode.getBoundingClientRect();
    if (!bounds.width || !bounds.height) return;
    const x = Math.min(1, Math.max(0, (event.clientX - bounds.left) / bounds.width));
    const y = Math.min(1, Math.max(0, (event.clientY - bounds.top) / bounds.height));
    try {
      await fetch(`/accounts/scan/${encodeURIComponent(scanId)}/interact`, {
        method: "POST",
        credentials: "same-origin",
        headers: {"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
        body: new URLSearchParams({csrf_token: csrfToken, x: String(x), y: String(y)}),
      });
      loadQrOnce(scanId, true);
    } catch (_error) {
      showMessage("云端页面点击未送达，请重试", 0);
    }
  }

  async function sendBrowserText() {
    if (!scanId || TERMINAL.has(currentStatus)) return;
    const value = textNode.value.trim();
    if (!/^\d{4,8}$/.test(value)) {
      showMessage("请输入 4–8 位数字验证码", 0);
      textNode.focus();
      return;
    }
    textNode.value = "";
    typeButton.disabled = true;
    try {
      const response = await fetch(`/accounts/scan/${encodeURIComponent(scanId)}/interact`, {
        method: "POST",
        credentials: "same-origin",
        headers: {"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
        body: new URLSearchParams({csrf_token: csrfToken, kind: "text", text: value}),
      });
      await readJson(response);
      showMessage("验证码已输入云端，2 秒后将自动验证", 0);
      loadQrOnce(scanId, true);
    } catch (_error) {
      showMessage("验证码未输入云端，请重新输入", 0);
    } finally {
      typeButton.disabled = false;
    }
  }

  startButton.addEventListener("click", startScan);
  // Primary binding mode follows the viewport: phones default to SMS-code
  // login (their screen cannot be scanned by their own Douyin app), larger
  // screens default to the QR flow. The toggle switches before starting.
  function updateModeUi() {
    if (smsMode) {
      startButton.textContent = "手机号验证码绑定";
      modeToggle.textContent = "更习惯扫码？改用扫码绑定（需另一台设备的屏幕）";
    } else {
      startButton.textContent = "扫码绑定抖音账号";
      modeToggle.textContent = "手机在身边但没法扫码？改用短信验证码登录（无需第二台设备）";
    }
  }
  async function toggleMode() {
    if (startPending) return;
    if (scanId && currentStatus && !TERMINAL.has(currentStatus)) {
      const stopped = await cancelScan();
      if (!stopped) return;
    }
    smsMode = !smsMode;
    updateModeUi();
    startScan();
  }
  modeToggle.addEventListener("click", (event) => {
    event.preventDefault();
    toggleMode();
  });
  updateModeUi();
  // Warm intent: creating the session on hover/first touch lets the cloud
  // browser boot while the user is still moving toward the click, so the QR
  // is often ready the moment the dialog opens.
  let preloadArmed = false;
  function armPreload() {
    if (preloadArmed || startPending) return;
    if (scanId && preparedState && !TERMINAL.has(preparedState.status)) return;
    preloadArmed = true;
    preloadPromise = preloadScan().finally(() => { preloadPromise = null; });
  }
  startButton.addEventListener("pointerenter", armPreload);
  startButton.addEventListener("pointerdown", armPreload);
  closeButton.addEventListener("click", () => requestClose(true));
  cancelButton.addEventListener("click", () => requestClose(false));
  qrNode.addEventListener("click", forwardBrowserClick);
  typeButton.addEventListener("click", sendBrowserText);
  phoneButton.addEventListener("click", sendPhone);
  phoneInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      sendPhone();
    }
  });
  textNode.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      sendBrowserText();
    }
  });
  dialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    requestClose(true);
  });
  // Mobile app switches suspend timers and may fire pagehide; cancelling the
  // session there killed the QR the moment the user left to open the Douyin
  // app. Abandoned sessions are reclaimed server-side by their own TTL, so
  // returning users just resume polling where they left off.
  window.addEventListener("pageshow", () => {
    if (scanId && currentStatus && !TERMINAL.has(currentStatus)) {
      clearTimer();
      timer = window.setTimeout(pollScan, 300);
    }
  });
})();
