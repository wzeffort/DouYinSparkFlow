const fs = require("fs");
const path = require("path");
const vm = require("vm");

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return {promise, resolve};
}

function response(status, body) {
  return {
    status,
    ok: status >= 200 && status < 300,
    redirected: false,
    async json() { return body; },
  };
}

class Element {
  constructor() {
    this.listeners = new Map();
    this.dataset = {};
    this.attributes = new Map();
    this.textContent = "";
    this.hidden = false;
    this.disabled = false;
    this.open = false;
    this.closeCount = 0;
    this.srcSetCount = 0;
    this._src = "";
  }

  set src(value) {
    this._src = value;
    this.srcSetCount += 1;
  }

  get src() {
    return this._src;
  }

  addEventListener(name, listener) {
    this.listeners.set(name, listener);
  }

  emit(name, supplied = {}) {
    const event = {preventDefault() { this.defaultPrevented = true; }, ...supplied};
    return this.listeners.get(name)(event);
  }

  getBoundingClientRect() {
    return {left: 10, top: 20, width: 800, height: 450};
  }

  setAttribute(name, value) {
    this.attributes.set(name, value);
  }

  removeAttribute(name) {
    this.attributes.delete(name);
  }

  showModal() {
    this.open = true;
  }

  close() {
    this.open = false;
    this.closeCount += 1;
  }
}

function createEnvironment(fetchImpl, {preload = false, mobile = false} = {}) {
  const selectors = [
    "[data-account-scan]",
    "#scan-start",
    "#scan-dialog",
    "#scan-close",
    "#scan-cancel",
    "#scan-retry",
    "#scan-alternate",
    "#scan-stage",
    "#scan-status",
    "#scan-countdown",
    "#scan-qr",
    "#scan-qr-crop",
    "#scan-qr-save",
    "#scan-mobile-actions",
    "#scan-placeholder",
    "#scan-text",
    "#scan-type",
    "#scan-mode-toggle",
    "#scan-phone",
    "#scan-phone-send",
    "#scan-phone-row",
    ".scan-sms-hint",
  ];
  const elements = Object.fromEntries(selectors.map((selector) => [selector, new Element()]));
  elements["[data-account-scan]"].dataset.csrfToken = "csrf-fixture";
  elements["[data-account-scan]"].dataset.preload = preload ? "true" : "false";
  const timers = new Map();
  const windowListeners = new Map();
  const beacons = [];
  let nextTimer = 1;
  global.document = {
    querySelector(selector) { return elements[selector]; },
    querySelectorAll() { return []; },
  };
  global.fetch = fetchImpl;
  Object.defineProperty(global, "navigator", {
    configurable: true,
    value: {
      sendBeacon(url, body) {
        beacons.push({url, body: String(body)});
        return true;
      },
    },
  });
  let reloadCount = 0;
  global.window = {
    addEventListener(name, listener) { windowListeners.set(name, listener); },
    clearTimeout(id) { timers.delete(id); },
    confirm() { return true; },
    location: {assign() {}, reload() { reloadCount += 1; }},
    matchMedia() { return {matches: mobile}; },
    setTimeout(callback, delay) {
      const id = nextTimer++;
      timers.set(id, {callback, delay});
      return id;
    },
  };
  const source = fs.readFileSync(
    path.resolve(__dirname, "../../spark_console/static/account_scan.js"),
    "utf8",
  );
  vm.runInThisContext(source, {filename: "account_scan.js"});
  return {
    beacons,
    elements,
    reloadCount: () => reloadCount,
    timers,
    windowListeners,
  };
}

async function flush() {
  await Promise.resolve();
  await new Promise((resolve) => setImmediate(resolve));
}

