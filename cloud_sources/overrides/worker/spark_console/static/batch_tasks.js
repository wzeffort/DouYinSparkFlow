(() => {
  const root = document.querySelector('[data-batch-composer]');
  if (!root) return;
  const form = root.closest('form');
  const account = form.querySelector('#task-account');
  const field = root.querySelector('[name="recipients_json"]');
  const cards = root.querySelector('[data-recipient-cards]');
  const count = root.querySelector('[data-batch-count]');
  const error = root.querySelector('[data-batch-error]');
  const search = root.querySelector('[data-friend-search]');
  const options = root.querySelector('[data-friend-options]');
  const status = root.querySelector('[data-friend-status]');
  let friends = [], requestVersion = 0, items = [];
  const notify = (text) => { error.textContent = text; error.hidden = !text; };
  try { items = JSON.parse(field.value || '[]'); } catch { notify('好友列表格式无效，请重新选择。'); }
  if (!Array.isArray(items)) items = [];
  if (!items.length) {
    items = [{target_name: root.querySelector('[name="target_name"]').value,
      target_sec_uid: root.querySelector('[name="target_sec_uid"]').value,
      message_template: root.querySelector('[name="message_template"]').value}];
  }
  items = items.filter(x => x && typeof x === 'object').map(x => ({
    target_name: typeof x.target_name === 'string' ? x.target_name : '',
    target_sec_uid: typeof x.target_sec_uid === 'string' ? x.target_sec_uid : '',
    message_template: typeof x.message_template === 'string' ? x.message_template : ''
  }));
  const fallback = root.querySelector('[data-batch-fallback]');
  fallback.hidden = true;
  fallback.querySelectorAll('input,textarea,button').forEach(el => { el.disabled = true; });
  root.querySelector('.batch-enhanced').hidden = false;
  function sync() {
    field.value = JSON.stringify(items);
    count.textContent = `已选 ${items.length}/5 位`;
    root.querySelector('[data-add-recipient]').disabled = items.length >= 5;
  }
  function render() {
    cards.replaceChildren();
    items.forEach((item, index) => {
      const card = document.createElement('article'); card.className = 'batch-recipient-card';
      const header = document.createElement('div'); header.className = 'batch-recipient-heading';
      const title = document.createElement('strong'); title.textContent = `好友 ${index + 1}`;
      const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'quiet'; remove.textContent = '移除';
      remove.setAttribute('aria-label', `移除好友 ${index + 1}`);
      remove.addEventListener('click', () => { items.splice(index, 1); render(); renderOptions(); });
      header.append(title, remove);
      const nameLabel = document.createElement('label'); nameLabel.textContent = '好友昵称或备注';
      const name = document.createElement('input'); name.value = item.target_name; name.maxLength = 64; name.required = true;
      name.placeholder = '输入准确昵称，或从上方列表选择';
      name.addEventListener('input', () => { item.target_name = name.value; item.target_sec_uid = ''; sync(); });
      nameLabel.append(name);
      const label = document.createElement('label'); label.textContent = '给这位好友的专属内容';
      const message = document.createElement('textarea'); message.value = item.message_template; message.maxLength = 500; message.rows = 3; message.required = true;
      message.placeholder = '例如：今天也记得开心呀';
      message.addEventListener('input', () => { item.message_template = message.value; sync(); });
      label.append(message); card.append(header, nameLabel, label); cards.append(card);
    });
    sync();
  }
  function selected(friend) {
    return items.some(item => (friend.sec_uid && item.target_sec_uid === friend.sec_uid) || item.target_name === friend.name);
  }
  function add(friend) {
    if (selected(friend)) return;
    const blank = items.find(item => !item.target_name.trim());
    if (blank) { blank.target_name = friend.name; blank.target_sec_uid = friend.sec_uid || ''; }
    else if (items.length < 5) items.push({target_name: friend.name, target_sec_uid: friend.sec_uid || '', message_template: ''});
    else { notify('每个任务最多 5 位好友，请移除后再添加。'); return; }
    notify(''); render(); renderOptions();
  }
  function filtered() { return friends.filter(friend => friend.name.toLocaleLowerCase().includes(search.value.trim().toLocaleLowerCase())); }
  function renderOptions() {
    options.replaceChildren();
    filtered().slice(0, 100).forEach(friend => {
      const button = document.createElement('button'); button.type = 'button'; button.className = 'quiet';
      button.textContent = `${selected(friend) ? '✓ ' : '＋ '}${friend.name}`; button.disabled = selected(friend);
      button.addEventListener('click', () => add(friend)); options.append(button);
    });
  }
  async function load() {
    const version = ++requestVersion;
    friends = []; renderOptions(); status.textContent = '正在读取已保存的好友列表…';
    try {
      const prefix = account.dataset.conversationPrefix || '/accounts';
      const response = await fetch(`${prefix}/${encodeURIComponent(account.value)}/conversations`, {credentials: 'same-origin', headers: {Accept:'application/json'}});
      if (!response.ok) throw new Error('读取失败，可手动输入准确昵称');
      const body = await response.json();
      if (version !== requestVersion) return;
      friends = (body.items || []).filter(x => x && typeof x.name === 'string');
      status.textContent = friends.length ? `已读取 ${friends.length} 位最近会话好友；点击添加` : '暂无已保存好友，可手动添加。';
      renderOptions();
    } catch (failure) { if (version === requestVersion) status.textContent = failure.message; }
  }
  search.addEventListener('input', renderOptions);
  root.querySelector('[data-refresh-friends]').addEventListener('click', load);
  root.querySelector('[data-add-filtered]').addEventListener('click', () => {
    const pending = filtered().filter(friend => !selected(friend));
    const remaining = 5 - items.filter(item => item.target_name.trim()).length;
    if (pending.length > remaining) { notify(`筛选结果超过剩余 ${remaining} 个位置，请缩小筛选范围或逐个选择。`); return; }
    pending.forEach(add);
  });
  root.querySelector('[data-add-recipient]').addEventListener('click', () => {
    if (items.length < 5) { items.push({target_name:'', target_sec_uid:'', message_template:''}); render(); cards.lastElementChild.querySelector('input').focus(); }
  });
  root.querySelector('[data-fill-all]').addEventListener('click', () => {
    const value = root.querySelector('[data-bulk-message]').value.trim();
    if (!value) { notify('请先填写统一内容。'); return; }
    if (items.some(item => item.message_template.trim()) && !window.confirm('替换所有已选好友的消息内容？')) return;
    items.forEach(item => { item.message_template = value; }); notify(''); render();
  });
  let priorAccount = account.value;
  account.addEventListener('change', () => {
    if (items.some(item => item.target_name.trim()) && !window.confirm('切换账号会清空当前好友和消息，继续吗？')) { account.value = priorAccount; return; }
    priorAccount = account.value; items = []; search.value = ''; notify(''); render(); load();
  });
  form.addEventListener('submit', event => {
    if (!items.length || items.length > 5) { event.preventDefault(); notify('请选择 1–5 位好友。'); return; }
    const names = items.map(item => item.target_name.trim());
    if (new Set(names).size !== names.length) { event.preventDefault(); notify('请勿重复选择同一好友。'); return; }
    sync();
  });
  render(); load();
})();
