// External handlers preserve confirmations without permitting inline JavaScript.
document.addEventListener('submit', function (event) {
  if (event.target.hasAttribute('data-submitting')) { event.preventDefault(); return; }
  var message = event.target.getAttribute('data-confirm');
  if (message && !window.confirm(message)) { event.preventDefault(); return; }
  if (event.target.hasAttribute('data-once')) {
    event.target.setAttribute('data-submitting', 'true');
    event.target.setAttribute('aria-busy', 'true');
    if (event.submitter && event.submitter.name) {
      var hidden = document.createElement('input');
      hidden.type = 'hidden'; hidden.name = event.submitter.name; hidden.value = event.submitter.value;
      hidden.setAttribute('data-submit-value',''); event.target.appendChild(hidden);
    }
    event.target.querySelectorAll('button[type="submit"],button:not([type])').forEach(function(button) { button.disabled = true; });
  }
});

window.addEventListener('pageshow', function () {
  document.querySelectorAll('[data-submitting]').forEach(function(form) {
    form.removeAttribute('data-submitting'); form.removeAttribute('aria-busy');
    form.querySelectorAll('[data-submit-value]').forEach(function(node) { node.remove(); });
    form.querySelectorAll('button[type="submit"],button:not([type])').forEach(function(button) { button.disabled = false; });
  });
});
document.addEventListener('change', function (event) {
  if (event.target.hasAttribute('data-auto-submit') && event.target.form) {
    event.target.form.submit();
  }
});