async function pendingIntent(controlSelector) {
  const start = deferred();
  const cancel = deferred();
  const calls = [];
  const {elements, timers} = createEnvironment((url) => {
    calls.push(url);
    return url === "/accounts/scan" ? start.promise : cancel.promise;
  });
  const dialog = elements["#scan-dialog"];
  const startTask = elements["#scan-start"].emit("click");
  await flush();
  const intentTask = elements[controlSelector].emit("click");
  await flush();
  assert(dialog.closeCount === 0, "dialog closed while scan creation was pending");

  start.resolve(response(201, {
    id: "scan-1",
    status: "queued",
    remaining_seconds: 300,
    error: null,
    message: "等待开始扫码",
    account_id: null,
  }));
  await flush();
  assert(calls.includes("/accounts/scan/scan-1/cancel"), "pending close intent did not cancel returned scan id");
  assert(dialog.closeCount === 0, "dialog closed before cancellation completed");

  cancel.resolve(response(200, {
    id: "scan-1",
    status: "cancelled",
    remaining_seconds: 0,
    error: "cancelled",
    message: "扫码已取消",
    account_id: null,
  }));
  await Promise.all([startTask, intentTask]);
  await flush();
  assert(dialog.closeCount === 1, "dialog did not close after successful cancellation");
  assert(timers.size === 0, "polling remained scheduled after pending cancellation");
}

async function activeCancelFailure() {
  let cancelAttempt = 0;
  const {elements, timers} = createEnvironment(async (url) => {
    if (url === "/accounts/scan") {
      return response(201, {
        id: "scan-2",
        status: "queued",
        remaining_seconds: 300,
        error: null,
        message: "等待开始扫码",
        account_id: null,
      });
    }
    cancelAttempt += 1;
    if (cancelAttempt === 1) {
      return response(503, {error: "internal", message: "sensitive server detail"});
    }
    return response(200, {
      id: "scan-2",
      status: "cancelled",
      remaining_seconds: 0,
      error: "cancelled",
      message: "扫码已取消",
      account_id: null,
    });
  });
  const dialog = elements["#scan-dialog"];
  await elements["#scan-start"].emit("click");
  assert(timers.size === 1, "active scan did not schedule its poll");

  await elements["#scan-cancel"].emit("click");
  assert(dialog.closeCount === 0, "dialog closed after cancellation failed");
  assert(dialog.open, "dialog was hidden after cancellation failed");
  assert(elements["#scan-status"].textContent === "取消失败，请重试", "cancellation failure was not fixed user copy");
  assert(!elements["#scan-status"].textContent.includes("sensitive"), "server cancellation detail reached the UI");
  assert(!elements["#scan-cancel"].disabled, "cancel control was not re-enabled for retry");
  assert(timers.size === 1, "cancellation failure should resume one status poll");

  await elements["#scan-cancel"].emit("click");
  assert(dialog.closeCount === 1, "successful cancellation retry did not close dialog");
}

async function noAutoPreload() {
  const calls = [];
  const {elements} = createEnvironment(async (url) => {
    calls.push(url);
    return response(200, {
      id: "scan-preloaded",
      status: "awaiting_scan",
      remaining_seconds: 250,
      error: null,
      message: "请使用抖音 App 扫码并在手机确认",
      account_id: null,
    });
  }, {preload: true});

  await flush();
  assert(calls.length === 0, "opening the page claimed the global scan slot");
  await elements["#scan-start"].emit("click");
  assert(calls.length === 1 && calls[0] === "/accounts/scan", "button did not create the scan");
  assert(elements["#scan-dialog"].open, "scan dialog did not open");
}

async function qrStaysCachedWhilePolling() {
  const calls = [];
  const {elements, timers} = createEnvironment(async (url) => {
    calls.push(url);
    return response(200, {
      id: "scan-cached",
      status: "awaiting_scan",
      remaining_seconds: 250,
      error: null,
      message: "请使用抖音 App 扫码并在手机确认",
      account_id: null,
    });
  });

  await elements["#scan-start"].emit("click");
  const scheduled = [...timers.values()];
  assert(scheduled.length === 1, "active scan poll was not scheduled");
  scheduled[0].callback();
  await flush();
  assert(calls.length === 2, "status polling did not run exactly once");
  assert(elements["#scan-qr"].src.includes("/accounts/scan/scan-cached/qr"), "status polling lost the browser view");
  assert(elements["#scan-qr"].srcSetCount === 1, "status polling bypassed the QR refresh throttle");
}

