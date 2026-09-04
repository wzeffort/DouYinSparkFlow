(() => {
  "use strict";
  const form = document.querySelector("[data-registration-form]");
  if (!form) return;
  const fields = {};
  for (const name of ["username", "email", "password", "password_confirmation", "invite_code"]) {
    const input = form.querySelector(`[name="${name}"]`);
    if (input) fields[name] = input;
  }
  function errorNode(name) { return form.querySelector(`[data-field-error="${name}"]`); }
  function setError(name, message) {
    const input = fields[name];
    const node = errorNode(name);
    if (!input || !node) return false;
    if (message) {
      input.setAttribute("aria-invalid", "true");
      input.setAttribute("aria-describedby", node.id || `${name}-error`);
      node.textContent = message;
      node.hidden = false;
      return false;
    }
    input.removeAttribute("aria-invalid");
    node.textContent = "";
    node.hidden = true;
    return true;
  }
  function validateUsername() {
    return setError("username", /^[A-Za-z0-9_-]{3,32}$/.test(fields.username.value.trim()) ? "" : "用户名须为 3–32 位字母、数字、下划线或短横线");
  }
  function updatePasswordRules() {
    const value = fields.password.value;
    const states = {length: value.length >= 10, letter: /[A-Za-z]/.test(value), number: /[0-9]/.test(value)};
    for (const [rule, valid] of Object.entries(states)) {
      const node = form.querySelector(`[data-password-rule="${rule}"]`);
      if (node) node.dataset.valid = valid ? "true" : "false";
    }
    const message = !states.length ? "密码至少需要 10 位" : !states.letter ? "密码必须包含至少一个字母" : !states.number ? "密码必须包含至少一个数字" : "";
    return setError("password", message);
  }
  function validateConfirmation() {
    const value = fields.password_confirmation.value;
    return setError("password_confirmation", value && value === fields.password.value ? "" : "两次输入的密码不一致");
  }
  fields.username.addEventListener("input", validateUsername);
  fields.username.addEventListener("blur", validateUsername);
  fields.password.addEventListener("input", () => { updatePasswordRules(); if (fields.password_confirmation.value) validateConfirmation(); });
  fields.password.addEventListener("blur", updatePasswordRules);
  fields.password_confirmation.addEventListener("input", validateConfirmation);
  fields.password_confirmation.addEventListener("blur", validateConfirmation);
  form.addEventListener("submit", (event) => {
    const valid = Boolean(validateUsername() & updatePasswordRules() & validateConfirmation());
    if (!valid) event.preventDefault();
  });
})();
