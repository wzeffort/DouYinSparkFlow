document.querySelectorAll('[data-copy-invite]').forEach((button) => {
  button.addEventListener('click', async () => {
    const input = document.getElementById(button.dataset.copyInvite);
    const status = document.querySelector('[data-invite-copy-status]');
    if (!input || !status) return;
    try {
      await navigator.clipboard.writeText(input.value);
      status.textContent = '邀请链接已复制，可以发给好友了。';
      button.textContent = '已复制';
    } catch {
      input.focus();
      input.select();
      status.textContent = '请复制已选中的邀请链接。';
    }
  });
});