async function mobileCrop() {
  let statusPoll = 0;
  const {elements, timers} = createEnvironment(async (url) => {
    if (url === "/accounts/scan") return response(201, {
      id: "scan-mobile", login_mode: "qr", status: "awaiting_scan", remaining_seconds: 250,
      error: null, message: "请使用抖音 App 扫码并在手机确认", account_id: null,
    });
    statusPoll += 1;
    return response(200, {
      id: "scan-mobile", login_mode: "qr", status: "confirming", remaining_seconds: 220,
      error: null, message: "已扫码", account_id: null,
    });
  }, {mobile: true});

  await elements["#scan-start"].emit("click");
  assert(elements["#scan-qr"].hidden, "mobile showed the full browser before scan");
  assert(!elements["#scan-qr-crop"].hidden, "mobile QR crop was hidden");
  assert(elements["#scan-qr-crop"].src.includes("/qr-crop"), "mobile did not request QR crop");
  assert(elements["#scan-qr-save"].href.includes("/qr-crop"), "save action did not target QR crop");
  [...timers.values()][0].callback();
  await flush();
  assert(statusPoll === 1, "mobile status was not polled");
  assert(!elements["#scan-qr"].hidden, "mobile did not switch to full browser after scan");
  assert(elements["#scan-qr-crop"].hidden, "mobile crop stayed visible after scan");
}

async function pagehideCancel() {
  const {beacons, elements, windowListeners} = createEnvironment(async () => response(201, {
    id: "scan-leaving",
    status: "queued",
    remaining_seconds: 300,
    error: null,
    message: "等待开始扫码",
    account_id: null,
  }));

  await elements["#scan-start"].emit("click");
  assert(!windowListeners.has("pagehide"), "switching to Douyin must not cancel the login");
  windowListeners.get("pageshow")();
  assert(beacons.length === 0, "app switch sent a cancellation");
}

async function successClose() {
  const {elements, reloadCount, timers} = createEnvironment(async () => response(201, {
    id: "scan-success",
    status: "succeeded",
    remaining_seconds: 0,
    error: null,
    message: "绑定成功",
    account_id: "account-1",
  }));

  await elements["#scan-start"].emit("click");
  assert(elements["#scan-status"].textContent === "登录成功，正在刷新账号列表", "success feedback was not shown");
  assert(elements["#scan-dialog"].closeCount === 0, "dialog closed before success feedback was visible");
  assert(reloadCount() === 0, "page refreshed before success feedback was visible");
  const scheduled = [...timers.values()];
  assert(scheduled.length === 1, "success close was not scheduled");
  scheduled[0].callback();
  assert(elements["#scan-dialog"].closeCount === 1, "success did not close the dialog");
  assert(reloadCount() === 1, "success did not refresh the account list");
}

async function browserClick() {
  const calls = [];
  const {elements} = createEnvironment(async (url, options = {}) => {
    calls.push({url, options});
    if (url === "/accounts/scan") return response(201, {
      id: "scan-browser",
      status: "awaiting_scan",
      remaining_seconds: 300,
      error: null,
      message: "请使用抖音 App 扫码并在手机确认",
      account_id: null,
    });
    return response(202, {accepted: true});
  });

  await elements["#scan-start"].emit("click");
  assert(
    elements["#scan-qr"].listeners.has("click"),
    "cloud browser view did not register click forwarding",
  );
  await elements["#scan-qr"].emit("click", {clientX: 210, clientY: 245});
  const interaction = calls.find((call) => call.url.includes("/interact"));
  assert(interaction, "browser click was not sent to the scan interaction endpoint");
  const body = String(interaction.options.body);
  assert(body.includes("x=0.25"), "browser click x coordinate was not normalized");
  assert(body.includes("y=0.5"), "browser click y coordinate was not normalized");
  assert(body.includes("csrf_token=csrf-fixture"), "browser click omitted CSRF");
}

