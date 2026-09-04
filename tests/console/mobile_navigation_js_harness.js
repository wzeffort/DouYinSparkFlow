const fs = require("fs");
const path = require("path");
const vm = require("vm");

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

class ClassList {
  constructor() { this.values = new Set(); }
  add(value) { this.values.add(value); }
  remove(value) { this.values.delete(value); }
  contains(value) { return this.values.has(value); }
}

class Element {
  constructor() {
    this.listeners = new Map();
    this.attributes = new Map();
    this.focusCount = 0;
  }
  addEventListener(name, listener) { this.listeners.set(name, listener); }
  emit(name, supplied = {}) {
    const event = {preventDefault() {}, ...supplied};
    const listener = this.listeners.get(name);
    if (listener) listener(event);
  }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getAttribute(name) { return this.attributes.get(name); }
  focus() { this.focusCount += 1; }
  querySelectorAll() { return []; }
}

const opener = new Element();
const closer = new Element();
const scrim = new Element();
const drawer = new Element();
const documentListeners = new Map();
const selectors = {
  "[data-mobile-nav-open]": opener,
  "[data-mobile-nav-close]": closer,
  "[data-mobile-nav-scrim]": scrim,
  "#app-navigation": drawer,
};

global.document = {
  body: {classList: new ClassList()},
  querySelector(selector) { return selectors[selector] || null; },
  addEventListener(name, listener) { documentListeners.set(name, listener); },
};

const scriptPath = path.join(__dirname, "..", "..", "spark_console", "static", "navigation.js");
vm.runInThisContext(fs.readFileSync(scriptPath, "utf8"), {filename: scriptPath});

opener.emit("click");
assert(document.body.classList.contains("mobile-nav-open"), "drawer did not open");
assert(opener.getAttribute("aria-expanded") === "true", "opener did not expose expanded state");
assert(closer.focusCount === 1, "close control did not receive focus");

documentListeners.get("keydown")({key: "Escape", preventDefault() {}});
assert(!document.body.classList.contains("mobile-nav-open"), "escape did not close drawer");
assert(opener.getAttribute("aria-expanded") === "false", "opener remained expanded");
assert(opener.focusCount === 1, "focus did not return to opener");

opener.emit("click");
scrim.emit("click");
assert(!document.body.classList.contains("mobile-nav-open"), "scrim did not close drawer");

console.log(JSON.stringify({mobileNavigation: "ok"}));
