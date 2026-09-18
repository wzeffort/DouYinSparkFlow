(() => {
  const panel = document.querySelector('[data-platform-root]');
  const pet = panel?.querySelector('[data-spark-companion]');
  if (!pet) return;
  const speech = pet.querySelector('[data-spark-speech]');
  const toggle = panel.querySelector('[data-spark-toggle]');
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  const coarse = matchMedia('(pointer: coarse)');
  let enabled = true, x = 0, y = 0, lastEscape = 0, timer, settle, frame;
  let pointer = null;
  let activity = 0;
  const count = key => Number(panel.querySelector(`[data-platform-value="${key}"]`)?.textContent || 0);
  function syncColor() {
    const total = count('total'), done = count('success');
    pet.dataset.color = count('failed') ? 'attention' : count('running') ? 'working' :
      total && done >= total ? 'complete' : total && done / total >= .5 ? 'warm' : 'waiting';
  }
  new MutationObserver(syncColor).observe(panel.querySelector('.daily-console'), {subtree:true, childList:true, characterData:true});
  syncColor();
  try { enabled = localStorage.getItem('spark-companion-rest') !== '1'; } catch (_) {}
  const bounds = () => ({w: Math.max(0, panel.clientWidth - 82), h: Math.max(0, panel.clientHeight - 102)});
  function move(nx, ny) {
    const {w, h} = bounds();
    x = Math.max(8, Math.min(w, nx)); y = Math.max(55, Math.min(h, ny));
    pet.style.transform = `translate(${x}px, ${y}px)`;
    pet.classList.toggle('speech-left', x > panel.clientWidth / 2);
  }
  function say(text, mood = 'thinking') {
    speech.textContent = text; pet.dataset.mood = mood;
  }
  function peek() {
    clearTimeout(timer);
    if (enabled && !document.hidden) {
      const activities = [
        ['working', count('running') ? '认真盯进度呢！' : '让我检查一下进度…'],
        ['walking', '巡逻一下，嘿咻～'],
        ['stretching', '伸个懒腰，舒坦！'],
        ['skipping', '一、二、三！运动一下'],
        ['thinking', count('failed') ? '咦，有任务需要看看' : `完成 ${count('success')} 个啦，继续观察`],
        ['sleeping', count('running') ? '眯一小会儿…' : '呼噜…有动静叫我'],
      ];
      const [mood, text] = activities[activity++ % activities.length];
      say(text, mood);
      if (mood === 'walking' && !reduced.matches && !coarse.matches) {
        const {w, h} = bounds();
        // Roam near the perimeter; pointer escape handles close encounters.
        const nx = 8 + Math.random() * Math.max(0, w - 8);
        move(nx, Math.random() < .5 ? 55 : h);
      }
    }
    timer = setTimeout(peek, 6500 + Math.random() * 2500);
  }
  function applyPreference() {
    pet.hidden = !enabled; toggle.setAttribute('aria-pressed', String(enabled));
    toggle.textContent = enabled ? '小火伴：活动中' : '小火伴：休息中';
    clearTimeout(timer); clearTimeout(settle);
    if (enabled) say('让我瞅瞅…');
    if (enabled) timer = setTimeout(peek, 6500);
  }
  toggle.addEventListener('click', () => {
    enabled = !enabled;
    try { localStorage.setItem('spark-companion-rest', enabled ? '0' : '1'); } catch (_) {}
    applyPreference();
  });
  panel.addEventListener('pointermove', event => {
    if (!enabled || reduced.matches || coarse.matches || event.pointerType === 'touch') return;
    pointer = {x: event.clientX, y: event.clientY};
    if (frame) return;
    frame = requestAnimationFrame(() => {
      frame = null;
      const rect = pet.getBoundingClientRect();
      const dx = rect.left + rect.width / 2 - pointer.x;
      const dy = rect.top + rect.height / 2 - pointer.y;
      if (Math.hypot(dx, dy) > 125 || performance.now() - lastEscape < 450) return;
      lastEscape = performance.now();
      const box = panel.getBoundingClientRect(), {w,h} = bounds();
      // Run toward the opposite edge, even when already cornered.
      move(pointer.x - box.left < panel.clientWidth / 2 ? w : 8,
        pointer.y - box.top < panel.clientHeight / 2 ? h : 55);
      say(['哎呀！溜了溜了', '嘿，碰不到～', '我躲！'][Math.floor(Math.random()*3)], 'running');
      clearTimeout(settle);
      settle = setTimeout(() => { if (enabled) say('走了吗？偷偷看看…', 'peeking'); }, 850);
    });
  }, {passive: true});
  const resize = new ResizeObserver(() => move(x || panel.clientWidth - 90, y || 55));
  resize.observe(panel);
  document.addEventListener('visibilitychange', () => {
    clearTimeout(timer);
    if (!document.hidden && enabled) timer = setTimeout(peek, 6500);
  });
  move(panel.clientWidth - 90, 55);
  applyPreference();
})();