async function browserText() {
  const calls = [];
  const {elements} = createEnvironment(async (url, options = {}) => {
    calls.push({url, options});
    if (url === "/accounts/scan") return response(201, {
      id: "scan-text",
      status: "awaiting_scan",
      remaining_seconds: 300,
      error: null,
      message: "请使用抖音 App 扫码并在手机确认",
      account_id: null,
    });
    return response(202, {accepted: true});
  });
  await elements["#scan-start"].emit("click");
  elements["#scan-text"].value = "123456";
  await elements["#scan-type"].emit("click");

  const interaction = calls.find((call) => call.url.includes("/interact"));
  assert(interaction, "verification code was not sent to the interaction endpoint");
  const body = String(interaction.options.body);
  assert(body.includes("kind=text"), "verification input omitted the text action kind");
  assert(body.includes("text=123456"), "verification input omitted the code");
  assert(elements["#scan-text"].value === "", "verification code remained in the page after sending");
}

async function restartAfterCancel(toggle) {
  const starts = [];
  const {elements} = createEnvironment(async (url, options) => {
    if (url === "/accounts/scan") {
      const mode = new URLSearchParams(options.body).get("mode");
      starts.push(mode);
      return response(201, {id: `session-${starts.length}`, status: mode === "sms" ? "awaiting_phone" : "awaiting_scan", login_mode: mode});
    }
    if (url.endsWith("/cancel")) return response(200, {id: "session-1", status: "cancelled", login_mode: "sms"});
    throw new Error("unexpected request");
  }, {mobile: true});
  elements["#scan-start"].emit("pointerdown");
  await flush();
  await elements["#scan-start"].emit("click");
  if (toggle) {
    elements["#scan-mode-toggle"].emit("click");
    await flush();
  } else {
    await elements["#scan-cancel"].emit("click");
    await elements["#scan-start"].emit("click");
  }
  assert(starts.length === 2, "retry reused the cancelled prepared session");
  assert(starts[1] === (toggle ? "qr" : "sms"), "retry used the wrong login mode");
  assert(elements["#scan-dialog"].open, "retry dialog did not open");
}

async function qrSecondarySms() {
  const {elements} = createEnvironment(async () => response(201, {
    id: "qr-sms", status: "confirming", login_mode: "qr",
  }));
  await elements["#scan-start"].emit("click");
  assert(!elements["#scan-text"].disabled, "QR secondary SMS input is disabled");
  assert(!elements["#scan-type"].disabled, "QR secondary SMS submit is disabled");
}

async function phoneSubmitKeepsSinglePoll() {
  let polls = 0;
  const pendingPoll = deferred();
  const {elements, timers} = createEnvironment(async (url) => {
    if (url.endsWith("/interact")) return response(202, {accepted: true});
    if (url === "/accounts/scan") return response(201, {id: "sms", status: "awaiting_phone", login_mode: "sms"});
    polls += 1;
    return pendingPoll.promise;
  }, {mobile: true});
  await elements["#scan-start"].emit("click");
  elements["#scan-phone"].value = "13800000000";
  await elements["#scan-phone-send"].emit("click");
  assert(timers.size === 1, "phone submission created a second poll timer");
  const [id, timer] = [...timers.entries()][0];
  timers.delete(id); // Browser removes a one-shot timer before its callback.
  timer.callback();
  await flush();
  await elements["#scan-phone-send"].emit("click");
  assert(timers.size === 0, "submission while polling started another timer");
  pendingPoll.resolve(response(200, {id: "sms", status: "awaiting_sms", login_mode: "sms"}));
  await flush();
  assert(polls === 1 && timers.size === 1, "poll completion did not retain exactly one loop");
}

