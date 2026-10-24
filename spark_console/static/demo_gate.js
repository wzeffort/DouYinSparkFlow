// Demo-mode operation gate: preview visitors keep their page and form state
// and get a signup pitch instead of being redirected away. The server-side
// 401 -> /register?demo=1 handler remains as the unconditional safety net.
(function () {
  if (!document.body.classList.contains('demo-on')) { return; }

  var modal = null;
  var lastFocus = null;

  function closeModal() {
    if (!modal) { return; }
    modal.remove();
    modal = null;
    document.body.classList.remove('demo-gate-open');
    if (lastFocus && lastFocus.focus) { lastFocus.focus(); }
  }

  function openModal() {
    if (modal) { return; }
    lastFocus = document.activeElement;
    modal = document.createElement('div');
    modal.className = 'demo-gate';
    modal.setAttribute('role', 'dialog');
    modal.setAttribute('aria-modal', 'true');
    modal.setAttribute('aria-labelledby', 'demo-gate-title');
    modal.innerHTML =
      '<div class="demo-gate-card" tabindex="-1">' +
        '<span class="demo-gate-mark" aria-hidden="true">火</span>' +
        '<h2 id="demo-gate-title">就差一步啦</h2>' +
        '<p>注册后即可完成刚才的操作：绑定账号、保存任务，让每天的问候自动准时送达。新用户注册即可免费试用。</p>' +
        '<div class="demo-gate-actions">' +
          '<a class="demo-gate-primary" href="/register?demo=1">立即注册，完成操作</a>' +
          '<button type="button" class="demo-gate-quiet">先随便看看</button>' +
        '</div>' +
        '<a class="demo-gate-login" href="/login">已有账号？直接登录</a>' +
      '</div>';
    modal.addEventListener('click', function (event) {
      if (event.target === modal || event.target.classList.contains('demo-gate-quiet')) { closeModal(); }
    });
    document.addEventListener('keydown', function onKey(event) {
      if (event.key === 'Escape') { closeModal(); document.removeEventListener('keydown', onKey); }
    });
    document.body.appendChild(modal);
    document.body.classList.add('demo-gate-open');
    var primary = modal.querySelector('.demo-gate-primary');
    if (primary && primary.focus) { primary.focus(); }
  }

  document.addEventListener('submit', function (event) {
    if (!document.body.classList.contains('demo-on')) { return; }
    event.preventDefault();
    event.stopImmediatePropagation();
    openModal();
  }, true);
})();
