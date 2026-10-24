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
    event.target.querySelectorAll('button[type="submit"],button:not([type])').forEach(function(button) {
      if (button.hasAttribute('data-busy-label')) {
        button.setAttribute('data-idle-label', button.textContent);
        button.textContent = button.getAttribute('data-busy-label');
      }
      button.disabled = true;
    });
  }
});

window.addEventListener('pageshow', function () {
  document.querySelectorAll('[data-submitting]').forEach(function(form) {
    form.removeAttribute('data-submitting'); form.removeAttribute('aria-busy');
    form.querySelectorAll('[data-submit-value]').forEach(function(node) { node.remove(); });
    form.querySelectorAll('button[type="submit"],button:not([type])').forEach(function(button) {
      button.disabled = false;
      if (button.hasAttribute('data-idle-label')) {
        button.textContent = button.getAttribute('data-idle-label');
        button.removeAttribute('data-idle-label');
      }
    });
  });
});
document.addEventListener('change', function (event) {
  if (event.target.hasAttribute('data-auto-submit') && event.target.form) {
    event.target.form.submit();
  }
});

// Password reveal buttons: <button data-password-toggle="<input-id>">.
document.addEventListener('click', function (event) {
  var toggle = event.target.closest ? event.target.closest('[data-password-toggle]') : null;
  if (!toggle) { return; }
  var input = document.getElementById(toggle.getAttribute('data-password-toggle'));
  if (!input) { return; }
  var reveal = input.type === 'password';
  input.type = reveal ? 'text' : 'password';
  toggle.textContent = reveal ? '隐藏' : '显示';
  toggle.setAttribute('aria-label', reveal ? '隐藏密码' : '显示密码');
  input.focus();
});

// Caps Lock notice for password fields paired with [data-capslock-hint].
['keydown', 'keyup', 'blur'].forEach(function (type) {
  document.addEventListener(type, function (event) {
    if (!event.target.matches || !event.target.matches('input[type="password"]')) { return; }
    var label = event.target.closest ? event.target.closest('label') : null;
    var hint = label ? label.querySelector('[data-capslock-hint]') : null;
    if (!hint) { return; }
    var active = type !== 'blur' && event.getModifierState && event.getModifierState('CapsLock');
    hint.hidden = !active;
  });
});

// QR zoom dialog: <button data-qr-zoom="<url>" data-qr-amount data-qr-channel>.
document.addEventListener('click', function (event) {
  var trigger = event.target.closest ? event.target.closest('[data-qr-zoom]') : null;
  if (trigger) {
    event.preventDefault();
    if (document.querySelector('.qr-zoom')) { return; }
    var dialog = document.createElement('div');
    dialog.className = 'qr-zoom';
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-modal', 'true');
    dialog.setAttribute('aria-label', '收款码大图');
    var amount = trigger.getAttribute('data-qr-amount') || '';
    var channel = trigger.getAttribute('data-qr-channel') || '';
    dialog.innerHTML =
      '<div class="qr-zoom-card">' +
        '<button type="button" class="qr-zoom-close" aria-label="关闭大图">×</button>' +
        '<div class="qr-zoom-frame"><img alt="' + channel + '收款码大图" src="' + trigger.getAttribute('data-qr-zoom') + '"></div>' +
        (amount ? '<p class="qr-zoom-caption">请用' + channel + '扫码，手动输入 <strong>' + amount + '</strong></p>' : '') +
      '</div>';
    dialog.addEventListener('click', function (closeEvent) {
      if (closeEvent.target === dialog || closeEvent.target.classList.contains('qr-zoom-close')) {
        dialog.remove();
        document.body.classList.remove('qr-zoom-open');
        if (trigger.focus) { trigger.focus(); }
      }
    });
    document.addEventListener('keydown', function onKey(closeEvent) {
      if (closeEvent.key === 'Escape' && document.querySelector('.qr-zoom')) {
        document.querySelector('.qr-zoom').remove();
        document.body.classList.remove('qr-zoom-open');
        document.removeEventListener('keydown', onKey);
      }
    });
    document.body.appendChild(dialog);
    document.body.classList.add('qr-zoom-open');
    var image = dialog.querySelector('img');
    if (image) { image.addEventListener('click', function () { dialog.remove(); document.body.classList.remove('qr-zoom-open'); }); }
    var closeButton = dialog.querySelector('.qr-zoom-close');
    if (closeButton && closeButton.focus) { closeButton.focus(); }
    return;
  }
});
