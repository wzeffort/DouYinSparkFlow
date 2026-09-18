(() => {
  const form = document.querySelector('[data-task-draft]');
  if (!form) return;
  const root = form.querySelector('[data-batch-composer]');
  const account = form.querySelector('#task-account');
  const time = form.querySelector('#task-send-time');
  const field = form.querySelector('[name="recipients_json"]');
  const revision = form.querySelector('[name="draft_revision"]');
  const status = form.querySelector('[data-draft-status]');
  const clear = form.querySelector('[data-clear-draft]');
  const key = `spark:task-draft:${root.dataset.viewerId}`;
  const read = () => {
    try { return JSON.parse(localStorage.getItem(key) || 'null'); } catch { return null; }
  };
  const remove = () => {
    try { localStorage.removeItem(key); return true; } catch { return false; }
  };
  let saved = read();
  const url = new URL(location.href);
  const completed = form.dataset.completedDraft;
  if (completed) {
    if (saved?.revision === completed && remove()) saved = null;
    url.searchParams.delete('created_draft');
    history.replaceState(null, '', url);
  }
  const valid = value => value && value.version === 1 &&
    typeof value.account_id === 'string' && typeof value.send_time === 'string' &&
    typeof value.revision === 'string' && /^[a-f0-9]{32}$/.test(value.revision) &&
    Array.isArray(value.recipients) && value.recipients.length <= 5 &&
    value.recipients.every(item => item && ['target_name','target_sec_uid','message_template'].every(
      name => typeof item[name] === 'string') && item.target_name.length <= 64 &&
      item.target_sec_uid.length <= 256 && item.message_template.length <= 500);
  if (valid(saved) && form.dataset.serverForm !== 'true') {
    if ([...account.options].some(option => option.value === saved.account_id)) {
      account.value = saved.account_id;
      account.selectedOptions[0].setAttribute('selected', '');
      time.value = saved.send_time;
      field.value = JSON.stringify(saved.recipients);
      revision.value = saved.revision;
      status.textContent = '已恢复上次草稿 · 仅保存在当前浏览器';
    } else {
      status.textContent = '草稿中的抖音账号已不可用，请重新选择账号和好友';
    }
  }
  function contents() {
    let recipients;
    try { recipients = JSON.parse(field.value || '[]'); } catch { recipients = []; }
    if (!Array.isArray(recipients)) recipients = [];
    if (!field.value) recipients = [{
      target_name: root.querySelector('[name="target_name"]').value,
      target_sec_uid: root.querySelector('[name="target_sec_uid"]').value,
      message_template: root.querySelector('[name="message_template"]').value
    }];
    return {account_id: account.value, send_time: time.value, recipients};
  }
  let previous = JSON.stringify(contents());
  function save() {
    const body = contents(), fingerprint = JSON.stringify(body);
    if (fingerprint === previous) return;
    const id = crypto.randomUUID().replaceAll('-', '');
    try {
      localStorage.setItem(key, JSON.stringify({...body, version:1, revision:id}));
      revision.value = id;
      previous = fingerprint;
      status.textContent = '草稿已保存 · 仅保存在当前浏览器';
    } catch {
      status.textContent = '浏览器未能保存草稿，请勿关闭页面';
    }
  }
  root.addEventListener('batchchange', save);
  form.addEventListener('input', save);
  form.addEventListener('change', save);
  if (time.value) time.dispatchEvent(new Event('change', {bubbles:true}));
  clear.addEventListener('click', () => {
    if (!window.confirm('清空当前草稿中的好友、消息和发送时间？')) return;
    if (remove()) location.assign('/tasks');
    else status.textContent = '草稿未能清空，请检查浏览器存储设置';
  });
})();
