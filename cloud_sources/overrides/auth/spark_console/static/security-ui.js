// External handlers preserve confirmations without permitting inline JavaScript.
document.addEventListener('submit', function (event) {
  var message = event.target.getAttribute('data-confirm');
  if (message && !window.confirm(message)) event.preventDefault();
});
document.addEventListener('change', function (event) {
  if (event.target.hasAttribute('data-auto-submit') && event.target.form) {
    event.target.form.submit();
  }
});
