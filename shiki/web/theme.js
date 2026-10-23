// Apply before the stylesheet loads so a saved light theme never flashes dark.
(() => {
  const root = document.documentElement;
  const systemTheme = window.matchMedia('(prefers-color-scheme: dark)');
  const readPreference = () => {
    try {
      const value = localStorage.getItem('shiki.theme');
      return ['light', 'dark'].includes(value) ? value : null;
    } catch { return null; }
  };
  let preference = readPreference();

  function applyTheme() {
    const theme = preference || (systemTheme.matches ? 'dark' : 'light');
    root.dataset.theme = theme;
    document.querySelector('meta[name="theme-color"]').content = theme === 'light' ? '#f4f6f2' : '#101314';
    document.querySelector('meta[name="color-scheme"]').content = theme;
    const button = document.getElementById('theme-toggle');
    if (button) {
      const label = theme === 'light' ? '切换到深色模式' : '切换到浅色模式';
      button.setAttribute('aria-label', label);
      button.title = label;
      button.querySelector('span').textContent = theme === 'light' ? '深色' : '浅色';
    }
  }

  applyTheme();
  document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('theme-toggle').addEventListener('click', () => {
      preference = root.dataset.theme === 'light' ? 'dark' : 'light';
      applyTheme();
      try { localStorage.setItem('shiki.theme', preference); }
      catch { /* Keep the selection for this visit when browser storage is unavailable. */ }
    });
    applyTheme();
  });
  systemTheme.addEventListener('change', () => { if (!preference) applyTheme(); });
  window.addEventListener('storage', event => {
    if (event.key === 'shiki.theme' || event.key === null) {
      preference = readPreference();
      applyTheme();
    }
  });
})();
