const fs = require("fs");
const path = require("path");
const vm = require("vm");

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

class Element {
  constructor(name = "") {
    this.name = name;
    this.value = "";
    this.textContent = "";
    this.hidden = true;
    this.dataset = {};
    this.attributes = new Map();
    this.listeners = new Map();
  }
  addEventListener(name, listener) { this.listeners.set(name, listener); }
  emit(name) {
    const event = {defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }};
    this.listeners.get(name)?.(event);
    return event;
  }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  removeAttribute(name) { this.attributes.delete(name); }
}

const fields = Object.fromEntries(
  ["username", "password", "password_confirmation", "invite_code"].map((name) => [name, new Element(name)]),
);
const errors = Object.fromEntries(
  Object.keys(fields).map((name) => [name, new Element(`${name}-error`)]),
);
const rules = Object.fromEntries(
  ["length", "letter", "number"].map((name) => [name, new Element(name)]),
);
const form = new Element("form");
form.querySelector = (selector) => {
  const field = selector.match(/^\[name="(.+)"\]$/);
  if (field) return fields[field[1]];
  const error = selector.match(/^\[data-field-error="(.+)"\]$/);
  if (error) return errors[error[1]];
  const rule = selector.match(/^\[data-password-rule="(.+)"\]$/);
  if (rule) return rules[rule[1]];
  return null;
};

global.document = {querySelector(selector) { return selector === "[data-registration-form]" ? form : null; }};
let fetchCount = 0;
global.fetch = () => { fetchCount += 1; throw new Error("registration validation must not call fetch"); };

const source = fs.readFileSync(
  path.resolve(__dirname, "../../spark_console/static/register.js"),
  "utf8",
);
vm.runInThisContext(source, {filename: "register.js"});

fields.username.value = "x!";
fields.username.emit("input");
assert(fields.username.attributes.get("aria-invalid") === "true", "invalid username was not marked");
assert(errors.username.textContent.includes("3–32"), "username explanation is missing");

fields.username.value = "valid_name";
fields.username.emit("input");
assert(!fields.username.attributes.has("aria-invalid"), "corrected username stayed invalid");
assert(errors.username.hidden, "corrected username error stayed visible");

fields.password.value = "abcdefghij";
fields.password.emit("input");
assert(rules.length.dataset.valid === "true", "length rule was not satisfied");
assert(rules.letter.dataset.valid === "true", "letter rule was not satisfied");
assert(rules.number.dataset.valid === "false", "number rule should be unsatisfied");

fields.password.value = "StrongPass10";
fields.password.emit("input");
fields.password_confirmation.value = "Different10";
fields.password_confirmation.emit("input");
assert(errors.password_confirmation.textContent.includes("不一致"), "confirmation mismatch is missing");

fields.password_confirmation.value = "StrongPass10";
fields.password_confirmation.emit("input");
assert(errors.password_confirmation.hidden, "matching confirmation stayed invalid");
assert(fetchCount === 0, "client attempted to probe invite state");

process.stdout.write(JSON.stringify({registrationValidation: "ok"}));
