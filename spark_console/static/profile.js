(() => {
  const expandSection = () => {
    const section = document.getElementById(location.hash.slice(1));
    if (section instanceof HTMLDetailsElement) section.open = true;
  };
  expandSection();
  window.addEventListener('hashchange', expandSection);
  const input = document.getElementById('profile-avatar');
  if (!input) return;
  const form = input.closest('form');
  const preview = form.querySelector('[data-avatar-preview]');
  const initial = form.querySelector('[data-avatar-initial]');
  const error = form.querySelector('[data-avatar-error]');
  const button = form.querySelector('button[type="submit"]');
  let previewUrl;
  input.addEventListener('change', () => {
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    const file = input.files[0];
    const invalid = file && (file.size > 2 * 1024 * 1024 || !['image/png', 'image/jpeg', 'image/webp'].includes(file.type));
    error.textContent = invalid ? '请选择不超过 2 MB 的 PNG、JPG 或 WebP 图片。' : '';
    error.hidden = !invalid;
    button.disabled = Boolean(invalid);
    if (!file || invalid) return;
    previewUrl = URL.createObjectURL(file);
    preview.src = previewUrl;
    preview.hidden = false;
    initial.hidden = true;
  });
  window.addEventListener('pagehide', () => { if (previewUrl) URL.revokeObjectURL(previewUrl); });
})();
