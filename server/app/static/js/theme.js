// Tema terang/gelap. Dimuat TANPA defer di <head> sebelum CSS agar halaman tidak sempat berkedip terang.
// Pilihan user (auto | light | dark) disimpan per browser di localStorage; "auto" mengikuti setelan OS.
(() => {
  const KEY = 'ronda-theme';
  const root = document.documentElement;
  const media = window.matchMedia('(prefers-color-scheme: dark)');

  let fallback = 'auto'; // localStorage diblokir (mode privat): pilihan hanya berlaku di halaman ini
  function pref() {
    try { return localStorage.getItem(KEY) || fallback; } catch { return fallback; }
  }
  function apply() {
    const p = pref();
    root.dataset.theme = p === 'auto' ? (media.matches ? 'dark' : 'light') : p;
    document.querySelectorAll('[data-action="theme"]').forEach((b) => {
      b.setAttribute('aria-pressed', String(b.getAttribute('data-value') === p));
    });
  }

  apply();
  media.addEventListener('change', apply);
  document.addEventListener('DOMContentLoaded', apply);
  // Ganti tema di tab lain: ikut berubah di tab ini
  window.addEventListener('storage', (evt) => { if (evt.key === KEY) apply(); });
  document.addEventListener('click', (evt) => {
    const el = evt.target.closest('[data-action="theme"]');
    if (!el) return;
    fallback = el.getAttribute('data-value');
    try { localStorage.setItem(KEY, fallback); } catch { /* lihat fallback */ }
    apply();
  });
  // Cetak (laporan PDF) selalu terang
  let beforePrint = null;
  window.addEventListener('beforeprint', () => { beforePrint = root.dataset.theme; root.dataset.theme = 'light'; });
  window.addEventListener('afterprint', () => { if (beforePrint) root.dataset.theme = beforePrint; beforePrint = null; });
})();
