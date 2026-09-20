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
  const storagePrefix = `spark:contact-view:${root.dataset.viewerId}:`;
  const readState = key => {
    try { return JSON.parse(localStorage.getItem(storagePrefix + key) || 'null'); } catch { return null; }
  };
  const writeState = (key, value) => {
    try { localStorage.setItem(storagePrefix + key, JSON.stringify(value)); } catch { /* Storage may be disabled. */ }
  };
  if (account.dataset.rememberSelection === 'true' && !account.querySelector('option[selected]')) {
    const saved = readState('account');
    if ([...account.options].some(option => option.value === saved)) account.value = saved;
  }
  let friends = [], requestVersion = 0, items = [], pollTimer = null, shown = {};
  let expanded = {};
  function restoreView() {
    const saved = readState(`view:${account.value}`);
    shown = {}; expanded = {};
    for (const kind of ['contact', 'group', 'unknown']) {
      if (Number.isInteger(saved?.shown?.[kind]) && saved.shown[kind] >= 50) shown[kind] = Math.min(saved.shown[kind], 10000);
      if (typeof saved?.expanded?.[kind] === 'boolean') expanded[kind] = saved.expanded[kind];
    }
  }
  const saveView = () => writeState(`view:${account.value}`, {shown, expanded});
  restoreView();
  const syncButton = root.querySelector('[data-refresh-friends]');
  const manualSearch = root.querySelector('[data-add-searched-name]');
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
    root.dispatchEvent(new Event('batchchange'));
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
      const suggestions = document.createElement('datalist');
      suggestions.id = `batch-name-suggestions-${index}`;
      for (const value of new Set(friends.flatMap(friend => [friend.name, ...(friend.aliases || [])]))) {
        const option = document.createElement('option'); option.value = value; suggestions.append(option);
      }
      name.setAttribute('list', suggestions.id);
      name.addEventListener('input', () => { item.target_name = name.value; item.target_sec_uid = ''; sync(); });
      nameLabel.append(name, suggestions);
      const label = document.createElement('label'); label.textContent = '给这位好友的专属内容';
      const message = document.createElement('textarea'); message.value = item.message_template; message.maxLength = 500; message.rows = 3; message.required = true;
      message.placeholder = '例如：今天也记得开心呀';
      message.addEventListener('input', () => { item.message_template = message.value; sync(); });
      const prefix = '@spark-content:';
      let spec = {mode:'fixed', text:item.message_template};
      if (item.message_template.startsWith(prefix)) {
        try { spec = JSON.parse(item.message_template.slice(prefix.length)); } catch { /* Server validates on save. */ }
      }
      const modeLabel = document.createElement('label'); modeLabel.textContent = '内容方式';
      const mode = document.createElement('select');
      for (const [value, text] of [['fixed','固定文案'], ['hitokoto','随机一言'], ['random','自定义文案库 · 随机'], ['daily','自定义文案库 · 按天轮换']]) {
        const option = document.createElement('option'); option.value = value; option.textContent = text; mode.append(option);
      }
      mode.value = spec.mode; modeLabel.append(mode);
      const typesLabel = document.createElement('label'); typesLabel.textContent = '一言分类';
      const types = document.createElement('select');
      for (const [value,text] of [['i','诗词'],['d','文学'],['a','动画'],['b','漫画'],['c','游戏'],['e','原创'],['f','网络'],['h','影视'],['k','哲学'],['l','抖机灵']]) {
        const option = document.createElement('option'); option.value = value; option.textContent = text; types.append(option);
      }
      types.value = spec.types?.[0] || 'i'; typesLabel.append(types);
      const fallbackLabel = document.createElement('label'); fallbackLabel.textContent = '接口失败时发送的备用文案';
      const fallbackInput = document.createElement('input'); fallbackInput.maxLength = 200;
      fallbackInput.value = spec.fallback || '今日火花，祝你今天开心！'; fallbackLabel.append(fallbackInput);
      const hint = document.createElement('p'); hint.className = 'field-hint';
      const preview = document.createElement('pre'); preview.className = 'field-hint recipient-code'; preview.setAttribute('aria-live','polite');
      const preset = document.createElement('button'); preset.type = 'button'; preset.className = 'quiet';
      preset.textContent = '使用每日一言排版';
      const dailyTemplate = '🤩今日火花 +1\n—— 👉 每日一言 👈 ——\n{一言}';
      message.value = spec.text || '';
      const contentSync = () => {
        typesLabel.hidden = fallbackLabel.hidden = mode.value !== 'hitokoto';
        preset.hidden = mode.value !== 'hitokoto';
        hint.textContent = mode.value === 'hitokoto' ? '模板须含一个 {一言}。第三方随机内容不保证适合所有好友；接口失败用备用文案。' :
          mode.value === 'fixed' ? '保持原有固定文案，不请求外部接口。' : '每行一句，填写 2–20 句。按天轮换以北京时间计算；本次重试沿用已生成文案。';
        message.maxLength = mode.value === 'fixed' ? 500 : 400;
        item.message_template = mode.value === 'fixed' ? message.value : prefix + JSON.stringify({mode:mode.value, text:message.value,
          ...(mode.value === 'hitokoto' ? {types:[types.value], fallback:fallbackInput.value} : {})});
        preview.textContent = '示例预览（非实际抽取）：\n' + (mode.value === 'hitokoto' ? message.value.replace('{一言}','行到水穷处，坐看云起时。\n—— 终南别业（王维）') :
          mode.value === 'fixed' ? message.value : message.value.split('\n').find(line=>line.trim()) || '请填写文案');
        if (item.message_template.length > 500) hint.textContent += ' 当前配置超过 500 字，请缩短文案。';
        sync();
      };
      mode.addEventListener('change', () => {
        if (mode.value === 'hitokoto' && !message.value.includes('{一言}')) message.value = message.value ? message.value + '\n{一言}' : dailyTemplate;
        contentSync();
      });
      preset.addEventListener('click', () => { message.value = dailyTemplate; contentSync(); });
      for (const element of [message,types,fallbackInput]) element.addEventListener('input',contentSync);
      contentSync();
      label.append(message); card.append(header, nameLabel, modeLabel, typesLabel, fallbackLabel, preset, label, hint, preview); cards.append(card);
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
  function filtered() {
    const query = search.value.trim().toLocaleLowerCase();
    return friends.filter(friend => [friend.name, ...(friend.aliases || [])].some(value => typeof value === 'string' && value.toLocaleLowerCase().includes(query)));
  }
  function friendKind(friend) { return friend.kind || (friend.sec_uid ? 'contact' : 'unknown'); }
  function renderOptions() {
    options.replaceChildren();
    const matches = filtered();
    if (matches.some(friend => friend.same_name)) {
      const hint = document.createElement('p'); hint.className = 'field-hint';
      hint.textContent = '发现同名好友：请按抖音号区分，并在抖音设置不同备注后重新同步。';
      options.append(hint);
    }
    for (const [kind, label] of [['contact','已识别联系人'], ['group','群聊'], ['unknown','其他会话（含群聊 / 未识别联系人）']]) {
      const group = matches.filter(friend => friendKind(friend) === kind);
      if (!group.length) continue;
      const section = document.createElement(kind === 'contact' ? 'section' : 'details');
      section.className = 'batch-friend-group';
      const title = document.createElement(kind === 'contact' ? 'strong' : 'summary');
      title.textContent = `${label}（${group.length}）`; section.append(title);
      if (kind !== 'contact') {
        section.open = Boolean(search.value.trim()) || expanded[kind] === true;
        section.addEventListener('toggle', () => {
          if (!section.isConnected || search.value.trim()) return;
          expanded[kind] = section.open; saveView();
        });
      }
      const list = document.createElement('div'); section.append(list);
      const limit = shown[kind] || 50;
      group.slice(0, limit).forEach(friend => {
        const button = document.createElement('button'); button.type = 'button'; button.className = 'quiet';
        button.textContent = `${selected(friend) ? '✓ ' : '＋ '}${friend.name}`; button.disabled = selected(friend);
        if (friend.same_name) button.textContent += ` · ${friend.identity_hint || '同名好友'}`;
        button.title = (friend.aliases || []).join(' / ');
        if (friend.same_name) button.title += '；同名好友请在抖音设置不同备注后重新同步';
        button.addEventListener('click', () => add(friend)); list.append(button);
      });
      if (group.length > limit) {
        const more = document.createElement('button'); more.type = 'button'; more.className = 'quiet';
        more.textContent = `加载更多（已显示 ${limit}/${group.length}）`;
        more.addEventListener('click', () => { shown[kind] = limit + 50; saveView(); renderOptions(); }); section.append(more);
      }
      options.append(section);
    }
    manualSearch.hidden = !search.value.trim();
    manualSearch.textContent = `按准确昵称“${search.value.trim()}”添加`;
  }
  function syncStatus(body, version, accountId) {
    if (version !== requestVersion || accountId !== account.value) return;
    const active = ['queued','running'].includes(body.status);
    syncButton.disabled = active;
    const accountName = account.selectedOptions[0]?.textContent || '';
    const time = body.finished_at ? new Date(body.finished_at).toLocaleString() : '';
    const labels = {queued:'等待浏览器空闲后同步', running:'正在从抖音读取最近会话',
      partial:`最近会话已更新 ${time}；未确认全部好友`, failed:'同步未完成，保留原名单；可稍后重试',
      cancelled:'同步已取消，保留原名单', never:'当前为绑定时保存的名单，可点击从抖音同步'};
    status.textContent = `${accountName} · 已保存 ${friends.length} 个联系人/会话 · ${labels[body.status] || labels.never}`;
    if (body.error_code === 'login_expired') status.textContent += '；请先重新绑定账号';
    else if (body.status === 'partial' && body.error_code) status.textContent += '；本次读取中断，已保留读到的结果，可稍后再同步';
    if (active) pollTimer = setTimeout(() => pollSync(version, accountId), 2000);
  }
  async function pollSync(version, accountId) {
    if (version !== requestVersion || account.value !== accountId) return;
    try {
      const response = await fetch(`/accounts/${encodeURIComponent(accountId)}/contact-sync`, {credentials:'same-origin'});
      if (!response.ok) throw new Error('无法读取同步进度，原名单仍可用');
      const body = await response.json();
      if (version !== requestVersion || account.value !== accountId) return;
      if (['queued','running'].includes(body.status)) syncStatus(body,version,accountId);
      else load();
    } catch (error) { if (version === requestVersion) { status.textContent = error.message; syncButton.disabled = false; } }
  }
  async function load() {
    const version = ++requestVersion;
    clearTimeout(pollTimer);
    syncButton.disabled = false;
    const accountId = account.value;
    status.textContent = '正在读取已保存的联系人…';
    try {
      const prefix = account.dataset.conversationPrefix || '/accounts';
      const response = await fetch(`${prefix}/${encodeURIComponent(accountId)}/conversations`, {credentials: 'same-origin', headers: {Accept:'application/json'}});
      if (!response.ok) throw new Error('读取失败，可手动输入准确昵称');
      const body = await response.json();
      if (version !== requestVersion) return;
      friends = (body.items || []).filter(x => x && typeof x.name === 'string');
      status.textContent = `已读取 ${friends.length} 个联系人/会话；可选择或手动输入准确昵称。`;
      renderOptions();
      render();
      const progress = await fetch(`/accounts/${encodeURIComponent(accountId)}/contact-sync`, {credentials:'same-origin'});
      if (progress.ok) syncStatus(await progress.json(),version,accountId);
    } catch (failure) { if (version === requestVersion) status.textContent = failure.message; }
  }
  search.addEventListener('input', () => { shown = {}; renderOptions(); });
  syncButton.addEventListener('click', async () => {
    const accountId = account.value, version = requestVersion;
    syncButton.disabled = true; clearTimeout(pollTimer);
    try {
      const payload = new URLSearchParams({csrf_token:form.querySelector('[name="csrf_token"]').value});
      const response = await fetch(`/accounts/${encodeURIComponent(accountId)}/contact-sync`, {method:'POST',credentials:'same-origin',body:payload});
      const body = await response.json();
      if (!response.ok) throw new Error(body.message || '同步未启动，请稍后再试');
      syncStatus(body,version,accountId);
    } catch (error) { if (version === requestVersion) { status.textContent = error.message; syncButton.disabled = false; } }
  });
  manualSearch.addEventListener('click', () => {
    const name = search.value.trim();
    if (!name || name.length > 64) { notify('准确昵称需为 1–64 个字符。'); return; }
    add({name, sec_uid:''});
  });
  root.querySelector('[data-add-filtered]').addEventListener('click', () => {
    const pending = filtered().filter(friend => friendKind(friend) === 'contact' && !selected(friend));
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
    priorAccount = account.value;
    if (account.dataset.rememberSelection === 'true') writeState('account', account.value);
    items = []; friends = []; restoreView(); search.value = ''; syncButton.disabled = false; notify(''); render(); renderOptions(); load();
  });
  form.addEventListener('submit', event => {
    if (!items.length || items.length > 5) { event.preventDefault(); notify('请选择 1–5 位好友。'); return; }
    const names = items.map(item => item.target_name.trim());
    if (new Set(names).size !== names.length) { event.preventDefault(); notify('请勿重复选择同一好友。'); return; }
    sync();
  });
  render(); load();
  window.addEventListener('pagehide', () => { ++requestVersion; clearTimeout(pollTimer); });
})();