async function recovery(kind) {
  let starts = 0, polls = 0;
  const late = deferred();
  const env = createEnvironment(async (url) => {
    if (url === "/accounts/scan") return response(201, {id: `s${++starts}`, status: "awaiting_phone", login_mode: "sms"});
    if (url.endsWith("/cancel")) return response(200, {id: "s1", status: "cancelled"});
    polls++;
    if (kind === "late") return late.promise;
    if (kind === "permanent") return response(404, {message: "not found"});
    if (polls === 1) throw new Error("offline");
    return response(200, {id: "s1", status: "awaiting_sms", login_mode: "sms"});
  }, {mobile: true});
  const fire = async () => {
    const [id, t] = [...env.timers][0]; env.timers.delete(id); t.callback(); await flush();
  };
  await env.elements["#scan-start"].emit("click");
  await fire();
  if (kind === "late") {
    await env.elements["#scan-cancel"].emit("click");
    late.resolve(response(200, {id: "s1", status: "awaiting_phone", login_mode: "sms"}));
    await flush();
    await env.elements["#scan-start"].emit("click");
    assert(starts === 2, "late response resurrected cancelled session");
  } else if (kind === "permanent") {
    assert(env.timers.size === 0, "permanent error must stop retry");
    assert(!env.elements["#scan-retry"].hidden, "missing recovery entry");
    await env.elements["#scan-alternate"].emit("click"); await flush();
    assert(starts === 2, "alternate mode must start new session");
  } else {
    assert(env.timers.size === 1, "transient failure lost polling");
    await fire();
    assert(polls === 2 && env.timers.size === 1, "recovery duplicated or stopped polling");
    assert(!env.elements["#scan-text"].disabled, "recovery did not render SMS stage");
  }
}

async function main() {
  const scenario = process.argv[2];
  if (["late", "reconnect", "permanent"].includes(scenario)) await recovery(scenario);
  else if (scenario === "pending-close") await pendingIntent("#scan-close");
  else if (scenario === "pending-cancel") await pendingIntent("#scan-cancel");
  else if (scenario === "active-cancel-failure") await activeCancelFailure();
  else if (scenario === "no-auto-preload") await noAutoPreload();
  else if (scenario === "mobile-crop") await mobileCrop();
  else if (scenario === "qr-stays-cached") await qrStaysCachedWhilePolling();
  else if (scenario === "pagehide-cancel") await pagehideCancel();
  else if (scenario === "success-close") await successClose();
  else if (scenario === "browser-click") await browserClick();
  else if (scenario === "browser-text") await browserText();
  else if (scenario === "desktop-start") await startByViewport(false);
  else if (scenario === "mobile-start") await startByViewport(true);
  else if (scenario === "retry-cancelled") await restartAfterCancel(false);
  else if (scenario === "toggle-after-preload") await restartAfterCancel(true);
  else if (scenario === "qr-secondary-sms") await qrSecondarySms();
  else if (scenario === "phone-single-poll") await phoneSubmitKeepsSinglePoll();
  else throw new Error(`unknown scenario: ${scenario}`);
  process.stdout.write(JSON.stringify({scenario, ok: true}));
}

async function startByViewport(mobile) {
  const calls = [];
  const {elements} = createEnvironment(async (url, options) => {
    calls.push({url, options});
    return response(201, {id: 'viewport-scan', status: mobile ? 'awaiting_phone' : 'awaiting_scan',
      login_mode: mobile ? 'sms' : 'qr', remaining_seconds: 250, message: 'ready'});
  }, {mobile});
  assert(elements['#scan-start'].listeners.has('click'), 'initialization failed before click binding');
  await elements['#scan-start'].emit('click');
  assert(elements['#scan-dialog'].open, 'click did not open dialog');
  assert(calls.length === 1 && calls[0].url === '/accounts/scan', 'click did not create exactly one session');
  assert(new URLSearchParams(calls[0].options.body).get('mode') === (mobile ? 'sms' : 'qr'), 'wrong viewport login mode');
  assert(elements['#scan-phone-row'].hidden === !mobile, 'phone input visibility incorrect');
}

main().catch((error) => {
  process.stderr.write(`${error.stack}\n`);
  process.exitCode = 1;
});
