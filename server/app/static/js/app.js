// Dimuat dari file (bukan <script> inline) agar lolos CSP script-src 'self'.
// `document`, bukan `document.body`: script di <head> berjalan sebelum <body> ada.
document.addEventListener('htmx:beforeSwap', (evt) => {
  const xhr = evt.detail.xhr;
  // 400/422: form dirender ulang dengan pesan error.
  // HX-Retarget: server sengaja mengirim fragment error (toast) untuk status lain (403/404/429/5xx).
  if (xhr.status === 400 || xhr.status === 422 || xhr.getResponseHeader('HX-Retarget')) {
    evt.detail.shouldSwap = true;
    evt.detail.isError = false;
  }
});

// Pesan singkat dari server lewat header HX-Trigger: {"toast": "teks"}. textContent, bukan innerHTML.
function showToast(message, isError) {
  const box = document.getElementById('toast');
  if (!box || !message) return;
  const p = document.createElement('p');
  p.className = isError ? 'toast err' : 'toast';
  p.setAttribute('role', 'status');
  p.textContent = message;
  box.appendChild(p);
  setTimeout(() => p.remove(), 5000);
}
document.addEventListener('toast', (evt) => showToast(evt.detail.value));

// Toast error dari server (components/_toast.html) juga hilang sendiri
document.addEventListener('htmx:afterSwap', (evt) => {
  if (evt.detail.target && evt.detail.target.id === 'toast') {
    const items = evt.detail.target.querySelectorAll('.toast');
    items.forEach((el) => setTimeout(() => el.remove(), 6000));
  }
});

// Elemen dengan data-confirm: minta konfirmasi sebelum submit (pengganti hx-confirm untuk form biasa)
document.addEventListener('submit', (evt) => {
  const msg = evt.target.getAttribute('data-confirm');
  if (msg && !window.confirm(msg)) evt.preventDefault();
}, true);

// Tombol cetak (laporan PDF lewat dialog cetak browser) & tombol tutup <dialog>
document.addEventListener('click', (evt) => {
  const el = evt.target.closest('[data-action]');
  if (!el) return;
  const action = el.getAttribute('data-action');
  if (action === 'print') window.print();
  if (action === 'open-dialog') document.getElementById(el.getAttribute('data-target'))?.showModal();
  if (action === 'close-dialog') el.closest('dialog')?.close();
  if (action === 'copy') {
    const text = document.getElementById(el.getAttribute('data-target'))?.textContent || '';
    navigator.clipboard?.writeText(text.trim()).then(() => showToast('Disalin.'));
  }
});

// Fragment yang meminta dialog ditutup setelah sukses: HX-Trigger {"close-dialog": true}
document.addEventListener('close-dialog', () => document.querySelectorAll('dialog[open]').forEach((d) => d.close()));
